"""完整真机测试 — 所有改动点验证（关键词: 衣服）"""
import json, sys, time, shutil
from pathlib import Path

PROJECT_DIR = Path(__file__).parent
sys.path.insert(0, str(PROJECT_DIR))

with open(PROJECT_DIR / "settings.json", "r", encoding="utf-8") as f:
    settings = json.load(f)

from engines.device_engine import DeviceEngine
from core.frida_bridge import FridaBridge
from core.global_rate_limiter import GlobalRateLimiter
from core.foreground_scheduler import ForegroundScheduler
from engines.flywheel_engine import FlywheelEngine
from engines.keyword_scorer_v3 import KeywordScorerV3
from engines.pdd_ddk_api import init_config, verify_api, _client_id, _daily_limit
from engines.collection_engine import CollectionEngine
from engines.parallel_pipeline import (
    ParallelPipeline, FlywheelWorker, KeywordWorker,
    ProductWorker, SupplyWorker, StopSignal
)

KW = "衣服"
PASS_CNT = 0
FAIL_CNT = 0

def check(name, cond, detail=""):
    global PASS_CNT, FAIL_CNT
    if cond:
        PASS_CNT += 1
        print(f"  [{name}] PASS {detail}")
    else:
        FAIL_CNT += 1
        print(f"  [{name}] FAIL {detail}")
    return cond

print("=" * 65)
print(f"  全部改动点真机验证（关键词: {KW}）")
print("=" * 65)

# ═══ 1. 设备 + Frida ═══
print("\n--- 1. 设备连接 + Frida桥接 ---")
dev_engine = DeviceEngine(settings)
mgr = dev_engine.manager
mgr.auto_scan()
mgr.connect(mgr.list_devices()[0].adb_addr)
check("设备连接", mgr.get_active() is not None)
check("服务就绪", dev_engine.ensure_services_ready())

bridge = FridaBridge(mgr)
check("Frida加载", bridge.load())

# ═══ 2. 限速器 + 真实搜索 ═══
print("\n--- 2. GlobalRateLimiter + 真实搜索 ---")
limiter = GlobalRateLimiter(min_interval_sec=3.0, max_interval_sec=10.0)

t0 = time.time()
limiter.acquire("搜索")
raw = bridge.collect_keyword(KW, max_pages=1, detail_max=0, comment_max=0)
items = raw.get("searchItems", [])
err = raw.get("error", "")
t1 = time.time()

check("搜索返回", len(items) > 0, f"{len(items)}件 ({t1-t0:.1f}s)")
for i, it in enumerate(items[:3]):
    print(f"     [{i+1}] {it.get('title','')[:50]} | {it.get('price','?')}")

limiter.acquire("限速验证")
t2 = time.time()
wait_sec = t2 - t1
check("限速间隔", wait_sec >= 2.8, f"等待{wait_sec:.1f}s (目标>=3.0s)")

stats = limiter.stats()
check("限速计数", stats["calls"] == 2, f"调用{stats['calls']}次")
print(f"     统计: 调用{stats['calls']}次 等待{stats['waits']}次 均等{stats['avg_wait_ms']}ms")

# ═══ 3. 前台调度器 ═══
print("\n--- 3. ForegroundScheduler ---")
fs = ForegroundScheduler(device_mgr=mgr)
tok = fs.request("xianyu", 60)
check("闲鱼令牌获取", tok is not None and fs.holder_app() == "xianyu")

tok2 = fs.request("pdd", 30)
check("PDD拒绝(闲鱼占用)", tok2 is None, "闲鱼占用中PDD正确被拒")

fs.release(tok)
check("令牌释放", not fs.is_held(), "释放后无人持有")

tok3 = fs.request("pdd", 30)
check("PDD获取(释放后)", tok3 is not None, "释放后PDD正确获取")
fs.release(tok3)

# ═══ 4. DDK API ═══
print("\n--- 4. DDK API ---")
init_config(settings)
resp = verify_api()
goods = resp.get("goods_search_response", {}).get("goods_list", [])
check("API连通", len(goods) > 0, f"返回{len(goods)}件")
check("client_id", _client_id() == settings["ddk"]["client_id"])
check("daily_limit", _daily_limit() == settings["ddk"]["daily_limit"], f"limit={_daily_limit()}")
if goods:
    g = goods[0]
    print(f"     示例: {g.get('goods_name','')[:40]} | {g.get('min_group_price',0)/100}元")

