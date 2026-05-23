"""
测试搜索词飞轮 — 阶段B AI分析
用真实结构的样本数据，调智谱AI，对比有无AI的效果
"""

import json, sys, os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from engines.ai_client import AIClient

# ═══════════════════════════════════════════════════════════════
# 样本数据：模拟搜索"自行车"3页60条的结果
# 结构完全对标真实闲鱼搜索返回
# ═══════════════════════════════════════════════════════════════

SAMPLE_PARENT_SEARCH = {
    "自行车": {
        "keyword": "自行车",
        "pages": 3,
        "numFound": 23000,
        "summary": {
            "total_items": 60,
            "items_with_soldCount": 35,
            "soldCount_distribution": {"1-10": 18, "10-50": 10, "50-100": 5, "100+": 2},
            "avg_price": 1250,
            "avg_wantNum": 8.3,
            "professional_seller_ratio": 0.45,
            "top_service_tags": ["包邮(42)", "已售(35)", "百分百好评(12)", "信用极好(8)"],
        },
        # 候选词在父级搜索结果中出现的数据
        "candidate_signals": [
            {
                "word": "瓜车",
                "appeared_in": 4,
                "items_with_sold": 3,
                "avg_soldCount": 45,
                "avg_price": 3850,
                "avg_wantNum": 6.3,
                "has_professional": True,
                "sample_titles": [
                    "瓜车gravel砾石公路车 铝合金车架 油碟 自用闲置",
                    "全新组装瓜车 gravel碳纤维前叉 禧玛诺套件",
                    "瓜车砾石公路车 27.5寸 仅骑行200km 99新",
                ],
            },
            {
                "word": "捷安特ATX830",
                "appeared_in": 9,
                "items_with_sold": 7,
                "avg_soldCount": 120,
                "avg_price": 2200,
                "avg_wantNum": 12.5,
                "has_professional": True,
                "sample_titles": [
                    "捷安特ATX830 27速铝合金山地车 油碟刹车 九成新",
                    "自用捷安特ATX830 禧玛诺变速 26寸 包邮 可小刀",
                    "全新捷安特ATX830 山地自行车 支持验货 实体店同款",
                ],
            },
            {
                "word": "27速山地车",
                "appeared_in": 12,
                "items_with_sold": 8,
                "avg_soldCount": 85,
                "avg_price": 1800,
                "avg_wantNum": 10.1,
                "has_professional": True,
                "sample_titles": [
                    "27速山地车 铝合金车架 油碟 自用",
                    "全新27速山地自行车 双碟刹 包邮",
                ],
            },
            {
                "word": "死飞倒刹",
                "appeared_in": 0,
                "items_with_sold": 0,
                "has_any_signal": False,
            },
            {
                "word": "凤凰26寸",
                "appeared_in": 4,
                "items_with_sold": 2,
                "avg_soldCount": 25,
                "avg_price": 580,
                "avg_wantNum": 5.2,
                "has_professional": False,
                "sample_titles": [
                    "凤凰26寸自行车 复古款 自用闲置 搬家出",
                    "凤凰26寸城市车 女士款 全新未拆封 包邮",
                ],
            },
            {
                "word": "铝合金车架",
                "appeared_in": 18,
                "items_with_sold": 10,
                "avg_soldCount": 55,
                "avg_price": 1500,
                "avg_wantNum": 9.0,
                "has_professional": True,
            },
            {
                "word": "闲置自用",
                "appeared_in": 15,
                "items_with_sold": 5,
                "type": "seller_state",
            },
            {
                "word": "99新",
                "appeared_in": 8,
                "items_with_sold": 4,
                "type": "condition",
            },
            {
                "word": "包邮",
                "appeared_in": 42,
                "items_with_sold": 28,
                "type": "transaction",
            },
            {
                "word": "年会奖品",
                "appeared_in": 2,
                "items_with_sold": 1,
                "type": "seller_state",
            },
            {
                "word": "好物推荐",
                "appeared_in": 1,
                "items_with_sold": 0,
                "type": "marketing",
            },
        ],
    }
}

