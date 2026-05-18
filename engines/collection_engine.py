"""采集引擎：三阶段流水线编排（后台线程），真实Frida RPC + 评分 + 推送货源 + 断点续传"""
import json
import random
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from engines.device_engine import DeviceEngine
from core.frida_bridge import FridaBridge
from engines.keyword_scorer_v3 import KeywordScorerV3
from utils.log_manager import get_logger

STATE_VERSION = 2


def jitter(lo: float, hi: float) -> float:
    return random.uniform(lo, hi)


STATE_MAX_AGE_SEC = 7200  # 断点文件最大有效时间（2小时），超过视为无效

def _load_state(state_path: Path) -> Optional[dict]:
    """加载断点状态文件，校验版本、完整性和时效性"""
    if not state_path.exists():
        return None
    try:
        raw = state_path.read_text(encoding="utf-8")
        state = json.loads(raw)
    except (json.JSONDecodeError, OSError):
        return None
    if state.get("version") != STATE_VERSION:
        return None
    if not state.get("keywords"):
        return None
    # 时效性检查：超过 STATE_MAX_AGE_SEC 的断点视为无效，避免陈旧恢复
    ts_str = state.get("timestamp", "")
    if ts_str:
        try:
            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            age = (datetime.now() - ts).total_seconds()
            if age > STATE_MAX_AGE_SEC:
                return None
        except ValueError:
            pass
    return state


