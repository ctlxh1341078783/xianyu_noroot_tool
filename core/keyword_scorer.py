"""
选词模型引擎：5维100分评分
"""
from __future__ import annotations
from typing import Dict, List, Any, Optional

from .log_handler import get_log_handler


class KeywordScorer:
    """选词模型：需求规模 + 成交效率 + 利润确定性 + 竞争格局 + 趋势信号"""

    def __init__(self, config: dict):
        self._config = config
        self._log = get_log_handler()
        self._dims = config.get("keyword_model", {}).get("dimensions", {})
        self._grades = config.get("keyword_model", {}).get("grades", {"S": 80, "A": 65, "B": 50, "C": 0})

    def score_all(self, market_data: Dict[str, dict]) -> List[dict]:
        """对所有关键词评分，返回排序后的结果列表"""
        results = []
        for kw, data in market_data.items():
            try:
                scores = self._score_one(kw, data)
                results.append(scores)
            except Exception as e:
                self._log.warn(f"[选词] {kw} 评分异常: {e}")
                results.append({"keyword": kw, "total": 0, "grade": "N/A", "error": str(e)})

        results.sort(key=lambda x: x.get("total", 0), reverse=True)
        return results

    def _score_one(self, keyword: str, data: dict) -> dict:
        # 提取子数据
        topbar = data.get("topbar", {})
        spu_header = topbar.get("spuHeader", {}) or {}
        hs = data.get("historysale", {})
        pt = data.get("pricetrend", {})
        avg_price = self._to_float(spu_header.get("avgPrice", 0))
        avg_price_inc = self._to_float(spu_header.get("avgPriceInc", 0))

        dims = {}
        dims["demand_scale"] = self._score_demand_scale(pt)
        dims["deal_efficiency"] = self._score_deal_efficiency(hs)
        dims["profit_certainty"] = self._score_profit_certainty(hs, avg_price)
        dims["competition"] = self._score_competition(data, pt)
        dims["trend_signal"] = self._score_trend_signal(pt, avg_price_inc)

        total = sum(dims.values())
        grade = self._get_grade(total)
        return {
            "keyword": keyword,
            "total": round(total, 1),
            "grade": grade,
            "scores": {k: round(v, 1) for k, v in dims.items()},
            "avg_price": avg_price,
            "avg_price_inc": avg_price_inc,
        }

    # ── 1. 需求规模 (0-15) ──
    def _score_demand_scale(self, pt: dict) -> float:
        cfg = self._dims.get("demand_scale", {}).get("params", {})
        max_uv = cfg.get("max_uv_per_day", 50000)

        # 汇总 24h UV
        hot_trend = pt.get("hotTrendListData", {}) or {}
        total_uv = 0
        for ht in hot_trend.get("hotTrendList", []) or []:
            total_uv += self._to_int(ht.get("historySearchUv", 0))

        return min(total_uv / max_uv * 15, 15)

    # ── 2. 成交效率 (0-30) ──
    def _score_deal_efficiency(self, hs: dict) -> float:
        cfg = self._dims.get("deal_efficiency", {}).get("params", {})
        same_day_w = cfg.get("same_day_weight", 15)
        no_bargain_w = cfg.get("no_bargain_weight", 15)

        sale_list = hs.get("itemSaleList", []) or []
        if not sale_list:
            return 0

        total = len(sale_list)
        same_day = 0
        no_bargain = 0
        for si in sale_list:
            # 当天成交：发布时间 ≈ 成交时间（用 recentSoldTimeDescribe 包含"刚刚""今天"）
            desc = si.get("salesDescribe", "") or ""
            if any(w in desc for w in ["刚刚", "今天", "发布当天"]):
                same_day += 1
            # 不砍价：dealPrice ≈ publishPrice (±5%)
            dp = self._to_float(si.get("dealPrice", 0))
            pp = self._to_float(si.get("publishPrice", 0))
            if pp > 0 and abs(dp - pp) / pp < 0.05:
                no_bargain += 1

        return (same_day / total) * same_day_w + (no_bargain / total) * no_bargain_w

    # ── 3. 利润确定性 (0-25) ──
    def _score_profit_certainty(self, hs: dict, avg_price: float) -> float:
        cfg = self._dims.get("profit_certainty", {}).get("params", {})

        # 计算议价折扣率
        sale_list = hs.get("itemSaleList", []) or []
        discounts = []
        for si in sale_list:
            dp = self._to_float(si.get("dealPrice", 0))
            pp = self._to_float(si.get("publishPrice", 0))
            if pp > 0 and dp > 0:
                discounts.append((pp - dp) / pp)

        discount_rate = (sum(discounts) / len(discounts) * 100) if discounts else 0

        # 议价得分
        thresholds = cfg.get("bargain_score_thresholds", [5, 15, 25])
        bargain_scores = cfg.get("bargain_scores", [15, 10, 5, 0])
        bargain_score = 0
        for t, s in zip(thresholds, bargain_scores):
            if discount_rate < t:
                bargain_score = s
                break
        else:
            bargain_score = bargain_scores[-1] if bargain_scores else 0

        # 利润空间得分
        profit_thresholds = cfg.get("profit_space_thresholds", [200, 100, 50])
        profit_scores = cfg.get("profit_scores", [10, 6, 3, 0])
        profit_score = 0
        for t, s in zip(profit_thresholds, profit_scores):
            if avg_price > t:
                profit_score = s
                break

        return bargain_score + profit_score

    # ── 4. 竞争格局 (0-20) ──
    def _score_competition(self, data: dict, pt: dict) -> float:
        cfg = self._dims.get("competition", {}).get("params", {})

        # numFound 优先从外部注入的 search_meta 读取
        num_found = self._to_int(data.get("numFound", 0))
        if num_found == 0:
            # 降级：从 pricetrend 的 searchResControlFields 或 sqiControlFields 尝试
            # 如果都没有，返回一半的竞争分（中性）
            return max(cfg.get("ratio_scores", [20, 15, 8, 4, 0])[2], 8)

        # 日均 UV
        hot_trend = pt.get("hotTrendListData", {}) or {}
        total_uv = 0
        for ht in hot_trend.get("hotTrendList", []) or []:
            total_uv += self._to_int(ht.get("historySearchUv", 0))
        if total_uv == 0:
            total_uv = 1

        ratio = num_found / total_uv
        thresholds = cfg.get("ratio_thresholds", [2, 5, 10, 20])
        ratio_scores = cfg.get("ratio_scores", [20, 15, 8, 4, 0])

        for t, s in zip(thresholds, ratio_scores):
            if ratio < t:
                return s
        return ratio_scores[-1] if ratio_scores else 0

    # ── 5. 趋势信号 (0-10) ──
    def _score_trend_signal(self, pt: dict, avg_price_inc: float) -> float:
        cfg = self._dims.get("trend_signal", {}).get("params", {})

        declare = pt.get("declareData", {}) or {}
        hot_spot = pt.get("hotSpotIndexData", {}) or {}

        # 热度多空
        hot_bull = self._to_float(declare.get("hotBullishRatio", 0))
        hot_score = 0
        if hot_bull > cfg.get("hot_bullish_threshold", 50):
            hot_score = cfg.get("hot_bullish_score", 4)
        elif hot_bull > 40:
            hot_score = cfg.get("hot_bullish_score", 4) / 2

        # 价格趋势
        price_score = 0
        if avg_price_inc > cfg.get("price_trend_threshold", 0):
            price_score = cfg.get("price_trend_score", 4)
        elif avg_price_inc > -5:
            price_score = cfg.get("price_trend_score", 4) / 2

        # 热点指数
        hs_idx = self._to_float(hot_spot.get("hotSpotIndex", 0))
        ai_score = cfg.get("hot_spot_score", 2) if hs_idx > cfg.get("hot_spot_threshold", 1.0) else 0

        return hot_score + price_score + ai_score

    # ── 工具方法 ──
    def _get_grade(self, total: float) -> str:
        for grade, threshold in sorted(self._grades.items(), key=lambda x: -x[1]):
            if total >= threshold:
                return grade
        return "C"

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
