"""
全优化点综合测试 — 100种子词 + 角色③-1/③-2 全覆盖
"""
import json
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))

from engines.flywheel_engine import (
    FlywheelEngine, extract_candidates_with_ai, extract_candidates_from_titles,
    WordLibrary
)
from engines.ai_client import AIClient


def test_01_seed_keywords():
    """测试1: 种子词库加载"""
    print("=" * 70)
    print("测试1: 种子词库 (100词 × 12品类)")
    print("=" * 70)

    seed_path = Path(__file__).parent / "engines" / "seed_keywords.json"
    with open(seed_path) as f:
        seeds = json.load(f)

    cats = seeds["categories"]
    flat = seeds["flat_keywords"]

    print(f"品类数: {len(cats)}")
    print(f"总词数: {len(flat)}")
    for name, cat in cats.items():
        kws = cat["keywords"]
        print(f"  {name:12s} ({len(kws)}词): {', '.join(kws[:3])}...")
    return len(flat) == 100


def test_02_ai_extraction(client, data):
    """测试2: AI 分词提取 vs jieba"""
    print("\n" + "=" * 70)
    print("测试2: AI 分词提取 vs jieba")
    print("=" * 70)

    titles_all = []
    stats_all = []
    for kw, val in data.items():
        for it in val.get("items", []):
            titles_all.append(it.get("title", ""))
            stats_all.append({
                "soldCount": it.get("soldCount", 0),
                "price": it.get("price", ""),
                "wantNum": it.get("wantNum", 0),
                "serviceTags": it.get("serviceTags", []),
            })

    # jieba
    c_jieba, _ = extract_candidates_from_titles(titles_all, stats_all, 30)
    # AI
    c_ai, _ = extract_candidates_with_ai(client, titles_all, stats_all, 25)

    print(f"\njieba 提取: {len(c_jieba)} 候选词")
    print(f"  Top10: {[c['word'] for c in c_jieba[:10]]}")

    print(f"\nAI 提取: {len(c_ai)} 候选词")
    ai_sw = [c for c in c_ai if c.get("type") == "search_word"]
    ai_tb = [c for c in c_ai if c.get("type") == "title_boost"]
    ai_no = [c for c in c_ai if c.get("type") == "noise"]
    print(f"  search_word: {len(ai_sw)} — {[c.get('word','?') for c in ai_sw[:10]]}")
    print(f"  title_boost: {len(ai_tb)} — {[c.get('word','?') for c in ai_tb[:10]]}")
    print(f"  noise: {len(ai_no)} — {[c.get('word','?') for c in ai_no[:5]]}")

    # 质量评估: AI应该不包含噪声字符
    noise_chars = ["！！", "||", "｜", "★"]
    jieba_noise = [c for c in c_jieba if any(n in c["word"] for n in noise_chars)]
    ai_noise = [c for c in c_ai if any(n in c.get("word", "") for n in noise_chars)]
    print(f"\n质量对比:")
    print(f"  jieba含噪声: {len(jieba_noise)}个 — {[c['word'] for c in jieba_noise[:5]]}")
    print(f"  AI含噪声: {len(ai_noise)}个")

    return len(ai_sw) > 0 and len(ai_noise) == 0


def test_03_phase_b_full(engine, data):
    """测试3: 完整 Phase B 流程"""
    print("\n" + "=" * 70)
    print("测试3: Phase B 批量打分 (3关键词)")
    print("=" * 70)

    keyword_data = []
    for kw, val in data.items():
        items = val.get("items", [])
        if items:
            keyword_data.append({
                "keyword": kw,
                "search_items": items,
                "numFound": val.get("numFound", len(items)),
            })

    result = engine.run_phase_b_batch(keyword_data)
    s = result["summary"]
    stats = result["word_library_stats"]

    print(f"处理关键词: {s['keywords_processed']} 个")
    print(f"pass: {s['pass_words']}, watch: {s['watch_words']}, pending: {s['pending_words']}")
    print(f"新素材: {s['new_materials']} 条")
    print(f"词库总量: {stats['total_words']}词 (通过{stats['pass_count']} 观察{stats['watch_count']} 待验证{stats['pending_count']})")
    print(f"素材库总量: {stats['total_materials']} 条")

    # 打印每个关键词的详情
    for r in result["results"]:
        kw = r["parent_keyword"]
        sw = r["search_words"]
        tm = r.get("title_materials", {})
        n_tm = sum(len(v) for v in tm.values())
        pass_w = [w["word"] for w in sw if w.get("status") == "pass"]
        pend_w = [w["word"] for w in sw if w.get("status") == "pending_verify"]
        watch_w = [w["word"] for w in sw if w.get("status") == "watch"]
        discard_w = [w["word"] for w in sw if w.get("status") == "discard"]

        print(f"\n  【{kw}】提取={r['extraction']}, 方法={r['method']}")
        if pass_w: print(f"    ✅ pass: {pass_w}")
        if watch_w: print(f"    👀 watch: {watch_w}")
        if pend_w: print(f"    ⏳ pending: {pend_w}")
        if discard_w: print(f"    ❌ discard: {discard_w[:5]}..." if len(discard_w) > 5 else f"    ❌ discard: {discard_w}")
        if n_tm: print(f"    📝 素材: {n_tm}条 ({', '.join(f'{k}({len(v)})' for k,v in tm.items() if v)})")
        note = r.get("analysis_note", "")
        if note: print(f"    💡 {note[:100]}")

    return result


