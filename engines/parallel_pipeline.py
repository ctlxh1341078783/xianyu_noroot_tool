"""并行流水线编排器：4线程 + 3队列 + 限速 + 前台调度"""
import json
import queue
import random
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from core.global_rate_limiter import GlobalRateLimiter
from core.foreground_scheduler import ForegroundScheduler
from core.frida_bridge import FridaBridge
from engines.collection_engine import XianyuRiskGuard
from engines.flywheel_engine import FlywheelEngine
from engines.keyword_scorer_v3 import KeywordScorerV3
from utils.log_manager import get_logger

MARKET_CACHE_TTL_SEC = 3600


def jitter(lo: float, hi: float) -> float:
    return random.uniform(lo, hi)


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


class StopSignal:
    def __init__(self):
        self._event = threading.Event()

    @property
    def is_set(self) -> bool:
        return self._event.is_set()

    def set(self):
        self._event.set()

    def clear(self):
        self._event.clear()

    def wait(self, timeout=None) -> bool:
        return self._event.wait(timeout)


# ════════════════════════════════════════════════════════════════
# Worker 基类
# ════════════════════════════════════════════════════════════════

class BaseWorker(threading.Thread):
    def __init__(self, name: str, pipeline: "ParallelPipeline"):
        super().__init__(daemon=True, name=name)
        self._name = name
        self._p = pipeline
        self._logger = get_logger()

    def log(self, msg: str):
        self._logger.info(f"[{self._name}] {msg}")

    @property
    def stop(self) -> StopSignal:
        return self._p._stop

    @property
    def rate_limiter(self) -> GlobalRateLimiter:
        return self._p._rate_limiter

    @property
    def foreground(self) -> ForegroundScheduler:
        return self._p._foreground

    @property
    def bridge(self) -> Optional[FridaBridge]:
        return self._p._bridge

    def rpc_call(self, method_name: str, *args, caller_id: str = None,
                 risk_kw: str = "", **kwargs):
        """通过限速器包装的 RPC 调用，自动报告风控"""
        cid = caller_id or self._name
        self.rate_limiter.acquire(cid)
        try:
            method = getattr(self.bridge, method_name)
            result = method(*args, **kwargs)
            if isinstance(result, dict) and result.get("error"):
                self._p._risk_guard.report_error(
                    self._name, risk_kw or str(args[:1]), str(result["error"])[:50])
            else:
                self._p._risk_guard.report_success()
            return result
        except Exception as e:
            self._p._risk_guard.report_error(
                self._name, risk_kw or str(args[:1]), str(e)[:50])
            raise
        finally:
            self.rate_limiter.release()

    def save_checkpoint(self, state: dict):
        self._p._save_worker_checkpoint(self._name, state)

    def load_checkpoint(self) -> dict:
        return self._p._load_worker_checkpoint(self._name)

    def notify_stage(self, stage: str, info: str, done: int = 0, total: int = 0):
        if self._p._on_stage:
            self._p._on_stage(stage, info, done, total)

    def notify_kw(self, kw: str, idx: int, total: int, status: str,
                  search_cnt: int = 0, has_market: bool = False,
                  detail_cnt: int = 0, comment_cnt: int = 0,
                  market_uv: int = 0, market_price_inc: float = 0):
        if self._p._on_keyword:
            self._p._on_keyword(kw, idx, total, status, search_cnt,
                               has_market, detail_cnt, comment_cnt,
                               market_uv, market_price_inc)

    def notify_product(self, product: dict):
        if self._p._on_product:
            self._p._on_product(product)


# ════════════════════════════════════════════════════════════════
# Thread 1: 飞轮 Worker
# ════════════════════════════════════════════════════════════════

