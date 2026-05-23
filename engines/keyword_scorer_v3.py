"""
╔══════════════════════════════════════════════════════════════════╗
║            闲鱼选词模型 v3 — 三阶段高效漏斗                       ║
║  目标：快速找到"能快速卖出有利润"的搜索词，最大化时间效率              ║
╚══════════════════════════════════════════════════════════════════╝

核心设计：三段漏斗
  第1段：极速预检 (3秒/词)  → 均价极低/热度极低淘汰；无行情→直通选品线
  第2段：快速海选 (15秒/词) → 6维评分，保留 A 级以上（有行情词）
  第3段：精选完整 (40秒/词) → 补充完整数据，最终决策（有行情词）

评分维度 (总分100分制，内部120分归一化):
  1. 需求规模     0-20  — 每天有多少人搜这个词
  2. 成交效率     0-30  — 能不能快速卖出 ★权重最高
  3. 成交质量     0-20  — 卖出的品质/销量是否扎实 ★v3新增
  4. 利润确定性   0-25  — 能赚多少、赚多确定
  5. 竞争格局     0-15  — 供需比 + 在售卖家密度
  6. 趋势信号     0-10  — 热度/价格/AI解读

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
字段依据速查表
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

【需求规模】
  historySearchUv    market.pricetrend → hotTrendListData.hotTrendList[].historySearchUv
                     24小时各时段的独立访客数，求和得日UV
  historyOrder       market.historysale → historyOrder / historyOrder2
                     历史成交总量（字符串如"500+"），反映品类总体活跃度

【成交效率】★ 最核心
  salesDescribe      market.historysale → itemSaleList[].salesDescribe
                     成交描述，含"刚刚""今天""当天"= 当天成交
  dealPrice          market.historysale → itemSaleList[].dealPrice
                     实际成交价格
  publishPrice       market.historysale → itemSaleList[].publishPrice
                     发布标价。dealPrice/publishPrice ≈ 1 → 不砍价
  recentSoldTimeDescribe  market.historysale → itemSaleList[].recentSoldTimeDescribe
                     最近成交时间描述，"当天/近1天"= 近期活跃

【成交质量】★ v3新增
  salesCountDescribe market.historysale → itemSaleList[].salesCountDescribe
                     "已售5000+"/"已售1万+" — 解析出单品累计销量
  soldOut            market.historysale → itemSaleList[].soldOut
                     true=已售罄，售罄率30-70% = 最佳需求信号

【利润确定性】
  avgPrice           market.topbar → spuHeader.avgPrice
                     行情成交均价（元），价格越高利润空间越大
  historyMaxPrice    market.historysale → historyMaxPrice
  historyMinPrice    market.historysale → historyMinPrice
                     价格带宽：(max-min)/均值，合理带宽=可差异化定价
  rangeList          market.historysale → rangeList[].{floorLimit, upLimit, num}
                     成交价格区间分布，找成交最密集的区间 = 最佳定价参考

【竞争格局】
  numFound           search → numFound
                     搜索命中总数 = 在售商品总量（供给侧）
  historySearchUv    同需求规模，用于计算供需比
  sellingOrder       search → sellingOrder
                     在售卖家总数（如"4000+"）

【趋势信号】
  hotBullishRatio    market.pricetrend → declareData.hotBullishRatio
                     热度看多比例（%），>50 = 热度上升信号
  avgPriceInc        market.topbar → spuHeader.avgPriceInc
                     均价涨跌（元），>0 = 价格在涨
  aiInterpretationData market.pricetrend → aiInterpretationData
                     AI解读文本，含"热门/增长/活跃"等积极词加分

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
三阶段高效批量流程（100个词只需~48分钟，vs 全量700分钟）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

阶段1 极速预检 [3秒/词] → 100词→保留~50词
  采集: market.topbar + market.tabs
  直接淘汰:
    ✗ tabs中无行情Tab (spuId=null) → 非标品/无行情词
    ✗ avgPrice < 15元             → 利润空间太低
    ✗ hot(热度值) < 100           → 几乎没人搜索
    ✗ avgPriceInc < -20           → 价格崩塌信号
  调用: precheck()

阶段2 快速海选 [15秒/词] → 50词→保留~15词
  采集: + market.historysale(p1) + market.pricetrend + search(p1)
  调用: score_fast() → 保留 grade A/S (≥75分)

阶段3 精选完整 [40秒/词] → 15词→保留Top5
  采集: + search翻页(5-10页) + market.historysale翻页
  调用: score_full() → 保留 grade S (≥90分)
"""

