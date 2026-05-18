"""全流程端到端测试：闲鱼采集(阶段1-4) → 货源查找 → PDD搜索+匹配+利润评估"""
import sys
import time
import threading
from pathlib import Path

# Windows GBK终端兼容
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

PROJECT_ROOT = Path(__file__).parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.config_manager import ConfigManager
from utils.log_manager import get_logger
from engines.device_engine import DeviceEngine
from engines.keyword_scorer_v3 import KeywordScorerV3
from engines.product_scorer_v3 import ProductScorerV3
from engines.supply_finder_engine import SupplyFinderEngine
from engines.collection_engine import CollectionEngine

# 设置日志
logger = get_logger()
logger.setup_file()

# 加载配置
config = ConfigManager()
config.load_all()

# 临时调低相似度阈值（text2vec 实际匹配度约 0.7，默认 0.8 太严格）
orig_sim = config.settings.get("supply_finder", {}).get("sim_threshold", 0.8)
config.settings.setdefault("supply_finder", {})["sim_threshold"] = 0.55
print(f"[配置] 相似度阈值: {orig_sim} -> 0.55 (临时)")

# 减少等待时间加速测试
config.settings["supply_finder"]["delay_between_products"] = 3
config.settings["supply_finder"]["pause_every"] = 10
config.settings["supply_finder"]["pause_duration"] = 15

# 测试关键词（只用2个加速测试）
KEYWORDS = ["iPhone15手机壳", "机械键盘", "AirPods"]

# 完成事件
collection_done = threading.Event()
supply_done = threading.Event()

kw_results = []
pd_results = []
supply_results = []

def on_stage(stage, info, done=0, total=0):
    print(f"[STAGE] {stage}: {info}")

def on_keyword(kw, idx, total, status,
               search_cnt=0, has_market=False, detail_cnt=0, comment_cnt=0,
               market_uv=0, market_price_inc=0):
    extras = []
    if search_cnt: extras.append(f"搜索{search_cnt}条")
    if detail_cnt: extras.append(f"详情{detail_cnt}")
    if comment_cnt: extras.append(f"评论{comment_cnt}")
    extra_str = " " + " ".join(extras) if extras else ""
    print(f"[KW] [{idx}/{total}] {kw} -> {status}{extra_str}")

def on_product(pd_result):
    grade = pd_result.get("grade", "?")
    total = pd_result.get("total_100", 0)
    title = pd_result.get("title", "")[:30]
    print(f"[商品] {title}: {total}分 {grade}级")

def on_collection_complete(kw_r, pd_r, supply_pushed):
    global kw_results, pd_results
    kw_results = kw_r or []
    pd_results = pd_r or []
    a_plus = sum(1 for r in kw_results if r.get("grade") in ("S", "A"))
    s_a = sum(1 for r in pd_results if r.get("grade") in ("S", "A"))
    print(f"\n[采集完成] {len(kw_results)}词 {len(pd_results)}商品 | A+词:{a_plus} S/A品:{s_a} | 推送到货源:{len(supply_pushed)}")
    collection_done.set()

def on_supply_result(result):
    print(f"[货源] {result.quadrant_emoji} {result.quadrant} {result.quadrant_label} | "
          f"利润 {result.final_profit or 0:.1f}元 | {result.recommendation[:40]}")
    supply_results.append(result)

def on_supply_complete():
    print(f"[货源完成] 共处理 {len(supply_results)} 件")
    supply_done.set()

# ==== 初始化引擎 ====
print("\n======== 初始化引擎 ========")

# 1. 设备引擎
dev_engine = DeviceEngine(config.settings)
active = dev_engine.get_active()

