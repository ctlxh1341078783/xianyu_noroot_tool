"""
╔══════════════════════════════════════════════════════════════════╗
║          闲鱼选品模型 v3 — 两层过滤 + 6维评分                     ║
║  目标：从搜索列表快速识别"值得上架的高潜力商品"                     ║
╚══════════════════════════════════════════════════════════════════╝

核心设计：两层过滤
  第1层：搜索列表预筛选（不进详情，节省90%时间）
         字段来源: search 接口的商品字段
  第2层：详情页深度评分（进入详情后，6维打分）
         字段来源: detail 接口的 itemDO + sellerDO

评分维度（总分100分制，内部110分归一化）:
  1. 需求信号     0-20  — soldCnt/wantCnt/collectCnt/browseCnt
  2. 价格优势     0-25  — 溢价率 + 包邮 + 促销 ★核心
  3. 卖家验证     0-25  — 卖家销量/好评/信用/活跃度
  4. 时效性       0-15  — 上架天数 + 卖家最近活跃
  5. 货源属性     0-15  — 标品/半标品/非标品
  6. 商品质量     0-10  — 图片数/描述详细度/视频 ★v3新增

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
第1层：搜索列表预筛选字段（search接口）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  isAliMaMaAD     → 广告商品，直接跳过
  isAuction       → 拍卖商品，价格不确定，跳过
  price           → 价格显示文本（如"¥36.30"），解析后比较均价±60%
  soldCountLabel  ★ → "已售65"/"已售700+" 搜索层唯一已售来源，有值=优先进入
  serviceTags     → 服务标签，含"包邮""百分百好评""24小时发货"等 = 正向信号
  wantNum         → 想要数（clickParam.args.wantNum，数值型，更准确）
  fishTags        → 鱼标签，r1/r3/r4/r5/r88，含"降价""行情价"等信号
  userFishShopLabel → 鱼铺标签，含评价数 = 有运营经验的卖家
  detailPageType  → "detailCommonBuy"=普通商品，其他类型需单独确认

预筛选标准汇总（不进详情的商品）:
  ✗ isAliMaMaAD=true          广告
  ✗ isAuction=true            拍卖
  ✗ price > 行情均价           无利润空间（进价高于市场均价无法获利）
  ✗ serviceTags有负面标签      差评信号
  ✓ soldCountLabel有值         优先进入（不是强制，但提高优先级）
  ✓ serviceTags含包邮/好评      加分信号
  ✓ price ≤ 行情均价           有加价空间，低价品也可能有利润

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
第2层：详情评分字段（detail接口）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

【需求信号】
  search.soldCount    ★最强信号，单品真实已售数量（搜索层独有，detail层soldCnt始终为0）
  itemDO.wantCnt      想要数（同 wantNum，但 detail 中更准确）
  itemDO.collectCnt   收藏数
  itemDO.browseCnt    浏览量

【价格优势】
  search.price         → 商品标价
  itemDO.soldPrice     → 实际成交价（反映成交折扣）
  itemDO.transportFee  → 运费，=0 表示包邮 (+5分)
  itemDO.promotionPriceDO → 有促销活动 (+3分)
  market.avgPrice      → 行情均价（从选词模型传入）

  核心公式:
    溢价率 = (标价 - 行情均价) / 行情均价
    -20% ≤ 溢价率 < -5%   → 略低于均价最好卖 = 12分
    -5% ≤ 溢价率 < 5%     → 接近均价 = 10分

【卖家验证】
  sellerDO.hasSoldNumInteger ★ → 卖家历史累计销量（数值）
  sellerDO.newGoodRatioRate    → 好评率（"100%"格式）
  sellerDO.replyRatio24h       → 24h回复率
  sellerDO.lastVisitTime       → 上次来访时间（"2分钟前来过"）
  sellerDO.zhimaAuth           → 芝麻信用认证
  sellerDO.identityTags        → 身份标签列表（有=加分）
  sellerDO.itemCount           → 在售商品数，10-200 = 活跃但不刷店

【时效性】
  itemDO.gmtCreate          → 发布时间（毫秒时间戳），越新越有流量扶持
  sellerDO.lastVisitTime    → 卖家活跃度，"分钟前"=在线状态

【货源属性】
  search.title / detail.title → 品牌型号判断标品/半标品/非标品
  itemDO.cpvLabels            → [{propertyName:"品牌", valueName:"苹果"}]
  itemDO.itemCatDTO           → 类目，类目明确=更容易找同款

【商品质量】
  itemDO.imageInfos   → 图片列表，>=6张=充分展示 (0-4分)
  itemDO.desc         → 描述文本，>500字=详细 (0-3分)
  itemDO.hasVideo     → 有视频 (0-1分)
  itemDO.commonTags   → 含"包邮"标签 (0-2分)
"""