class FlywheelWorker(BaseWorker):
    """种子词搜索(1页) → Phase B AI提取 → 攒批 Phase C 验证 → 产词入 word_queue"""

    PHASE_C_BATCH_SIZE = 20
    PHASE_C_LEASE_SEC = 120

    def __init__(self, pipeline: "ParallelPipeline"):
        super().__init__("飞轮", pipeline)
        fw_cfg = pipeline._settings.get("flywheel", {})
        self._batch_size = fw_cfg.get("phase_c_batch_size", self.PHASE_C_BATCH_SIZE)
        self._output_interval = fw_cfg.get("output_min_interval_sec", 30)
        self._processed_seeds: set = set()        # 已处理的种子词
        self._pushed_words: set = set()            # 已推入 word_queue 的词（防重复）
        self._recycled_seeds: set = set()          # 已回种的 category_seed（防循环）
        self._category_stats: dict = {}            # 品类产出统计 {category: count}

    def run(self):
        self.log("飞轮线程启动")

        # 初始化飞轮引擎（如果尚未初始化）
        if not self._p._flywheel_engine:
            self._p._flywheel_engine = FlywheelEngine(
                self._p._settings, output_dir=self._p._output_dir)

        fw_engine = self._p._flywheel_engine

        # 恢复断点
        cp = self.load_checkpoint()
        seed_queue = list(self._p._seed_keywords) if cp.get("pending_seeds") is None else cp.get("pending_seeds", [])
        self._processed_seeds = set(cp.get("processed_seeds", []))
        self._recycled_seeds = set(cp.get("recycled_seeds", []))
        self._pushed_words = set(cp.get("pushed_words", []))

        # 过滤已处理的种子词
        seed_queue = [s for s in seed_queue if s not in self._processed_seeds]

        pending_for_c = []  # 攒批 Phase C 的词
        recycle_count = 0   # 回种轮次计数

        while (seed_queue or recycle_count < 3) and not self.stop.is_set:
            # 种子队列耗尽但还有回种空间 → 等待新种（避免空转）
            if not seed_queue:
                time.sleep(5)
                recycle_count += 1
                continue

            seed = seed_queue.pop(0)

            if seed in self._processed_seeds:
                continue

            self.log(f"种子词搜索: 【{seed}】")
            self.notify_stage("flywheel", f"飞轮: {seed} 搜索中...", done=0, total=0)

            try:
                # 搜索1页获取标题
                raw = self.rpc_call("collect_keyword", seed,
                                    max_pages=1, detail_max=0, comment_max=0,
                                    caller_id="飞轮-搜索", risk_kw="seed")
                if raw.get("error"):
                    self.log(f"种子词 {seed} 搜索失败: {raw['error']}")
                    self._processed_seeds.add(seed)
                    self.save_checkpoint({
                        "pending_seeds": seed_queue,
                        "processed_seeds": list(self._processed_seeds),
                        "recycled_seeds": list(self._recycled_seeds),
                        "pushed_words": list(self._pushed_words),
                    })
                    continue

                items = raw.get("searchItems", [])
                titles = [it.get("title", "") for it in items if it.get("title")]
                self.log(f"种子词 {seed}: {len(titles)} 个标题")

                # Phase B: AI 提取候选词
                if titles:
                    fb_result = fw_engine.run_phase_b(
                        parent_keyword=seed,
                        search_titles=titles,
                        item_stats=items,
                        num_found=raw.get("searchMeta", {}).get("numFound", 0),
                    )
                    n_new = fb_result.get("candidates_extracted", 0)
                    n_seeds = len(fb_result.get("category_seeds", []))
                    self.log(f"Phase B: {seed} → {n_new}候选词 (含{n_seeds}品类扩展词)")
                    self.notify_stage("flywheel",
                        f"飞轮B: {seed} → {n_new}词", done=1, total=1)

                    # 收集 pending 词，攒够一批跑 Phase C
                    new_pending = fw_engine.get_pending_words()
                    pending_for_c.extend(
                        w for w in new_pending
                        if w not in (p.get("word") for p in pending_for_c)
                    )

            except Exception as e:
                self.log(f"种子词 {seed} 异常: {e}")

            self._processed_seeds.add(seed)
            self.save_checkpoint({
                "pending_seeds": seed_queue,
                "processed_seeds": list(self._processed_seeds),
                "recycled_seeds": list(self._recycled_seeds),
                "pushed_words": list(self._pushed_words),
            })

            # Phase C: 攒够一批，申请前台令牌，批量验证
            if len(pending_for_c) >= self._batch_size:
                self._run_phase_c_batch(pending_for_c[:self._batch_size], fw_engine)
                pending_for_c = pending_for_c[self._batch_size:]

            # ★ 产出分流: search_word → word_queue
            self._push_pass_words(fw_engine)

            # ★ 回种: category_seed → seed_queue（飞轮持续转）
            self._recycle_category_seeds(fw_engine, seed_queue)

            # 品类多样性日志
            if self._category_stats:
                total = sum(self._category_stats.values())
                top_cat, top_n = max(self._category_stats.items(), key=lambda x: x[1])
                pct = top_n / total * 100 if total else 0
                self.log(f"品类分布: {len(self._category_stats)}类 "
                         f"| 主导:{top_cat}({pct:.0f}%) "
                         f"| 详情:{dict(sorted(self._category_stats.items(), key=lambda x:-x[1])[:5])}")

            # 飞轮产出间隔
            if seed_queue:
                time.sleep(self._output_interval)

        # 处理剩余的 pending 词
        if pending_for_c and not self.stop.is_set:
            self._run_phase_c_batch(pending_for_c, fw_engine)

        # 最后的产出
        self._push_pass_words(fw_engine)

        # 发送结束哨兵
        try:
            self._p._word_queue.put(None, timeout=1)
        except queue.Full:
            pass

        self.log("飞轮线程结束")
        self.notify_stage("flywheel", "飞轮完成", done=1, total=1)

    def _run_phase_c_batch(self, pending_words: List[str], fw_engine: FlywheelEngine):
        """申请前台令牌 → 批量搜索验证 → 释放令牌"""
        self.log(f"Phase C 攒批: {len(pending_words)} 词待验证")

        token = self.foreground.request("xianyu", self.PHASE_C_LEASE_SEC)
        if not token:
            self.log("Phase C: 无法获取前台令牌，待定词暂存")
            return

        searches = []
        for i, word in enumerate(pending_words):
            if self.stop.is_set:
                break
            try:
                raw = self.rpc_call("collect_keyword", word,
                                    max_pages=1, detail_max=0, comment_max=0,
                                    caller_id="飞轮C", risk_kw="phase_c")
                if not raw.get("error"):
                    searches.append({
                        "word": word,
                        "numFound": raw.get("searchMeta", {}).get("numFound", 0),
                        "search_items": raw.get("searchItems", []),
                    })
                self.notify_stage("flywheel_c", f"Phase C: {word}",
                                 done=i + 1, total=len(pending_words))
            except Exception as e:
                self.log(f"Phase C {word} 异常: {e}")

            # 检查是否需要收尾
            if self.foreground.check_renewal(token):
                self.log("Phase C: 收到收尾信号，保存进度")
                break
            time.sleep(jitter(2, 4))

        self.foreground.release(token)

        if searches:
            c_result = fw_engine.run_phase_c(searches)
            self.log(f"Phase C 完成: {c_result.get('pass_count', 0)}通过 "
                     f"{c_result.get('watch_count', 0)}观察 "
                     f"{c_result.get('discard_count', 0)}淘汰")

    def _push_pass_words(self, fw_engine: FlywheelEngine):
        """只将 search_word 类型的 pass 词推入 word_queue（跳过 category_seed）"""
        all_pass = fw_engine.word_lib.get_pass_words()
        pushed = 0
        skipped = 0
        for w in all_pass:
            if self.stop.is_set:
                break
            if w in self._pushed_words:
                continue
            entry = fw_engine.word_lib.get_entry(w)
            # category_seed 不回评分流水线，走回种通道
            if entry.get("word_type") == "category_seed":
                skipped += 1
                continue
            try:
                self._p._word_queue.put(w, timeout=2)
                self._pushed_words.add(w)
                pushed += 1
            except queue.Full:
                self.log("word_queue 已满，暂停产出")
                break
        if pushed or skipped:
            self.notify_stage("flywheel",
                f"产出: {pushed}词入队 + {skipped}品类种子待回种",
                done=pushed, total=len(all_pass))

    def _recycle_category_seeds(self, fw_engine: FlywheelEngine, seed_queue: list):
        """将 category_seed 回种到飞轮种子队列，驱动跨品类拓展"""
        MAX_RECYCLE_PER_ROUND = 5  # 每轮最多回种5个，防止种子爆炸

        seeds = fw_engine.word_lib.get_category_seeds()
        if not seeds:
            return

        # 优先回种来自弱势品类的种子（抑制单一品类垄断）
        recycled = []
        for s in seeds:
            word = s["word"]
            if word in self._recycled_seeds or word in self._processed_seeds:
                continue
            if len(recycled) >= MAX_RECYCLE_PER_ROUND:
                break

            direction = s.get("category_direction", "")
            # 如果某品类已占主导(>50%)，降低该方向种子的优先级
            if direction and self._category_stats:
                total = sum(self._category_stats.values())
                dom_pct = self._category_stats.get(direction, 0) / total * 100 if total else 0
                if dom_pct > 50:
                    continue  # 跳过主导品类的种子，等弱势品类

            seed_queue.append(word)
            self._recycled_seeds.add(word)
            recycled.append(word)

            # 更新品类统计
            if direction:
                self._category_stats[direction] = self._category_stats.get(direction, 0) + 1

            # 如果有 seed_for 提示，也加入种子队列（组合探索）
            for combo in s.get("seed_for", [])[:2]:
                if combo not in self._processed_seeds and combo not in self._recycled_seeds:
                    seed_queue.append(combo)
                    self._recycled_seeds.add(combo)
                    recycled.append(f"{word}→{combo}")

        if recycled:
            self.log(f"回种: {recycled} → 种子队列现有{len(seed_queue)}个")


