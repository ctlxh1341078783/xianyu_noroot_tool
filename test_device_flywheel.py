"""
真实设备 + 飞轮全流程测试
跑3个种子词 → 采集 → Phase B AI提取+评分 → Phase C验证
"""
import json, sys, time
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))

from engines.device_engine import DeviceEngine
from engines.collection_engine import CollectionEngine
from engines.keyword_scorer_v3 import KeywordScorerV3
from engines.product_scorer_v3 import ProductScorerV3
from engines.flywheel_engine import FlywheelEngine
from engines.supply_finder_engine import SupplyFinderEngine


def main():
    settings_path = Path(__file__).parent / "settings.json"
    with open(settings_path) as f:
        settings = json.load(f)

    # 降低阈值让数据能流通
    test_settings = json.loads(json.dumps(settings))
    test_settings["collection"]["search_pages"] = 3  # 快速测试
    test_settings["collection"]["detail_max"] = 3
    test_settings["collection"]["comment_max"] = 0
    test_settings["collection"]["kw_push_threshold"] = 30  # 降低阈值
    test_settings["collection"]["pd_push_threshold"] = 30

    output_dir = Path(__file__).parent / "collected_data"

    # 清除旧数据
    for old in output_dir.glob("*.json"):
        if old.name not in ("phase0_raw_data.json", "phase1_data.json", "raw_collection.json"):
            old.unlink()
    print(f"输出目录: {output_dir}")

    # 连接设备
    print("=" * 70)
    print("[1/4] 连接设备")
    print("=" * 70)
    dev_engine = DeviceEngine(settings)
    devices = dev_engine.list_devices()
    if not devices:
        print("❌ 无设备")
        return
    target = devices[0]
    print(f"设备: {target.name}")
    state = dev_engine.connect(target.adb_addr)
    print(f"连接: {state.connected}")

    # 测试关键词（3个不同品类）
    test_keywords = ["蓝牙耳机", "瑜伽垫", "帆布袋"]
    print(f"\n测试关键词: {test_keywords}")

    # 评分引擎 (降低阈值确保数据流通)
    scorer_cfg = {
        "keyword_model": {"grades": {"S": 90, "A": 30, "B": 20, "C": 10, "D": 0}},
        "product_model": {"grades": {"S": 90, "A": 30, "B": 20, "C": 10, "D": 0}},
    }
    kw_scorer = KeywordScorerV3(scorer_cfg)
    pd_scorer = ProductScorerV3(scorer_cfg)

    # 采集引擎
    print("\n" + "=" * 70)
    print("[2/4] 采集 + 飞轮 (Phase B)")
    print("=" * 70)

    engine = CollectionEngine(dev_engine, test_settings)
    engine.set_scorers(kw_scorer, pd_scorer)

    # 完成事件
    import threading
    done_event = threading.Event()
    results_holder = {}

    def on_stage(stage, info, done, total):
        print(f"  [{stage}] {info}")

    def on_complete(kw_results, pd_results, supply_pushed):
        results_holder["kw"] = kw_results
        results_holder["pd"] = pd_results
        results_holder["supply"] = supply_pushed
        done_event.set()

    engine.set_callbacks(on_stage=on_stage, on_complete=on_complete)

    start = time.time()
    engine.start(test_keywords, output_dir=output_dir)

    print("等待采集完成...")
    done_event.wait(timeout=600)  # 最多等10分钟

    elapsed = time.time() - start
    print(f"\n采集完成: {elapsed/60:.1f} 分钟")

    # 查看结果
    print("\n" + "=" * 70)
    print("[3/4] 飞轮结果分析")
    print("=" * 70)

    # 加载飞轮结果
    flywheel_path = output_dir / "flywheel_results.json"
    wordlib_path = output_dir / "word_library.json"

    if flywheel_path.exists():
        with open(flywheel_path) as f:
            fb_result = json.load(f)

        s = fb_result.get("summary", {})
        print(f"Phase B 完成:")
        print(f"  处理关键词: {s.get('keywords_processed', '?')}")
        print(f"  通过: {s.get('pass_words', 0)}")
        print(f"  观察: {s.get('watch_words', 0)}")
        print(f"  待验证: {s.get('pending_words', 0)}")
        print(f"  新素材: {s.get('new_materials', 0)}")

        wls = fb_result.get("word_library_stats", {})
        print(f"\n词库: {wls.get('total_words', 0)}词 (通过{wls.get('pass_count',0)} 观察{wls.get('watch_count',0)})")
        print(f"素材库: {wls.get('total_materials', 0)} 条")

        # 各词详情
        for r in fb_result.get("results", []):
            kw = r["parent_keyword"]
            sw = r.get("search_words", [])
            pass_w = [w["word"] for w in sw if w.get("status") == "pass"]
            watch_w = [w["word"] for w in sw if w.get("status") == "watch"]
            pend_w = [w["word"] for w in sw if w.get("status") == "pending_verify"]
            disc_w = [w["word"] for w in sw if w.get("status") == "discard"]
            tm = r.get("title_materials", {})
            n_tm = sum(len(v) for v in tm.values())

            print(f"\n  【{kw}】提取={r.get('extraction','?')}, 评分方法={r.get('method','?')}")
            if pass_w:
                for w in sw:
                    if w.get("status") == "pass":
                        print(f"    ✅ {w['word']:20s} comp={w.get('composite',0):.1f}  {w.get('evidence','')[:60]}")
            if watch_w:
                print(f"    👀 watch: {watch_w}")
            if pend_w:
                print(f"    ⏳ pending: {pend_w[:5]}...")
            if disc_w:
                print(f"    ❌ discard: {disc_w[:5]}...")
            if n_tm:
                print(f"    📝 素材: {n_tm}条")

    # 词库详情
    print("\n" + "=" * 70)
    print("[4/4] 词库详情")
    print("=" * 70)

    if wordlib_path.exists():
        with open(wordlib_path) as f:
            wl = json.load(f)
        words = wl.get("words", {})
        for word, info in list(words.items())[:30]:
            status = info.get("status", "?")
            comp = info.get("composite", 0)
            if status in ("pass", "watch"):
                print(f"  {word:25s} {status:12s} {comp:.1f}")

    print(f"\n✅ 设备飞轮测试完成 ({datetime.now().strftime('%H:%M:%S')})")


if __name__ == "__main__":
    main()