EXISTING_WORD_LIBRARY = [
    "自行车", "山地车", "公路车", "折叠车", "电动车",
    "捷安特ATX830", "美利达公爵600", "喜德盛传奇500",
    "27速山地车", "26寸自行车", "铝合金车架",
    "电饭煲", "美的电饭煲", "苏泊尔电饭煲",
    "机械键盘", "Cherry键盘", "红轴键盘",
]


# ═══════════════════════════════════════════════════════════════
# Phase B Prompt — AI 分析候选词
# ═══════════════════════════════════════════════════════════════

PHASE_B_SYSTEM = """你是闲鱼选品关键词分析师。你的任务是从搜索结果标题中提取的候选词进行分流和评分。

核心原则：一切评分基于数据，绝不凭常识猜测。数据不足就标记，不强打分数。

评分标准请严格遵循用户消息中的规范。输出必须为合法JSON。"""


def build_phase_b_prompt(parent_data: dict, candidates: list, existing_library: list) -> str:
    """构建阶段B的完整Prompt"""
    # 只取关键数据，减少token
    summary = parent_data["summary"]
    signals = parent_data["candidate_signals"]

    # 构建精简的信号数据
    signal_lines = []
    for s in signals:
        lines = [f"\n  【{s['word']}】"]
        if s.get("type"):
            lines.append(f"    类型: {s['type']}")
        else:
            lines.append(f"    出现: {s.get('appeared_in', 0)}次/60条 | "
                         f"有售出: {s.get('items_with_sold', 0)}条 | "
                         f"均价: ¥{s.get('avg_price', '?')} | "
                         f"平均want: {s.get('avg_wantNum', '?')}")
            if s.get("has_professional"):
                lines.append(f"    专业卖家: 是")
            if s.get("sample_titles"):
                lines.append(f"    标题样本: {s['sample_titles'][0][:80]}")
        signal_lines.extend(lines)

    return f"""
══════════════════════════════════════
父级搜索数据："{parent_data['keyword']}" — {parent_data['pages']}页{parent_data['summary']['total_items']}条
══════════════════════════════════════

整体统计：
  有售出记录: {summary['items_with_soldCount']}/{summary['total_items']} ({summary['items_with_soldCount']/summary['total_items']*100:.0f}%)
  均价: ¥{summary['avg_price']}
  平均want: {summary['avg_wantNum']}
  专业卖家比例: {summary['professional_seller_ratio']:.0%}
  高频服务标签: {', '.join(summary['top_service_tags'])}

候选人信号数据：
{chr(10).join(signal_lines)}

══════════════════════════════════════
已有词库：
{', '.join(existing_library[:20])}
...共{len(existing_library)}个词

══════════════════════════════════════
任务：对每个候选词做分流+评分

步骤1 — 分流为三种类型：
  "search_word" = 能定位到商品的品类/品牌/型号词 → 步骤2评分
  "title_boost" = 不能搜索但能提升标题点击率（卖家状态/成色/交易/信任词）→ 步骤3归类
  "noise" = 完全无价值 → 淘汰

步骤2 — 对search_word评分（0-5分，数据不足标记signal_insufficient）：
  商品指向性：品牌+型号+参数=5，品牌+品类=3，纯品类=1
  需求信号：出现≥3且售出率>50%=5分，出现0次=标记signal_insufficient
  竞争预估：出现率<10%且有售出=5分(蓝海)，>50%=2分
  货源可得性：品牌+型号=5，含"自组"=1
  利润空间：均价>2000=5，300-800=3，<100=1。品类修正：数码-1，品牌耐用品+1
  词库边际贡献：全新品类=5，近义词重叠>70%=1

  综合分 = 指向性×1.5 + 需求×1.5 + 竞争×2.0 + 货源×1.5 + 利润×1.5 + 边际×1.5
  composite≥7.5且信号足够→pass, 信号不足→pending_verify, 5.5-7.4→watch, <5.5→discard

步骤3 — 对title_boost归类：
  真实感：闲置/自用/搬家出/年会奖品/退坑
  成色：99新/几乎全新/仅拆封/未使用/九成新
  信任：正品保证/支持验货/品牌授权/假一赔十
  交易：包邮/可小刀/同城面交/信用极好
  紧迫：急出/最后一天/马上搬家/清仓价

  标注每个素材的适用品类、使用限制、冲突规则

输出JSON（不要markdown代码块，直接输出JSON）：
{{
  "search_words": [
    {{
      "word": "瓜车",
      "merged_with": [],
      "scores": {{
        "specificity": 4, "demand_signal": 4, "competition": 5,
        "supply_access": 3, "profit_potential": 4, "marginal_value": 4
      }},
      "evidence": "出现4次/60条，3条有售出(75%售出率)，均价¥3850",
      "composite": 8.0, "status": "pass"
    }}
  ],
  "title_materials": {{
    "真实感": [{{"text": "...", "usage": "...", "conflict_with": ["..."]}}],
    "成色": [], "信任": [], "交易": [], "紧迫": []
  }},
  "noise": [{{"text": "...", "reason": "..."}}],
  "category_overlaps": [{{"words": [], "merged_to": "", "overlap_pct": 0}}],
  "analysis_note": "一句话总结本轮最重要的发现"
}}"""