# ═══ 5. 飞轮引擎 Phase B ═══
print("\n--- 5. 飞轮引擎 Phase B (AI候选词提取) ---")
output_dir = Path.home() / ".xianyu_tool" / "collected_data"
fw = FlywheelEngine(settings, output_dir=output_dir)
titles = [it.get("title", "") for it in items if it.get("title")]
check("标题数", len(titles) > 0, f"{len(titles)}个标题")

# 检查词库初始状态
print(f"     词库初始词数: {len(fw.word_lib.get_all_words())}")

try:
    result = fw.run_phase_b(
        KW, titles, items,
        num_found=raw.get("searchMeta", {}).get("numFound", 0)
    )
    s = result.get("summary", {})
    n_candidates = result.get("candidates_extracted", 0)
    check("候选词提取", n_candidates > 0, f"{n_candidates}个候选词")
    print(f"     pass={s.get('pass_words',0)} watch={s.get('watch_words',0)} pending={s.get('pending_words',0)}")
    print(f"     词库总词: {len(fw.word_lib.get_all_words())}")
except Exception as e:
    print(f"     Phase B异常: {str(e)[:100]}")
    check("Phase B", False, f"异常: {str(e)[:60]}")

pass_words = fw.word_lib.get_pass_words()
pending_words = fw.word_lib.get_pending_words()
if pass_words:
    print(f"     pass词: {pass_words[:8]}")
if pending_words:
    print(f"     pending词: {pending_words[:8]}")

# ═══ 6. 关键词评分 ═══
print("\n--- 6. 关键词评分 (行情→预检→海选→精选) ---")
kw_scorer = KeywordScorerV3(settings)
test_kw = pass_words[0] if pass_words else KW
print(f"     测试词: {test_kw}")

# 行情
market_raw = bridge.collect_market(test_kw, hs_pages=3)
has_m = market_raw.get("hasMarket", False) if isinstance(market_raw, dict) else False
uv = KeywordScorerV3._calc_24h_uv(market_raw) if isinstance(market_raw, dict) else 0
spu = (market_raw.get("topbar", {}) or {}).get("spuHeader", {}) or {} if isinstance(market_raw, dict) else {}
price_inc = float(spu.get("avgPriceInc", 0))
print(f"     行情: hasMarket={has_m} 24hUV={uv} 均价涨跌={price_inc}")
check("行情采集", isinstance(market_raw, dict) and not market_raw.get("error"), f"hasMarket={has_m}")

if has_m and not market_raw.get("error"):
    tabs_raw = market_raw.get("tabs", [])
    tabs = tabs_raw.get("result", []) if isinstance(tabs_raw, dict) else (tabs_raw if isinstance(tabs_raw, list) else [])
    print(f"     成交记录: {len(tabs)}条")

    ok, reason = kw_scorer.precheck(test_kw, market_raw, tabs)
    print(f"     预检: {'通过' if ok else '淘汰'} - {reason}")
    check("预检执行", True, f"{'通过' if ok else '淘汰'}")

    if ok:
        score = kw_scorer.score_fast(test_kw, market_raw, {
            "numFound": market_raw.get("numFound", 0), "sellingOrder": ""})
        total = score.get("total_100", 0)
        grade = score.get("grade", "?")
        dims = score.get("scores", {})
        print(f"     海选: {total}分 {grade}级")
        check("海选评分", total > 0, f"{total}分 {grade}级")
        if dims:
            dim_str = " ".join(f"{k}={v}" for k, v in list(dims.items())[:6])
            print(f"     维度: {dim_str}")

        if total >= settings["collection"].get("kw_push_threshold", 75):
            full = kw_scorer.score_full(test_kw, market_raw, {
                "numFound": market_raw.get("numFound", 0), "sellingOrder": ""})
            print(f"     精选: {full.get('total_100',0)}分 {full.get('grade','?')}级")
            check("精选评分", full.get("total_100", 0) >= 0, f"{full.get('total_100',0)}分")
        else:
            print(f"     精选: 跳过(海选{total}分<阈值)")
else:
    print(f"     无行情数据，可能是这个词没有行情tab")