# ════════════════════════════════════════════════════════════════
# Thread 2: 选词 Worker
# ════════════════════════════════════════════════════════════════

class KeywordWorker(BaseWorker):
    """取词 → 行情(RPC) → 预检 → 海选评分 → 精选评分 → A+词入 a_plus_queue"""

    def __init__(self, pipeline: "ParallelPipeline"):
        super().__init__("选词", pipeline)

    def run(self):
        self.log("选词线程启动")

        cp = self.load_checkpoint()
        processed_kws = set(cp.get("processed_keywords", []))
        kw_score_map = cp.get("kw_score_map", {})
        precheck_passed = cp.get("precheck_passed", 0)
        a_plus_count = cp.get("a_plus_count", 0)

        kw_total = 0

        while not self.stop.is_set:
            try:
                word = self._p._word_queue.get(timeout=3)
            except queue.Empty:
                continue

            # 结束哨兵
            if word is None:
                self._p._word_queue.task_done()
                break

            if word in processed_kws:
                self._p._word_queue.task_done()
                continue

            kw_total += 1
            self.log(f"行情采集: 【{word}】")

            # 行情缓存
            market_cache_dir = self._p._output_dir / "_market_cache"
            cached = _market_cache_get(market_cache_dir, word)
            if cached is not None:
                md = cached
                self.log(f"{word}: 行情缓存命中")
            else:
                self.notify_kw(word, kw_total, kw_total, "行情采集中")
                try:
                    hs_pages = self._p._settings.get("collection", {}).get("hs_pages", 3)
                    md = self.rpc_call("collect_market", word, hs_pages, caller_id="选词-行情", risk_kw="market")
                    _market_cache_put(market_cache_dir, word, md)
                except Exception as e:
                    self.log(f"{word} 行情异常: {e}")
                    md = {"error": str(e)}

            has_market = bool(md.get("hasMarket")) if isinstance(md, dict) else False
            uv_24h = KeywordScorerV3._calc_24h_uv(md) if self._p._kw_scorer and isinstance(md, dict) else 0
            spu = (md.get("topbar", {}) or {}).get("spuHeader", {}) or {} if isinstance(md, dict) else {}
            price_inc = float(spu.get("avgPriceInc", 0))
            self.notify_kw(word, kw_total, kw_total, "行情完成",
                          has_market=has_market, market_uv=uv_24h,
                          market_price_inc=price_inc)

            # 预检 + 海选评分
            if self._p._kw_scorer and isinstance(md, dict) and not md.get("error"):
                tabs_raw = md.get("tabs", [])
                if isinstance(tabs_raw, dict):
                    tabs = tabs_raw.get("result", [])
                else:
                    tabs = tabs_raw if isinstance(tabs_raw, list) else []

                ok, reason = self._p._kw_scorer.precheck(word, md, tabs)
                if not ok:
                    self.log(f"{word}: 预检淘汰 - {reason}")
                    self.notify_kw(word, kw_total, kw_total,
                                  f"淘汰: {reason[:20]}")
                    processed_kws.add(word)
                    self._p._word_queue.task_done()
                    self.save_checkpoint({
                        "processed_keywords": list(processed_kws),
                        "kw_score_map": kw_score_map,
                        "precheck_passed": precheck_passed,
                        "a_plus_count": a_plus_count,
                    })
                    continue

                precheck_passed += 1
                no_market = "无行情" in reason

                if no_market:
                    # 无行情词跳过评分，直通选品线（选品线用商品数据做最终判断）
                    score = {
                        "keyword": word, "total_100": 0, "grade": "no_market",
                        "method": "no_market", "has_market": False,
                        "num_found": md.get("numFound", 0),
                    }
                    kw_score_map[word] = score
                    a_plus_count += 1

                    try:
                        self._p._a_plus_queue.put((word, score), timeout=5)
                    except queue.Full:
                        self._p._a_plus_queue.put((word, score), timeout=120)

                    self.notify_kw(word, kw_total, kw_total, "直通选品", has_market=False)
                    self.log(f"{word}: 无行情直通选品线")

                else:
                    # 海选评分
                    score = self._p._kw_scorer.score_fast(word, md, {
                        "numFound": md.get("numFound", 0),
                        "sellingOrder": "",
                    })
                    total_score = score.get("total_100", 0)
                    grade = score.get("grade", "?")

                    kw_push_threshold = self._p._settings.get("collection", {}).get("kw_push_threshold", 75)
                    if total_score >= kw_push_threshold:
                        # 精选评分
                        evo = self._p._scoring_evo
                        evo_weights = evo.get_weights_for(word) if evo else None
                        evo_thresholds = evo.get_thresholds_for(word) if evo else None
                        full_score = self._p._kw_scorer.score_full(
                            word, md, {"numFound": md.get("numFound", 0), "sellingOrder": ""},
                            category_weights=evo_weights, category_thresholds=evo_thresholds)
                        kw_score_map[word] = full_score
                        a_plus_count += 1

                        self.notify_stage("keyword_scoring",
                            f"海选: {precheck_passed}通过", done=precheck_passed, total=kw_total)
                        self.notify_stage("keyword_full",
                            f"精选: {a_plus_count}个A+", done=a_plus_count, total=precheck_passed)

                        # A+ 词入队列
                        self._p._a_plus_queue.put((word, full_score), timeout=5)

                        self.notify_kw(word, kw_total, kw_total,
                                      f"{full_score.get('total_100', total_score)}分 {full_score.get('grade', grade)}级",
                                      has_market=has_market, market_uv=uv_24h,
                                      market_price_inc=price_inc)
                    else:
                        self.notify_kw(word, kw_total, kw_total,
                                      f"{total_score}分 {grade}级", has_market=has_market)

                # 结果累积
                with self._p._results_lock:
                    self._p._all_kw_results.append(score)
            else:
                self.notify_kw(word, kw_total, kw_total,
                              "无行情" if isinstance(md, dict) and md.get("error") else "无评分引擎")

            processed_kws.add(word)
            self._p._word_queue.task_done()

            # 保存断点
            self.save_checkpoint({
                "processed_keywords": list(processed_kws),
                "kw_score_map": {k: {"total_100": v.get("total_100", 0)}
                                for k, v in kw_score_map.items()},
                "precheck_passed": precheck_passed,
                "a_plus_count": a_plus_count,
            })

            time.sleep(jitter(0.5, 1.5))

        # 发送哨兵
        try:
            self._p._a_plus_queue.put(None, timeout=1)
        except queue.Full:
            pass

        self.log(f"选词线程结束 (处理{len(processed_kws)}词, {a_plus_count}A+)")
        self.notify_stage("keyword_full", f"完成: {a_plus_count}A+", done=a_plus_count, total=a_plus_count)


