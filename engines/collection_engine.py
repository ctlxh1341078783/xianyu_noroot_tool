"""采集引擎：三阶段流水线编排（后台线程），真实Frida RPC + 评分 + 推送货源 + 断点续传"""
import json
import random
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from engines.device_engine import DeviceEngine
from engines.flywheel_engine import FlywheelEngine
from engines.scoring_evolution import ScoringEvolution
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


# ════════════════════════════════════════════════════════════════
# 闲鱼风控守护（两阶段：重启 → 30分钟冷却）
# ════════════════════════════════════════════════════════════════

class XianyuRiskGuard:
    """闲鱼风控检测与自动恢复

    检测信号：
      - 连续 N 次 API 返回 error
      - 连续 N 个关键词搜索结果为 0

    两阶段响应：
      阶段1: 达到阈值 → kill App + 重启 + 短暂冷却 → 继续采集
      阶段2: 阶段1后再次触发 → kill App + 等30分钟 → 重启 → 继续采集
    """

    STAGE1_ERROR_THRESHOLD = 3       # 连续3次错误触发阶段1
    STAGE2_ERROR_THRESHOLD = 2       # 重启后连续2次错误触发阶段2
    STAGE1_COOLDOWN_SEC = 30         # 阶段1冷却时间（秒）
    STAGE2_COOLDOWN_SEC = 30 * 60    # 阶段2冷却时间（30分钟）
    SUCCESS_RESET_COUNT = 3          # 连续3次成功后重置所有状态

    def __init__(self, device_mgr, logger, webhook_url: str = ""):
        self._mgr = device_mgr
        self._log = logger
        self._webhook_url = webhook_url

        self._consecutive_errors = 0
        self._consecutive_successes = 0
        self._stage1_triggered = False
        self._last_error_type = ""

    def report_error(self, stage: str, keyword: str, detail: str = "") -> bool:
        """报告一次错误，返回 True 表示需要暂停当前关键词"""
        self._consecutive_errors += 1
        self._consecutive_successes = 0
        self._last_error_type = f"[{stage}] {keyword}: {detail}"

        if not self._stage1_triggered and self._consecutive_errors >= self.STAGE1_ERROR_THRESHOLD:
            return self._handle_stage1()
        elif self._stage1_triggered and self._consecutive_errors >= self.STAGE2_ERROR_THRESHOLD:
            return self._handle_stage2()
        return False

    def report_success(self):
        """报告一次成功"""
        self._consecutive_errors = 0
        self._consecutive_successes += 1
        if self._consecutive_successes >= self.SUCCESS_RESET_COUNT and self._stage1_triggered:
            self._log.info("[风控] 恢复正常，重置风控状态")
            self._stage1_triggered = False
            self._consecutive_successes = 0

    def _handle_stage1(self) -> bool:
        """阶段1：立即杀进程+重启，短暂冷却后恢复"""
        self._log.warning(
            f"[风控] 阶段1触发: 连续{self._consecutive_errors}次错误 "
            f"({self._last_error_type}) → 重启闲鱼App"
        )
        try:
            if self._mgr:
                self._mgr.restart_app()
                self._log.info("[风控] 阶段1: 闲鱼已重启，等待冷却...")
                time.sleep(self.STAGE1_COOLDOWN_SEC)
                self._log.info("[风控] 阶段1: 冷却完成，恢复采集")
        except Exception as e:
            self._log.error(f"[风控] 阶段1 重启失败: {e}")

        self._stage1_triggered = True
        self._consecutive_errors = 0
        return True  # 让调用者重新尝试

    def _handle_stage2(self) -> bool:
        """阶段2：杀进程+等待30分钟+重启"""
        cooldown_min = self.STAGE2_COOLDOWN_SEC // 60
        self._log.warning(
            f"[风控] 阶段2触发: 重启后仍连续{self._consecutive_errors}次错误 "
            f"({self._last_error_type}) → 进入{cooldown_min}分钟冷却"
        )

        if self._webhook_url:
            self._send_webhook_alert(cooldown_min)

        try:
            if self._mgr:
                self._mgr.restart_app()
                self._log.info(f"[风控] 阶段2: 已重启，等待 {cooldown_min} 分钟冷却...")
                # 分段等待，每10秒打印进度
                for i in range(cooldown_min):
                    if i % 5 == 0 and i > 0:
                        self._log.info(f"[风控] 冷却中... {i}/{cooldown_min} 分钟")
                    time.sleep(60)
                self._log.info("[风控] 阶段2: 冷却完成，恢复采集")
        except Exception as e:
            self._log.error(f"[风控] 阶段2 重启失败: {e}")

        self._stage1_triggered = False
        self._consecutive_errors = 0
        return True

    def _send_webhook_alert(self, cooldown_min: int):
        try:
            import requests
            msg = {
                "msgtype": "markdown",
                "markdown": {
                    "content": (
                        f"## 🚨 闲鱼采集风控告警\n"
                        f"> 触发时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                        f"> 最后错误: {self._last_error_type}\n"
                        f"> 处理: 已自动重启App，进入{cooldown_min}分钟冷却\n"
                        f"> 状态: 冷却完成后自动恢复采集"
                    )
                }
            }
            requests.post(self._webhook_url, json=msg, timeout=10)
            self._log.info("[风控] 已发送企微告警")
        except Exception as e:
            self._log.warn(f"[风控] 企微告警发送失败: {e}")

    def reset(self):
        self._consecutive_errors = 0
        self._consecutive_successes = 0
        self._stage1_triggered = False


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
        self._kw_push_threshold = c.get("kw_push_threshold", 10)
        self._pd_push_threshold = c.get("pd_push_threshold", 10)
        # 飞轮引擎（Phase B AI 候选词提取 + 词库膨胀）
        self._flywheel_engine: Optional[FlywheelEngine] = None
        # 评分进化（品类级别权重学习，延迟初始化）
        self._scoring_evo = None
        # 飞轮闭环：已处理过的词（全局去重）
        self._processed_keywords: set = set()
        self._flywheel_max_rounds = c.get("flywheel_max_rounds", 3)
        self._flywheel_round_size = c.get("flywheel_round_size", 50)
        # 跨轮累积结果
        self._all_kw_acc = []
        self._all_pd_acc = []
        self._all_supply_acc = []

        # 风控守护
        webhook_url = settings.get("wechat", {}).get("webhook_url", "")
        self._risk_guard = XianyuRiskGuard(
            device_engine.manager if device_engine else None,
            self._logger,
            webhook_url,
        )

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
        self._kw_push_threshold = c.get("kw_push_threshold", 10)
        self._pd_push_threshold = c.get("pd_push_threshold", 10)
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
        if hasattr(self, '_parallel_pipeline') and self._parallel_pipeline:
            self._parallel_pipeline.stop()
        if hasattr(self, '_risk_guard') and self._risk_guard:
            self._risk_guard.reset()
        self._logger.info("采集已停止")

    def start_parallel(self, keywords: List[str], output_dir: Path):
        """并行模式入口：4线程+3队列流水线，飞轮前置持续运转"""
        if self._running:
            self._logger.warn("采集已在运行中")
            return

        if self._dev:
            self._logger.info("[并行] 前置检查：确保手机服务就绪...")
            if not self._dev.ensure_services_ready():
                self._logger.error("[并行] 手机服务未就绪，采集中止")
                return

        from engines.parallel_pipeline import ParallelPipeline

        self._parallel_pipeline = ParallelPipeline(
            self._settings, self._dev, self._kw_scorer,
            self._pd_scorer, self._supply_engine,
        )

        self._parallel_pipeline.set_callbacks(
            on_stage=self._on_stage,
            on_keyword=self._on_keyword,
            on_product=self._on_product,
            on_complete=self._on_parallel_done,
        )

        self._running = True
        self._parallel_pipeline.start(keywords, output_dir)

    def _on_parallel_done(self, kw_results, pd_results, supply_results):
        if self._on_complete:
            self._on_complete(kw_results, pd_results, supply_results)
        self._running = False

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

    def _run(self, keywords: List[str], output_dir: Path, _round: int = 1):
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        state_path = output_dir / "_pipeline_state.json"

        # 首轮初始化评分进化
        if self._scoring_evo is None:
            evo_path = output_dir / "scoring_evolution.json"
            self._scoring_evo = ScoringEvolution(None, storage_path=evo_path)

        # 首轮初始化已处理词集
        if _round == 1:
            self._processed_keywords = set(keywords)

        if _round > 1:
            self._logger.info(f"\n{'='*60}")
            self._logger.info(f"[飞轮闭环] 第{_round}轮: {len(keywords)}个新词")
            self._logger.info(f"{'='*60}")
            self._notify_stage("market", f"飞轮第{_round}轮: 行情采集", done=0, total=len(keywords))
        else:
            # 首轮清空累积
            self._all_kw_acc = []
            self._all_pd_acc = []
            self._all_supply_acc = []

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
                    self._risk_guard.report_error("行情", kw, str(e)[:50])
                _checkpoint("market")

            _checkpoint("keyword_scoring")
            last_stage = "keyword_scoring"

        # ═══════════ 阶段2: 选词评分（预检+海选）═══════════
        if last_stage in ("keyword_scoring",):
            if not self._running:
                return self._finish(all_kw_results, all_pd_results, all_supply_pushed, output_dir)

            self._notify_stage("keyword_scoring", "阶段2: 海选评分", done=0, total=total_kw)
            self._logger.info("[采集] 阶段2: 预检+海选评分")

            # 恢复时跳过已评分的词
            scored_kws = {r.get("keyword") for r in all_kw_results}
            precheck_passed = sum(1 for r in all_kw_results if r.get("grade") not in ("N/A", "?", None))
            scored_count = len(all_kw_results)
            market_count = sum(1 for md in market_results.values() if isinstance(md, dict) and md.get("hasMarket"))

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
                        self._notify_stage("market", f"预检: {precheck_passed}/{total_kw}通过",
                                          done=precheck_passed, total=total_kw)
                        _checkpoint("keyword_scoring")
                        continue
                    precheck_passed += 1
                    self._notify_stage("market", f"预检: {precheck_passed}/{total_kw}通过",
                                      done=precheck_passed, total=total_kw)

                # 海选评分（score_fast 粗筛），A+词再走 score_full 精选
                if self._kw_scorer:
                    score = self._kw_scorer.score_fast(kw, md, {
                        "numFound": md.get("numFound", 0),
                        "sellingOrder": "",
                    })
                    grade = score.get("grade", "?")
                    total_score = score.get("total_100", 0)
                    scored_count += 1
                    self._notify_stage("keyword_scoring", f"评分: {len(a_plus_keywords)}A+/{precheck_passed}通过",
                                      done=len(a_plus_keywords), total=precheck_passed)
                    self._logger.info(f"[评分] {kw}: {total_score}分 {grade}级（海选）")

                    if total_score >= self._kw_push_threshold:
                        # 精选评分：品类进化权重 + 成交记录深度分析
                        evo_weights = None
                        evo_thresholds = None
                        if self._scoring_evo:
                            evo_weights = self._scoring_evo.get_weights_for(kw)
                            evo_thresholds = self._scoring_evo.get_thresholds_for(kw)
                        full_score = self._kw_scorer.score_full(kw, md, {
                            "numFound": md.get("numFound", 0),
                            "sellingOrder": "",
                        }, category_weights=evo_weights, category_thresholds=evo_thresholds)
                        a_plus_keywords.append((kw, full_score))
                        kw_score_map[kw] = full_score
                        evo_note = ""
                        if full_score.get("_evo_applied"):
                            delta = full_score.get("_evo_score_delta", 0)
                            evo_note = f" [进化权重{'↑' if delta > 0 else '↓'}{abs(delta):.1f}分]"
                        self._logger.info(f"  >>> 达标关键词: {kw} ({total_score}分海选→{full_score.get('total_100', total_score)}分精选≥{self._kw_push_threshold}){evo_note}")
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
                # 无A+词 → 取TOP-N跑搜索+Phase B，挖新词给下轮
                rescue_top_n = 10
                rescue_words = sorted(
                    [r for r in all_kw_results if r.get("total_100", 0) > 0],
                    key=lambda r: r.get("total_100", 0), reverse=True
                )[:rescue_top_n]
                if rescue_words:
                    self._logger.info(
                        f"[采集] 无A+词，取TOP{len(rescue_words)}词跑搜索+飞轮挖新词: "
                        f"{', '.join(r['keyword'] for r in rescue_words)}")
                    self._notify_stage("keyword_scoring",
                        f"无A+词，飞轮挖词中...", done=0, total=len(rescue_words))

                    if not self._bridge or not self._bridge.loaded:
                        self._init_bridge()
                        self._bridge.set_progress_callback(self._on_js_progress)

                    rescue_titles = {}
                    for idx, r in enumerate(rescue_words):
                        if not self._running:
                            break
                        kw = r["keyword"]
                        self._notify_kw(kw, idx + 1, len(rescue_words), "搜索中")
                        try:
                            raw = self._bridge.collect_keyword(kw, max_pages=5, detail_max=0, comment_max=0)
                            if not raw.get("error"):
                                items = raw.get("searchItems", [])
                                rescue_titles[kw] = [it.get("title", "") for it in items if it.get("title")]
                                self._logger.info(f"[飞轮挖词] {kw}: {len(items)}条搜索")
                        except Exception as e:
                            self._logger.error(f"[飞轮挖词] {kw}: {e}")

                    # 跑 Phase B 从标题中提取新词
                    if rescue_titles:
                        self._logger.info("[飞轮挖词] 运行 Phase B...")
                        if self._flywheel_engine is None:
                            self._flywheel_engine = FlywheelEngine(self._settings, output_dir=output_dir)

                        kw_data = [
                            {"keyword": kw, "search_items": [{"title": t} for t in titles], "numFound": len(titles)}
                            for kw, titles in rescue_titles.items()
                        ]
                        try:
                            fb_result = self._flywheel_engine.run_phase_b_batch(kw_data)
                            n_pass = fb_result["summary"]["pass_words"]
                            self._logger.info(
                                f"[飞轮挖词] Phase B 完成: {n_pass}个新pass词 → 进入下轮")
                            self._notify_stage("flywheel",
                                f"飞轮挖词: {n_pass}个新词", done=1, total=1)
                        except Exception as e:
                            self._logger.error(f"[飞轮挖词] Phase B 异常: {e}")

                # 保存本轮结果，积累样本
                self._save_all_results(all_kw_results, all_pd_results, output_dir)

                # 直接检查飞轮是否产出了新pass词，有就启动下轮
                if _round < self._flywheel_max_rounds and self._flywheel_engine:
                    new_pass = self._flywheel_engine.word_lib.get_pass_words()
                    new_round_words = [w for w in new_pass
                                      if w not in self._processed_keywords]
                    if new_round_words:
                        new_round_words = new_round_words[:self._flywheel_round_size]
                        self._processed_keywords.update(new_round_words)
                        self._logger.info(
                            f"[飞轮挖词] 第{_round}轮救急 → 发现{len(new_round_words)}个新词，启动第{_round+1}轮")
                        return self._run(new_round_words, output_dir, _round=_round + 1)
                    else:
                        self._logger.info("[飞轮挖词] 无新pass词，飞轮停止")

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

            total_details_done = 0

            for idx, kw in enumerate(a_plus_kw_list):
                if kw in product_results and product_results[kw].get("search_items"):
                    existing = product_results[kw]
                    total_details_done += len(existing.get("details", {}))
                    continue
                if not self._running:
                    break

                # 每个新关键词重置产品漏斗（品·预筛选 + 品·详情）
                self._notify_stage("product_search", f"{kw} 搜索中...", done=0, total=0)
                self._notify_stage("product_detail", f"{kw} 详情: 0/0", done=0, total=0)

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
                        self._risk_guard.report_error("搜索", kw, str(raw.get("error", ""))[:50])
                        _checkpoint("product_search")
                        continue

                    items = raw.get("searchItems", [])
                    search_meta = raw.get("searchMeta", {})
                    kw_data["search_items"] = items
                    kw_data["numFound"] = search_meta.get("numFound", 0)

                    self._logger.info(f"[搜索] {kw}: {len(items)} 条搜索结果")

                    if not items:
                        self._logger.warn(f"[搜索] {kw}: 无搜索结果")
                        product_results[kw] = kw_data
                        self._notify_stage("product_search", f"{kw}: 0结果", done=0, total=0)
                        self._risk_guard.report_error("搜索", kw, "0结果")
                        _checkpoint("product_search")
                        continue

                    self._risk_guard.report_success()

                    # 预筛选（Python 侧，决定哪些商品值得进详情）
                    if self._pd_scorer:
                        keep_list, discard_list = self._pd_scorer.prefilter(items, avg_price)
                        kw_data["keep_list"] = keep_list
                        kw_data["discard_list"] = discard_list
                        self._logger.info(f"[预筛选] {kw}: 保留{len(keep_list)}/淘汰{len(discard_list)}")
                    else:
                        keep_list = items

                    # 每词漏斗：预筛通过数 / 搜索总数
                    self._notify_stage("product_search", f"{kw}: 预筛{len(keep_list)}/{len(items)}",
                                      done=len(keep_list), total=len(items))

                    # keep_list 已按优先级排序，取前 detail_max 条进详情
                    detail_keep = keep_list[:self._detail_max] if self._detail_max > 0 else keep_list
                    detail_ids = [it.get("itemId", "") for it in detail_keep if it.get("itemId")]

                    # Step B: 精准详情采集
                    if detail_ids:
                        self._notify_stage("product_detail", f"{kw} 详情: 0/{len(detail_ids)}",
                                          done=0, total=len(detail_ids))
                        try:
                            details_raw = self._bridge.collect_details(detail_ids)
                            kw_data["details"] = details_raw if isinstance(details_raw, dict) else {}
                        except Exception as e:
                            self._logger.error(f"[详情] {kw} 批量采集异常: {e}")
                            kw_data["details"] = {}
                            self._risk_guard.report_error("详情", kw, str(e)[:50])
                        total_details_done += len(kw_data["details"])
                        n_detail = len(kw_data["details"])
                        self._notify_stage("product_detail", f"{kw} 详情: {n_detail}/{len(detail_ids)}",
                                          done=n_detail, total=len(detail_ids))
                        self._logger.info(f"[详情] {kw}: {n_detail}/{len(detail_ids)} 采集成功")

                    # Step C: 精准评论采集（取已采详情的前 comment_max 条）
                    if self._comment_max > 0 and kw_data["details"]:
                        comment_ids = list(kw_data["details"].keys())[:self._comment_max]
                        try:
                            comments_raw = self._bridge.collect_comments(comment_ids)
                            kw_data["comments"] = comments_raw if isinstance(comments_raw, dict) else {}
                        except Exception as e:
                            self._logger.error(f"[评论] {kw} 批量采集异常: {e}")
                            kw_data["comments"] = {}
                            self._risk_guard.report_error("评论", kw, str(e)[:50])
                        self._logger.info(f"[评论] {kw}: {len(kw_data['comments'])}/{len(comment_ids)} 采集成功")

                    product_results[kw] = kw_data
                    self._notify_kw(kw, idx + 1, n_aplus, "闭环完成",
                                    search_cnt=len(items), has_market=True,
                                    detail_cnt=len(kw_data["details"]),
                                    comment_cnt=len(kw_data.get("comments", {})))
                except Exception as e:
                    self._logger.error(f"[闭环] {kw} 异常: {e}")
                    product_results[kw] = {"keyword": kw, "error": str(e)}
                    self._notify_stage("product_search", f"闭环: {idx+1}/{n_aplus}",
                                      done=idx + 1, total=n_aplus)
                _checkpoint("product_search")

            # 阶段3完成后，汇总：品·预筛选 = 所有词累计
            all_kept = sum(len(v.get("keep_list", [])) for v in product_results.values())
            all_searched = sum(len(v.get("search_items", [])) for v in product_results.values())
            all_details = sum(len(v.get("details", {})) for v in product_results.values())
            if all_searched > 0:
                self._notify_stage("product_search", f"完成: {all_kept}/{all_searched}保留",
                                  done=all_kept, total=all_searched)
            if all_kept > 0:
                self._notify_stage("product_detail", f"完成: {all_details}/{all_kept}已采",
                                  done=all_details, total=all_kept)

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

            # ═══════════ 阶段5a: 飞轮 Phase B（AI分析，不占手机，可与PDD并发）═══════════
            if last_stage in ("product_scoring",):
                self._notify_stage("flywheel", "阶段5a: AI飞轮分析", done=0, total=1)
                self._logger.info("[采集] 阶段5a: 飞轮 Phase B (AI分析)")

                try:
                    keyword_data_for_flywheel = []
                    for kw, kw_score in a_plus_keywords:
                        kw_data = product_results.get(kw, {})
                        items = kw_data.get("search_items", [])
                        if items:
                            keyword_data_for_flywheel.append({
                                "keyword": kw,
                                "search_items": items,
                                "numFound": kw_data.get("numFound", 0),
                            })

                    if keyword_data_for_flywheel and self._flywheel_engine is None:
                        self._flywheel_engine = FlywheelEngine(
                            self._settings, output_dir=output_dir)

                    if keyword_data_for_flywheel and self._flywheel_engine:
                        fb_result = self._flywheel_engine.run_phase_b_batch(
                            keyword_data_for_flywheel)
                        n_pass = fb_result["summary"]["pass_words"]
                        n_watch = fb_result["summary"]["watch_words"]
                        n_pending = fb_result["summary"]["pending_words"]
                        n_materials = fb_result["summary"]["new_materials"]
                        self._logger.info(
                            f"[飞轮 Phase B] 完成: 通过{n_pass}词 观察{n_watch}词 "
                            f"待验证{n_pending}词 素材{n_materials}条")
                        self._notify_stage("flywheel",
                            f"Phase B: {n_pass}通过 {n_watch}观察 {n_pending}待验证",
                            done=1, total=1)
                        # pass词已由飞轮引擎写入词库，本轮结束后由闭环自动启动下一轮
                    else:
                        self._logger.info("[飞轮] 无A+关键词搜索数据，跳过 Phase B")
                        self._notify_stage("flywheel", "Phase B: 跳过(无数据)", done=1, total=1)
                except Exception as e:
                    self._logger.error(f"[飞轮 Phase B] 异常: {e}")
                    import traceback
                    self._logger.error(traceback.format_exc())

                _checkpoint("flywheel_b")

            # ═══════════ 阶段5b: 推送货源 + 等待PDD完成 ═══════════
            if last_stage in ("product_scoring",):
                if sa_items_to_push and self._supply_engine:
                    self._logger.info(f"[货源推送] 共 {len(sa_items_to_push)} 件S/A商品推送到货源查找")
                    try:
                        self._supply_engine.start(sa_items_to_push)
                    except Exception as e:
                        self._logger.error(f"[货源推送] 启动失败: {e}")

                _checkpoint("product_scoring")

        # ═══════════ 等待货源查找完成（PDD占着手机，Phase C不能跑）═══════════
        if self._supply_engine and self._supply_engine.running:
            self._logger.info("[采集] 等待货源查找队列完成（PDD占用手机）...")
            while self._supply_engine.running and self._running:
                time.sleep(5)
            if self._supply_engine.running:
                self._supply_engine.stop()
            self._logger.info("[采集] 货源查找已完成")

        if not self._running:
            return self._finish(all_kw_results, all_pd_results, all_supply_pushed, output_dir)

        # ═══════════ 阶段5c: Phase C 验证搜索（需要闲鱼，PDD已结束）═══════════
        if (last_stage in ("product_scoring",) and
            self._flywheel_engine and self._bridge):
            pending_words = self._flywheel_engine.get_pending_words()
            if pending_words:
                n_pending = len(pending_words)
                self._notify_stage("flywheel_c", f"Phase C: 验证{n_pending}个待定词",
                                  done=0, total=n_pending)
                self._logger.info(f"[飞轮 Phase C] 验证 {n_pending} 个待定词")

                pending_searches = []
                for i, word in enumerate(pending_words):
                    if not self._running:
                        break
                    try:
                        self._notify_stage("flywheel_c",
                            f"Phase C: {word}", done=i, total=n_pending)
                        raw = self._bridge.collect_keyword(
                            word, max_pages=1, detail_max=0, comment_max=0)
                        if not raw.get("error"):
                            pending_searches.append({
                                "word": word,
                                "numFound": raw.get("searchMeta", {}).get("numFound", 0),
                                "search_items": raw.get("searchItems", []),
                            })
                            self._logger.info(f"[Phase C] {word}: "
                                f"numFound={pending_searches[-1]['numFound']}")
                    except Exception as e:
                        self._logger.error(f"[Phase C] {word} 搜索异常: {e}")
                    time.sleep(jitter(3, 5))

                if pending_searches:
                    c_result = self._flywheel_engine.run_phase_c(pending_searches)
                    self._logger.info(
                        f"[Phase C] 完成: {c_result['pass_count']}通过 "
                        f"{c_result['watch_count']}观察 {c_result['discard_count']}淘汰")
                    self._notify_stage("flywheel_c",
                        f"Phase C: {c_result['pass_count']}通过 {c_result['discard_count']}淘汰",
                        done=n_pending, total=n_pending)
            else:
                self._logger.info("[飞轮] 无待验证词，跳过 Phase C")
                self._notify_stage("flywheel_c", "Phase C: 跳过(无待验证词)", done=1, total=1)

        # ═══════════ 飞轮闭环：pass词自动进入下一轮 ═══════════
        # 本轮数据加入累积
        self._all_kw_acc.extend(all_kw_results)
        self._all_pd_acc.extend(all_pd_results)
        self._all_supply_acc.extend(all_supply_pushed)

        if _round < self._flywheel_max_rounds and self._flywheel_engine:
            new_pass = self._flywheel_engine.word_lib.get_pass_words()
            new_round_words = [w for w in new_pass
                              if w not in self._processed_keywords]
            if new_round_words:
                new_round_words = new_round_words[:self._flywheel_round_size]
                self._processed_keywords.update(new_round_words)
                self._logger.info(
                    f"[飞轮闭环] 第{_round}轮完成 → 第{_round+1}轮: "
                    f"{len(new_round_words)}个新词 → {', '.join(new_round_words[:10])}"
                    f"{'...' if len(new_round_words) > 10 else ''}")
                self._notify_stage("flywheel_c",
                    f"飞轮闭环: 第{_round+1}轮 {len(new_round_words)}词",
                    done=_round, total=self._flywheel_max_rounds)
                return self._run(new_round_words, output_dir, _round=_round + 1)
            else:
                self._logger.info(f"[飞轮闭环] 第{_round}轮: 无新pass词，飞轮停止")

        # ═══════════ 最终轮：保存全部累积结果 ═══════════
        self._save_all_results(self._all_kw_acc, self._all_pd_acc, output_dir)
        _checkpoint("done")
        try:
            state_path.unlink(missing_ok=True)
        except Exception:
            pass
        return self._finish(self._all_kw_acc, self._all_pd_acc, self._all_supply_acc, output_dir)

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

        # ── 评分进化：积累训练样本（AI品类归并）──
        if self._scoring_evo:
            # 先批量 AI 推断品类
            kw_names = [kw.get("keyword", "") for kw in kw_results if kw.get("grade") != "N/A"]
            self._batch_infer_categories(kw_names)

            for kw in kw_results:
                kw_name = kw.get("keyword", "")
                if not kw_name or kw.get("grade") == "N/A":
                    continue
                dims = kw.get("scores", {})
                if not dims:
                    continue
                category = self._infer_category(kw_name)
                self._scoring_evo.add_record(
                    category, kw_name, kw.get("total_100", 0), dims,
                    actual_profit=None,
                )
            stats = self._scoring_evo.stats()
            self._logger.info(
                f"[进化] 已积累 {stats['total_records']} 条样本 "
                f"({stats['categories_with_data']} 品类)")

    def _infer_category(self, keyword: str) -> str:
        """从关键词推断品类（AI批量分类，结果缓存）"""
        # 初始化缓存
        if not hasattr(self, '_category_cache'):
            self._category_cache = {}
        if keyword in self._category_cache:
            return self._category_cache[keyword]

        # 延迟到 _save_all_results 批量调用
        self._category_cache[keyword] = keyword
        return keyword

    def _batch_infer_categories(self, keywords: List[str]):
        """批量用 AI 推断品类（一次API调用覆盖所有词）"""
        if not hasattr(self, '_category_cache'):
            self._category_cache = {}

        uncached = [k for k in keywords if self._category_cache.get(k) == k]
        if not uncached:
            return

        api_key = self._settings.get("api", {}).get("deepseek_api_key", "")
        if not api_key:
            self._logger.info("[进化] 无 API Key，使用关键词自身作为品类")
            return

        try:
            from engines.ai_client import AIClient
            client = AIClient(api_key, provider="deepseek")

            kw_list = "\n".join(uncached)
            prompt = f"""将以下闲鱼搜索词归类到品类。每个词只归入一个品类，品类名用2-4个字（如：自行车、家电、服饰、户外露营、数码3C）。

搜索词：
{kw_list}

返回JSON（不要markdown代码块）：
{{"categories": [{{"keyword": "捷安特ATX660", "category": "自行车"}}, ...]}}"""

            result = client.chat_json(
                "你是电商选品品类分析师。将搜索词归入品类，品类名简短通用。",
                prompt,
            )
            for item in result.get("categories", []):
                kw = item.get("keyword", "")
                cat = item.get("category", kw)
                if kw:
                    self._category_cache[kw] = cat
            self._logger.info(f"[进化] AI品类推断: {len(result.get('categories', []))}词")
        except Exception as e:
            self._logger.warn(f"[进化] AI品类推断失败，使用规则兜底: {e}")
            self._fallback_infer_categories(uncached)

    def _fallback_infer_categories(self, keywords: List[str]):
        """规则兜底：关键词→品类（AI不可用时）"""
        patterns = [
            ("自行车", ["自行车", "公路车", "山地车", "atx", "escape", "黑客", "ad350", "捷安特", "喜德盛", "骑行"]),
            ("服饰", ["三叶草", "adidas", "华夫格", "外套", "夹克", "卫衣", "针织", "耐克", "nike", "半拉链"]),
            ("户外露营", ["天幕", "露营", "帐篷", "遮阳", "月亮椅", "克米特", "户外"]),
            ("打印机", ["打印机", "激光", "惠普", "兄弟", "一体机"]),
            ("家电", ["微波炉", "洗衣机", "空气净化", "吸尘器", "戴森", "美的", "松下"]),
            ("家居收纳", ["衣帽架", "衣架", "晾衣", "置物架", "收纳", "宜家", "书桌"]),
            ("数码3C", ["笔记本", "电脑", "显示器", "充电宝", "小米", "大疆", "无人机"]),
            ("户外运动", ["凯乐石", "始祖鸟", "猛犸象", "露露", "冲锋衣", "软壳", "速干"]),
            ("轻奢饰品", ["apm", "coach", "古驰", "托特包", "手镯", "玉镯", "戒指"]),
            ("钓具", ["钓鱼", "钓伞"]),
            ("汽车周边", ["充电桩", "雨刷器"]),
            ("家具", ["床", "柜", "桌", "椅", "沙发", "茶台"]),
            ("宠物用品", ["猫", "狗", "宠物", "鸡笼", "鸟笼", "兔笼", "鱼缸"]),
            ("男装", ["西服", "休闲裤", "哈吉斯", "比音勒芬", "利郎", "哥伦比亚"]),
        ]
        for kw in keywords:
            # 已分类的跳过
            cached = self._category_cache.get(kw)
            if cached and cached != kw:
                continue
            kw_lower = kw.lower()
            for cat, terms in patterns:
                if any(t in kw_lower for t in terms):
                    self._category_cache[kw] = cat
                    break

        self._logger.info(f"[采集] 全部数据已保存到 {output_dir}")

    def _finish(self, kw_results: list, pd_results: list, supply_pushed: list, output_dir: Path):
        """采集完成"""
        self._logger.info(f"[采集] ===== 流水线完成: {len(kw_results)}词 {len(pd_results)}商品 {len(supply_pushed)}货源 =====")
        if self._on_complete:
            self._on_complete(kw_results, pd_results, supply_pushed)
        self._running = False