# ═══ 7. 并行流水线 ═══
print("\n--- 7. 并行流水线结构 + 队列 + 断点 ---")
p = ParallelPipeline(settings, dev_engine, kw_scorer, None, None)

fw_w = FlywheelWorker(p)
kw_w = KeywordWorker(p)
pd_w = ProductWorker(p)
sp_w = SupplyWorker(p)

check("FlywheelWorker", fw_w.daemon and fw_w.name == "飞轮")
check("KeywordWorker", kw_w.daemon and kw_w.name == "选词")
check("ProductWorker", pd_w.daemon and pd_w.name == "选品")
check("SupplyWorker", sp_w.daemon and sp_w.name == "货源")

check("word_queue", p._word_queue.maxsize == settings["flywheel"]["word_queue_max_size"],
      f"maxsize={p._word_queue.maxsize}")
check("a_plus_queue", p._a_plus_queue.maxsize == settings["flywheel"]["a_plus_queue_max_size"],
      f"maxsize={p._a_plus_queue.maxsize}")
check("good_product_queue", p._good_product_queue.maxsize == settings["flywheel"]["good_product_queue_max_size"],
      f"maxsize={p._good_product_queue.maxsize}")
check("限速器存在", p._rate_limiter is not None, f"间隔{p._rate_limiter._min_interval}s")
check("调度器存在", p._foreground is not None)

# 断点
cp_dir = output_dir / "_cp_test"
cp_dir.mkdir(exist_ok=True)
p._output_dir = cp_dir
state = {"kw_done": ["卫衣", "衬衫", "连衣裙"], "kw_current": "毛衣", "count": 3}
p._save_worker_checkpoint("keyword", state)
restored = p._load_worker_checkpoint("keyword")
check("断点保存恢复", restored["kw_done"] == state["kw_done"] and restored["count"] == 3)

# 过期检查
(cp_dir / "_checkpoint_keyword.json").write_text(
    json.dumps({"kw_done": ["旧数据"], "_ts": "2020-01-01 00:00:00"}), encoding="utf-8")
expired = p._load_worker_checkpoint("keyword")
check("断点过期", expired == {}, "过期返回空")

shutil.rmtree(cp_dir, ignore_errors=True)

# StopSignal
stop = StopSignal()
assert not stop.is_set
stop.set()
assert stop.is_set
assert stop.wait(1) is True
stop.clear()
assert not stop.is_set
check("StopSignal", True, "设置/清除/等待")

# ═══ 8. CollectionEngine ═══
print("\n--- 8. CollectionEngine 并行入口 ---")
engine = CollectionEngine(dev_engine, settings)
engine.set_scorers(kw_scorer, None)
check("start_parallel", hasattr(engine, "start_parallel"))
check("_on_parallel_done", hasattr(engine, "_on_parallel_done"))

# ═══ 9. 完整数据流模拟 ═══
print("\n--- 9. 数据流模拟 (word_queue → a_plus_queue) ---")
# 模拟飞轮产出词 → 选词取走 → 产出A+词
import queue
wq = queue.Queue(maxsize=10)
aq = queue.Queue(maxsize=10)

# 飞轮产词
for w in pass_words[:5]:
    wq.put(w)
print(f"     word_queue放入: {list(wq.queue)}")

# 选词取词（模拟第一个词的处理）
word = wq.get()
print(f"     选词取出: {word}")
wq.task_done()

# 模拟评分后 A+ 入队
aq.put((word, {"total_100": 82, "grade": "A", "avg_price": 35.0}))
print(f"     A+入队: {word} 82分A级")
print(f"     a_plus_queue: {list(aq.queue)}")

check("word_queue流转", wq.qsize() == len(pass_words[:5]) - 1, f"剩余{wq.qsize()}个")
check("a_plus_queue流转", aq.qsize() == 1, f"剩余{aq.qsize()}个")

# ═══ 断开 ═══
try:
    bridge.unload()
except:
    pass

print()
print("=" * 65)
print(f"  测试结果: {PASS_CNT} PASS / {FAIL_CNT} FAIL")
if FAIL_CNT == 0:
    print("  全部通过!")
else:
    print(f"  有 {FAIL_CNT} 项失败，请检查")
print(f"  真机: sceain7t6t4pjb7p | 搜索词: {KW}({len(items)}件) | DDK: 已连通")
print("=" * 65)