def _save_state(state_path: Path, state: dict):
    """原子写入断点状态（先写临时文件再替换）"""
    state["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state["version"] = STATE_VERSION
    tmp = state_path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(state_path)
    except OSError:
        pass


# ═══ 行情缓存（TTL 1小时，避免重复 RPC）═══
MARKET_CACHE_TTL_SEC = 3600


def _market_cache_path(cache_dir: Path, kw: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in kw)
    return cache_dir / f"market_{safe}.json"


def _market_cache_get(cache_dir: Path, kw: str) -> Optional[dict]:
    p = _market_cache_path(cache_dir, kw)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        age = time.time() - data.get("_cached_at", 0)
        if age > MARKET_CACHE_TTL_SEC:
            p.unlink(missing_ok=True)
            return None
        return data.get("_payload")
    except (json.JSONDecodeError, OSError):
        p.unlink(missing_ok=True)
        return None


def _market_cache_put(cache_dir: Path, kw: str, payload: dict):
    cache_dir.mkdir(parents=True, exist_ok=True)
    p = _market_cache_path(cache_dir, kw)
    try:
        p.write_text(json.dumps({
            "_cached_at": time.time(),
            "_payload": payload
        }, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


class CollectionEngine:
    def __init__(self, device_engine: DeviceEngine, settings: dict):
        self._dev = device_engine
        self._settings = settings
        self._logger = get_logger()
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # 评分引擎（外部注入）
        self._kw_scorer = None
        self._pd_scorer = None
        self._supply_engine = None

        # Frida 组件
        self._bridge: Optional[FridaBridge] = None

        # 回调
        self._on_stage: Optional[Callable[[str, str, int, int], None]] = None
        self._on_keyword: Optional[Callable[[str, int, int, str, int, bool, int, int], None]] = None
        self._on_product: Optional[Callable[[dict], None]] = None
        self._on_complete: Optional[Callable[[list, list, list], None]] = None

        # 配置
        c = settings.get("collection", {})
        self._search_pages = c.get("search_pages", 10)
        self._hs_pages = c.get("hs_pages", 3)
        self._detail_max = c.get("detail_max", 5)
        self._comment_max = c.get("comment_max", 3)
        self._rate_search = c.get("rate_search", [4, 8])
        self._rate_detail = c.get("rate_detail", [3, 6])
        self._rate_comment = c.get("rate_comment", [3, 6])
        self._rate_market = c.get("rate_market", [3, 5])
        self._rate_keyword = c.get("rate_keyword", [8, 15])
        # 推送阈值：关键词评分达此分才进选品阶段，商品评分达此分才推货源
        self._kw_push_threshold = c.get("kw_push_threshold", 75)
        self._pd_push_threshold = c.get("pd_push_threshold", 75)

    @property
    def running(self) -> bool:
        return self._running

    def set_scorers(self, kw_scorer, pd_scorer):
        self._kw_scorer = kw_scorer
        self._pd_scorer = pd_scorer

    def set_supply_engine(self, supply_engine):
        self._supply_engine = supply_engine

    def update_thresholds(self, kw_push: int = None, pd_push: int = None):
        """运行时更新推送阈值（设置保存后调用）"""
        if kw_push is not None:
            self._kw_push_threshold = kw_push
        if pd_push is not None:
            self._pd_push_threshold = pd_push

    def update_params(self, settings: dict):
        """运行时刷新全部采集参数（设置保存后调用）"""
        c = settings.get("collection", {})
        self._search_pages = c.get("search_pages", 10)
        self._hs_pages = c.get("hs_pages", 3)
        self._detail_max = c.get("detail_max", 5)
        self._comment_max = c.get("comment_max", 3)
        self._rate_search = c.get("rate_search", [4, 8])
        self._rate_detail = c.get("rate_detail", [3, 6])
        self._rate_comment = c.get("rate_comment", [3, 6])
        self._rate_market = c.get("rate_market", [3, 5])
        self._rate_keyword = c.get("rate_keyword", [8, 15])
        self._kw_push_threshold = c.get("kw_push_threshold", 75)
        self._pd_push_threshold = c.get("pd_push_threshold", 75)
        self._logger.info(f"[采集] 参数已刷新: hs_pages={self._hs_pages}, detail={self._detail_max}, comment={self._comment_max}, kw_push={self._kw_push_threshold}, pd_push={self._pd_push_threshold}")

    def set_callbacks(self, on_stage=None, on_keyword=None, on_product=None, on_complete=None):
        # on_stage: (stage: str, info: str, done: int, total: int)
        # on_keyword: (kw, idx, total, status, search_cnt, has_market, detail_cnt, comment_cnt, market_uv, market_price_inc)
        self._on_stage = on_stage
        self._on_keyword = on_keyword
        self._on_product = on_product
        self._on_complete = on_complete

    def start(self, keywords: List[str], output_dir: Path):
        if self._running:
            self._logger.warn("采集已在运行中")
            return

        # 前置守护检查：确保 LSPatch → 闲鱼App → Frida Gadget 全部就绪
        if self._dev:
            self._logger.info("[采集] 前置检查：确保手机服务就绪...")
            if not self._dev.ensure_services_ready():
                self._logger.error("[采集] 手机服务未就绪，采集中止")
                return

        self._running = True
        self._thread = threading.Thread(target=self._run, args=(keywords, output_dir), daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        self._logger.info("采集已停止")

    def _init_bridge(self) -> bool:
        if self._bridge and self._bridge.loaded:
            return True
        if not self._dev or not self._dev.manager:
            self._logger.error("[采集] 设备引擎未初始化")
            return False
        self._bridge = FridaBridge(self._dev.manager)
        if not self._bridge.load():
            self._logger.error("[采集] Frida桥接初始化失败，请确认设备已连接且App在运行")
            return False
        self._logger.info("[采集] Frida桥接就绪")
        return True

    def _notify_stage(self, stage: str, info: str, done: int = 1, total: int = 0):
        if self._on_stage:
            self._on_stage(stage, info, done, total)

    def _notify_kw(self, kw: str, idx: int, total: int, status: str,
                   search_cnt: int = 0, has_market: bool = False,
                   detail_cnt: int = 0, comment_cnt: int = 0,
                   market_uv: int = 0, market_price_inc: float = 0):
        if self._on_keyword:
            self._on_keyword(kw, idx, total, status,
                             search_cnt, has_market, detail_cnt, comment_cnt,
                             market_uv, market_price_inc)

    def _on_js_progress(self, stage: str, kw: str, done: int, total: int):
        """闭环采集 JS 进度回调 → UI 阶段进度条"""
        if stage.startswith("market_"):
            if stage == "market_hs_batch":
                self._notify_stage("market", f"{kw} 成交记录: {total}页并行采集...", done=0, total=total)
            elif stage == "market_hs_done":
                self._notify_stage("market", f"{kw} 成交记录: {done}条", done=done, total=done)
            elif stage == "market_hs":
                self._notify_stage("market", f"{kw} 成交记录: {done}/{total}页", done=done, total=total)
            elif stage != "market_done":
                label = {"market_search": "搜索", "market_tabs": "Tabs", "market_topbar": "概况", "market_trend": "趋势"}.get(stage, stage)
                self._notify_stage("market", f"{kw} {label}", done=0, total=0)
        elif stage == "search":
            self._notify_stage("_info", f"{kw} 搜索: {done}/{total}页", done=0, total=0)
        elif stage == "detail_batch":
            self._notify_stage("_info", f"{kw} 详情: {total}个并行采集...", done=0, total=0)
        elif stage == "detail_done":
            self._notify_stage("_info", f"{kw} 详情: {done}个完成", done=0, total=0)
        elif stage == "comment_batch":
            self._notify_stage("_info", f"{kw} 评论: {total}个并行采集...", done=0, total=0)
        elif stage == "comment_done":
            self._notify_stage("_info", f"{kw} 评论: {done}个完成", done=0, total=0)
        # 保留旧版兼容（逐个采集模式）
        elif stage == "detail":
            self._notify_stage("_info", f"{kw} 详情: {done}/{total}", done=0, total=0)
        elif stage == "comment":
            self._notify_stage("_info", f"{kw} 评论: {done}/{total}", done=0, total=0)

    def _run(self, keywords: List[str], output_dir: Path):
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        state_path = output_dir / "_pipeline_state.json"

        # ─── 断点恢复 ───
        state = _load_state(state_path)
        if state and state.get("keywords") == keywords:
            all_kw_results = state.get("all_kw_results", [])
            all_pd_results = state.get("all_pd_results", [])
            all_supply_pushed = state.get("all_supply_pushed", [])
            market_results = state.get("market_results", {})
            kw_score_map = state.get("kw_score_map", {})
            # 断点存储的是 (kw, total_100数字)，恢复时从 kw_score_map 取完整 dict
            a_plus_keywords = [(kw, kw_score_map.get(kw, {"total_100": s}))
                               for kw, s in state.get("a_plus_keywords", [])]
            product_results = state.get("product_results", {})
            last_stage = state.get("stage", "market")
            self._logger.info(f"[采集] 断点恢复: 从阶段 '{last_stage}' 继续，已完成 {len(all_kw_results)} 词评分 {len(all_pd_results)} 商品")
        else:
            all_kw_results = []
            all_pd_results = []
            all_supply_pushed = []
            market_results = {}
            a_plus_keywords = []
            kw_score_map = {}
            product_results = {}
            last_stage = "market"
            # 首次启动保存初始状态
            _save_state(state_path, {
                "keywords": keywords,
                "output_dir": str(output_dir),
                "stage": "market",
                "market_results": {},
                "all_kw_results": [],
                "a_plus_keywords": [],
                "kw_score_map": {},
                "product_results": {},
                "all_pd_results": [],
                "all_supply_pushed": [],
            })

        total_kw = len(keywords)

        def _checkpoint(stage: str):
            """保存当前进度到状态文件"""
            _save_state(state_path, {
                "keywords": keywords,
                "output_dir": str(output_dir),
                "stage": stage,
                "market_results": market_results,
                "all_kw_results": all_kw_results,
                "a_plus_keywords": [(kw, s.get("total_100", 0)) for kw, s in a_plus_keywords],
                "kw_score_map": {kw: {"total_100": s.get("total_100", 0), "avg_price": s.get("avg_price", 0),
                                      "no_bargain_rate": s.get("no_bargain_rate", 0.5)}
                                 for kw, s in (kw_score_map.items() or {})},
                "product_results": product_results,
                "all_pd_results": all_pd_results,
                "all_supply_pushed": all_supply_pushed,
            })

        self._logger.info(f"[采集] ===== 开始全自动流水线: {total_kw} 个关键词 =====")

        # ═══════════ 阶段1: 行情采集 ═══════════
        if last_stage in ("market",):
            if not self._running:
                return self._finish(all_kw_results, all_pd_results, all_supply_pushed, output_dir)
            if not self._init_bridge():
                self._running = False
                return

            self._notify_stage("market", "阶段1: 行情闭环采集", done=0, total=total_kw)
            self._logger.info(f"[采集] 阶段1: 行情闭环采集 ({total_kw}词)")

            # 注册 JS 进度回调（行情 + 选品共用）
            self._bridge.set_progress_callback(self._on_js_progress)

            market_cache_dir = output_dir / "_market_cache"

            for idx, kw in enumerate(keywords):
                if kw in market_results:
                    continue  # 已采集过的跳过
                if not self._running:
                    return self._finish(all_kw_results, all_pd_results, all_supply_pushed, output_dir)

                # 先查缓存
                cached = _market_cache_get(market_cache_dir, kw)
                if cached is not None:
                    market_results[kw] = cached
                    has_market = cached.get("hasMarket", False)
                    uv_24h = KeywordScorerV3._calc_24h_uv(cached) if self._kw_scorer else 0
                    spu = (cached.get("topbar", {}) or {}).get("spuHeader", {}) or {}
                    price_inc = float(spu.get("avgPriceInc", 0))
                    self._notify_kw(kw, idx + 1, total_kw, "行情完成(缓存)",
                                    has_market=has_market, market_uv=uv_24h, market_price_inc=price_inc)
                    self._notify_stage("market", f"行情: {idx+1}/{total_kw}", done=idx + 1, total=total_kw)
                    self._logger.info(f"[行情] [{idx+1}/{total_kw}] {kw}: 缓存命中")
                    _checkpoint("market")
                    continue

                self._notify_kw(kw, idx + 1, total_kw, "行情采集中")
                try:
                    result = self._bridge.collect_market(kw, self._hs_pages)
                    market_results[kw] = result
                    _market_cache_put(market_cache_dir, kw, result)  # 写入缓存
                    has_market = result.get("hasMarket", False)
                    uv_24h = KeywordScorerV3._calc_24h_uv(result) if self._kw_scorer else 0
                    spu = (result.get("topbar", {}) or {}).get("spuHeader", {}) or {}
                    price_inc = float(spu.get("avgPriceInc", 0))
                    self._notify_kw(kw, idx + 1, total_kw, "行情完成",
                                    has_market=has_market, market_uv=uv_24h, market_price_inc=price_inc)
                    self._notify_stage("market", f"行情: {idx+1}/{total_kw}", done=idx + 1, total=total_kw)
                    self._logger.info(f"[行情] [{idx+1}/{total_kw}] {kw}: {'有行情' if has_market else '无行情'}")
                except Exception as e:
                    self._logger.error(f"[行情] {kw} 采集异常: {e}")
                    market_results[kw] = {"error": str(e)}
                    self._notify_stage("market", f"行情: {idx+1}/{total_kw}", done=idx + 1, total=total_kw)
                _checkpoint("market")

            _checkpoint("keyword_scoring")
            last_stage = "keyword_scoring"

        # ═══════════ 阶段2: 选词评分（预检+海选）═══════════
        if last_stage in ("keyword_scoring",):
            if not self._running:
                return self._finish(all_kw_results, all_pd_results, all_supply_pushed, output_dir)

            self._notify_stage("keyword_scoring", "阶段2: 预检+海选评分", done=0, total=total_kw)
            self._logger.info("[采集] 阶段2: 预检+海选评分")

            # 恢复时跳过已评分的词
            scored_kws = {r.get("keyword") for r in all_kw_results}
            precheck_passed = sum(1 for r in all_kw_results if r.get("grade") not in ("N/A", "?", None))
            scored_count = len(all_kw_results)

            for idx, kw in enumerate(keywords):
                if kw in scored_kws:
                    continue
                if not self._running:
                    break
                self._notify_kw(kw, idx + 1, total_kw, "评分中")

                md = market_results.get(kw, {})
                if md.get("error"):
                    all_kw_results.append({"keyword": kw, "total_100": 0, "grade": "N/A", "reason": md["error"]})
                    self._logger.warn(f"[评分] {kw}: 行情数据异常，跳过 → {md['error']}")
                    scored_count += 1
                    _checkpoint("keyword_scoring")
                    continue

                # 预检（基于24hUV + 均价 + 价格涨跌）
                if self._kw_scorer:
                    tabs_raw = md.get("tabs", [])
                    if isinstance(tabs_raw, dict):
                        tabs = tabs_raw.get("result", [])
                    else:
                        tabs = tabs_raw if isinstance(tabs_raw, list) else []
                    ok, reason = self._kw_scorer.precheck(kw, md, tabs)
                    if not ok:
                        all_kw_results.append({"keyword": kw, "total_100": 0, "grade": "N/A", "reason": reason})
                        self._logger.info(f"[预检淘汰] {kw}: {reason}")
                        self._notify_kw(kw, idx + 1, total_kw, f"淘汰: {reason[:20]}")
                        scored_count += 1
                        self._notify_stage("keyword_scoring", f"预检+海选: {scored_count}/{total_kw} (通过{precheck_passed})",
                                          done=scored_count, total=total_kw)
                        _checkpoint("keyword_scoring")
                        continue
                    precheck_passed += 1

                # 海选评分（score_fast 粗筛），A+词再走 score_full 精选
                if self._kw_scorer:
                    score = self._kw_scorer.score_fast(kw, md, {
                        "numFound": md.get("numFound", 0),
                        "sellingOrder": "",
                    })
                    grade = score.get("grade", "?")
                    total_score = score.get("total_100", 0)
                    scored_count += 1
                    self._notify_stage("keyword_scoring", f"预检+海选: {scored_count}/{total_kw} (A+{len(a_plus_keywords)})",
                                      done=scored_count, total=total_kw)
                    self._logger.info(f"[评分] {kw}: {total_score}分 {grade}级（海选）")

                    if total_score >= self._kw_push_threshold:
                        # 精选评分
                        full_score = self._kw_scorer.score_full(kw, md, {
                            "numFound": md.get("numFound", 0),
                            "sellingOrder": "",
                        })
                        a_plus_keywords.append((kw, full_score))
                        kw_score_map[kw] = full_score
                        self._logger.info(f"  >>> 达标关键词: {kw} ({total_score}分海选→{full_score.get('total_100', total_score)}分精选≥{self._kw_push_threshold})")
                        self._notify_kw(kw, idx + 1, total_kw, f"{full_score.get('total_100', total_score)}分 {full_score.get('grade', grade)}级",
                                        has_market=bool(md.get("topbar")))
                        self._notify_stage("keyword_full", f"精选: {len(a_plus_keywords)}个A+", done=len(a_plus_keywords), total=max(scored_count, 1))
                    else:
                        self._notify_kw(kw, idx + 1, total_kw, f"{total_score}分 {grade}级",
                                        has_market=bool(md.get("topbar")))

                    all_kw_results.append(score)
                else:
                    all_kw_results.append({"keyword": kw, "total_100": 0, "grade": "?", "reason": "评分引擎未配置"})
                    scored_count += 1
                _checkpoint("keyword_scoring")

            if not a_plus_keywords:
                self._logger.warn("[采集] 无A级以上关键词，跳过选品阶段")
                self._save_all_results(all_kw_results, all_pd_results, output_dir)
                _checkpoint("done")
                return self._finish(all_kw_results, all_pd_results, all_supply_pushed, output_dir)

            self._logger.info(f"[采集] A+关键词: {[(kw, s['total_100']) for kw, s in a_plus_keywords]}")
            _checkpoint("product_search")
            last_stage = "product_search"

        # ═══════════ 阶段3: 搜索 + 预筛选 + 详情采集 ═══════════
        if last_stage in ("product_search",):
            if not self._running:
                return self._finish(all_kw_results, all_pd_results, all_supply_pushed, output_dir)

            a_plus_kw_list = [kw for kw, _ in a_plus_keywords]
            n_aplus = len(a_plus_kw_list)
            self._notify_stage("product_search", "阶段3: 闭环采集(搜索+详情+评论)", done=0, total=n_aplus)

            # 确保 bridge 和进度回调就绪
            if not self._bridge or not self._bridge.loaded:
                self._init_bridge()
            self._bridge.set_progress_callback(self._on_js_progress)

            total_items_searched = 0
            total_items_kept = 0
            total_details_done = 0

            for idx, kw in enumerate(a_plus_kw_list):
                if kw in product_results and product_results[kw].get("search_items"):
                    # 恢复时累计已有数据
                    existing = product_results[kw]
                    total_items_searched += len(existing.get("search_items", []))
                    total_items_kept += len(existing.get("keep_list", []))
                    total_details_done += len(existing.get("details", {}))
                    continue
                if not self._running:
                    break
                self._notify_kw(kw, idx + 1, n_aplus, "搜索中")
                try:
                    kw_score = kw_score_map.get(kw, {})
                    avg_price = kw_score.get("avg_price", 0)

                    kw_data = {
                        "keyword": kw, "search_items": [], "details": {}, "comments": {},
                        "keep_list": [], "discard_list": [],
                    }

                    # Step A: 只搜索，不进详情（detailMax=0, commentMax=0）
                    raw = self._bridge.collect_keyword(
                        kw, self._search_pages, 0, 0)
                    if raw.get("error"):
                        self._logger.error(f"[搜索] {kw}: {raw.get('error')}")
                        product_results[kw] = kw_data
                        self._notify_kw(kw, idx + 1, n_aplus, f"失败: {raw.get('error', '')[:20]}")
                        self._notify_stage("product_search", f"搜索: {idx+1}/{n_aplus}", done=idx + 1, total=n_aplus)
                        _checkpoint("product_search")
                        continue

                    items = raw.get("searchItems", [])
                    search_meta = raw.get("searchMeta", {})
                    kw_data["search_items"] = items
                    kw_data["numFound"] = search_meta.get("numFound", 0)
                    total_items_searched += len(items)

                    self._logger.info(f"[搜索] {kw}: {len(items)} 条搜索结果")

                    if not items:
                        self._logger.warn(f"[搜索] {kw}: 无搜索结果")
                        product_results[kw] = kw_data
                        self._notify_stage("product_search", f"搜索: {idx+1}/{n_aplus}", done=idx + 1, total=n_aplus)
                        _checkpoint("product_search")
                        continue

                    # 预筛选（Python 侧，决定哪些商品值得进详情）
                    if self._pd_scorer:
                        keep_list, discard_list = self._pd_scorer.prefilter(items, avg_price)
                        kw_data["keep_list"] = keep_list
                        kw_data["discard_list"] = discard_list
                        total_items_kept += len(keep_list)
                        self._logger.info(f"[预筛选] {kw}: 保留{len(keep_list)}/淘汰{len(discard_list)}")
                    else:
                        keep_list = items
                        total_items_kept += len(keep_list)

                    # 更新漏斗：搜索+预筛
                    self._notify_stage("product_search", f"搜索+预筛: {total_items_kept}/{total_items_searched}",
                                      done=total_items_kept, total=total_items_searched)

                    # keep_list 已按优先级排序，取前 detail_max 条进详情
                    detail_keep = keep_list[:self._detail_max] if self._detail_max > 0 else keep_list
                    detail_ids = [it.get("itemId", "") for it in detail_keep if it.get("itemId")]

                    # Step B: 精准详情采集
                    if detail_ids:
                        self._notify_stage("product_detail", f"详情: 0/{len(detail_ids)}",
                                          done=0, total=len(detail_ids))
                        try:
                            details_raw = self._bridge.collect_details(detail_ids)
                            kw_data["details"] = details_raw if isinstance(details_raw, dict) else {}
                        except Exception as e:
                            self._logger.error(f"[详情] {kw} 批量采集异常: {e}")
                            kw_data["details"] = {}
                        total_details_done += len(kw_data["details"])
                        self._logger.info(f"[详情] {kw}: {len(kw_data['details'])}/{len(detail_ids)} 采集成功")

                    # Step C: 精准评论采集（取已采详情的前 comment_max 条）
                    if self._comment_max > 0 and kw_data["details"]:
                        comment_ids = list(kw_data["details"].keys())[:self._comment_max]
                        try:
                            comments_raw = self._bridge.collect_comments(comment_ids)
                            kw_data["comments"] = comments_raw if isinstance(comments_raw, dict) else {}
                        except Exception as e:
                            self._logger.error(f"[评论] {kw} 批量采集异常: {e}")
                            kw_data["comments"] = {}
                        self._logger.info(f"[评论] {kw}: {len(kw_data['comments'])}/{len(comment_ids)} 采集成功")

                    product_results[kw] = kw_data
                    self._notify_kw(kw, idx + 1, n_aplus, "闭环完成",
                                    search_cnt=len(items), has_market=True,
                                    detail_cnt=len(kw_data["details"]),
                                    comment_cnt=len(kw_data.get("comments", {})))
                    self._notify_stage("product_detail", f"详情: {total_details_done}/{total_items_kept}",
                                      done=total_details_done, total=total_items_kept)

                except Exception as e:
                    self._logger.error(f"[闭环] {kw} 异常: {e}")
                    product_results[kw] = {"keyword": kw, "error": str(e)}
                    self._notify_stage("product_search", f"闭环: {idx+1}/{n_aplus}", done=idx + 1, total=n_aplus)
                _checkpoint("product_search")

            # 阶段3完成后，确保漏斗显示最终值
            if total_items_searched > 0:
                self._notify_stage("product_search", f"搜索+预筛: {total_items_kept}/{total_items_searched}",
                                  done=total_items_kept, total=total_items_searched)
            if total_items_kept > 0:
                self._notify_stage("product_detail", f"详情: {total_details_done}/{total_items_kept}",
                                  done=total_details_done, total=total_items_kept)

            # 清除进度回调
            self._bridge.set_progress_callback(None)
            _checkpoint("product_scoring")
            last_stage = "product_scoring"

        # ═══════════ 阶段4: 选品深度评分 ═══════════
        if last_stage in ("product_scoring",):
            if not self._running:
                return self._finish(all_kw_results, all_pd_results, all_supply_pushed, output_dir)

            # 恢复时重新计算各项累计值
            total_items_kept = sum(len(v.get("keep_list", [])) for v in product_results.values())
            total_details_done = sum(len(v.get("details", {})) for v in product_results.values())
            # 待评分总数 = 已采集详情的商品数（没详情的无法评分）
            total_to_score = total_details_done

            # 已评分的商品 ID 集合
            scored_ids = {p.get("itemId", "") for p in all_pd_results}

            self._notify_stage("product_scoring", "阶段4: 选品评分+推送",
                              done=len(all_pd_results), total=total_to_score)
            self._logger.info("[采集] 阶段4: 选品评分+推送货源")

            sa_items_to_push = []
            total_scored = len(all_pd_results)  # 从已恢复的已评分数开始

            for kw, kw_score in a_plus_keywords:
                if not self._running:
                    break

                # 恢复模式下 kw_score 可能是数字（断点存储格式），用 kw_score_map 兜底
                if not isinstance(kw_score, dict):
                    kw_score = kw_score_map.get(kw, {})
                if not kw_score:
                    kw_score = {}

                kw_data = product_results.get(kw, {})
                if kw_data.get("error"):
                    continue

                keep_list = kw_data.get("keep_list", [])
                if not keep_list:
                    continue

                avg_price = kw_score.get("avg_price", 0)
                no_bargain_rate = kw_score.get("no_bargain_rate", 0.5)

                if self._pd_scorer:
                    details_map = kw_data.get("details", {})
                    for item in keep_list:
                        if not self._running:
                            break
                        try:
                            item_id = item.get("itemId", "")
                            if item_id in scored_ids:
                                continue
                            # 只评分已采集详情的商品
                            if item_id not in details_map:
                                continue
                            detail_raw = details_map[item_id]
                            detail_api = {
                                "item": detail_raw.get("itemDO", detail_raw.get("item", {})),
                                "seller": detail_raw.get("sellerDO", detail_raw.get("seller", {})),
                            }

                            # DEBUG: 打印首个商品的 detail itemDO keys 和 soldCnt 实际值
                            if total_scored == 0:
                                item_do = detail_api["item"]
                                self._logger.info(
                                    f"[DEBUG detail] itemDO keys: {sorted(item_do.keys()) if item_do else 'EMPTY'}")
                                self._logger.info(
                                    f"[DEBUG detail] soldCnt={item_do.get('soldCnt', 'MISSING')!r}  "
                                    f"wantCnt={item_do.get('wantCnt', 'MISSING')!r}  "
                                    f"collectCnt={item_do.get('collectCnt', 'MISSING')!r}")

                            pd_score = self._pd_scorer.score_one(
                                keyword=kw,
                                search_item=item,
                                detail=detail_api,
                                market_avg_price=avg_price,
                                no_bargain_rate=no_bargain_rate,
                            )

                            if pd_score:
                                all_pd_results.append(pd_score)
                                total_scored += 1
                                grade = pd_score.get("grade", "?")
                                total = pd_score.get("total_100", 0)
                                title = pd_score.get("title", item.get("title", ""))[:30]
                                self._logger.info(f"[商品评分] {title}: {total}分 {grade}级")
                                push_cnt = len(all_supply_pushed)
                                self._notify_stage("product_scoring", f"评分+推送: {total_scored}/{total_to_score}",
                                                  done=total_scored, total=total_to_score)

                                if self._on_product:
                                    self._on_product(pd_score)

                                if total >= self._pd_push_threshold and self._supply_engine:
                                    self._logger.info(f"  >>> 货源候选: {title} ({total}分≥{self._pd_push_threshold}/{grade}级)")
                                    detail_item = detail_api.get("item", {})
                                    seller_item = detail_api.get("seller", {})
                                    enriched = dict(item)
                                    enriched["productScore"] = total
                                    # 闲鱼详情字段（使用中文键名，供货源查找和Excel导出使用）
                                    enriched["商品标题"] = pd_score.get("title", item.get("title", ""))
                                    enriched["商品价格"] = pd_score.get("price", item.get("price", ""))
                                    enriched["商品描述"] = detail_item.get("desc", "") or item.get("title", "")
                                    enriched["商品链接"] = item.get("itemUrl", f"https://www.goofish.com/item?id={item_id}")
                                    enriched["商品图片"] = item.get("picUrl", item.get("pics", ""))
                                    enriched["商品视频"] = item.get("videoUrl", "")
                                    enriched["综合评分"] = total
                                    enriched["评分等级"] = grade
                                    if detail_item:
                                        enriched["wantCnt"] = detail_item.get("wantCnt", item.get("wantCnt", 0))
                                        enriched["soldCount"] = item.get("soldCount", detail_item.get("soldCnt", 0))
                                        enriched["collectCnt"] = detail_item.get("collectCnt", 0)
                                        enriched["browseCnt"] = detail_item.get("browseCnt", 0)
                                        enriched["soldPrice"] = detail_item.get("soldPrice", "")
                                        gmt_create = detail_item.get("gmtCreate", 0)
                                        if gmt_create:
                                            try:
                                                created = datetime.fromtimestamp(int(gmt_create) / 1000)
                                            except (ValueError, TypeError, OSError):
                                                created = None
                                            enriched["daysOnSale"] = (datetime.now() - created).days
                                        else:
                                            enriched["daysOnSale"] = item.get("daysOnSale", 0)
                                    else:
                                        enriched["wantCnt"] = item.get("wantCnt", item.get("wantNum", 0))
                                        enriched["soldCount"] = item.get("soldCount", 0)
                                        enriched["daysOnSale"] = item.get("daysOnSale", 0)
                                    # 中文别名（供 supply finder 使用）
                                    enriched["已售数量"] = enriched.get("soldCount", 0)
                                    enriched["想要人数"] = enriched.get("wantCnt", 0)
                                    try:
                                        want = int(enriched.get("wantCnt", 0))
                                        days = max(int(enriched.get("daysOnSale", 1)), 1)
                                        enriched["日均想要数"] = round(want / days, 1)
                                    except (ValueError, TypeError):
                                        enriched["日均想要数"] = 0
                                    enriched["已上架天数"] = enriched.get("daysOnSale", 0)
                                    enriched["收藏数"] = enriched.get("collectCnt", 0)
                                    enriched["浏览数"] = enriched.get("browseCnt", 0)
                                    # 卖家信息
                                    if seller_item:
                                        enriched["卖家昵称"] = seller_item.get("nick", "")
                                        enriched["卖家已售"] = seller_item.get("hasSoldNumInteger", 0)
                                        enriched["卖家好评率"] = seller_item.get("newGoodRatioRate", "")
                                        enriched["卖家回复率"] = seller_item.get("replyRatio24h", "")
                                    all_supply_pushed.append({"item": enriched, "score": pd_score})
                                    sa_items_to_push.append(enriched)
                        except Exception as e:
                            self._logger.error(f"[商品评分] {kw}/{item.get('itemId', '?')} 异常: {e}")

            # 阶段4结束后，统一推送所有S/A商品到货源引擎
            if sa_items_to_push and self._supply_engine:
                self._logger.info(f"[货源推送] 共 {len(sa_items_to_push)} 件S/A商品推送到货源查找")
                try:
                    self._supply_engine.start(sa_items_to_push)
                except Exception as e:
                    self._logger.error(f"[货源推送] 启动失败: {e}")

            _checkpoint("product_scoring")

        # ═══════════ 保存结果 ═══════════
        self._save_all_results(all_kw_results, all_pd_results, output_dir)
        _checkpoint("done")
        # 清理状态文件（成功完成）
        try:
            state_path.unlink(missing_ok=True)
        except Exception:
            pass
        return self._finish(all_kw_results, all_pd_results, all_supply_pushed, output_dir)

    def _save_all_results(self, kw_results: list, pd_results: list, output_dir: Path):
        """保存完整结果到JSON + Excel"""
        # 汇总JSON
        summary = {
            "collectedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "keywords": kw_results,
            "products": pd_results,
        }
        path = output_dir / "_pipeline_summary.json"
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        self._logger.info(f"[采集] 汇总已保存: {path}")

        # 每词独立JSON
        for kw_result in kw_results:
            kw_name = kw_result.get("keyword", "")
            if not kw_name:
                continue
            safe_name = "".join(c for c in kw_name if c.isalnum() or c in "._- ") or kw_name
            kw_file = output_dir / f"{safe_name}.json"
            kw_file.write_text(json.dumps(kw_result, ensure_ascii=False, indent=2), encoding="utf-8")

        # Excel导出
        try:
            from exporter.excel_exporter import ExcelExporter
            xlsx_path = output_dir / f"采集结果_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
            ExcelExporter.export_dashboard(str(xlsx_path), kw_results, pd_results)
            self._logger.info(f"[采集] Excel已保存: {xlsx_path}")
        except Exception as e:
            self._logger.warn(f"[采集] Excel导出失败: {e}")

        self._logger.info(f"[采集] 全部数据已保存到 {output_dir}")

    def _finish(self, kw_results: list, pd_results: list, supply_pushed: list, output_dir: Path):
        """采集完成"""
        self._logger.info(f"[采集] ===== 流水线完成: {len(kw_results)}词 {len(pd_results)}商品 {len(supply_pushed)}货源 =====")
        if self._on_complete:
            self._on_complete(kw_results, pd_results, supply_pushed)
        self._running = False