# ═══════════════════════════════════════════════════════════════
# 无AI对比：纯规则评分（baseline）
# ═══════════════════════════════════════════════════════════════

def baseline_rule_score(candidate: dict) -> dict:
    """纯规则评分，对照AI效果"""
    word = candidate["word"]
    appeared = candidate.get("appeared_in", 0)
    sold = candidate.get("items_with_sold", 0)
    price = candidate.get("avg_price", 0)
    has_pro = candidate.get("has_professional", False)
    ctype = candidate.get("type", "")

    # 类型分流
    if ctype in ("seller_state",):
        return {"word": word, "type": "title_boost", "category": "真实感",
                "method": "rule", "reason": f"卖家状态词: {ctype}"}
    if ctype == "condition":
        return {"word": word, "type": "title_boost", "category": "成色",
                "method": "rule", "reason": f"成色描述词: {ctype}"}
    if ctype == "transaction":
        return {"word": word, "type": "title_boost", "category": "交易",
                "method": "rule", "reason": f"交易条件词: {ctype}"}
    if ctype == "marketing":
        return {"word": word, "type": "noise", "method": "rule",
                "reason": "营销用语,无商品信息"}
    if appeared == 0:
        return {"word": word, "type": "unknown", "method": "rule",
                "reason": "无数据信号,规则无法判断"}

    # 商品指向性：简单规则
    has_brand = any(b in word for b in ["捷安特","美利达","喜德盛","凤凰","永久",
                    "华为","小米","美的","苏泊尔","Cherry","Filco"])
    has_model = any(c.isdigit() for c in word) and len(word) > 3
    has_param = any(p in word for p in ["速","寸","L","ml","mm","cm","斤","kg","铝合金","碳纤维"])

    if has_brand and has_model:
        specificity = 5
    elif has_brand or (has_model and has_param):
        specificity = 3
    elif has_param:
        specificity = 2
    else:
        specificity = 1

    # 需求信号
    if appeared >= 3 and sold / appeared >= 0.5:
        demand = 5
    elif appeared >= 3 and sold / appeared >= 0.3:
        demand = 4
    elif appeared >= 1 and sold > 0:
        demand = 3
    elif appeared >= 1:
        demand = 2
    else:
        demand = 0  # 数据不足

    # 竞争预估
    appear_rate = appeared / 60
    if appear_rate < 0.1 and sold > 0:
        competition = 5
    elif appear_rate < 0.25:
        competition = 4
    elif appear_rate < 0.5:
        competition = 3
    else:
        competition = 2

    # 货源可得性
    if has_brand and has_model:
        supply = 5
    elif has_brand:
        supply = 4
    elif has_param:
        supply = 3
    else:
        supply = 2
    if has_pro:
        supply = min(5, supply + 1)
    if "自组" in word or "手作" in word or "定制" in word:
        supply = 1

    # 利润空间
    if price > 2000:      profit = 5
    elif price > 800:     profit = 4
    elif price > 300:     profit = 3
    elif price > 100:     profit = 2
    elif price > 0:       profit = 1
    else:                 profit = 3

    # 词库边际贡献
    exact_match = any(w == word for w in EXISTING_WORD_LIBRARY)
    similar = any(w in word or word in w for w in EXISTING_WORD_LIBRARY
                  if len(word) > 2 and len(w) > 2)
    if exact_match:       marginal = 0
    elif similar:         marginal = 2
    else:                 marginal = 4

    composite_raw = (specificity * 1.5 + demand * 1.5 + competition * 2.0 +
                 supply * 1.5 + profit * 1.5 + marginal * 1.5)
    # 归一化到0-10: 满分 = 5×(1.5+1.5+2.0+1.5+1.5+1.5) = 5×9.0 = 45
    composite = round(composite_raw / 45 * 10, 1)

    if composite >= 7.5:
        status = "pass"
    elif composite >= 5.5:
        status = "watch"
    else:
        status = "discard"

    return {
        "word": word, "type": "search_word", "method": "rule",
        "scores": {"specificity": specificity, "demand_signal": demand,
                   "competition": competition, "supply_access": supply,
                   "profit_potential": profit, "marginal_value": marginal},
        "composite": round(composite, 1), "status": status,
        "evidence": f"出现{appeared}次, 售出{sold}条, 均价¥{price}"
    }