from __future__ import annotations
import re
from typing import Dict, List, Any, Optional, Tuple


class KeywordScorerV3:
    """闲鱼选词模型 v3 — 三阶段高效漏斗"""

    # ── 等级阈值（100分制）──────────────────────────
    DEFAULT_GRADES = {"S": 90, "A": 75, "B": 55, "C": 35, "D": 0}

    # ── 阶段1极速预检阈值 ───────────────────────────
    PRECHECK_MIN_PRICE   = 15    # 均价最低门槛（元）
    PRECHECK_MIN_UV_24H  = 200   # 24h最低UV门槛（hotTrendList[].historySearchUv求和）
    PRECHECK_MAX_PRICE_DROP = -20  # 价格跌幅超过-20元直接淘汰

    def __init__(self, config: dict = None):
        self._cfg = config or {}
        self._load_params(self._cfg)

    def _load_params(self, cfg: dict):
        """从配置字典加载所有参数"""
        sc = cfg.get("scoring", {})
        self._grades = dict(self.DEFAULT_GRADES)
        # 用户自定义等级阈值覆盖默认值
        user_grades = sc.get("keyword_grade_thresholds", {})
        self._grades.update(user_grades)
        self._precheck_min_price = sc.get("precheck_min_price", self.PRECHECK_MIN_PRICE)
        self._precheck_min_uv    = sc.get("precheck_min_uv", self.PRECHECK_MIN_UV_24H)
        self._precheck_max_drop  = sc.get("precheck_max_price_drop", self.PRECHECK_MAX_PRICE_DROP)

    def update_params(self, config: dict):
        """运行时刷新评分参数（设置保存后调用）"""
        self._cfg = config
        self._load_params(config)

    # ════════════════════════════════════════════════
    # 阶段1：极速预检（只需topbar + tabs数据，~3秒/词）
    # 返回: (通过=True, 原因说明)
    # ════════════════════════════════════════════════
    def precheck(self, keyword: str, market_data: dict, tabs: list) -> Tuple[bool, str]:
        """
        极速预检：用 market 全量数据判断是否继续。

        字段依据:
          tabs[].extra.spuId                                    → 是否存在行情Tab
          topbar.spuHeader.avgPrice / avgPriceInc               → 均价/价格涨跌
          pricetrend.hotTrendListData.hotTrendList[].historySearchUv → 24h各时段UV

        返回: (True=通过继续, False=淘汰) + 原因说明
        """
        # ① 检查是否有行情Tab
        has_market_tab = any(
            t.get("searchTabType") == "SEARCH_TAB_MARKET" and
            t.get("extra", {}).get("spuId")
            for t in (tabs or [])
        )
        if not has_market_tab:
            # 无行情词：供给侧达阈值 → 直通选品线（选品线用完整商品数据判断）
            num_found = market_data.get("numFound", 0)
            if num_found >= 200:
                return True, f"无行情Tab-直通选品线(numFound={num_found}>=200)"
            else:
                return False, f"无行情Tab且numFound={num_found}<200，供给侧不足"

        topbar = market_data.get("topbar", {}) or {}
        spu = topbar.get("spuHeader", {}) or {}
        avg_price = self._to_float(spu.get("avgPrice", 0))
        price_inc = self._to_float(spu.get("avgPriceInc", 0))

        # ② 均价门槛（已移除硬性均价淘汰，改为在商品预筛阶段按 price<=avg_price 筛选）

        # ③ 24h UV 门槛（比 spuHeader.hot 更直观准确）
        total_uv = self._calc_24h_uv(market_data)
        if total_uv > 0 and total_uv < self._precheck_min_uv:
            return False, f"24hUV {total_uv} < {self._precheck_min_uv}，需求极低"

        # ④ 价格崩塌信号
        if price_inc < self._precheck_max_drop:
            return False, f"价格跌幅{price_inc:.1f}元，市场在崩"

        return True, "通过预检"

    # ════════════════════════════════════════════════
    # 阶段2：快速海选评分（~15秒/词）
    # ════════════════════════════════════════════════
    def score_fast(self, keyword: str, market_data: dict, search_meta: dict) -> dict:
        """
        快速海选评分（粗筛用）。

        market_data 需包含:
          topbar        → market.topbar（均价、热度、涨跌）
          historysale   → market.historysale（第1页，含itemSaleList）
          pricetrend    → market.pricetrend（hotTrendList、declareData、aiInterpretation）

        search_meta 需包含:
          numFound      → 搜索命中总数
          sellingOrder  → 在售卖家数（"4000+"格式）
        """
        topbar = market_data.get("topbar", {}) or {}
        hs     = market_data.get("historysale", {}) or {}
        pt     = market_data.get("pricetrend", {}) or {}

        if not topbar:
            return {"keyword": keyword, "total": 0, "total_100": 0, "grade": "N/A",
                    "reason": "无行情数据"}

        spu       = topbar.get("spuHeader", {}) or {}
        avg_price = self._to_float(spu.get("avgPrice", 0))
        price_inc = self._to_float(spu.get("avgPriceInc", 0))
        num_found = self._to_int(search_meta.get("numFound", 0))
        sell_ord  = search_meta.get("sellingOrder", "")

        dims = {
            "demand_scale":    self._dim_demand_scale(pt, hs),         # 0-20
            "deal_efficiency": self._dim_deal_efficiency(hs),          # 0-30
            "deal_quality":    self._dim_deal_quality(hs),             # 0-20 ★新增
            "profit_certainty":self._dim_profit_certainty(hs, avg_price),  # 0-25
            "competition":     self._dim_competition(num_found, pt, sell_ord), # 0-15
            "trend_signal":    self._dim_trend_signal(pt, price_inc),  # 0-10
        }

        raw   = sum(dims.values())              # 满分120
        score = round(raw / 120 * 100, 1)       # 归一化到100分
        grade = self._grade(score)

        # 提取关键决策数据（供后续选品模型使用）
        sale_list   = hs.get("itemSaleList", []) or []
        no_bargain_rate = self._calc_no_bargain_rate(sale_list)
        same_day_rate   = self._calc_same_day_rate(sale_list)
        avg_discount    = self._calc_avg_discount(sale_list)
        best_price_zone = self._find_best_price_zone(hs.get("rangeList", []))

        return {
            "keyword":         keyword,
            "total_raw":       round(raw, 1),
            "total_100":       score,
            "grade":           grade,
            "scores":          {k: round(v, 1) for k, v in dims.items()},
            # 关键行情数据（传递给选品模型）
            "avg_price":        avg_price,
            "avg_price_inc":    price_inc,
            "no_bargain_rate":  round(no_bargain_rate, 3),   # 不砍价率
            "same_day_rate":    round(same_day_rate, 3),     # 当天成交率
            "avg_discount_pct": round(avg_discount * 100, 1),# 平均折扣率(%)
            "best_price_zone":  best_price_zone,             # 最佳成交价区间
            "num_found":        num_found,
            "selling_order":    sell_ord,
            "has_market":       True,
        }

    # ════════════════════════════════════════════════
    # 阶段3：精选完整评分（~40秒/词，当前与快速模式同逻辑）
    # 未来扩展：可传入多页 historysale + search 数据
    # ════════════════════════════════════════════════
    def score_full(self, keyword: str, market_data: dict, search_meta: dict) -> dict:
        """精选完整评分。当前与 score_fast 同逻辑，未来可接入多页数据加权。"""
        return self.score_fast(keyword, market_data, search_meta)

    # ════════════════════════════════════════════════
    # 维度1：需求规模 (0-20)
    # ════════════════════════════════════════════════
    def _dim_demand_scale(self, pt: dict, hs: dict) -> float:
        """
        字段:
          pt → hotTrendListData.hotTrendList[].historySearchUv  24h各时段UV
          hs → historyOrder / historyOrder2                     历史成交总量

        逻辑:
          UV得分(0-15) = min(日总UV / 50000 × 15, 15)
          成交量得分(0-5) = min(max(historyOrder, historyOrder2) / 500 × 5, 5)
        """
        # UV
        total_uv = sum(
            self._to_int(ht.get("historySearchUv", 0))
            for ht in (pt.get("hotTrendListData") or {}).get("hotTrendList", []) or []
        )
        uv_score = min(total_uv / 50000 * 15, 15)

        # 历史成交量
        ho1 = self._parse_count(hs.get("historyOrder", ""))
        ho2 = self._parse_count(hs.get("historyOrder2", ""))
        order_score = min(max(ho1, ho2) / 500 * 5, 5) if max(ho1, ho2) > 0 else 0

        return uv_score + order_score

    # ════════════════════════════════════════════════
    # 维度2：成交效率 (0-30) ★最高权重
    # ════════════════════════════════════════════════
    def _dim_deal_efficiency(self, hs: dict) -> float:
        """
        字段:
          salesDescribe           → 含"刚刚/今天/当天" = 当天成交 (0-15分)
          dealPrice / publishPrice → 差值<5% = 不砍价 (0-10分)
          recentSoldTimeDescribe  → 含"当天/近1天/近2天" = 近期活跃 (0-5分)

        含义: 卖家最关心的"快"与"稳"
        """
        items = hs.get("itemSaleList", []) or []
        if not items:
            return 0

        n = len(items)
        same_day = no_bargain = recent = 0

        for si in items:
            desc = (si.get("salesDescribe", "") or "").lower()
            if any(w in desc for w in ["刚刚", "今天", "当天"]):
                same_day += 1

            dp = self._to_float(si.get("dealPrice", 0))
            pp = self._to_float(si.get("publishPrice", 0))
            if pp > 0 and abs(dp - pp) / pp < 0.05:
                no_bargain += 1

            rd = (si.get("recentSoldTimeDescribe", "") or "").lower()
            if any(w in rd for w in ["当天", "近1天", "近2天"]):
                recent += 1

        return (same_day / n * 15) + (no_bargain / n * 10) + (recent / n * 5)

    # ════════════════════════════════════════════════
    # 维度3：成交质量 (0-20) ★v3新增
    # ════════════════════════════════════════════════
    def _dim_deal_quality(self, hs: dict) -> float:
        """
        字段:
          salesCountDescribe → "已售5000+"/"已售1万+" 单品累计销量 (0-12分)
          soldOut            → true=已售罄，最佳售罄率30-70% (0-8分)

        含义: 成交记录中的"爆款比例"和"供不应求程度"
          高销量单品 = 这个品类有人持续在卖且卖得好
          合理售罄率 = 需求强但市场未饱和
        """
        items = hs.get("itemSaleList", []) or []
        if not items:
            return 0

        n = len(items)
        high_s = medium_s = sold_out_n = 0

        for si in items:
            cnt = self._parse_sold_label(si.get("salesCountDescribe", "") or "")
            if cnt >= 5000:
                high_s += 1
            elif cnt >= 100:
                medium_s += 1
            if str(si.get("soldOut", "false")).lower() == "true":
                sold_out_n += 1

        # 销量得分
        sales_score = min((high_s / n * 12) + (medium_s / n * 6), 12)

        # 售罄率得分
        rate = sold_out_n / n
        if 0.3 <= rate <= 0.7:
            sold_out_score = 8   # 黄金区间：卖得动又没饱和
        elif rate > 0.7:
            sold_out_score = 5   # 太高：可能供应不足
        elif rate > 0.1:
            sold_out_score = 4
        else:
            sold_out_score = 0

        return sales_score + sold_out_score

    # ════════════════════════════════════════════════
    # 维度4：利润确定性 (0-25)
    # ════════════════════════════════════════════════
    def _dim_profit_certainty(self, hs: dict, avg_price: float) -> float:
        """
        字段:
          avgPrice          → 行情均价，>300元利润空间最大 (0-8分)
          historyMaxPrice / historyMinPrice → 价格带宽，合理带宽=可差异化 (0-5分)
          dealPrice / publishPrice → 议价折扣率，<5%=利润稳定 (0-7分)
          rangeList[].{floorLimit, upLimit, num} → 最佳成交价区间 (0-5分)

        含义: 卖出去后能赚多少、赚多确定
        """
        # ① 均价水平
        if avg_price > 300:  price_lvl = 8
        elif avg_price > 150: price_lvl = 6
        elif avg_price > 80:  price_lvl = 4
        elif avg_price > 30:  price_lvl = 2
        else:                 price_lvl = 1

        # ② 价格带宽（合理宽度=可差异化定价，太窄或太宽都不好）
        hmax = self._to_float(hs.get("historyMaxPrice", 0))
        hmin = self._to_float(hs.get("historyMinPrice", 0))
        if hmax > 0 and hmin > 0:
            mid = (hmax + hmin) / 2
            spread = (hmax - hmin) / mid if mid > 0 else 0
            if 0.3 <= spread <= 1.0:  band_score = 5
            elif spread > 1.0:         band_score = 3
            else:                      band_score = 2
        else:
            band_score = 2

        # ③ 议价折扣率
        discounts = []
        for si in (hs.get("itemSaleList", []) or []):
            dp = self._to_float(si.get("dealPrice", 0))
            pp = self._to_float(si.get("publishPrice", 0))
            if pp > 0 and dp > 0:
                discounts.append((pp - dp) / pp)

        if discounts:
            avg_d = sum(discounts) / len(discounts) * 100
            if avg_d < 5:   bargain_score = 7
            elif avg_d < 10: bargain_score = 5
            elif avg_d < 15: bargain_score = 3
            else:            bargain_score = 1
        else:
            bargain_score = 3

        # ④ 最佳价格区间校验
        range_score = 0
        best_zone = self._find_best_price_zone(hs.get("rangeList", []))
        if best_zone and avg_price > 0:
            mid_zone = (best_zone[0] + best_zone[1]) / 2
            ratio = mid_zone / avg_price
            range_score = 5 if 0.7 <= ratio <= 1.3 else 3

        return price_lvl + band_score + bargain_score + range_score

    # ════════════════════════════════════════════════
    # 维度5：竞争格局 (0-15)
    # ════════════════════════════════════════════════
    def _dim_competition(self, num_found: int, pt: dict, selling_order) -> float:
        """
        字段:
          numFound        → search搜索命中总数（供给侧总量）
          historySearchUv → 需求侧UV（与供给形成供需比）
          sellingOrder    → 在售卖家总数

        供需比 = numFound / 日UV，越低越蓝海
        """
        # 供需比（0-10）
        total_uv = max(sum(
            self._to_int(ht.get("historySearchUv", 0))
            for ht in (pt.get("hotTrendListData") or {}).get("hotTrendList", []) or []
        ), 1)

        if num_found == 0:
            supply_score = 5
        else:
            ratio = num_found / total_uv
            if ratio < 2:   supply_score = 10
            elif ratio < 5:  supply_score = 8
            elif ratio < 10: supply_score = 5
            elif ratio < 20: supply_score = 3
            else:            supply_score = 1

        # 卖家密度（0-5）
        sc = self._parse_count(str(selling_order))
        if sc > 0:
            if sc < 100:    seller_score = 5
            elif sc < 500:   seller_score = 4
            elif sc < 2000:  seller_score = 3
            elif sc < 5000:  seller_score = 2
            else:            seller_score = 1
        else:
            seller_score = 3

        return supply_score + seller_score

    # ════════════════════════════════════════════════
    # 维度6：趋势信号 (0-10)
    # ════════════════════════════════════════════════
    def _dim_trend_signal(self, pt: dict, avg_price_inc: float) -> float:
        """
        字段:
          hotBullishRatio     → declareData.hotBullishRatio，看多比例(0-4分)
          avgPriceInc         → 均价涨跌，>0=在涨(0-3分)
          aiInterpretationData → AI解读，含积极词(0-3分)
        """
        declare = pt.get("declareData", {}) or {}
        hot_bull = self._to_float(declare.get("hotBullishRatio", 0))

        if hot_bull > 60:   hot_score = 4
        elif hot_bull > 50:  hot_score = 3
        elif hot_bull > 40:  hot_score = 2
        elif hot_bull > 30:  hot_score = 1
        else:                hot_score = 0

        if avg_price_inc > 5:    price_score = 3
        elif avg_price_inc > 0:   price_score = 2
        elif avg_price_inc > -3:  price_score = 1
        else:                     price_score = 0

        ai = str(pt.get("aiInterpretationData", {}) or {})
        pos = sum(1 for w in ["热门", "增长", "上升", "活跃", "畅销", "高需求", "热度高"] if w in ai)
        neg = sum(1 for w in ["冷门", "下降", "低迷", "饱和", "供过于求"] if w in ai)
        ai_score = 3 if pos > neg else (2 if pos > 0 else (0 if neg > pos else 1))

        return hot_score + price_score + ai_score

    # ════════════════════════════════════════════════
    # 辅助计算（供选品模型复用）
    # ════════════════════════════════════════════════
    def _calc_no_bargain_rate(self, items: list) -> float:
        if not items: return 0.5
        n = sum(1 for si in items
                if (pp := self._to_float(si.get("publishPrice", 0))) > 0
                and abs(self._to_float(si.get("dealPrice", 0)) - pp) / pp < 0.05)
        return n / len(items)

    def _calc_same_day_rate(self, items: list) -> float:
        if not items: return 0
        n = sum(1 for si in items
                if any(w in (si.get("salesDescribe", "") or "") for w in ["刚刚", "今天", "当天"]))
        return n / len(items)

    def _calc_avg_discount(self, items: list) -> float:
        discounts = [
            (pp - self._to_float(si.get("dealPrice", 0))) / pp
            for si in items
            if (pp := self._to_float(si.get("publishPrice", 0))) > 0
            and self._to_float(si.get("dealPrice", 0)) > 0
        ]
        return sum(discounts) / len(discounts) if discounts else 0.1

    def _find_best_price_zone(self, range_list: list) -> Optional[tuple]:
        if not range_list: return None
        best = max(range_list, key=lambda r: self._to_int(r.get("num", 0)), default=None)
        if not best: return None
        lo = self._to_float(best.get("floorLimit", 0))
        hi = self._to_float(best.get("upLimit", 0))
        return (lo, hi) if hi > lo else None

    # ════════════════════════════════════════════════
    # 工具方法
    # ════════════════════════════════════════════════
    def _grade(self, score: float) -> str:
        for g in sorted(self._grades, key=lambda x: -self._grades[x]):
            if score >= self._grades[g]:
                return g
        return "D"

    @staticmethod
    def _parse_sold_label(text: str) -> int:
        """已售5000+ → 5000, 已售1万+ → 10000"""
        if not text: return 0
        s = text.replace("已售", "").replace("+", "").replace("件", "").strip()
        if "万" in s:
            s = s.replace("万", "")
            try: return int(float(s) * 10000)
            except: return 0
        try: return int(s)
        except: return 0

    @staticmethod
    def _calc_24h_uv(market_data: dict) -> int:
        """从 pricetrend.hotTrendList 计算24小时总UV"""
        pt = market_data.get("pricetrend", {}) or {}
        htl = (pt.get("hotTrendListData") or {}).get("hotTrendList", []) or []
        return sum(
            KeywordScorerV3._to_int(ht.get("historySearchUv", 0))
            for ht in htl
        )

    @staticmethod
    def _parse_count(val) -> int:
        """解析 "4000+" / "100" 等数字字符串"""
        if isinstance(val, (int, float)): return int(val)
        s = str(val).replace("+", "").strip()
        try: return int(s)
        except: return 0

    @staticmethod
    def _to_float(v, d=0.0) -> float:
        if isinstance(v, str):
            v = v.strip().replace("%", "").replace(",", "").replace("+", "")
        try: return float(v)
        except: return d

    @staticmethod
    def _to_int(v, d=0) -> int:
        try: return int(float(v))
        except: return d


