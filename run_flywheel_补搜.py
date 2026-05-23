"""
补搜索 + 全量飞轮：对评分阶段被过滤的关键词，直接做轻量搜索
然后全量跑飞轮 Phase B + C
"""
import json, sys, time, random
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))

from engines.device_engine import DeviceEngine
from core.device_mgr import DeviceManager
from core.frida_bridge import FridaBridge
from engines.flywheel_engine import FlywheelEngine


def main():
    base = Path(__file__).parent
    output_dir = base / "collected_data"

    with open(base / "settings.json") as f:
        settings = json.load(f)

    # 找出没有 search_items 的关键词
    need_search = []
    for f in sorted(output_dir.glob("*.json")):
        name = f.name
        if name.startswith("_"):
            continue
        try:
            with open(f) as fp:
                d = json.load(fp)
            if not d.get("search_items"):
                need_search.append((name.replace(".json", ""), d))
        except Exception:
            pass

    print(f"需要补搜索: {len(need_search)} 词")
    print(f"已有数据: {66 - len(need_search)} 词 (之前飞轮已处理)")

    if not need_search:
        print("无需补搜，直接跑飞轮")
    else:
        # 连接设备
        dev_engine = DeviceEngine(settings)
        devices = dev_engine.list_devices()
        if not devices:
            print("❌ 无设备")
            return
        target = devices[0]
        state = dev_engine.connect(target.adb_addr)
        print(f"设备: {target.name}, 连接={state.connected}")

        bridge = FridaBridge(dev_engine._mgr)
        if not bridge.load():
            print("❌ Frida 加载失败")
            return
        print("Frida 就绪")

        # 逐个搜索（1页，轻量）
        total = len(need_search)
        for i, (kw, kw_data) in enumerate(need_search):
            delay = random.uniform(10, 20)
            time.sleep(delay)

            try:
                result = bridge.collect_keyword(kw, max_pages=1, detail_max=0, comment_max=0)
                if result.get("error"):
                    print(f"  [{i+1}/{total}] {kw}: ❌ {result['error'][:40]}")
                    continue

                items = result.get("searchItems", [])
                kw_data["search_items"] = items
                kw_data["numFound"] = result.get("searchMeta", {}).get("numFound", 0)

                # 保存
                kw_file = output_dir / f"{kw}.json"
                kw_file.write_text(json.dumps(kw_data, ensure_ascii=False, indent=2))

                print(f"  [{i+1}/{total}] {kw}: ✅ {len(items)}条, numFound={kw_data['numFound']}")

            except Exception as e:
                print(f"  [{i+1}/{total}] {kw}: ❌ {e}")

            # 每15个词暂停一会
            if (i + 1) % 15 == 0 and i + 1 < total:
                pause = random.randint(60, 120)
                print(f"  ⏸ 暂停{pause}秒...")
                time.sleep(pause)

        dev_engine.disconnect()
        print(f"\n补搜完成")

    # ── 全量飞轮 ──
    print(f"\n{'='*60}")
    print("全量飞轮 Phase B + C")
    print(f"{'='*60}")

    fw = FlywheelEngine(settings, output_dir=output_dir)
    keyword_data = []

    for f in sorted(output_dir.glob("*.json")):
        name = f.name
        if name.startswith("_") or name in ("flywheel_results.json", "word_library.json",
                                              "phase0_raw_data.json", "phase1_data.json",
                                              "pipeline_result.json", "pdd_search_test.json",
                                              "full_test_results.json",
                                              "_pipeline_summary.json", "_pipeline_state.json"):
            continue
        try:
            with open(f) as fp:
                d = json.load(fp)
            items = d.get("search_items", [])
            if items:
                keyword_data.append({
                    "keyword": name.replace(".json", ""),
                    "search_items": items,
                    "numFound": d.get("numFound", 0),
                })
        except Exception:
            pass

    print(f"有搜索数据的关键词: {len(keyword_data)} 个")

    if keyword_data:
        result = fw.run_phase_b_batch(keyword_data)
        s = result["summary"]
        wls = result["word_library_stats"]
        ctrls = result.get("controls", {})

        print(f"\n=== 飞轮结果 ===")
        print(f"处理: {s['keywords_processed']} 词")
        print(f"通过: {s['pass_words']} 词")
        print(f"观察: {s['watch_words']} 词")
        print(f"待验证: {s['pending_words']} 词")
        print(f"新素材: {s['new_materials']} 条")
        print(f"\n词库: {wls['total_words']}词 (通过{wls['pass_count']} 观察{wls['watch_count']})")
        print(f"素材库: {wls['total_materials']} 条")

        # 控制
        if ctrls.get("deduplicates"):
            print(f"\n控制-去重: {len(ctrls['deduplicates'])} 组")
        if ctrls.get("dominance"):
            print(f"控制-偏见: {ctrls['dominance'].get('action', '')}")
        if ctrls.get("expired"):
            print(f"控制-过期: {len(ctrls['expired'])} 词")

        # 通过词 TOP
        with open(output_dir / "word_library.json") as f:
            words = json.load(f).get("words", {})
        pass_words = [(w, i) for w, i in words.items() if i.get("status") == "pass"]
        pass_words.sort(key=lambda x: x[1].get("composite", 0), reverse=True)

        print(f"\n=== TOP 40 通过词 ===")
        for w, info in pass_words[:40]:
            print(f"  {w:30s} {info.get('composite',0):.1f}")

    print(f"\n{datetime.now().strftime('%H:%M:%S')} 全量飞轮完成")


if __name__ == "__main__":
    main()
