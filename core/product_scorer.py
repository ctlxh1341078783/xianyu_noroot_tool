"""
选品模型引擎：5维100分评分
"""
from __future__ import annotations
from typing import Dict, List, Any, Optional
from datetime import datetime, timezone

from .log_handler import get_log_handler


class ProductScorer:
    """选品模型：需求信号 + 价格优势 + 卖家验证 + 时效性 + 货源属性"""

    # 标品关键词（品牌+型号模式）
    BRAND_PATTERNS = [
        "捷安特", "美利达", "喜德盛", "凤凰", "永久", "飞鸽", "大行",
        "华为", "小米", "苹果", "三星", "OPPO", "vivo", "联想", "戴尔",
        "格力", "美的", "海尔", "苏泊尔", "九阳", "松下",
        "型号", "ATX", "XTC", "Pro", "Max", "Plus", "Ultra",
    ]

    def __init__(self, config: dict):
        self._config = config
        self._log = get_log_handler()
        self._dims = config.get("product_model", {}).get("dimensions", {})
        self._grades = config.get("product_model", {}).get("grades", {"S": 80, "A": 65, "B": 50, "C": 35, "D": 0})

    def score_all(self, product_data: Dict[str, dict],
                  market_data: Optional[Dict[str, dict]] = None) -> List[dict]:
        """
        对所有商品评分。
        product_data: {keyword: {search_items, details, comments, ...}}
        market_data: {keyword: {topbar, historysale, ...}} 用于价格对照
        """
        results = []
        for kw, data in product_data.items():
            market = (market_data or {}).get(kw, {})
            kw_results = self._score_keyword(kw, data, market)
            results.extend(kw_results)

        results.sort(key=lambda x: x.get("total", 0), reverse=True)
        return results

    def _score_keyword(self, keyword: str, data: dict, market: dict) -> List[dict]:
        items = data.get("search_items", [])
        details = data.get("details", {})

        # 行情均价（用于价格优势计算）
        topbar = market.get("topbar", {})
        spu_header = topbar.get("spuHeader", {}) or {}
        market_avg_price = self._to_float(spu_header.get("avgPrice", 0))

        # 品类不砍价率（用于利润预估）
        hs = market.get("historysale", {})
        no_bargain_rate = self._calc_no_bargain_rate(hs)

        results = []
        for item in items:
            try:
                item_id = item.get("itemId", "")
                detail = details.get(item_id, {})
                scores = self._score_one(keyword, item, detail, market_avg_price, no_bargain_rate)
                results.append(scores)
            except Exception as e:
                self._log.debug(f"[选品] {item.get('itemId','')} 评分异常: {e}")

        return results

    def _score_one(self, keyword: str, item: dict, detail: dict,
                   market_avg_price: float, no_bargain_rate: float) -> dict:
        item_do = detail.get("itemDO", {}) or {}
        seller = detail.get("sellerDO", {}) or {}

        dims = {}
        dims["demand_signal"] = self._score_demand_signal(item_do, item)
        dims["price_advantage"] = self._score_price_advantage(item, market_avg_price, no_bargain_rate)
        dims["seller_verification"] = self._score_seller_verification(seller)
        dims["timeliness"] = self._score_timeliness(item_do)
        dims["supply_attribute"] = self._score_supply_attribute(item, market)

        total = sum(dims.values())
        grade = self._get_grade(total)

        return {
            "keyword": keyword,
            "item_id": item.get("itemId", ""),
            "title": item.get("title", "")[:100],
            "price": item.get("price", ""),
            "total": round(total, 1),
            "grade": grade,
            "scores": {k: round(v, 1) for k, v in dims.items()},
            "want_cnt": item_do.get("wantCnt", item.get("want", 0)),
            "browse_cnt": item_do.get("browseCnt", 0),
            "collect_cnt": item_do.get("collectCnt", 0),
            "seller_nick": item.get("userNick", ""),
            "area": item.get("area", ""),
            "gmt_create": item_do.get("gmtCreate", ""),
        }

    # ── 1. 需求信号 (0-20) ──
    def _score_demand_signal(self, item_do: dict, item: dict) -> float:
        cfg = self._dims.get("demand_signal", {}).get("params", {})

        browse = self._to_int(item_do.get("browseCnt", 0))
        collect = self._to_int(item_do.get("collectCnt", 0))
        want = self._to_int(item_do.get("wantCnt", item.get("want", 0)))

        # 浏览
        browse_score = self._piecewise(browse, cfg.get("browse_thresholds", [10000, 5000, 1000, 100]),
                                       cfg.get("browse_scores", [8, 6, 4, 2, 0]))
        # 收藏
        collect_score = self._piecewise(collect, cfg.get("collect_thresholds", [100, 50, 10, 1]),
                                        cfg.get("collect_scores", [7, 5, 3, 1, 0]))
        # 想要
        want_score = self._piecewise(want, cfg.get("want_thresholds", [100, 50, 10, 1]),
                                     cfg.get("want_scores", [5, 3, 2, 1, 0]))

        return browse_score + collect_score + want_score

    # ── 2. 价格优势 (0-25) ──
    def _score_price_advantage(self, item: dict, market_avg_price: float, no_bargain_rate: float) -> float:
        cfg = self._dims.get("price_advantage", {}).get("params", {})

        price_str = item.get("price", "0")
        price = self._parse_price(price_str)

        # 溢价率
        if market_avg_price > 0:
            premium_pct = (price - market_avg_price) / market_avg_price * 100
        else:
            premium_pct = 0

        premium_score = self._piecewise(premium_pct, cfg.get("premium_thresholds", [-20, -10, 0, 10, 20]),
                                        cfg.get("premium_scores", [15, 12, 10, 6, 3, 0]))

        # 利润预估（假设货源成本为价格的某个比例）
        # 实际到手价 = 价格 × (1 - 不砍价率折扣)
        effective_price = price * (1 - no_bargain_rate * 0.5)
        # 假设货源成本 = 价格的 50%（可配置）
        cost_ratio = 0.5
        profit_ratio = effective_price / (price * cost_ratio) if price > 0 else 0

        profit_score = self._piecewise(profit_ratio, cfg.get("profit_ratio_thresholds", [1.5, 1.3, 1.2, 1.1]),
                                       cfg.get("profit_ratio_scores", [10, 8, 6, 3, 0]))

        return premium_score + profit_score

    # ── 3. 卖家验证 (0-25) ──
    def _score_seller_verification(self, seller: dict) -> float:
        cfg = self._dims.get("seller_verification", {}).get("params", {})

        # 已售
        sold = self._to_int(seller.get("hasSoldNumInteger", 0))
        sold_score = self._piecewise(sold, cfg.get("sold_thresholds", [5000, 1000, 500, 100, 10]),
                                     cfg.get("sold_scores", [10, 8, 6, 4, 2, 0]))

        # 好评率 (如 "97%" → 97, 或 "0.97" → 97)
        good_rate_val = self._parse_percent(seller.get("newGoodRatioRate", 0))
        good_score = self._piecewise(good_rate_val, cfg.get("good_rate_thresholds", [99, 97, 95, 90, 80]),
                                     cfg.get("good_rate_scores", [8, 6, 5, 3, 1, 0]))

        # 回复率
        reply_val = self._parse_percent(seller.get("replyRatio24h", 0))
        reply_score = self._piecewise(reply_val, cfg.get("reply_rate_thresholds", [95, 80, 50]),
                                      cfg.get("reply_rate_scores", [4, 3, 1, 0]))

        # 在售商品数
        stock = self._to_int(seller.get("itemCount", 0))
        stock_score = self._piecewise(stock, cfg.get("stock_count_thresholds", [50, 10]),
                                      cfg.get("stock_count_scores", [3, 2, 0]))

        return sold_score + good_score + reply_score + stock_score

    # ── 4. 时效性 (0-15) ──
    def _score_timeliness(self, item_do: dict) -> float:
        cfg = self._dims.get("timeliness", {}).get("params", {})

        gmt_create = item_do.get("gmtCreate", "")
        if not gmt_create:
            return 0

        try:
            ts = int(gmt_create)
            if ts > 1e12:
                ts //= 1000
            create_dt = datetime.fromtimestamp(ts)
            days = (datetime.now() - create_dt).days
        except:
            return 0

        return self._piecewise(days, cfg.get("shelf_day_thresholds", [3, 7, 30, 90]),
                               cfg.get("shelf_day_scores", [10, 8, 5, 2, 0]))

    # ── 5. 货源属性 (0-15) ──
    def _score_supply_attribute(self, item: dict, market: dict) -> float:
        cfg = self._dims.get("supply_attribute", {}).get("params", {})

        title = item.get("title", "")
        # 判断商品类型
        if self._is_standard_product(title):
            type_score = cfg.get("product_type_scores", {}).get("standard", 10)
        elif self._is_semi_standard(title):
            type_score = cfg.get("product_type_scores", {}).get("semi_standard", 7)
        elif self._is_non_standard(title):
            type_score = cfg.get("product_type_scores", {}).get("non_standard", 2)
        else:
            type_score = cfg.get("product_type_scores", {}).get("unknown", 5)

        # 价格波动率
        pt = market.get("pricetrend", {})
        prices = []
        for pp in pt.get("priceTrendList", []) or []:
            p = self._to_float(pp.get("dayAvgPrice", 0))
            if p > 0:
                prices.append(p)
        vol_score = 0
        if prices:
            avg = sum(prices) / len(prices)
            std = (sum((p - avg) ** 2 for p in prices) / len(prices)) ** 0.5
            vol_pct = (std / avg * 100) if avg > 0 else 0
            thresholds = cfg.get("price_volatility_thresholds", [10, 25])
            vol_scores = cfg.get("price_volatility_scores", [5, 3, 0])
            vol_score = self._piecewise(vol_pct, thresholds, vol_scores)

        return type_score + vol_score

    # ── 商品类型判断 ──
    def _is_standard_product(self, title: str) -> bool:
        """含明确品牌+型号 → 标品"""
        brand_count = sum(1 for p in self.BRAND_PATTERNS if p in title)
        return brand_count >= 2 or (
            brand_count >= 1 and any(c.isdigit() or c.isalpha() and c.upper() == c
                                     for c in title.split() if len(c) >= 3 and any(d.isdigit() for d in c)))

    def _is_semi_standard(self, title: str) -> bool:
        """含品类+规格 → 半标品"""
        spec_keywords = ["cm", "mm", "m", "米", "寸", "英寸", "升", "L", "斤", "kg", "克", "g",
                        "x", "X", "×", "尺", "码", "款"]
        return any(w in title for w in spec_keywords) or any(p in title for p in self.BRAND_PATTERNS)

    def _is_non_standard(self, title: str) -> bool:
        """孤品/纯二手 → 非标品"""
        kw = ["闲置", "二手", "家里", "搬家", "不用了", "清仓", "转让", "自用", "瑕疵",
              "旧的", "古着", "vintage", "孤品"]
        return any(w in title for w in kw)

    # ── 工具 ──
    @staticmethod
    def _calc_no_bargain_rate(hs: dict) -> float:
        sale_list = hs.get("itemSaleList", []) or []
        if not sale_list:
            return 0.5
        no_bargain = 0
        for si in sale_list:
            dp = ProductScorer._to_float(si.get("dealPrice", 0))
            pp = ProductScorer._to_float(si.get("publishPrice", 0))
            if pp > 0 and abs(dp - pp) / pp < 0.05:
                no_bargain += 1
        return no_bargain / len(sale_list)

    def _get_grade(self, total: float) -> str:
        for grade in sorted(self._grades, key=lambda x: -self._grades[x]):
            if total >= self._grades[grade]:
                return grade
        return "D"

    @staticmethod
    def _to_float(v, default=0.0) -> float:
        try:
            return float(v)
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _to_int(v, default=0) -> int:
        try:
            return int(float(v))
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _piecewise(value: float, thresholds: list, scores: list) -> float:
        """分段打分：value >= threshold[0] 得 scores[0]，以此类推，否则 scores[-1]"""
        for t, s in zip(thresholds, scores):
            if value >= t:
                return float(s)
        return float(scores[-1]) if scores else 0

    @staticmethod
    def _parse_price(price_str) -> float:
        """解析价格字符串，如 '¥42.5', '42.5', '42 - 50'"""
        if isinstance(price_str, (int, float)):
            return float(price_str)
        s = str(price_str).replace("¥", "").replace("￥", "").replace("元", "").replace(",", "").strip()
        # 取第一个数字
        import re
        m = re.search(r'[\d.]+', s)
        return float(m.group()) if m else 0

    @staticmethod
    def _parse_percent(v) -> float:
        """解析百分比，如 '97%' → 97, 0.97 → 97"""
        if isinstance(v, (int, float)):
            return float(v) * 100 if float(v) <= 1 else float(v)
        s = str(v).replace("%", "").strip()
        try:
            val = float(s)
            return val * 100 if val <= 1 else val
        except:
            return 0