from __future__ import annotations
import re
from datetime import datetime
from typing import Dict, List, Any, Optional, Tuple


class ProductScorerV3:
    """闲鱼选品模型 v3 — 两层过滤 + 6维评分"""

    DEFAULT_GRADES = {"S": 90, "A": 75, "B": 55, "C": 40, "D": 0}

    # 标品品牌关键词（用于判断是否为标品，可复制性高）
    BRAND_KEYWORDS = [
        "华为", "小米", "苹果", "三星", "OPPO", "vivo", "荣耀", "联想", "戴尔", "华硕",
        "格力", "美的", "海尔", "苏泊尔", "九阳", "松下", "索尼", "戴森", "飞利浦",
        "捷安特", "美利达", "喜德盛", "凤凰", "永久", "飞鸽", "大行",
        "罗技", "雷蛇", "Cherry", "樱桃", "斐尔可", "阿米洛",
        "BOSE", "森海塞尔", "铁三角", "AKG", "beats",
        "Pro", "Max", "Plus", "Ultra", "Air", "SE",
    ]

    # 非标品关键词（孤品/纯二手，可复制性低）
    NON_STANDARD_KEYWORDS = [
        "闲置", "二手", "搬家", "不用了", "清仓", "转让", "自用",
        "瑕疵", "旧的", "旧款", "古着", "vintage", "孤品", "仅此一件",
    ]

    # 尺寸/规格关键词（半标品信号）
    SPEC_KEYWORDS = [
        "cm", "mm", "米", "寸", "英寸", "升", "L", "斤", "kg",
        "克", "g", "×", "尺", "码", "款", "色", "号", "型",
    ]

    def __init__(self, config: dict = None):
        self._cfg = config or {}
        self._load_params(self._cfg)

    def _load_params(self, cfg: dict):
        sc = cfg.get("scoring", {})
        self._grades = dict(self.DEFAULT_GRADES)
        user_grades = sc.get("product_grade_thresholds", {})
        self._grades.update(user_grades)

    def update_params(self, config: dict):
        self._cfg = config
        self._load_params(config)

    # ════════════════════════════════════════════════
    # 第1层：搜索列表预筛选（不进详情）
    # ════════════════════════════════════════════════
    def prefilter(
        self,
        items: List[dict],
        market_avg_price: float = 0,
    ) -> Tuple[List[dict], List[dict]]:
        """
        对搜索结果商品列表进行预筛选，决定哪些值得进入详情。

        输入:
          items            → search 接口返回的商品列表
          market_avg_price → 行情均价（从选词模型传入）

        返回:
          (keep_list, discard_list)
          keep_list 已按优先级排序（有soldCountLabel的优先）

        字段依据（search接口）:
          isAliMaMaAD, isAuction, price, soldCountLabel,
          serviceTags, wantNum, fishTags, userFishShopLabel
        """
        keep, discard = [], []

        for item in items:
            reason = self._check_prefilter(item, market_avg_price)
            if reason is None:
                # 计算预筛选优先级分
                item["_priority"] = self._calc_search_priority(item, market_avg_price)
                keep.append(item)
            else:
                item["_discard_reason"] = reason
                discard.append(item)

        # 按优先级降序排列（有销量 + 有服务标签 + 价格合理的优先）
        keep.sort(key=lambda x: x.get("_priority", 0), reverse=True)
        return keep, discard

    def _check_prefilter(self, item: dict, avg_price: float) -> Optional[str]:
        """
        返回 None = 通过，str = 淘汰原因

        判断顺序（从快到慢）:
          1. 硬性淘汰（广告/拍卖）
          2. 价格异常
          3. 负面服务标签
        """
        # 1. 广告和拍卖
        if str(item.get("isAliMaMaAD", "false")).lower() == "true":
            return "广告商品"
        if str(item.get("isAuction", "false")).lower() == "true":
            return "拍卖商品（价格不确定）"

        # 2. 价格解析：超均价无利润，低于均价有加价空间
        price = self._parse_price(item.get("price", "0"))
        if price <= 0:
            return "无法解析价格"
        if avg_price > 0 and price > avg_price:
            return f"定价¥{price:.0f} > 行情均价¥{avg_price:.0f}，无利润空间"

        # 3. 负面服务标签（服务标签可能是字符串列表或dict列表）
        tags = item.get("serviceTags", []) or []
        neg_keywords = ["描述不符", "退货率高", "投诉"]
        pos_keywords = ["包邮", "百分百好评", "24小时发货", "48小时发货", "卖家信用极好", "已售"]
        neg = 0
        pos = 0
        for t in tags:
            content = t if isinstance(t, str) else (t.get("content", "") or "")
            if any(w in content for w in neg_keywords):
                neg += 1
            if any(w in content for w in pos_keywords):
                pos += 1
        if neg > pos:
            return "负面服务标签过多"

        return None  # 通过

    def _calc_search_priority(self, item: dict, avg_price: float) -> float:
        """
        计算搜索层优先级分（0-100），决定进入详情的顺序。
        不是最终评分，只用于排序。
        """
        score = 0

        # 已售标签（最强信号）
        sold_label = item.get("soldCountLabel", "") or ""
        sold_cnt = self._parse_sold_label(sold_label)
        if sold_cnt >= 1000:  score += 40
        elif sold_cnt >= 100:  score += 30
        elif sold_cnt >= 10:   score += 20
        elif sold_cnt > 0:     score += 10

        # 服务标签（正向信号，可能为字符串或dict）
        tags = item.get("serviceTags", []) or []
        for t in tags:
            c = t if isinstance(t, str) else (t.get("content", "") or "")
            if "百分百好评" in c:   score += 15
            elif "包邮" in c:       score += 10
            elif "24小时发货" in c: score += 8
            elif "已售" in c:       score += 5

        # 价格合理性
        price = self._parse_price(item.get("price", "0"))
        avg_price = self._to_float(avg_price)
        price = self._to_float(price)
        if avg_price > 0 and price > 0:
            pct = (price - avg_price) / avg_price
            if -0.2 <= pct < 0.1:    score += 15  # 价格合理区间
            elif -0.35 <= pct < -0.2: score += 8

        # 想要数
        want = self._to_int(item.get("wantNum", 0))
        if want >= 10:    score += 10
        elif want >= 1:   score += 5

        return score

    # ════════════════════════════════════════════════
    # 第2层：详情深度评分（进入详情后）
    # ════════════════════════════════════════════════
    def score_one(
        self,
        keyword: str,
        search_item: dict,
        detail: dict,
        market_avg_price: float = 0,
        no_bargain_rate: float = 0.5,
    ) -> dict:
        """
        对单个商品进行完整的6维评分。

        输入:
          keyword          → 来源搜索词
          search_item      → search接口该商品的数据
          detail           → detail接口返回，含 item{} 和 seller{}
          market_avg_price → 行情均价（来自选词模型 keyword_result["avg_price"]）
          no_bargain_rate  → 品类不砍价率（来自选词模型 keyword_result["no_bargain_rate"]）

        返回: 完整评分字典，含维度得分、关键指标、决策建议
        """
        item   = detail.get("item", {}) or {}
        seller = detail.get("seller", {}) or {}

        dims = {
            "demand_signal":      self._dim_demand_signal(item, search_item),
            "price_advantage":    self._dim_price_advantage(search_item, item,
                                                             market_avg_price, no_bargain_rate),
            "seller_verification":self._dim_seller_verification(seller),
            "timeliness":         self._dim_timeliness(item, seller),
            "supply_attribute":   self._dim_supply_attribute(search_item, item),
            "item_quality":       self._dim_item_quality(item),
        }

        raw   = sum(dims.values())       # 满分110
        score = round(raw / 110 * 100, 1)
        grade = self._grade(score)

        # 生成决策建议
        advice = self._gen_advice(grade, dims, item, seller, market_avg_price, no_bargain_rate)

        return {
            "keyword":      keyword,
            "item_id":      search_item.get("itemId", ""),
            "title":        (search_item.get("title") or item.get("title", ""))[:80],
            "price":        search_item.get("price", ""),
            "area":         search_item.get("area", ""),
            "total_raw":    round(raw, 1),
            "total_100":    score,
            "grade":        grade,
            "scores":       {k: round(v, 1) for k, v in dims.items()},
            # 关键指标
            "sold_cnt":     search_item.get("soldCount", item.get("soldCnt", "0")),
            "want_cnt":     item.get("wantCnt", "0"),
            "collect_cnt":  item.get("collectCnt", "0"),
            "browse_cnt":   item.get("browseCnt", "0"),
            "sold_price":   item.get("soldPrice", ""),
            "seller_nick":  seller.get("nick", ""),
            "seller_sold":  seller.get("hasSoldNumInteger", "0"),
            "seller_good_rate": seller.get("newGoodRatioRate", ""),
            "gmt_create":   item.get("gmtCreate", ""),
            "advice":       advice,
        }

    # ════════════════════════════════════════════════
    # 维度1：需求信号 (0-20)
    # ════════════════════════════════════════════════
    def _dim_demand_signal(self, item: dict, search_item: dict) -> float:
        """
        字段:
          search.soldCount   ★最强，单品真实已售（0-8分）— 搜索层才有，detail层soldCnt始终为0
          itemDO.wantCnt      想要数 → 日均想要（0-6分）
          itemDO.collectCnt   收藏 → 日均收藏（0-3分）
          itemDO.browseCnt    浏览 → 日均浏览（0-3分）

        v3.1: want/collect/browse 改用日均值，避免老商品靠时间堆积拿高分
        v3.2: sold 从搜索层 soldCount 取值（detail API 的 itemDO.soldCnt 始终为0）
        """
        sold = self._to_int(search_item.get("soldCount", 0)) or self._to_int(item.get("soldCnt", 0))
        want    = self._to_int(item.get("wantCnt",
                               search_item.get("wantNum", 0)))
        collect = self._to_int(item.get("collectCnt", 0))
        browse  = self._to_int(item.get("browseCnt", 0))
        days    = max(self._days_on_shelf(item), 1)

        # 已售（0-8）— 最硬的信号，不看日均
        if sold >= 500:  sold_s = 8
        elif sold >= 100: sold_s = 7
        elif sold >= 50:  sold_s = 5
        elif sold >= 10:  sold_s = 3
        elif sold >= 1:   sold_s = 1
        else:             sold_s = 0

        # 日均想要（0-6）
        daily_want = want / days
        if daily_want >= 20:   want_s = 6
        elif daily_want >= 10: want_s = 5
        elif daily_want >= 5:  want_s = 4
        elif daily_want >= 2:  want_s = 3
        elif daily_want >= 1:  want_s = 2
        elif daily_want > 0:   want_s = 1
        else:                  want_s = 0

        # 日均收藏（0-3）
        daily_collect = collect / days
        if daily_collect >= 5:    collect_s = 3
        elif daily_collect >= 2:  collect_s = 2
        elif daily_collect >= 0.5: collect_s = 1
        else:                      collect_s = 0

        # 日均浏览（0-3）
        daily_browse = browse / days
        if daily_browse >= 500:   browse_s = 3
        elif daily_browse >= 200: browse_s = 2
        elif daily_browse >= 50:  browse_s = 1
        else:                      browse_s = 0

        return sold_s + want_s + collect_s + browse_s

    # ════════════════════════════════════════════════
    # 维度2：价格优势 (0-25) ★核心
    # ════════════════════════════════════════════════
    def _dim_price_advantage(
        self, search_item: dict, item: dict,
        avg_price: float, no_bargain_rate: float
    ) -> float:
        """
        字段:
          search.price          → 标价（如"¥36.30"）
          itemDO.soldPrice      → 实际成交价（0-5分）
          itemDO.transportFee   → =0 包邮加分（0-5分）
          itemDO.promotionPriceDO → 有促销（0-3分）
          market_avg_price      → 行情均价（来自选词模型）

        核心逻辑:
          溢价率 = (标价 - 行情均价) / 行情均价
          -20%~-5% 最理想：低于市场，容易卖出，还有利润
        """
        price        = self._parse_price(search_item.get("price", "0"))
        sold_price   = self._to_float(item.get("soldPrice", 0))
        transport    = self._to_float(item.get("transportFee", -1))
        promo        = item.get("promotionPriceDO", {}) or {}
        # 确保所有数值为 float，防止 str/int 类型错误
        avg_price    = self._to_float(avg_price)
        price        = self._to_float(price)
        sold_price   = self._to_float(sold_price)

        # 溢价率得分（0-12）
        if avg_price > 0 and price > 0:
            prem = (price - avg_price) / avg_price * 100
            if -20 <= prem < -5:    prem_s = 12   # 略低于均价：最好卖
            elif -5 <= prem < 5:    prem_s = 10   # 接近均价
            elif 5 <= prem < 15:    prem_s = 7    # 略高于均价
            elif -35 <= prem < -20: prem_s = 5    # 偏低（可能品质差）
            elif 15 <= prem < 30:   prem_s = 4    # 偏高
            elif prem < -35:        prem_s = 2    # 太低（可疑）
            else:                   prem_s = 1    # 太高
        else:
            prem_s = 6  # 无行情数据，中性分

        # 成交价折扣分析（0-5）
        if sold_price > 0 and price > 0:
            disc = (price - sold_price) / price * 100
            if 0 <= disc <= 10:    sold_s = 5    # 轻微折扣：合理
            elif 10 < disc <= 20:  sold_s = 3    # 折扣较大
            elif disc < 0:         sold_s = 2    # 加价（滞销或涨价）
            else:                  sold_s = 1
        else:
            sold_s = 3

        # 包邮（0-5）
        ship_s = 5 if transport == 0 else (2 if transport > 0 else 3)

        # 促销（0-3）
        promo_s = 3 if promo.get("promotionName") else 0

        return prem_s + sold_s + ship_s + promo_s

    # ════════════════════════════════════════════════
    # 维度3：卖家验证 (0-25)
    # ════════════════════════════════════════════════
    def _dim_seller_verification(self, seller: dict) -> float:
        """
        字段:
          sellerDO.hasSoldNumInteger  ★卖家累计销量（0-8分）
          sellerDO.newGoodRatioRate    好评率（0-6分）
          sellerDO.replyRatio24h       24h回复率（0-4分）
          sellerDO.zhimaAuth           芝麻信用（0-3分）
          sellerDO.identityTags        身份认证（0-2分）
          sellerDO.itemCount           在售商品数（0-2分）

        含义: 参考成功卖家的特征，卖家销量越高 = 这个品类验证越充分
        """
        has_sold = self._to_int(seller.get("hasSoldNumInteger", 0))
        if has_sold >= 10000: sold_s = 8
        elif has_sold >= 5000: sold_s = 7
        elif has_sold >= 1000: sold_s = 6
        elif has_sold >= 500:  sold_s = 5
        elif has_sold >= 100:  sold_s = 4
        elif has_sold >= 10:   sold_s = 2
        else:                  sold_s = 1

        good_rate = self._parse_pct(seller.get("newGoodRatioRate", 0))
        if good_rate >= 99:  good_s = 6
        elif good_rate >= 97: good_s = 5
        elif good_rate >= 95: good_s = 4
        elif good_rate >= 90: good_s = 3
        elif good_rate >= 80: good_s = 2
        else:                 good_s = 0

        reply = self._parse_pct(seller.get("replyRatio24h", 0))
        if reply >= 90:   reply_s = 4
        elif reply >= 70:  reply_s = 3
        elif reply >= 50:  reply_s = 2
        else:              reply_s = 1

        zhima      = seller.get("zhimaAuth", "")
        zhima_info = seller.get("zhimaLevelInfo", {})
        zhima_s = 3 if str(zhima).lower() == "true" else (2 if zhima_info else 0)

        identity   = seller.get("identityTags", []) or []
        identity_s = 2 if identity else 0

        count = self._to_int(seller.get("itemCount", 0))
        count_s = 2 if 10 <= count <= 200 else (1 if 3 <= count <= 9 else 0)

        return sold_s + good_s + reply_s + zhima_s + identity_s + count_s

    # ════════════════════════════════════════════════
    # 维度4：时效性 (0-15)
    # ════════════════════════════════════════════════
    def _dim_timeliness(self, item: dict, seller: dict) -> float:
        """
        字段:
          itemDO.gmtCreate         → 发布时间（毫秒时间戳），越新越有平台流量扶持（0-12分）
          sellerDO.lastVisitTime   → 卖家最近访问（"2分钟前来过"），活跃=有人管（0-3分）

        v3.1: >90天老商品降至0分（原1分），老商品+0销量额外扣分
        """
        days = self._days_on_shelf(item)

        if days <= 1:    shelf = 12
        elif days <= 3:  shelf = 10
        elif days <= 7:  shelf = 8
        elif days <= 14: shelf = 6
        elif days <= 30: shelf = 4
        elif days <= 90: shelf = 2
        else:            shelf = 0  # >90天无流量扶持

        last_v = seller.get("lastVisitTime", "") or ""
        if any(w in last_v for w in ["分钟前", "刚刚"]): act_s = 3
        elif "小时前" in last_v:                         act_s = 2
        elif "天前" in last_v:                           act_s = 1
        else:                                            act_s = 0

        return shelf + act_s

    def _days_on_shelf(self, item: dict) -> int:
        """计算商品在售天数，解析失败返回0"""
        gmt = item.get("gmtCreate", "")
        if not gmt:
            return 0
        try:
            ts = int(gmt)
            ts = ts // 1000 if ts > 1e12 else ts
            return (datetime.now() - datetime.fromtimestamp(ts)).days
        except (ValueError, TypeError, OSError):
            return 0

    # ════════════════════════════════════════════════
    # 维度5：货源属性 (0-15)
    # ════════════════════════════════════════════════
    def _dim_supply_attribute(self, search_item: dict, item: dict) -> float:
        """
        字段:
          title（search 或 detail） → 标题关键词判断标品/半标品/非标品（0-10分）
          itemDO.cpvLabels         → [{propertyName:"品牌"}] 有品牌属性=标品（0-5分）

        分类逻辑:
          标品   = 品牌+型号明确（如"捷安特ATX830"），可批量找货 = 10分
          半标品 = 有规格参数（如"实木床1.8米"），可找同类 = 7分
          非标品 = 孤品/个人闲置（如"搬家处理旧车"），难找同款 = 2分
        """
        title      = (search_item.get("title") or item.get("title") or "")
        cpv_labels = item.get("cpvLabels", []) or []

        if self._is_standard(title, cpv_labels):   type_s = 10
        elif self._is_semi_standard(title):         type_s = 7
        elif self._is_non_standard(title):          type_s = 2
        else:                                       type_s = 5

        # cpvLabels 越多说明类目越精准，越容易找同款
        cat_s = 5 if len(cpv_labels) >= 2 else (3 if len(cpv_labels) == 1 else 1)

        return type_s + cat_s

    def _is_standard(self, title: str, cpv_labels: list) -> bool:
        """含品牌属性标签 OR 标题有品牌+型号特征"""
        for cl in cpv_labels:
            if cl.get("propertyName", "") in ("品牌", "型号"):
                return True
        n_brand = sum(1 for b in self.BRAND_KEYWORDS if b in title)
        if n_brand >= 2:
            return True
        if n_brand >= 1:
            if re.search(r'\d{3,}[A-Za-z]|[A-Za-z]+\d{2,}|第\d代|\d代', title):
                return True
        return False

    def _is_semi_standard(self, title: str) -> bool:
        """含规格参数或品牌关键词"""
        return (any(w in title for w in self.SPEC_KEYWORDS) or
                any(b in title for b in self.BRAND_KEYWORDS))

    def _is_non_standard(self, title: str) -> bool:
        """孤品/纯二手信号"""
        return any(w in title for w in self.NON_STANDARD_KEYWORDS)

    # ════════════════════════════════════════════════
    # 维度6：商品质量 (0-10) ★v3新增
    # ════════════════════════════════════════════════
    def _dim_item_quality(self, item: dict) -> float:
        """
        字段:
          itemDO.imageInfos   → 图片列表，>=6张表示充分展示（0-4分）
          itemDO.desc         → 描述文本长度，>500字=详细诚信（0-3分）
          itemDO.hasVideo     → 有视频=高参与度（0-1分）
          itemDO.commonTags   → [{text:"包邮"}]（0-2分）

        含义: 展示质量高 = 买家信任度高 = 转化率高 = 值得参考的商品
        """
        images = item.get("imageInfos", []) or []
        img_s  = 4 if len(images) >= 6 else (3 if len(images) >= 4 else
                 (2 if len(images) >= 2 else (1 if len(images) >= 1 else 0)))

        desc   = item.get("desc", "") or ""
        desc_s = 3 if len(desc) > 500 else (2 if len(desc) > 200 else
                 (1 if len(desc) > 50 else 0))

        video_s = 1 if str(item.get("hasVideo", "false")).lower() == "true" else 0

        tags     = item.get("commonTags", []) or []
        tag_s    = 2 if any("包邮" in str(t if isinstance(t, str) else t.get("text", "")) for t in tags) else 0

        return img_s + desc_s + video_s + tag_s

    # ════════════════════════════════════════════════
    # 决策建议生成
    # ════════════════════════════════════════════════
    def _gen_advice(
        self, grade: str, dims: dict,
        item: dict, seller: dict,
        avg_price: float, no_bargain_rate: float
    ) -> str:
        """
        根据评分结果生成具体操作建议。
        """
        if grade == "S":
            tips = ["★★★ 立刻上架！参考此商品定价和标题"]
        elif grade == "A":
            tips = ["★★ 优先上架，竞争力强"]
        elif grade == "B":
            tips = ["★ 可上架，注意控制风险"]
        elif grade == "C":
            tips = ["⚠ 观望，需进一步核实货源"]
        else:
            tips = ["✗ 跳过，换下一个"]
            return " | ".join(tips)

        # 弱项提示
        if dims["price_advantage"] < 12:
            tips.append("定价偏高，考虑降价5-10%")
        if dims["timeliness"] < 8:
            tips.append("商品偏旧，流量扶持已过，优先找新上架")
        if dims["supply_attribute"] < 8:
            tips.append("非标品难复制，需确认能找到同款货源")
        if dims["seller_verification"] < 12:
            tips.append("卖家信用一般，参考价值有限")

        # 利润提示
        price = self._parse_price(seller.get("price", "0") or "0")
        if avg_price > 0 and no_bargain_rate > 0:
            est_income = price * (1 - (1 - no_bargain_rate) * 0.15)
            tips.append(f"预估到手价约¥{est_income:.0f}（按品类{no_bargain_rate:.0%}不砍价率）")

        return " | ".join(tips)

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
        """已售700+ → 700, 已售1万+ → 10000"""
        if not text: return 0
        s = text.replace("已售", "").replace("+", "").replace("件", "").strip()
        if "万" in s:
            s = s.replace("万", "")
            try: return int(float(s) * 10000)
            except: return 0
        try: return int(s)
        except: return 0

    @staticmethod
    def _parse_price(val) -> float:
        if isinstance(val, (int, float)): return float(val)
        s = str(val).replace("¥", "").replace("￥", "").replace("元", "").replace(",", "").strip()
        m = re.search(r'[\d.]+', s)
        return float(m.group()) if m else 0

    @staticmethod
    def _parse_pct(v) -> float:
        if isinstance(v, (int, float)):
            return float(v) * 100 if float(v) <= 1 else float(v)
        s = str(v).replace("%", "").strip()
        try:
            val = float(s)
            return val * 100 if val <= 1 else val
        except:
            return 0

    @staticmethod
    def _to_float(v, d=0.0) -> float:
        try: return float(v)
        except: return d

    @staticmethod
    def _to_int(v, d=0) -> int:
        try: return int(float(v))
        except: return d