# ════════════════════════════════════════════════════════════════
# Thread 3: 选品 Worker
# ════════════════════════════════════════════════════════════════

class ProductWorker(BaseWorker):
    """取A+词 → 搜索(RPC) → 预筛选 → 详情(RPC) → 评论(RPC) → 深度评分 → 好品入 good_product_queue"""

    def __init__(self, pipeline: "ParallelPipeline"):
        super().__init__("选品", pipeline)

    def run(self):
        self.log("选品线程启动")

        cp = self.load_checkpoint()
        processed_kws = set(cp.get("processed_keywords", []))
        product_results = cp.get("product_results", {})
        all_pd_results = cp.get("all_pd_results", [])

        while not self.stop.is_set:
            try:
                item = self._p._a_plus_queue.get(timeout=3)
            except queue.Empty:
                continue

            if item is None:
                self._p._a_plus_queue.task_done()
                break

            kw, kw_score = item
            if not isinstance(kw_score, dict):
                kw_score = {"total_100": 0}
            if kw in processed_kws:
                self._p._a_plus_queue.task_done()
                continue

            self.log(f"搜索: 【{kw}】")
            self.notify_stage("product_prefilter", f"{kw}: 搜索中...", done=0, total=0)
            self.notify_stage("product_detail", f"{kw}: 详情: 0/0", done=0, total=0)

            search_pages = self._p._settings.get("collection", {}).get("search_pages", 10)
            detail_max = self._p._settings.get("collection", {}).get("detail_max", 5)
            comment_max = self._p._settings.get("collection", {}).get("comment_max", 3)

            try:
                # Step A: 搜索
                raw = self.rpc_call("collect_keyword", kw, search_pages, 0, 0,
                                    caller_id="选品-搜索", risk_kw="search")
                if raw.get("error"):
                    self.log(f"{kw} 搜索失败: {raw['error']}")
                    processed_kws.add(kw)
                    self._p._a_plus_queue.task_done()
                    self._save_cp(processed_kws, product_results, all_pd_results)
                    continue

                items = raw.get("searchItems", [])
                self.log(f"{kw}: {len(items)} 搜索结果")

                if not items:
                    processed_kws.add(kw)
                    self._p._a_plus_queue.task_done()
                    self._save_cp(processed_kws, product_results, all_pd_results)
                    continue

                # 预筛选
                avg_price = kw_score.get("avg_price", 0)
                if self._p._pd_scorer:
                    keep_list, discard_list = self._p._pd_scorer.prefilter(items, avg_price)
                    self.log(f"{kw}: 预筛保留{len(keep_list)}/淘汰{len(discard_list)}")
                else:
                    keep_list = items

                self.notify_stage("product_prefilter", f"{kw}: 预筛{len(keep_list)}/{len(items)}",
                                 done=len(keep_list), total=len(items))

                # Step B: 精准详情
                detail_keep = keep_list[:detail_max] if detail_max > 0 else keep_list
                detail_ids = [it.get("itemId", "") for it in detail_keep if it.get("itemId")]
                details_map = {}
                if detail_ids:
                    try:
                        details_raw = self.rpc_call("collect_details", detail_ids,
                                                    caller_id="选品-详情", risk_kw="detail")
                        details_map = details_raw if isinstance(details_raw, dict) else {}
                    except Exception as e:
                        self.log(f"{kw} 详情异常: {e}")

                n_detail = len(details_map)
                self.notify_stage("product_detail", f"{kw}: 详情{n_detail}/{len(detail_ids)}",
                                 done=n_detail, total=len(detail_ids))

                # Step C: 评论
                comments_map = {}
                if comment_max > 0 and details_map:
                    comment_ids = list(details_map.keys())[:comment_max]
                    try:
                        comments_raw = self.rpc_call("collect_comments", comment_ids,
                                                     caller_id="选品-评论", risk_kw="comment")
                        comments_map = comments_raw if isinstance(comments_raw, dict) else {}
                    except Exception as e:
                        self.log(f"{kw} 评论异常: {e}")

                # 保存结果
                kw_data = {
                    "keyword": kw, "search_items": items,
                    "details": details_map, "comments": comments_map,
                    "keep_list": keep_list, "discard_list": discard_list,
                }
                product_results[kw] = kw_data

                # 深度评分
                if self._p._pd_scorer:
                    no_bargain_rate = kw_score.get("no_bargain_rate", 0.5)
                    pd_push_threshold = self._p._settings.get("collection", {}).get("pd_push_threshold", 75)

                    for item in keep_list:
                        if self.stop.is_set:
                            break
                        item_id = item.get("itemId", "")
                        if item_id not in details_map:
                            continue

                        detail_raw = details_map[item_id]
                        detail_api = {
                            "item": detail_raw.get("itemDO", detail_raw.get("item", {})),
                            "seller": detail_raw.get("sellerDO", detail_raw.get("seller", {})),
                        }

                        try:
                            pd_score = self._p._pd_scorer.score_one(
                                keyword=kw, search_item=item, detail=detail_api,
                                market_avg_price=avg_price, no_bargain_rate=no_bargain_rate)

                            if pd_score:
                                all_pd_results.append(pd_score)
                                total_scored = len(all_pd_results)

                                self.notify_stage("supply",
                                    f"评分: {total_scored}件",
                                    done=total_scored, total=total_scored)

                                if pd_score.get("total_100", 0) >= pd_push_threshold:
                                    # 构建丰富商品数据
                                    detail_item = detail_api.get("item", {})
                                    seller_item = detail_api.get("seller", {})
                                    enriched = dict(item)
                                    enriched["商品标题"] = pd_score.get("title", item.get("title", ""))
                                    enriched["商品价格"] = pd_score.get("price", item.get("price", ""))
                                    enriched["商品描述"] = detail_item.get("desc", "") or item.get("title", "")
                                    enriched["商品链接"] = item.get("itemUrl",
                                        f"https://www.goofish.com/item?id={item_id}")
                                    enriched["商品图片"] = item.get("picUrl", item.get("pics", ""))
                                    enriched["综合评分"] = pd_score.get("total_100", 0)
                                    enriched["评分等级"] = pd_score.get("grade", "?")
                                    if detail_item:
                                        enriched["wantCnt"] = detail_item.get("wantCnt", 0)
                                        enriched["soldCount"] = detail_item.get("soldCnt", item.get("soldCount", 0))
                                        enriched["collectCnt"] = detail_item.get("collectCnt", 0)
                                        enriched["browseCnt"] = detail_item.get("browseCnt", 0)
                                        gmt_create = detail_item.get("gmtCreate", 0)
                                        if gmt_create:
                                            try:
                                                created = datetime.fromtimestamp(int(gmt_create) / 1000)
                                                enriched["daysOnSale"] = (datetime.now() - created).days
                                            except (ValueError, TypeError, OSError):
                                                enriched["daysOnSale"] = item.get("daysOnSale", 0)
                                        else:
                                            enriched["daysOnSale"] = item.get("daysOnSale", 0)
                                    if seller_item:
                                        enriched["卖家昵称"] = seller_item.get("nick", "")
                                        enriched["卖家已售"] = seller_item.get("hasSoldNumInteger", 0)

                                    try:
                                        self._p._good_product_queue.put(enriched, timeout=5)
                                    except queue.Full:
                                        self.log("good_product_queue 已满，等待消费")
                                        self._p._good_product_queue.put(enriched, timeout=120)

                                    self.notify_product(pd_score)
                        except Exception as e:
                            self.log(f"评分 {kw}/{item_id} 异常: {e}")

                self.notify_kw(kw, kw_total_aplus=len(processed_kws) + 1,
                              processed_kws_total=len(processed_kws) + 1,
                              status="闭环完成",
                              search_cnt=len(items), detail_cnt=n_detail,
                              comment_cnt=len(comments_map))

            except Exception as e:
                self.log(f"{kw} 闭环异常: {e}")

            processed_kws.add(kw)
            self._p._a_plus_queue.task_done()
            self._save_cp(processed_kws, product_results, all_pd_results)

            time.sleep(jitter(1, 3))

        # 发送哨兵
        try:
            self._p._good_product_queue.put(None, timeout=1)
        except queue.Full:
            pass

        self.log(f"选品线程结束 ({len(processed_kws)}词, {len(all_pd_results)}商品)")

    def _save_cp(self, processed_kws, product_results, all_pd_results):
        self.save_checkpoint({
            "processed_keywords": list(processed_kws),
            "product_results": {k: {"keep_list": v.get("keep_list", []),
                                    "details": v.get("details", {}),
                                    "search_items_count": len(v.get("search_items", []))}
                               for k, v in product_results.items()},
            "all_pd_results": all_pd_results[:50],  # 只保存最近50条
        })

    def notify_kw(self, kw: str, kw_total_aplus: int, processed_kws_total: int,
                  status: str, search_cnt: int = 0, detail_cnt: int = 0, comment_cnt: int = 0):
        """简化版通知（选品阶段）"""
        if self._p._on_keyword:
            self._p._on_keyword(kw, kw_total_aplus, processed_kws_total,
                               status, search_cnt, True, detail_cnt, comment_cnt, 0, 0)


