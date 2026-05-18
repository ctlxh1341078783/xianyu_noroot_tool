"""
行情采集器：调用 4 个行情 API（tab列表→概况→成交记录→价格趋势），带风控延迟
"""
from __future__ import annotations
import time
import random
from typing import Dict, List, Optional, Callable

from .frida_bridge import FridaBridge
from utils.log_manager import get_logger


class MarketCollector:
    """行情数据采集器，内置风控安全延迟"""

    def __init__(self, bridge: FridaBridge, hs_max_pages: int = 3, hs_page_size: int = 6,
                 step_interval: float = 3.0, step_jitter: float = 1.5,
                 keyword_interval: float = 10.0, keyword_jitter: float = 4.0):
        self._bridge = bridge
        self._hs_max_pages = hs_max_pages
        self._hs_page_size = hs_page_size
        self._step_interval = step_interval
        self._step_jitter = step_jitter
        self._keyword_interval = keyword_interval
        self._keyword_jitter = keyword_jitter
        self._log = get_logger()
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def collect(self, keywords: List[str],
                progress_cb: Optional[Callable[[str, str, int, int], None]] = None) -> Dict[str, dict]:
        """
        采集所有关键词的行情数据。
        progress_cb(keyword, step, current, total) - step: 'search'|'topbar'|'historysale'|'pricetrend'
        """
        results = {}
        total = len(keywords)
        self._cancelled = False

        for idx, kw in enumerate(keywords):
            if self._cancelled:
                self._log.warn("[行情] 采集已取消")
                break

            # 关键词间延迟
            if idx > 0:
                delay = self._keyword_interval + random.uniform(-self._keyword_jitter, self._keyword_jitter)
                self._log.info(f"[行情] 等待 {delay:.0f}s 后开始下一个关键词...")
                time.sleep(max(1.0, delay))

            self._log.info(f"[行情] [{idx+1}/{total}] {kw} 开始采集...")
            if progress_cb:
                progress_cb(kw, "init", idx + 1, total)

            market_data = self._collect_one(kw, progress_cb)
            results[kw] = market_data

        return results

    def _collect_one(self, keyword: str, progress_cb=None) -> dict:
        """采集单个关键词的完整行情数据"""
        result = {}

        # Step 0: 发搜索请求获取 market tab list（包含 spuId/categoryId）
        self._log.info(f"[行情] {keyword} Step0: 获取市场 Tab 列表...")
        if progress_cb:
            progress_cb(keyword, "search", 0, 4)

        search_result = self._bridge.search(keyword, 1, 20)
        num_found = search_result.get("numFound", 0) if isinstance(search_result, dict) else 0
        self._step_sleep()

        tabs = self._bridge.get_market_tabs(keyword)
        if isinstance(tabs, dict) and tabs.get("error"):
            result["tabs"] = tabs
            return result
        result["tabs"] = tabs
        result["numFound"] = num_found

        spu_id, category_id, spu_name, category_name = self._parse_tabs(tabs)
        if not spu_id or not category_id:
            self._log.warn(f"[行情] {keyword} 未找到 spuId/categoryId，跳过后续")
            return result

        self._log.info(f"[行情] {keyword} spuId={spu_id}, categoryId={category_id}, spuName={spu_name}")

        # Step 1: 行情概况
        self._log.info(f"[行情] {keyword} Step1: 行情概况...")
        if progress_cb:
            progress_cb(keyword, "topbar", 1, 4)
        self._step_sleep()
        topbar = self._bridge.get_market_topbar(keyword, spu_id, category_id, spu_name, category_name)
        result["topbar"] = topbar

        # Step 2: 成交记录（翻页）
        self._log.info(f"[行情] {keyword} Step2: 成交记录 (最多{self._hs_max_pages}页)...")
        all_sale_items = []
        hs_last = {}
        for page in range(1, self._hs_max_pages + 1):
            if progress_cb:
                progress_cb(keyword, f"historysale_p{page}", 2, 4)
            self._step_sleep()
            hs_data = self._bridge.get_market_history_sale(keyword, spu_id, category_id, spu_name, category_name, page)
            if isinstance(hs_data, dict) and hs_data.get("error"):
                break
            hs_last = hs_data
            sale_items = hs_data.get("itemSaleList", [])
            if not sale_items:
                break
            all_sale_items.extend(sale_items)

        result["historysale"] = {
            "historyMaxPrice": hs_last.get("historyMaxPrice"),
            "historyMinPrice": hs_last.get("historyMinPrice"),
            "historyOrder": hs_last.get("historyOrder"),
            "itemSaleList": all_sale_items,
        }
        self._log.info(f"[行情] {keyword} 成交记录: {len(all_sale_items)} 条")

        # Step 3: 价格趋势
        self._log.info(f"[行情] {keyword} Step3: 价格趋势...")
        if progress_cb:
            progress_cb(keyword, "pricetrend", 3, 4)
        self._step_sleep()
        trend = self._bridge.get_market_price_trend(keyword, spu_id, category_id, spu_name, category_name)
        result["pricetrend"] = trend

        if progress_cb:
            progress_cb(keyword, "done", 4, 4)
        self._log.info(f"[行情] {keyword} 采集完成")

        return result

    def _step_sleep(self):
        delay = max(1.0, self._step_interval + random.uniform(-self._step_jitter, self._step_jitter))
        time.sleep(delay)

    @staticmethod
    def _parse_tabs(tabs: dict) -> tuple:
        spu_id = ""
        category_id = ""
        spu_name = ""
        category_name = ""
        try:
            for tab in tabs.get("result", []):
                if tab.get("searchTabType") == "SEARCH_TAB_MARKET" and tab.get("extra"):
                    spu_id = tab["extra"].get("spuId", "")
                    category_id = tab["extra"].get("categoryId", "")
                    spu_name = tab["extra"].get("spuName", "")
                    category_name = tab["extra"].get("categoryName", "")
        except:
            pass
        return spu_id, category_id, spu_name, category_name