def test_04_title_materials(engine):
    """测试4: 标题素材库"""
    print("\n" + "=" * 70)
    print("测试4: 标题素材库")
    print("=" * 70)

    materials = engine.export_title_materials()
    total = 0
    for cat in ["真实感", "成色", "信任", "交易", "紧迫"]:
        items = materials.get(cat, [])
        total += len(items)
        print(f"  {cat} ({len(items)}条): {[i.get('text','') for i in items[:8]]}")

    print(f"\n  总计: {total} 条素材")

    # 测试标题生成
    print(f"\n  AI 推荐标题片段:")
    for _ in range(3):
        gen = engine.generate_title()
        print(f"    → {gen}")

    return total > 0


def test_05_word_library(engine):
    """测试5: 词库持久化"""
    print("\n" + "=" * 70)
    print("测试5: 词库管理")
    print("=" * 70)

    stats = engine.word_lib.stats()
    pass_words = engine.word_lib.get_pass_words()
    pending_words = engine.word_lib.get_pending_words()

    print(f"  总词数: {stats['total_words']}")
    print(f"  通过 ({stats['pass_count']}): {pass_words}")
    print(f"  观察: {stats['watch_count']}")
    print(f"  待验证 ({stats['pending_count']}): {pending_words[:10]}...")

    # 验证持久化
    lib_path = Path(__file__).parent / "collected_data" / "word_library.json"
    exists = lib_path.exists()
    size = lib_path.stat().st_size if exists else 0
    print(f"  持久化文件: {lib_path} ({size:,} bytes)")

    return exists and size > 0


def test_06_optimization_coverage():
    """测试6: 优化点覆盖检查"""
    print("\n" + "=" * 70)
    print("测试6: 优化点覆盖")
    print("=" * 70)

    checklist = {
        # 角色③-1: 搜索词飞轮
        "③-1a AI候选词提取": True,
        "③-1b 五维评分(specificity/demand/competition/supply/profit)": True,
        "③-1c 分流(pass/watch/pending/discard)": True,
        "③-1d 词库持久化+去重": True,
        "③-1e Phase C 验证搜索": True,
        "③-1f AI评分降级规则基线": True,
        # 角色③-2: 标题素材库
        "③-2a 5类素材归类(真实感/成色/信任/交易/紧迫)": True,
        "③-2b 素材去重": True,
        "③-2c 标题自动生成": True,
        # 角色③-3: PDD搜索词优化
        "③-3a AI提取核心产品词": False,
        "③-3b 4种策略PDD变体": False,
        "③-3c 变体评估择优": False,
        # 角色②: 品类评分进化
        "②-1 预测vs实际数据积累": False,
        "②-2 AI维度相关性分析": False,
        "②-3 自动权重调整": False,
        # 控制机制
        "控制-1 品类重叠语义去重": False,
        "控制-2 偏见循环检测": False,
        "控制-3 队列上限自动降级": False,
        "控制-4 30天无产出过期淘汰": False,
    }

    done = sum(1 for v in checklist.values() if v)
    total = len(checklist)
    for name, status in checklist.items():
        icon = "✅" if status else "❌"
        print(f"  {icon} {name}")

    print(f"\n完成度: {done}/{total} ({done/total*100:.0f}%)")
    return done, total


def main():
    # 加载配置
    settings_path = Path(__file__).parent / "settings.json"
    with open(settings_path) as f:
        settings = json.load(f)

    # 加载数据
    data_path = Path(__file__).parent / "collected_data" / "phase0_raw_data.json"
    with open(data_path) as f:
        data = json.load(f)

    api_key = settings["api"]["deepseek_api_key"]

    results = {}

    # 测试1: 种子词库
    results["01_种子词库"] = test_01_seed_keywords()

    # 初始化
    client = AIClient(api_key, provider="deepseek")
    engine = FlywheelEngine(settings, output_dir=data_path.parent)

    # 测试2: AI提取
    results["02_AI分词提取"] = test_02_ai_extraction(client, data)

    # 测试3: Phase B
    results["03_PhaseB批量"] = bool(test_03_phase_b_full(engine, data))

    # 测试4: 素材库
    results["04_标题素材"] = test_04_title_materials(engine)

    # 测试5: 词库
    results["05_词库管理"] = test_05_word_library(engine)

    # 测试6: 覆盖检查
    done, total = test_06_optimization_coverage()

    # 汇总
    print("\n" + "=" * 70)
    print("综合测试汇总")
    print("=" * 70)
    passed = sum(1 for v in results.values() if v)
    for name, ok in results.items():
        print(f"  {'✅' if ok else '❌'} {name}")
    print(f"\n通过: {passed}/{len(results)} 项")
    print(f"优化点完成度: {done}/{total}")
    print(f"\n{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 测试完成")


if __name__ == "__main__":
    main()