# ════════════════════════════════════════════════════════════════
# 完整选品流程示例
# ════════════════════════════════════════════════════════════════
"""
# 假设已有选词模型输出
kw_result = {
    "avg_price":       45.0,
    "no_bargain_rate": 0.65,
    "same_day_rate":   0.57,
    "best_price_zone": (35, 55),
}

scorer = ProductScorerV3()

# Step 1: 搜索层预筛选（不进详情）
keep, discarded = scorer.prefilter(search_results, kw_result["avg_price"])
print(f"预筛选: 保留{len(keep)}件, 淘汰{len(discarded)}件")
for d in discarded[:3]:
    print(f"  ✗ {d.get('title','')[:30]} → {d['_discard_reason']}")

# Step 2: 按优先级进入详情评分（只取前8件）
top_items = keep[:8]
scored = []
for item in top_items:
    detail = fetch_detail(item["itemId"])
    result = scorer.score_one(
        "手机壳", item, detail,
        kw_result["avg_price"],
        kw_result["no_bargain_rate"]
    )
    scored.append(result)
    flag = "★" if result["grade"] in ("S", "A") else "·"
    print(f"{flag} {result['title'][:30]} "
          f"→ {result['total_100']:.1f}分 {result['grade']} | {result['advice']}")

# Step 3: 筛选A级以上，按分数排序
final_list = sorted(
    [r for r in scored if r["grade"] in ("S", "A")],
    key=lambda x: -x["total_100"]
)
print(f"\n最终选品 {len(final_list)} 件，Top3:")
for r in final_list[:3]:
    print(f"  {r['total_100']}分 | {r['title'][:40]} | ¥{r['price']}")
"""