# ════════════════════════════════════════════════════════════════
# Thread 4: 货源 Worker
# ════════════════════════════════════════════════════════════════

class SupplyWorker(BaseWorker):
    """取好品 → DDK API(优先) → 无结果才走手机 → 写入结果"""

    def __init__(self, pipeline: "ParallelPipeline"):
        super().__init__("货源", pipeline)

    def run(self):
        self.log("货源线程启动")

        ddk_finder = None
        try:
            from engines.pdd_ddk_api import DdkSupplyFinder
            ddk_finder = DdkSupplyFinder()
            ddk_finder.set_logger(self.log)
        except Exception as e:
            self.log(f"DDK初始化失败: {e}")

        processed = 0
        ddk_success = 0
        mobile_fallback = 0

        while not self.stop.is_set:
            try:
                item = self._p._good_product_queue.get(timeout=5)
            except queue.Empty:
                continue

            if item is None:
                self._p._good_product_queue.task_done()
                break

            processed += 1
            title = item.get("商品标题", item.get("title", ""))[:30]
            self.log(f"货源查找: {title}")

            # Step 1: DDK API 优先
            ddk_results = []
            if ddk_finder:
                try:
                    keyword = self._extract_search_keyword(item)
                    if keyword:
                        ddk_results = ddk_finder.search_and_collect(
                            keyword, scroll_pages=3, max_items=30)
                        if ddk_results:
                            ddk_success += 1
                            self.log(f"DDK: {title} → {len(ddk_results)}件货源")
                except Exception as e:
                    self.log(f"DDK {title} 异常: {e}")

            # Step 2: 无DDK结果 → 手机兜底
            if not ddk_results and self._p._supply_engine:
                token = self.foreground.request("pdd", 60)
                if token:
                    self.log(f"手机兜底: {title}")
                    try:
                        self._p._supply_engine.add_item(item)
                        # 等待 supply_engine 处理完成或令牌到期
                        deadline = time.time() + 50
                        while self._p._supply_engine.running and time.time() < deadline:
                            if self.stop.is_set or self.foreground.check_renewal(token):
                                break
                            time.sleep(2)
                        mobile_fallback += 1
                    except Exception as e:
                        self.log(f"手机兜底 {title} 异常: {e}")
                    finally:
                        self.foreground.release(token)
                else:
                    self.log(f"手机不可用，{title} 仅DDK结果")

            # 更新漏斗
            self.notify_stage("supply",
                f"DDK:{ddk_success} 手机:{mobile_fallback} / 推送{processed}",
                done=ddk_success + mobile_fallback, total=processed)

            # 累积货源结果
            with self._p._results_lock:
                self._p._all_supply_results.append({
                    "item": item,
                    "ddk_results": ddk_results,
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                })

            self._p._good_product_queue.task_done()
            self.save_checkpoint({
                "processed": processed,
                "ddk_success": ddk_success,
                "mobile_fallback": mobile_fallback,
            })

            time.sleep(jitter(1, 3))

        self.log(f"货源线程结束 (DDK{ddk_success}/手机{mobile_fallback}/共{processed})")

    def _extract_search_keyword(self, item: dict) -> str:
        """从商品数据中提取拼多多搜索关键词"""
        title = item.get("商品标题", item.get("title", ""))
        # 取前2-3个关键词组合
        parts = title.replace(" ", "").replace("，", ",").replace("、", ",").split(",")
        if parts:
            return " ".join(parts[:3])[:50]
        return title[:30]