# ═══════════════════════════════════════════════════════════════
# 主测试流程
# ═══════════════════════════════════════════════════════════════

def main():
    api_key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("ZHIPU_API_KEY")

    if not api_key:
        # Try settings.json
        settings_path = Path(__file__).parent / "settings.json"
        if settings_path.exists():
            with open(settings_path) as f:
                settings = json.load(f)
            api_key = settings.get("api", {}).get("deepseek_api_key", "")

    # 先跑规则baseline
    print("=" * 70)
    print("阶段1: 纯规则评分 (Baseline)")
    print("=" * 70)

    parent = SAMPLE_PARENT_SEARCH["自行车"]
    candidates = parent["candidate_signals"]

    rule_results = [baseline_rule_score(c) for c in candidates]

    print(f"\n{'词':20s} {'类型':12s} {'综合分':>6s} {'状态':10s} {'方法'}")
    print("-" * 60)
    for r in rule_results:
        print(f"{r['word']:20s} {r.get('type','?'):12s} "
              f"{r.get('composite',0):6.1f} {r.get('status','?'):10s} {r.get('method','?')}")
        if r.get("type") == "search_word":
            s = r.get("scores", {})
            print(f"  维度: 指向性={s.get('specificity')} 需求={s.get('demand_signal')} "
                  f"竞争={s.get('competition')} 货源={s.get('supply_access')} "
                  f"利润={s.get('profit_potential')} 边际={s.get('marginal_value')}")

    # 规则发现的title_boost
    print(f"\n规则识别title_boost: {[r['word'] for r in rule_results if r.get('type')=='title_boost']}")
    print(f"规则识别noise: {[r['word'] for r in rule_results if r.get('type')=='noise']}")
    print(f"规则无法判断: {[r['word'] for r in rule_results if r.get('type')=='unknown']}")

    # 如果有API key，跑AI对比
    if api_key:
        print("\n" + "=" * 70)
        print("阶段2: AI (DeepSeek) 评分对比")
        print("=" * 70)

        # 选择provider — 默认 deepseek
        provider = "deepseek"
        if os.environ.get("ZHIPU_API_KEY"):
            provider = "zhipu"
            api_key = os.environ["ZHIPU_API_KEY"]

        print(f"使用: {provider} / API key: {api_key[:8]}...")
        client = AIClient(api_key, provider=provider)

        prompt = build_phase_b_prompt(parent, candidates, EXISTING_WORD_LIBRARY)

        try:
            print("调用AI中...")
            ai_result = client.chat_json(PHASE_B_SYSTEM, prompt)

            print("\nAI分析结果：")
            print("-" * 60)

            # search_words
            sw = ai_result.get("search_words", [])
            print(f"\nsearch_words ({len(sw)}个):")
            print(f"{'词':20s} {'综合分':>6s} {'状态':16s} {'证据'}")
            print("-" * 80)
            for w in sw:
                evidence = w.get("evidence", "")[:60]
                print(f"{w['word']:20s} {w.get('composite',0):6.1f} "
                      f"{w.get('status','?'):16s} {evidence}")

            # title_materials
            tm = ai_result.get("title_materials", {})
            total_materials = sum(len(v) for v in tm.values())
            print(f"\ntitle_materials ({total_materials}个新素材):")
            for cat, items in tm.items():
                if items:
                    print(f"  {cat}: {[i.get('text','') for i in items]}")

            # noise
            noise = ai_result.get("noise", [])
            if noise:
                print(f"\nnoise ({len(noise)}个淘汰):")
                for n in noise:
                    print(f"  ✗ {n.get('text','')}: {n.get('reason','')}")

            # overlaps
            overlaps = ai_result.get("category_overlaps", [])
            if overlaps:
                print(f"\n品类重叠合并 ({len(overlaps)}组):")
                for o in overlaps:
                    print(f"  {o.get('words',[])} → {o.get('merged_to','')}")

            # AI note
            note = ai_result.get("analysis_note", "")
            if note:
                print(f"\n📝 AI观察: {note}")

            # ── 对比AI vs 规则 ──
            print("\n" + "=" * 70)
            print("AI vs 规则 对比")
            print("=" * 70)

            ai_words = {w["word"]: w for w in sw}
            rule_words = {r["word"]: r for r in rule_results}

            all_words = set(list(ai_words.keys()) + list(rule_words.keys())
                            + [c["word"] for c in candidates])

            print(f"\n{'词':20s} {'规则':>8s} {'AI':>8s} {'差异'}")
            print("-" * 50)

            ai_pass = []
            rule_pass = []
            for word in sorted(all_words):
                r = rule_words.get(word, {})
                a = ai_words.get(word, {})

                r_score = r.get("composite", "-")
                a_score = a.get("composite", "-")
                r_status = r.get("status", r.get("type", "-"))
                a_status = a.get("status", "-")

                if isinstance(r_score, (int, float)) and isinstance(a_score, (int, float)):
                    diff = a_score - r_score
                    diff_str = f"{diff:+.1f}"
                else:
                    diff_str = "-"

                print(f"{word:20s} {str(r_score):>8s} {str(a_score):>8s} {diff_str:>8s}")

                if a_status == "pass":
                    ai_pass.append(word)
                if r_status == "pass":
                    rule_pass.append(word)

            print(f"\nAI通过: {ai_pass}")
            print(f"规则通过: {rule_pass}")
            print(f"AI额外发现: {set(ai_pass) - set(rule_pass)}")
            print(f"规则通过但AI否定: {set(rule_pass) - set(ai_pass)}")

            # AI发现规则没发现的title_boost
            ai_boost = set()
            for cat, items in tm.items():
                for item in items:
                    ai_boost.add(item.get("text", ""))
            rule_boost = set(r["word"] for r in rule_results if r.get("type") == "title_boost")
            print(f"\nAI发现的title_boost: {ai_boost - rule_boost}")
            print(f"AI规则一致: {ai_boost & rule_boost}")

        except Exception as e:
            print(f"\nAI调用失败: {e}")
            import traceback
            traceback.print_exc()

    else:
        print("\n" + "=" * 70)
        print("未配置API Key。设置方式：")
        print("  1. 编辑 settings.json，填入 deepseek_api_key")
        print("  2. 或 export ZHIPU_API_KEY=your_key")
        print("  3. 智谱API申请: https://open.bigmodel.cn/")
        print("=" * 70)

        print("\n提供了API Key后，重新运行：python test_flywheel.py")
        print("将自动对比AI评分 vs 规则评分的效果差异。")


if __name__ == "__main__":
    main()
