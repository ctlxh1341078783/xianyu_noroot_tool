"""
选品采集器：搜索翻页 + 详情 + 评论，模拟人类浏览节奏，避免风控
"""
from __future__ import annotations
import time
import random
from typing import Dict, List, Optional, Callable

from .frida_bridge import FridaBridge
from utils.log_manager import get_logger


class ProductCollector:
    """选品数据采集器，内置风控安全延迟"""

    def __init__(self, bridge: FridaBridge, max_items: int = 30, page_size: int = 20,
                 detail_interval: float = 5.0, detail_jitter: float = 2.0,
                 comment_interval: float = 3.0, comment_jitter: float = 1.5,
                 search_page_interval: float = 2.0, search_page_jitter: float = 1.0,
                 keyword_interval: float = 15.0, keyword_jitter: float = 5.0):
        self._bridge = bridge
        self._max_items = max_items
        self._page_size = page_size
        self._detail_interval = detail_interval
        self._detail_jitter = detail_jitter
        self._comment_interval = comment_interval
        self._comment_jitter = comment_jitter
        self._search_page_interval = search_page_interval
        self._search_page_jitter = search_page_jitter
        self._keyword_interval = keyword_interval
        self._keyword_jitter = keyword_jitter
        self._log = get_logger()
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def collect(self, keywords: List[str],
                progress_cb: Optional[Callable[[str, str, int, int], None]] = None) -> Dict[str, dict]:
        """
        采集所有关键词的搜索+详情+评论数据。
        每个关键词之间有随机间隔，模拟人类切换搜索词的行为。
        """
        results = {}
        total_kw = len(keywords)
        self._cancelled = False

        for ki, kw in enumerate(keywords):
            if self._cancelled:
                break

            # 关键词间延迟（第一个关键词前也稍等下）
            if ki > 0:
                delay = self._keyword_interval + random.uniform(-self._keyword_jitter, self._keyword_jitter)
                self._log.info(f"[选品] 等待 {delay:.0f}s 后开始下一个关键词...")
                time.sleep(max(1.0, delay))

            self._log.info(f"[选品] [{ki+1}/{total_kw}] {kw} 搜索+详情采集...")
            kw_data = {"keyword": kw, "search_items": [], "details": {}, "comments": {}}

            # 1. 翻页搜索
            items, search_meta = self._search_all(kw, progress_cb)
            kw_data["search_items"] = items
            kw_data["numFound"] = search_meta.get("numFound", 0)
            kw_data["searchMaxPrice"] = search_meta.get("maxPrice", "")
            kw_data["searchMinPrice"] = search_meta.get("minPrice", "")

            if not items:
                self._log.warn(f"[选品] {kw} 无搜索结果")
                results[kw] = kw_data
                continue

            total_items = len(items)
            # 2. 每个商品：详情 + 评论（带随机抖动延迟模拟人类浏览）
            for i, item in enumerate(items):
                if self._cancelled:
                    break
                item_id = item.get("itemId", "")
                if not item_id or item_id in kw_data["details"]:
                    continue

                if progress_cb:
                    progress_cb(kw, "detail", i + 1, total_items)

                # 详情请求前延迟（模拟"点进去看看"的时间）
                self._jitter_sleep(self._detail_interval, self._detail_jitter)

                try:
                    detail = self._bridge.get_detail(item_id)
                    if detail and not detail.get("error"):
                        kw_data["details"][item_id] = detail
                except Exception as e:
                    self._log.debug(f"[选品] 详情失败 {item_id}: {e}")

                # 评论请求前延迟（模拟"翻看评论"的时间）
                self._jitter_sleep(self._comment_interval, self._comment_jitter)

                try:
                    comments = self._bridge.get_comments(item_id)
                    if comments and not comments.get("error"):
                        kw_data["comments"][item_id] = comments
                except Exception as e:
                    self._log.debug(f"[选品] 评论失败 {item_id}: {e}")

            results[kw] = kw_data
            self._log.info(f"[选品] {kw}: {len(items)}搜索 {len(kw_data['details'])}详情 {len(kw_data['comments'])}评论")

        return results

    def _search_all(self, keyword: str, progress_cb=None) -> tuple:
        all_items = []
        search_meta = {"numFound": 0, "maxPrice": "", "minPrice": ""}
        page = 1

        while len(all_items) < self._max_items and not self._cancelled:
            result = self._bridge.search(keyword, page, self._page_size)
            if result.get("error"):
                self._log.warn(f"[搜索] {keyword} 第{page}页失败: {result.get('error')}")
                break

            items = result.get("items", [])
            has_more = result.get("hasMore", False)
            all_items.extend(items)

            if page == 1:
                search_meta["numFound"] = result.get("numFound", 0)
                search_meta["maxPrice"] = result.get("maxPrice", "")
                search_meta["minPrice"] = result.get("minPrice", "")

            self._log.info(f"[搜索] {keyword} 第{page}页: {len(items)}条, 累计{len(all_items)}/{self._max_items}, numFound={search_meta['numFound']}")

            if not has_more or len(items) == 0:
                break
            page += 1
            # 搜索翻页延迟（模拟人类下滑翻页）
            self._jitter_sleep(self._search_page_interval, self._search_page_jitter)

        return all_items[:self._max_items], search_meta

    @staticmethod
    def _jitter_sleep(base: float, jitter: float):
        delay = max(0.5, base + random.uniform(-jitter, jitter))
        time.sleep(delay)
