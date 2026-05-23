"""
端到端飞轮测试：用真实采集数据跑 Phase B + Phase C
验证从种子词 → AI 候选词提取 → 词库膨胀的完整链路
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from engines.flywheel_engine import FlywheelEngine


def load_existing_data(data_path: str) -> list:
    """从 phase0_raw_data.json 加载，转为飞轮需要的格式"""
    with open(data_path, encoding="utf-8") as f:
        raw = json.load(f)

    keyword_data = []
    for kw, val in raw.items():
        items = val.get("items", val.get("searchItems", []))
        if not items:
            continue
        keyword_data.append({
            "keyword": kw,
            "search_items": items,
            "numFound": val.get("numFound", len(items)),
        })
    return keyword_data


def main():
    # 加载配置
    settings_path = Path(__file__).parent / "settings.json"
    with open(settings_path) as f:
        settings = json.load(f)

    output_dir = Path(__file__).parent / "collected_data"

    # 检查 API key
    api_cfg = settings.get("api", {})
    has_api = bool(api_cfg.get("deepseek_api_key") or api_cfg.get("zhipu_api_key"))
    print("=" * 70)
    print("飞轮 E2E 测试")
    print(f"AI 可用: {'是 (DeepSeek)' if has_api else '否 (仅规则降级)'}")
    print(f"输出目录: {output_dir}")
    print("=" * 70)

    # 初始化飞轮引擎
    engine = FlywheelEngine(settings, output_dir=output_dir)

    # 加载已有采集数据
    data_path = output_dir / "phase0_raw_data.json"
    if not data_path.exists():
        print(f"\n❌ 数据文件不存在: {data_path}")
        print("请先运行采集流水线生成数据")
        return

    keyword_data = load_existing_data(str(data_path))
    print(f"\n📦 加载采集数据: {len(keyword_data)} 个关键词")

    for kd in keyword_data:
        titles = [it.get("title", "") for it in kd["search_items"]
                  if it.get("title")]
        print(f"  {kd['keyword']}: {len(titles)} 条标题")

    # ═══ Phase B: 批量 AI 分析 ═══
    print("\n" + "=" * 70)
    print("Phase B: AI 候选词提取 + 评分")
    print("=" * 70)

    fb_result = engine.run_phase_b_batch(keyword_data)

    summary = fb_result["summary"]
    stats = fb_result["word_library_stats"]
    print(f"\n📊 Phase B 汇总:")
    print(f"  关键词处理: {summary['keywords_processed']} 个")
    print(f"  通过词: {summary['pass_words']} 个")
    print(f"  观察词: {summary['watch_words']} 个")
    print(f"  待验证: {summary['pending_words']} 个")
    print(f"  新素材: {summary['new_materials']} 条")

    print(f"\n📊 词库: {stats['total_words']}词 "
          f"(通过{stats['pass_count']} 观察{stats['watch_count']} "
          f"待验证{stats['pending_count']})")
    print(f"  标题素材库: {stats['total_materials']} 条")

    # 打印每个词的详细结果
    print("\n" + "-" * 70)
    for r in fb_result["results"]:
        kw = r["parent_keyword"]
        method = r["method"]
        n_candidates = r["candidates_extracted"]

        sw = r["search_words"]
        pass_words = [w for w in sw if w.get("status") == "pass"]
        watch_words = [w for w in sw if w.get("status") == "watch"]
        pending_words = [w for w in sw if w.get("status") == "pending_verify"]
        discard_words = [w for w in sw if w.get("status") == "discard"]

        print(f"\n【{kw}】方法={method}, 候选词={n_candidates}")
        if pass_words:
            print(f"  ✅ 通过: {[(w['word'], round(w.get('composite',0),1)) for w in pass_words]}")
        if watch_words:
            print(f"  👀 观察: {[(w['word'], round(w.get('composite',0),1)) for w in watch_words]}")
        if pending_words:
            print(f"  ⏳ 待验证: {[w['word'] for w in pending_words]}")
        if discard_words:
            print(f"  ❌ 淘汰: {[w['word'] for w in discard_words]}")

        # 显示标题素材
        tm = r.get("title_materials", {})
        for cat, items in tm.items():
            if items:
                texts = [it.get("text", "") for it in items[:5]]
                print(f"  📝 {cat}: {texts}")

        note = r.get("analysis_note", "")
        if note:
            print(f"  💡 {note}")

    # ═══ Phase C: 验证待定词 ═══
    pending = engine.get_pending_words()
    if pending:
        print("\n" + "=" * 70)
        print(f"Phase C: 验证 {len(pending)} 个待定词")
        print("=" * 70)
        print(f"待验证词: {pending}")
        print("(需要设备在线做轻量搜索验证，当前跳过)")
    else:
        print(f"\n✅ 无待验证词，跳过 Phase C")

    # ═══ 标题素材库 ═══
    print("\n" + "=" * 70)
    print("标题素材库")
    print("=" * 70)
    materials = engine.export_title_materials()
    for cat, items in materials.items():
        if items:
            print(f"\n  {cat} ({len(items)}条):")
            for it in items[:5]:
                print(f"    - {it.get('text','')}")

    # 测试标题生成
    print("\n" + "-" * 70)
    generated = engine.generate_title()
    print(f"AI 推荐标题片段: {generated}")

    # ═══ 最终统计 ═══
    print("\n" + "=" * 70)
    print("最终统计")
    print("=" * 70)
    lib_stats = engine.word_lib.stats()
    pass_words = engine.word_lib.get_pass_words()
    all_words = engine.word_lib.get_all_words()

    print(f"词库总量: {lib_stats['total_words']} 词")
    print(f"  正式入队 (pass): {lib_stats['pass_count']} → {pass_words}")
    print(f"  观察池 (watch): {lib_stats['watch_count']}")
    print(f"  待验证 (pending): {lib_stats['pending_count']}")
    print(f"标题素材: {lib_stats['total_materials']} 条")
    print(f"数据文件: collected_data/word_library.json")
    print(f"飞轮结果: collected_data/flywheel_results.json")

    # 显示词库详情
    print(f"\n词库列表: {all_words}")
    for w in all_words:
        info = engine.word_lib.data["words"].get(w, {})
        print(f"  {w:20s} status={info.get('status'):12s} "
              f"composite={info.get('composite',0):.1f} "
              f"added={info.get('added_at','?')}")

    print("\n✅ 端到端飞轮测试完成")


if __name__ == "__main__":
    main()