# ════════════════════════════════════════════════════════════════
# 批量运行示例
# ════════════════════════════════════════════════════════════════
"""
scorer = KeywordScorerV3()
results = []

for kw in keyword_list:
    # ── 阶段1：极速预检（只拉topbar+tabs）──────────────
    topbar = fetch("market.topbar", kw)
    tabs   = fetch("market.tabs", kw)
    ok, reason = scorer.precheck(kw, topbar, tabs)
    if not ok:
        print(f"✗ {kw}: {reason}")
        continue

    # ── 阶段2：快速海选（15秒/词）──────────────────────
    market_data = {
        "topbar":      topbar,
        "historysale": fetch("market.historysale", kw, page=1),
        "pricetrend":  fetch("market.pricetrend", kw),
    }
    search_meta = fetch_search_meta(kw, page=1)  # numFound + sellingOrder
    result = scorer.score_fast(kw, market_data, search_meta)
    results.append(result)
    print(f"{'★' if result['grade'] in ('S','A') else '·'} "
          f"{kw}: {result['total_100']:.1f}分 {result['grade']}级")

# 按分数排序，保留 A 级以上进入阶段3
finals = sorted([r for r in results if r["grade"] in ("S", "A")],
                key=lambda x: -x["total_100"])

print(f"\n海选保留 {len(finals)}/{len(results)} 个词进入精选")
for r in finals[:5]:
    print(f"  {r['keyword']}: {r['total_100']}分 | "
          f"不砍价率{r['no_bargain_rate']:.0%} | "
          f"当天成交{r['same_day_rate']:.0%} | "
          f"均价¥{r['avg_price']}")
"""