if not active or not active.app_pid:
    # 列出所有已知设备（包括settings中保存的）
    all_devices = dev_engine.list_devices()
    if not all_devices:
        # 尝试ADB扫描发现新设备
        dev_engine.scan()
        all_devices = dev_engine.list_devices()

    for d in all_devices:
        print(f"  设备: {d.name} ({d.adb_addr}) type={d.type} gadget={d.use_gadget}")

    if not all_devices:
        print("[ERROR] 未发现任何设备，请确认USB已连接")
        sys.exit(1)

    # 连接第一个设备
    target = all_devices[0]
    print(f"正在连接: {target.name} ({target.adb_addr})...")
    state = dev_engine.connect(target.adb_addr)
    print(f"连接结果: connected={state.connected}, adb={state.adb_ok}, frida={state.frida_server_ok}, app={state.app_ok}")
    if state.last_error:
        print(f"错误信息: {state.last_error}")
    active = dev_engine.get_active()

if not active or not active.app_pid:
    print("[ERROR] 设备连接失败，请确认：")
    print("   1. USB已连接手机")
    print("   2. 闲鱼App正在运行（带Frida Gadget）")
    sys.exit(1)

print(f"[OK] 设备就绪: {active.name} (PID {active.app_pid})")

# 2. 评分引擎（测试用：降低等级阈值让数据能流到货源阶段）
_low_grades = {"S": 90, "A": 30, "B": 20, "C": 10, "D": 0}
_test_scorer_cfg = {
    "keyword_model": {"grades": _low_grades},
    "product_model": {"grades": _low_grades},
}
kw_scorer = KeywordScorerV3(_test_scorer_cfg)
pd_scorer = ProductScorerV3(_test_scorer_cfg)
print(f"[OK] 评分引擎就绪 (测试阈值: A>=30)")

# 3. 货源引擎
sf_engine = SupplyFinderEngine(config.settings)
sf_engine.set_callbacks(
    on_progress=lambda msg: print(f"[货源进度] {msg}"),
    on_result=on_supply_result,
    on_complete=on_supply_complete,
)
print("[OK] 货源引擎就绪")

# 4. 采集引擎
col_engine = CollectionEngine(dev_engine, config.settings)
col_engine.set_scorers(kw_scorer, pd_scorer)
col_engine.set_supply_engine(sf_engine)
col_engine.set_callbacks(
    on_stage=on_stage,
    on_keyword=on_keyword,
    on_product=on_product,
    on_complete=on_collection_complete,
)
print("[OK] 采集引擎就绪")

# ==== 开始全流程 ====
print(f"\n======== 开始全流程: {KEYWORDS} ========")
output_dir = Path.home() / ".xianyu_tool" / "test_pipeline"
output_dir.mkdir(parents=True, exist_ok=True)

col_engine.start(KEYWORDS, output_dir)

# 等待采集完成
print("\n[等待] 采集阶段进行中...")
collection_done.wait(timeout=600)  # 10分钟超时

if not collection_done.is_set():
    print("[ERROR] 采集超时")
    col_engine.stop()
    sys.exit(1)

# 如果有货源推送，等待货源完成
if sf_engine.running:
    print("\n[等待] 货源查找进行中...")
    supply_done.wait(timeout=600)

# ==== 汇总 ====
print("\n======== 全流程汇总 ========")
print(f"选词结果: {len(kw_results)} 个")
for r in kw_results:
    print(f"  {r.get('keyword')}: {r.get('total_100')}分 {r.get('grade')}级")

print(f"\n商品结果: {len(pd_results)} 个")
s_a_products = [r for r in pd_results if r.get("grade") in ("S", "A")]
print(f"S/A级商品: {len(s_a_products)} 个")
for r in s_a_products[:5]:
    print(f"  {r.get('title','')[:30]}: {r.get('total_100')}分 {r.get('grade')}级")

print(f"\n货源结果: {len(supply_results)} 个")
for r in supply_results:
    print(f"  {r.quadrant_emoji} {r.quadrant} {r.quadrant_label} | "
          f"利润 {r.final_profit or 0:.1f}元 | 标题搜{len(r.title_matches)}件 图搜{len(r.image_matches)}件")

print("\n[OK] 全流程测试完成")