# ════════════════════════════════════════════════════════════════
# 并行流水线编排器
# ════════════════════════════════════════════════════════════════

class ParallelPipeline:
    """4线程 + 3队列并行流水线，替代 CollectionEngine._run()"""

    def __init__(self, settings: dict, device_engine, kw_scorer, pd_scorer, supply_engine):
        self._settings = settings
        self._dev = device_engine
        self._kw_scorer = kw_scorer
        self._pd_scorer = pd_scorer
        self._supply_engine = supply_engine
        self._logger = get_logger()

        fw = settings.get("flywheel", {})
        rl = settings.get("rate_limit", {})
        fg = settings.get("foreground", {})

        # 队列
        self._word_queue = queue.Queue(maxsize=fw.get("word_queue_max_size", 200))
        self._a_plus_queue = queue.Queue(maxsize=fw.get("a_plus_queue_max_size", 50))
        self._good_product_queue = queue.Queue(maxsize=fw.get("good_product_queue_max_size", 30))

        # 限速器
        self._rate_limiter = GlobalRateLimiter(
            min_interval_sec=rl.get("min_interval_sec", 3.0),
            max_interval_sec=rl.get("max_interval_sec", 10.0),
            log_cb=self._logger.info,
        )

        # 前台调度器
        self._foreground = ForegroundScheduler(
            device_mgr=device_engine.manager if device_engine else None,
            renewal_warning_sec=fg.get("renewal_warning_sec", 15),
            pdd_max_wait_sec=fg.get("pdd_max_wait_sec", 120),
            log_cb=self._logger.info,
        )

        # 风控守护（所有 Worker 共享）
        webhook_url = settings.get("api", {}).get("webhook_url", "")
        self._risk_guard = XianyuRiskGuard(
            device_engine.manager if device_engine else None,
            self._logger,
            webhook_url,
        )

        # 共享组件（延迟初始化）
        self._bridge: Optional[FridaBridge] = None
        self._flywheel_engine: Optional[FlywheelEngine] = None
        self._scoring_evo = None

        # 停止信号
        self._stop = StopSignal()

        # 回调
        self._on_stage: Optional[Callable] = None
        self._on_keyword: Optional[Callable] = None
        self._on_product: Optional[Callable] = None
        self._on_complete: Optional[Callable] = None

        # 累积结果
        self._all_kw_results: list = []
        self._all_pd_results: list = []
        self._all_supply_results: list = []
        self._results_lock = threading.Lock()

        # 运行时状态
        self._output_dir: Optional[Path] = None
        self._seed_keywords: List[str] = []
        self._running = False
        self._workers: List[BaseWorker] = []

        # 监控线程
        self._monitor_thread: Optional[threading.Thread] = None

    @property
    def running(self) -> bool:
        return self._running

    def set_callbacks(self, on_stage=None, on_keyword=None, on_product=None, on_complete=None):
        self._on_stage = on_stage
        self._on_keyword = on_keyword
        self._on_product = on_product
        self._on_complete = on_complete

    def start(self, keywords: List[str], output_dir: Path):
        if self._running:
            self._logger.warn("并行流水线已在运行")
            return

        self._seed_keywords = list(keywords)
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)

        # 初始化 FridaBridge
        if not self._init_bridge():
            self._logger.error("Frida桥接初始化失败")
            return

        # 初始化评分进化
        if self._scoring_evo is None:
            from engines.scoring_evolution import ScoringEvolution
            evo_path = self._output_dir / "scoring_evolution.json"
            self._scoring_evo = ScoringEvolution(None, storage_path=evo_path)

        self._stop.clear()
        self._running = True

        self._logger.info(f"[并行] 启动 4 线程流水线: {len(keywords)} 种子词")

        # 创建 Workers
        self._workers = [
            FlywheelWorker(self),
            KeywordWorker(self),
            ProductWorker(self),
            SupplyWorker(self),
        ]

        for w in self._workers:
            w.start()

        # 监控线程：检测全部完成或停止
        self._monitor_thread = threading.Thread(
            target=self._monitor, daemon=True, name="PipelineMonitor")
        self._monitor_thread.start()

    def stop(self):
        self._logger.info("[并行] 停止信号已发送，等待线程收尾...")
        self._stop.set()

        # 清空队列避免阻塞
        for q in [self._word_queue, self._a_plus_queue, self._good_product_queue]:
            try:
                q.put_nowait(None)
            except queue.Full:
                pass

        # 等待最多 10 秒
        deadline = time.time() + 10
        for w in self._workers:
            remaining = max(0, deadline - time.time())
            w.join(timeout=remaining)

        self._risk_guard.reset()
        self._running = False
        self._logger.info("[并行] 流水线已停止")

    def _init_bridge(self) -> bool:
        if self._bridge and self._bridge.loaded:
            return True
        if not self._dev or not self._dev.manager:
            self._logger.error("[并行] 设备引擎未初始化")
            return False
        self._bridge = FridaBridge(self._dev.manager)
        if not self._bridge.load():
            self._logger.error("[并行] Frida桥接初始化失败")
            return False
        self._logger.info("[并行] Frida桥接就绪")
        return True

    def _monitor(self):
        """监控所有线程，全部结束后触发完成回调"""
        while self._running and not self._stop.is_set:
            all_done = all(not w.is_alive() for w in self._workers)
            if all_done:
                self._logger.info("[并行] 全部 4 线程完成")
                self._running = False
                self._save_final_results()
                if self._on_complete:
                    self._on_complete(
                        self._all_kw_results,
                        self._all_pd_results,
                        self._all_supply_results,
                    )
                break
            time.sleep(2)

    def _save_final_results(self):
        """保存最终结果（含品类推断和进化数据积累）"""
        output_dir = self._output_dir
        if not output_dir:
            return

        # ── AI 品类推断 ──
        kw_names = [kw.get("keyword", "") for kw in self._all_kw_results
                    if kw.get("grade") != "N/A"]
        if kw_names:
            self._batch_infer_categories(kw_names)

        # ── 评分进化：积累训练样本 ──
        if self._scoring_evo:
            for kw in self._all_kw_results:
                kw_name = kw.get("keyword", "")
                if not kw_name or kw.get("grade") == "N/A":
                    continue
                dims = kw.get("scores", {})
                if not dims:
                    continue
                category = self._infer_category(kw_name)
                self._scoring_evo.add_record(
                    category, kw_name, kw.get("total_100", 0), dims,
                    actual_profit=None)
            stats = self._scoring_evo.stats()
            self._logger.info(
                f"[进化] 已积累 {stats['total_records']} 条样本 "
                f"({stats['categories_with_data']} 品类)")

        # 汇总 JSON
        summary = {
            "collectedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "keywords": self._all_kw_results,
            "products": self._all_pd_results,
            "supply": self._all_supply_results,
        }
        path = output_dir / "_pipeline_summary.json"
        try:
            path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            self._logger.info(f"[并行] 汇总已保存: {path}")
        except OSError as e:
            self._logger.error(f"[并行] 保存汇总失败: {e}")

        # Excel 导出
        try:
            from exporter.excel_exporter import ExcelExporter
            xlsx_path = output_dir / f"采集结果_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
            ExcelExporter.export_dashboard(str(xlsx_path), self._all_kw_results, self._all_pd_results)
            self._logger.info(f"[并行] Excel已保存: {xlsx_path}")
        except Exception as e:
            self._logger.warn(f"[并行] Excel导出失败: {e}")

        # 清理断点文件
        for w_name in ["飞轮", "选词", "选品", "货源"]:
            cp = self._checkpoint_path(w_name)
            cp.unlink(missing_ok=True)

    # ═══ AI 品类推断（从 CollectionEngine 复制，保持完全一致） ═══

    def _infer_category(self, keyword: str) -> str:
        if not hasattr(self, '_category_cache'):
            self._category_cache = {}
        if keyword in self._category_cache:
            return self._category_cache[keyword]
        self._category_cache[keyword] = keyword
        return keyword

    def _batch_infer_categories(self, keywords: list):
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
            prompt = (
                "将以下闲鱼搜索词归类到品类。每个词只归入一个品类，品类名用2-4个字"
                "（如：自行车、家电、服饰、户外露营、数码3C）。\n\n"
                f"搜索词：\n{kw_list}\n\n"
                "返回JSON（不要markdown代码块）：\n"
                '{"categories": [{"keyword": "捷安特ATX660", "category": "自行车"}, ...]}'
            )
            result = client.chat_json(
                "你是电商选品品类分析师。将搜索词归入品类，品类名简短通用。",
                prompt)
            for item in result.get("categories", []):
                kw = item.get("keyword", "")
                cat = item.get("category", kw)
                if kw:
                    self._category_cache[kw] = cat
            self._logger.info(f"[进化] AI品类推断: {len(result.get('categories', []))}词")
        except Exception as e:
            self._logger.warn(f"[进化] AI品类推断失败，使用规则兜底: {e}")
            self._fallback_infer_categories(uncached)

    def _fallback_infer_categories(self, keywords: list):
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
            cached = self._category_cache.get(kw)
            if cached and cached != kw:
                continue
            kw_lower = kw.lower()
            for cat, terms in patterns:
                if any(t in kw_lower for t in terms):
                    self._category_cache[kw] = cat
                    break
        self._logger.info(f"[并行] 全部数据已保存到 {self._output_dir}")

    # ═══ 断点管理 ═══

    def _checkpoint_path(self, worker_name: str) -> Path:
        return self._output_dir / f"_checkpoint_{worker_name}.json"

    def _save_worker_checkpoint(self, worker_name: str, state: dict):
        if not self._output_dir:
            return
        cp = self._checkpoint_path(worker_name)
        state["_ts"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tmp = cp.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(cp)
        except OSError:
            pass

    def _load_worker_checkpoint(self, worker_name: str) -> dict:
        if not self._output_dir:
            return {}
        cp = self._checkpoint_path(worker_name)
        if not cp.exists():
            return {}
        try:
            data = json.loads(cp.read_text(encoding="utf-8"))
            # 时效性检查：2小时
            ts_str = data.pop("_ts", "")
            if ts_str:
                try:
                    ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                    if (datetime.now() - ts).total_seconds() > 7200:
                        return {}
                except ValueError:
                    pass
            return data
        except (json.JSONDecodeError, OSError):
            return {}

    def stats(self) -> dict:
        return {
            "rate_limiter": self._rate_limiter.stats(),
            "word_queue": self._word_queue.qsize(),
            "a_plus_queue": self._a_plus_queue.qsize(),
            "good_product_queue": self._good_product_queue.qsize(),
            "kw_results": len(self._all_kw_results),
            "pd_results": len(self._all_pd_results),
            "supply_results": len(self._all_supply_results),
        }
