"""
飞轮引擎 — Phase B (AI提取) + Phase C (验证搜索)

从采集到的搜索标题中提取候选词 → AI 五维评分 → 入词库
噪声词不丢弃，按场景归类为标题素材
"""
from __future__ import annotations

import json
import os
import random
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import jieba

from engines.ai_client import AIClient

# ── 中文停用词 / 噪声词 ──────────────────────────────────────────
STOP_WORDS = {
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一",
    "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着",
    "没有", "看", "好", "自己", "这", "他", "她", "它", "们", "那", "些",
    "吗", "吧", "啊", "呢", "哦", "嗯", "哈", "啦", "呀", "嘛", "噢",
    # jieba 分词常见噪声
    "！！", "||", "｜", "【", "】", "《", "》", "★", "●", "◆", "○",
    "→", "👉", "✅", "❌", "⭐", "🔥", "💯",
    "不包", "不刀", "不退", "不换", "不议",
    "可", "用", "还", "能", "想", "让", "给", "把", "被", "从", "对",
    "为", "以", "及", "与", "或", "但", "而", "且", "因", "所", "其",
    "这个", "那个", "哪个", "什么", "怎么", "怎样", "为什么", "多少",
    "没", "过", "太", "最", "更", "只", "都", "才", "又", "再", "已",
    "1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "10",
    "99", "100", "200", "300", "500", "1000",
    "一个", "一套", "一件", "一台", "一只", "一条", "一双", "一对",
    "一下", "一些", "一些些", "一点", "一点点",
    "+", "-", "/", "\\", "|", "@", "#", "$", "%", "^", "&", "*",
    "(", ")", "[", "]", "{", "}", "<", ">",
    "【", "】", "《", "》", "「", "」", "『", "』",
    "，", "。", "！", "？", "、", "：", "；", "“", "”", "…",
    "~", "`", "!", "?", ",", ".", ":", ";", "\"", "'",
    "x", "X", "xl", "XXL", "M", "L", "S",
}

# 已知噪声模式 — 卖家状态 / 成色 / 交易 / 营销
NOISE_PATTERNS = [
    re.compile(p) for p in [
        r"闲置$", r"自用$", r"个人$", r"转让$", r"二手$",
        r"包邮$", r"可小刀$", r"可刀$", r"面交$", r"同城$",
        r"九成新$", r"九五新$", r"九九新$", r"99新$", r"全新$",
        r"仅拆封$", r"未使用$", r"几乎全新$", r"刚买$",
        r"正品$", r"正品保证$", r"支持验货$", r"假一赔十$",
        r"急出$", r"清仓$", r"搬家$", r"年会奖品$", r"抽奖$",
        r"退坑$", r"不玩了$", r"用不上$", r"太多$",
        r"好物推荐$", r"推荐$", r"种草$", r"必备$", r"神器$",
        r"实体店$", r"同款$", r"专柜$", r"代购$",
        r"信用极好$", r"百分百好评$", r"已售\d+$",
    ]
]

# ── Phase B System Prompt ────────────────────────────────────────
PHASE_B_SYSTEM = """你是闲鱼选品关键词分析师。你的任务是从搜索结果标题中提取的候选词进行分流和评分。

核心原则：一切评分基于数据，绝不凭常识猜测。数据不足就标记，不强打分数。
评分标准请严格遵循用户消息中的规范。输出必须为合法JSON。"""


def build_phase_b_prompt(parent_data: dict, candidates: list, existing_library: list) -> str:
    """构建阶段B的完整Prompt，精简数据结构减少token消耗"""
    summary = parent_data["summary"]
    signals = parent_data["candidate_signals"]

    signal_lines = []
    for s in signals:
        lines = [f"\n  【{s['word']}】"]
        if s.get("type"):
            lines.append(f"    类型: {s['type']}")
        else:
            lines.append(
                f"    出现: {s.get('appeared_in', 0)}次/{summary['total_items']}条 | "
                f"有售出: {s.get('items_with_sold', 0)}条 | "
                f"均价: ¥{s.get('avg_price', '?')} | "
                f"平均want: {s.get('avg_wantNum', '?')}"
            )
            if s.get("has_professional"):
                lines.append("    专业卖家: 是")
            if s.get("sample_titles"):
                t = s["sample_titles"][0][:80]
                lines.append(f"    标题样本: {t}")
        signal_lines.extend(lines)

    return f"""
══════════════════════════════════════
父级搜索数据："{parent_data['keyword']}" — {parent_data['pages']}页{summary['total_items']}条
══════════════════════════════════════

整体统计：
  有售出记录: {summary['items_with_soldCount']}/{summary['total_items']}
  均价: ¥{summary['avg_price']}
  平均want: {summary['avg_wantNum']}
  专业卖家比例: {summary['professional_seller_ratio']:.0%}
  高频服务标签: {', '.join(summary['top_service_tags'])}

候选人信号数据：
{chr(10).join(signal_lines)}

══════════════════════════════════════
已有词库：
{', '.join(existing_library[:30])}
...共{len(existing_library)}个词

══════════════════════════════════════
任务：对每个候选词做分流+评分

步骤1 — 分流为四种类型：
  "search_word" = 能定位到商品的品类/品牌/型号词 → 步骤2评分
  "category_seed" = 本身不能精准搜索，但能发现新品类方向（场景词/风格词/人群词/季节词/用途词）
                   标注 seed_for：列出2-3个可组合的具体品类名
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
  ★ 请根据这批标题的实际内容，自己发现和归纳素材类别（不限于下面列出的）。
  常见类别供参考：
  真实感：闲置/自用/搬家出/年会奖品/退坑
  成色：99新/几乎全新/仅拆封/未使用/九成新
  信任：正品保证/支持验货/品牌授权/假一赔十
  交易：包邮/可小刀/同城面交/信用极好
  紧迫：急出/最后一天/马上搬家/清仓价
  对比：原价XX现价XX/比官网便宜/专柜价XX
  场景：学生用/通勤/面试/旅行
  情感：前任送的/冲动消费/买错了/用不上
  服务：24小时发货/7天无理由/质保
	  渠道：厂家直发/批发价/外贸库存/专柜撤柜/工厂直销

  ⚠️ 不能用"其他"做类别名，每个词必须归入含义明确的类别（如：场景/渠道/成色/情感）。如果发现新类别，在 output 的 material_categories 字段中声明。
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
  "category_seeds": [
    {{"word": "学生党", "seed_for": ["蓝牙耳机","机械键盘"], "appeared_in": 8,
     "category_direction": "学生数码"}}
  ],
  "noise": [{{"text": "...", "reason": "..."}}],
  "category_overlaps": [{{"words": [], "merged_to": "", "overlap_pct": 0}}],
  "analysis_note": "一句话总结本轮最重要的发现"
"""


# ── AI 候选词提取 Prompt ─────────────────────────────────────────

EXTRACT_SYSTEM = """你是闲鱼商品标题分析专家。从给定的标题列表中提取候选搜索词和标题素材。

规则：
1. 提取可作为搜索词的商品相关词：品牌名、型号、品类细分、核心参数组合
2. ★ 品类扩展词（category_seed）：本身不能精准搜索商品，但能帮你发现新品类方向。
   例如：适用场景词（学生党/通勤/面试）、风格词（ins风/复古/极简）、
   用途词（送礼/考研/入门级）、人群词（儿童/女生/新手）、
   季节词（夏季/冬季/圣诞）、趋势词（最新款/全系列）
   这类词单独搜没用，但和品类组合就能打开新方向。
	   ★ seed_for 必须写2-3个具体品类名（如"机械键盘"而非"数码产品"），这直接影响飞轮能否跨品类拓展。
3. 不要提取完全无价值的词：纯卖家状态词（闲置/自用/搬家）、纯营销词（好物推荐/种草）
4. ★ title_boost需要归类。
   请根据这批标题的实际内容，自己归纳素材类别（不限于预设）。
   例如你可能发现：
   - 对比类：「原价XXX现价XXX」「比官网便宜」
   - 场景类：「适合学生」「通勤必备」
   - 情感类：「前任送的」「冲动消费」
   - 服务类：「24小时发货」「7天无理由」
   以及其他你发现的类别，不能用"其他"做类别名。类别名称用2-4个字。

输出JSON：
{
  "candidates": [
    {"word": "索尼1000XM4", "type": "search_word",
     "appeared_in": 5, "items_with_sold": 3,
     "avg_price": 1300, "sample_titles": ["索尼1000XM4..."]},
    {"word": "学生党", "type": "category_seed",
     "seed_for": ["蓝牙耳机","机械键盘"], "appeared_in": 8},
    {"word": "自用闲置", "type": "title_boost", "category": "真实感",
     "appeared_in": 8},
    {"word": "好物推荐", "type": "noise", "appeared_in": 1}
  ],
  "material_categories": [
    {"name": "真实感", "description": "增强买家信任的理由"},
    {"name": "对比", "description": "价格对比制造划算感"}
  ],
  "summary": {
    "total_items": 60, "items_with_soldCount": 10,
    "avg_price": 800, "avg_wantNum": 15.3,
    "top_tags": ["包邮(20)", "已售(15)", "信用极好(8)"]
  }
}"""


def extract_candidates_with_ai(
    client,
    titles: List[str],
    item_stats: List[dict] = None,
    max_candidates: int = 30,
) -> Tuple[List[dict], dict]:
    """
    使用 AI 从标题列表中提取候选词及统计。

    Args:
        client: AIClient 实例
        titles: 标题列表
        item_stats: 商品统计（soldCount, price, wantNum 等）
        max_candidates: 最多候选词数

    Returns:
        (candidates, summary) — 与 extract_candidates_from_titles 相同格式
    """
    if item_stats is None:
        item_stats = [{}] * len(titles)

    n_total = len(titles)

    # 构造精简 prompt（限制标题长度和数量，避免超时）
    MAX_TITLES = 30       # 最多30条标题
    MAX_TITLE_LEN = 40    # 每条标题截断到40字
    sample_titles = titles[:MAX_TITLES]
    sample_stats = item_stats[:MAX_TITLES] if item_stats else [{}] * len(sample_titles)

    title_lines = []
    for i, t in enumerate(sample_titles):
        sc = sample_stats[i].get("soldCount", 0) if i < len(sample_stats) else 0
        title_lines.append(f"  [{sc}售] {t[:MAX_TITLE_LEN]}")
    title_text = "\n".join(title_lines)

    # 统计 summary（基于全部数据，不只是样本）
    items_with_sold = sum(1 for s in item_stats if s.get("soldCount", 0) > 0)
    all_prices = []
    all_wants = []
    for s in item_stats:
        p = s.get("price", "")
        if p:
            try:
                all_prices.append(float(str(p).replace("¥", "").replace(",", "")))
            except (ValueError, TypeError):
                pass
        w = s.get("wantNum", s.get("wantCount", 0))
        if w:
            try:
                all_wants.append(int(w))
            except (ValueError, TypeError):
                pass

    tags_summary = _count_service_tags(item_stats)

    prompt = f"""从 {n_total} 条闲鱼搜索结果中抽样 {len(sample_titles)} 条标题，提取候选搜索词。

标题列表（[已售数] 标题，已截断）：
{title_text}

请提取最多 {max_candidates} 个候选词，按出现频率排序。返回JSON。"""

    try:
        result = client.chat_json(EXTRACT_SYSTEM, prompt)
    except Exception as e:
        print(f"[AI Extract] 调用失败: {e}")
        return [], {
            "total_items": n_total,
            "items_with_soldCount": items_with_sold,
            "avg_price": round(sum(all_prices) / max(len(all_prices), 1), 0),
            "avg_wantNum": round(sum(all_wants) / max(len(all_wants), 1), 1),
            "professional_seller_ratio": 0,
            "top_service_tags": tags_summary,
        }

    candidates = result.get("candidates", [])
    ai_summary = result.get("summary", {})

    # 补充 summary 数据
    summary = {
        "total_items": n_total,
        "items_with_soldCount": ai_summary.get("items_with_soldCount", items_with_sold),
        "avg_price": ai_summary.get("avg_price", round(sum(all_prices) / max(len(all_prices), 1), 0) if all_prices else 0),
        "avg_wantNum": ai_summary.get("avg_wantNum", round(sum(all_wants) / max(len(all_wants), 1), 1) if all_wants else 0),
        "professional_seller_ratio": ai_summary.get("professional_seller_ratio", 0),
        "top_service_tags": ai_summary.get("top_service_tags", tags_summary),
    }

    return candidates, summary


# ── 候选词提取器（jieba，不调 AI）───────────────────────────────

def _is_noise(word: str) -> Optional[str]:
    """检查是否命中噪声模式，返回噪声原因或None"""
    word_stripped = word.strip()
    if not word_stripped:
        return "空词"
    if len(word_stripped) <= 1:
        return "单字"
    if word_stripped in STOP_WORDS:
        return "停用词"
    if re.match(r'^\d+$', word_stripped):
        return "纯数字"
    if re.match(r'^[\d.]+$', word_stripped):
        return "纯数字/小数点"
    for pat in NOISE_PATTERNS:
        if pat.search(word_stripped):
            return f"噪声模式: {pat.pattern}"
    return None


def extract_candidates_from_titles(
    titles: List[str],
    item_stats: List[dict] = None,
    max_candidates: int = 30,
) -> Tuple[List[dict], dict]:
    """
    从标题列表中提取候选词及其统计信息。

    Args:
        titles: 搜索结果的标题列表
        item_stats: 每个标题对应的商品统计 [{soldCount, price, wantNum, sellerLevel}, ...]
        max_candidates: 最多返回的候选词数

    Returns:
        (candidate_signals, summary)
    """
    if item_stats is None:
        item_stats = [{}] * len(titles)

    n_total = len(titles)

    # Step 1: jieba 分词 + 提取 n-gram
    word_counter = Counter()
    word_items = defaultdict(list)  # word -> list of (idx, title)

    for idx, title in enumerate(titles):
        clean = title.strip()
        if not clean:
            continue

        # jieba 精确分词
        words = [w.strip() for w in jieba.cut(clean) if w.strip()]

        # 收集 unigrams, bigrams, trigrams
        for i, w in enumerate(words):
            if not _is_noise(w):
                word_counter[w] += 1
                word_items[w].append(idx)

            if i + 1 < len(words):
                bigram = words[i] + words[i + 1]
                if not _is_noise(bigram):
                    word_counter[bigram] += 1
                    word_items[bigram].append(idx)

            if i + 2 < len(words):
                trigram = words[i] + words[i + 1] + words[i + 2]
                if not _is_noise(trigram):
                    word_counter[trigram] += 1
                    word_items[trigram].append(idx)

    # Step 2: 选取 top-N 高频候选词（排除噪声）
    candidates = []
    for word, count in word_counter.most_common(max_candidates * 2):
        reason = _is_noise(word)
        if reason:
            continue
        if count < 2:  # 至少出现2次
            continue

        # 统计该候选词出现的商品
        indices = word_items[word]
        sold_count = 0
        prices = []
        want_nums = []
        has_pro = False
        sample_titles = []

        for i in indices[:10]:  # 最多采样10条
            sample_titles.append(titles[i])
            if i < len(item_stats):
                stat = item_stats[i]
                if stat.get("soldCount", 0) > 0:
                    sold_count += 1
                price_str = stat.get("price", "")
                if price_str:
                    try:
                        prices.append(float(str(price_str).replace("¥", "").replace(",", "")))
                    except (ValueError, TypeError):
                        pass
                want = stat.get("wantNum", stat.get("wantCount", 0))
                if want:
                    try:
                        want_nums.append(int(want))
                    except (ValueError, TypeError):
                        pass
                seller_level = stat.get("sellerLevel", "")
                if seller_level and "专家" in str(seller_level):
                    has_pro = True

        avg_price = sum(prices) / len(prices) if prices else 0
        avg_want = sum(want_nums) / len(want_nums) if want_nums else 0

        candidates.append({
            "word": word,
            "appeared_in": len(indices),
            "items_with_sold": sold_count,
            "avg_soldCount": round(sold_count / max(len(indices), 1), 1),
            "avg_price": round(avg_price, 0),
            "avg_wantNum": round(avg_want, 1),
            "has_professional": has_pro,
            "sample_titles": [t[:80] for t in sample_titles[:3]],
        })

        if len(candidates) >= max_candidates:
            break

    # Step 3: 按出现次数排序
    candidates.sort(key=lambda c: c["appeared_in"], reverse=True)

    # Step 4: 构建 summary
    items_with_sold = sum(1 for s in item_stats if s.get("soldCount", 0) > 0)
    all_prices = []
    all_wants = []
    for s in item_stats:
        p = s.get("price", "")
        if p:
            try:
                all_prices.append(float(str(p).replace("¥", "").replace(",", "")))
            except (ValueError, TypeError):
                pass
        w = s.get("wantNum", s.get("wantCount", 0))
        if w:
            try:
                all_wants.append(int(w))
            except (ValueError, TypeError):
                pass

    pro_count = sum(
        1 for s in item_stats
        if str(s.get("sellerLevel", "")).find("专家") >= 0
    )

    summary = {
        "total_items": n_total,
        "items_with_soldCount": items_with_sold,
        "avg_price": round(sum(all_prices) / max(len(all_prices), 1), 0),
        "avg_wantNum": round(sum(all_wants) / max(len(all_wants), 1), 1),
        "professional_seller_ratio": round(pro_count / max(n_total, 1), 2),
        "top_service_tags": _count_service_tags(item_stats),
    }

    return candidates, summary


def _count_service_tags(item_stats: List[dict]) -> List[str]:
    """统计服务标签频次"""
    tag_counter = Counter()
    for s in item_stats:
        tags = s.get("serviceTags", s.get("service_ut_params", []))
        if isinstance(tags, list):
            for t in tags:
                if isinstance(t, dict):
                    tag_counter[t.get("value", t.get("text", ""))] += 1
                elif isinstance(t, str):
                    tag_counter[t] += 1
    return [f"{tag}({cnt})" for tag, cnt in tag_counter.most_common(5)]


# ── 规则 Baseline 评分（AI 失败时降级）────────────────────────────

def baseline_rule_score(
    candidate: dict,
    existing_library: List[str],
    total_items: int = 60,
) -> dict:
    """纯规则评分 — 与 test_flywheel.py 对齐，AI 不可用时降级使用"""
    word = candidate["word"]
    appeared = candidate.get("appeared_in", 0)
    sold = candidate.get("items_with_sold", 0)
    price = candidate.get("avg_price", 0)
    has_pro = candidate.get("has_professional", False)

    # 类型检测
    ctype = candidate.get("type", _detect_word_type(word))
    if ctype == "category_seed":
        return {
            "word": word, "type": "category_seed",
            "seed_for": candidate.get("seed_for", []),
            "category_direction": candidate.get("category_direction", ""),
            "method": "rule", "reason": "品类扩展词，回种飞轮",
        }
    if ctype == "title_boost":
        return {
            "word": word, "type": "title_boost", "category": _guess_category(word),
            "method": "rule", "reason": f"按预定义类型: {ctype}",
        }
    if ctype == "noise":
        return {
            "word": word, "type": "noise", "method": "rule",
            "reason": "营销用语或无商品信息",
        }
    if appeared == 0:
        return {
            "word": word, "type": "unknown", "method": "rule",
            "reason": "无数据信号，规则无法判断",
        }

    # 商品指向性
    has_brand = any(b in word for b in [
        "捷安特", "美利达", "喜德盛", "凤凰", "永久",
        "华为", "小米", "美的", "苏泊尔", "Cherry", "Filco",
        "乐高", "LEGO", "戴森", "苹果", "三星", "索尼",
        "大疆", "BOSE", "Bose", "任天堂", "PlayStation",
        "Nike", "Adidas", "优衣库", "无印良品", "MUJI",
        "膳魔师", "虎牌", "象印",
    ])
    has_model = any(c.isdigit() for c in word) and len(word) > 3
    has_param = any(p in word for p in [
        "速", "寸", "L", "ml", "mm", "cm", "斤", "kg",
        "铝合金", "碳纤维", "不锈钢",
    ])

    if has_brand and has_model:
        specificity = 5
    elif has_brand or (has_model and has_param):
        specificity = 3
    elif has_param:
        specificity = 2
    else:
        specificity = 1

    # 需求信号
    if appeared >= 3 and sold / max(appeared, 1) >= 0.5:
        demand = 5
    elif appeared >= 3 and sold / max(appeared, 1) >= 0.3:
        demand = 4
    elif appeared >= 1 and sold > 0:
        demand = 3
    elif appeared >= 1:
        demand = 2
    else:
        demand = 0

    # 竞争预估
    appear_rate = appeared / max(total_items, 1)
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
    if any(kw in word for kw in ["自组", "手作", "定制", "改装"]):
        supply = 1

    # 利润空间
    if price > 2000:
        profit = 5
    elif price > 800:
        profit = 4
    elif price > 300:
        profit = 3
    elif price > 100:
        profit = 2
    elif price > 0:
        profit = 1
    else:
        profit = 3

    # 词库边际贡献
    exact_match = any(w == word for w in existing_library)
    similar = any(w in word or word in w for w in existing_library
                  if len(word) > 2 and len(w) > 2)
    if exact_match:
        marginal = 0
    elif similar:
        marginal = 2
    else:
        marginal = 4

    composite_raw = (
        specificity * 1.5 + demand * 1.5 + competition * 2.0 +
        supply * 1.5 + profit * 1.5 + marginal * 1.5
    )
    composite = round(composite_raw / 45 * 10, 1)

    if composite >= 7.5:
        status = "pass"
    elif composite >= 5.5:
        status = "watch"
    else:
        status = "discard"

    return {
        "word": word, "type": "search_word", "method": "rule",
        "scores": {
            "specificity": specificity, "demand_signal": demand,
            "competition": competition, "supply_access": supply,
            "profit_potential": profit, "marginal_value": marginal,
        },
        "composite": composite, "status": status,
        "evidence": f"出现{appeared}次, 售出{sold}条, 均价¥{price}",
    }


def _detect_word_type(word: str) -> str:
    """检测候选词类型 (search_word / category_seed / title_boost / noise)"""
    # ★ 品类扩展词：场景/风格/人群/季节/用途/趋势词
    seed_patterns = [
        # 场景词
        "学生党", "通勤", "面试", "旅行", "出差", "上班族", "宿舍", "户外",
        "运动", "健身", "跑步", "游泳", "露营", "骑行", "登山",
        # 风格词
        "ins风", "复古", "极简", "韩系", "日系", "北欧风", "工业风", "法式",
        "简约", "轻奢", "高级感", "设计感", "小众",
        # 人群词
        "儿童", "女生", "男生", "新手", "老人", "婴儿", "孕妇", "宠物",
        # 季节词
        "夏季", "冬季", "春季", "秋季", "圣诞", "春节", "开学季", "情人节",
        # 用途词
        "送礼", "考研", "入门级", "专业级", "初学", "进阶",
        # 趋势词
        "新款", "限量", "联名", "全系列",
    ]
    seed_suffixes = ["党", "族", "风", "感", "级", "款", "季", "礼"]

    word_clean = word.strip()

    # 先检查 category_seed（优先级高于 title_boost）
    for sp in seed_patterns:
        if sp == word_clean or sp in word_clean:
            return "category_seed"
    # 后缀匹配：以党/族/风/感结尾的2-4字词
    if 2 <= len(word_clean) <= 4:
        for suffix in seed_suffixes:
            if word_clean.endswith(suffix):
                return "category_seed"

    boost_patterns = [
        (["闲置", "自用", "个人", "搬家出", "年会奖品", "退坑", "不玩了",
          "用不上", "太多", "冲动消费"], "真实感"),
        (["99新", "几乎全新", "仅拆封", "未使用", "九成新", "九五新",
          "九九新", "刚买", "全新未拆"], "成色"),
        (["正品", "支持验货", "品牌授权", "假一赔十", "专柜", "实体店",
          "代购", "百分百好评", "信用极好"], "信任"),
        (["包邮", "可小刀", "可刀", "面交", "同城", "自提"], "交易"),
        (["急出", "最后一天", "马上搬家", "清仓价", "亏本", "不议价"], "紧迫"),
        (["好物推荐", "种草", "神器", "必备", "推荐", "好物"], "marketing"),
    ]

    for patterns, category in boost_patterns:
        for p in patterns:
            if p in word_clean:
                if category == "marketing":
                    return "noise"
                return "title_boost"
    return "search_word"


def _guess_category(word: str) -> str:
    """根据词内容猜测归类"""
    mappings = [
        (["闲置", "自用", "个人", "搬家出", "年会奖品", "退坑"], "真实感"),
        (["99新", "全新", "拆封", "未使用", "成新"], "成色"),
        (["正品", "验货", "授权", "赔十", "专柜", "代购"], "信任"),
        (["包邮", "小刀", "面交", "同城", "自提"], "交易"),
        (["急出", "最后", "搬家", "清仓", "亏本"], "紧迫"),
    ]
    for keywords, cat in mappings:
        for kw in keywords:
            if kw in word:
                return cat
    return "真实感"


# ── 词库管理 ──────────────────────────────────────────────────────

class WordLibrary:
    """持久化搜索词库，支持合并、去重、历史追踪"""

    def __init__(self, path: Path):
        self._path = Path(path)
        self.data: dict = self._load()

    def _load(self) -> dict:
        if self._path.exists():
            try:
                return json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return {
            "version": "1.0",
            "words": {},        # word -> {status, source, added_at, scores}
            "title_materials": {
                "真实感": [], "成色": [], "信任": [], "交易": [], "紧迫": [],
            },
            "history": [],      # [{action, words, timestamp}]
        }

    def save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get_all_words(self) -> List[str]:
        return list(self.data["words"].keys())

    def get_pass_words(self) -> List[str]:
        return [
            w for w, info in self.data["words"].items()
            if info.get("status") == "pass"
        ]

    def get_pending_words(self) -> List[str]:
        return [
            w for w, info in self.data["words"].items()
            if info.get("status") == "pending_verify"
        ]

    def get_search_words(self) -> List[str]:
        """只返回 search_word 类型的词（可直接搜索评分）"""
        return [
            w for w, info in self.data["words"].items()
            if info.get("word_type") != "category_seed"
        ]

    def get_category_seeds(self) -> List[dict]:
        """返回所有 category_seed 类型的词及其 seed_for 提示"""
        return [
            {"word": w, "seed_for": info.get("seed_for", []),
             "category_direction": info.get("category_direction", ""),
             "status": info.get("status", "watch")}
            for w, info in self.data["words"].items()
            if info.get("word_type") == "category_seed"
        ]

    def get_entry(self, word: str) -> dict:
        """获取单个词的完整信息"""
        return self.data["words"].get(word, {})

    def add_words(self, words: List[dict], source: str = "flywheel_phase_b"):
        """批量添加/更新词"""
        added = []
        updated = []
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for w in words:
            word = w["word"]
            status = w.get("status", "watch")
            scores = w.get("scores", {})
            composite = w.get("composite", 0)
            # 安全转换 composite 为 float（AI 可能返回字符串）
            try:
                composite = float(composite)
            except (ValueError, TypeError):
                composite = 0.0
            evidence = w.get("evidence", "")

            entry = {
                "status": status,
                "source": source,
                "composite": composite,
                "scores": scores,
                "evidence": evidence,
                "word_type": w.get("type", w.get("word_type", "search_word")),
                "seed_for": w.get("seed_for", []),
                "category_direction": w.get("category_direction", ""),
                "added_at": timestamp,
            }

            if word in self.data["words"]:
                old = self.data["words"][word]
                old_comp = old.get("composite", 0)
                try:
                    old_comp = float(old_comp)
                except (ValueError, TypeError):
                    old_comp = 0.0
                old_type = old.get("word_type", "search_word")
                new_type = entry.get("word_type", "search_word")
                # 类型升级（category_seed 是被 AI 确认的品类扩展词，优先保留）
                # 或 composite 更高，或新状态为 pass
                if new_type == "category_seed" and old_type != "category_seed":
                    self.data["words"][word] = entry
                    updated.append(word)
                elif composite > old_comp or status == "pass":
                    self.data["words"][word] = entry
                    updated.append(word)
            else:
                self.data["words"][word] = entry
                added.append(word)

        if added or updated:
            self.data["history"].append({
                "action": source,
                "added": added,
                "updated": updated,
                "timestamp": timestamp,
            })

        return added, updated

    def add_title_materials(self, materials: dict, source: str = "flywheel_phase_b"):
        """添加标题素材到对应分类（支持AI自发现的动态类别）"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        added_count = 0

        for category, items in materials.items():
            if not items:
                continue
            # ★ 动态类别：AI 发现的新类别自动创建
            if category not in self.data["title_materials"]:
                self.data["title_materials"][category] = []

            for item in items:
                text = item.get("text", "")
                if not text:
                    continue
                # 全局去重（跨类别也检查）
                all_texts = []
                for cat_items in self.data["title_materials"].values():
                    all_texts.extend(m.get("text") for m in cat_items)
                if text not in all_texts:
                    item["source"] = source
                    item["added_at"] = timestamp
                    self.data["title_materials"][category].append(item)
                    added_count += 1

        return added_count

    def stats(self) -> dict:
        words = self.data["words"]
        pass_count = sum(1 for w in words.values() if w.get("status") == "pass")
        watch_count = sum(1 for w in words.values() if w.get("status") == "watch")
        pending_count = sum(1 for w in words.values() if w.get("status") == "pending_verify")
        materials_count = sum(
            len(v) for v in self.data.get("title_materials", {}).values()
        )
        return {
            "total_words": len(words),
            "pass_count": pass_count,
            "watch_count": watch_count,
            "pending_count": pending_count,
            "total_materials": materials_count,
        }


# ── 飞轮引擎主类 ──────────────────────────────────────────────────

class FlywheelEngine:
    """
    飞轮引擎 — 管理 Phase B (AI 提取+评分) 和词库膨胀。

    Usage:
        engine = FlywheelEngine(settings)
        results = engine.run_phase_b(
            parent_keyword="蓝牙耳机",
            search_titles=[...],
            item_stats=[...],
        )
        # results 包含 pass/watch/pending 候选词 + title_materials
    """

    def __init__(self, settings: dict, output_dir: Path = None):
        self._settings = settings
        api_cfg = settings.get("api", {})
        self._api_key = api_cfg.get("deepseek_api_key", api_cfg.get("zhipu_api_key", ""))
        self._provider = "deepseek" if api_cfg.get("deepseek_api_key") else "zhipu"

        self._output_dir = Path(output_dir) if output_dir else Path("collected_data")
        self._output_dir.mkdir(parents=True, exist_ok=True)

        # 词库文件
        self._word_lib_path = self._output_dir / "word_library.json"
        self.word_lib = WordLibrary(self._word_lib_path)

        # 控制机制
        self.control = FlywheelControl(self.word_lib)

        # AI 客户端（按需创建）
        self._client = None
        self._use_ai_extraction = True  # 开启 AI 分词（已优化 prompt 大小）

        # 品类差异化配置
        self._category_map = {}  # keyword → product_type 映射
        self._profile_manager = None
        self._load_category_map()

    def _load_category_map(self):
        """从种子词库加载 keyword → product_type 映射"""
        seed_path = Path(__file__).parent / "seed_keywords.json"
        try:
            with open(seed_path) as f:
                seeds = json.load(f)
            for cat_name, cat_data in seeds.get("categories", {}).items():
                pt = cat_data.get("product_type", "")
                for kw in cat_data.get("keywords", []):
                    self._category_map[kw] = pt
        except Exception:
            pass

    def get_product_type(self, keyword: str) -> str:
        """获取关键词对应的品类类型"""
        return self._category_map.get(keyword, "")

    @property
    def client(self) -> AIClient:
        """智能选择模型：简单任务用本地Ollama，复杂任务用DeepSeek"""
        if self._client is None:
            # 优先尝试本地模型（简单任务快且免费）
            self._client = AIClient.auto_best(self._settings, task="simple")
            if self._client is None:
                self._client = AIClient(self._api_key, provider=self._provider)
        return self._client

    @property
    def cloud_client(self) -> AIClient:
        """复杂打分/分析专用 — 强制云端大模型"""
        if not hasattr(self, '_cloud_client') or self._cloud_client is None:
            self._cloud_client = AIClient.auto_best(self._settings, task="complex")
        return self._cloud_client

    # ── Phase B: AI 提取 + 评分 ──────────────────────────────────

    def run_phase_b(
        self,
        parent_keyword: str,
        search_titles: List[str],
        item_stats: List[dict] = None,
        search_pages: int = 15,
        num_found: int = 0,
    ) -> dict:
        """
        对单个父搜索词的采集结果运行 Phase B。

        Args:
            parent_keyword: 父搜索词 (如 "蓝牙耳机")
            search_titles: 搜索结果中所有商品标题
            item_stats: 商品统计数据列表 (soldCount, price, wantNum, sellerLevel)
            search_pages: 搜索页数
            num_found: API 返回的搜索结果总数

        Returns:
            {
                "parent_keyword": "...",
                "candidates_extracted": N,
                "search_words": [...],
                "title_materials": {...},
                "noise": [...],
                "analysis_note": "...",
                "method": "ai" | "rule" | "skipped",
            }
        """
        if item_stats is None:
            item_stats = [{}] * len(search_titles)

        # Step 1: 提取候选词（默认 jieba，AI 提取可选且带超时保护）
        candidates, summary = [], {}
        extraction_method = "jieba"

        # AI 提取默认关闭（prompt 太大容易超时），可选开启
        if self._use_ai_extraction and self.client and self._api_key:
            try:
                candidates, summary = extract_candidates_with_ai(
                    self.client, search_titles, item_stats, max_candidates=30
                )
                if candidates:
                    extraction_method = "ai"
            except Exception as e:
                print(f"[Flywheel] AI 提取失败，降级 jieba: {e}")

        if not candidates:
            candidates, summary = extract_candidates_from_titles(
                search_titles, item_stats, max_candidates=30
            )

        if not candidates:
            return {
                "parent_keyword": parent_keyword,
                "candidates_extracted": 0,
                "search_words": [],
                "category_seeds": [],
                "title_materials": {},
                "noise": [],
                "analysis_note": f"'{parent_keyword}' 搜索结果中无可提取的候选词",
                "method": "skipped",
                "extraction": extraction_method,
            }

        existing_library = self.word_lib.get_all_words()

        # Step 2: 先跑规则 baseline
        rule_results = [
            baseline_rule_score(c, existing_library, summary["total_items"])
            for c in candidates
        ]

        # Step 3: 尝试 AI 评分（复杂任务用云端大模型）
        ai_result = None
        scorer = self.cloud_client if self.cloud_client else self.client
        if scorer and self._api_key:
            parent_data = {
                "keyword": parent_keyword,
                "pages": search_pages,
                "summary": summary,
                "candidate_signals": candidates,
            }
            prompt = build_phase_b_prompt(parent_data, candidates, existing_library)
            try:
                ai_result = scorer.chat_json(PHASE_B_SYSTEM, prompt)
            except Exception as e:
                print(f"[Flywheel] AI 调用失败，降级为规则评分: {e}")

        # Step 4: 合并结果
        if ai_result:
            search_words = ai_result.get("search_words", [])
            category_seeds = ai_result.get("category_seeds", [])
            # ★ AI 自发现素材类别：动态合并
            title_materials = ai_result.get("title_materials", {})
            material_categories = ai_result.get("material_categories", [])
            if material_categories:
                for mc in material_categories:
                    cat_name = mc.get("name", "")
                    if cat_name and cat_name not in title_materials:
                        title_materials[cat_name] = []
            noise = ai_result.get("noise", [])
            analysis_note = ai_result.get("analysis_note", "")
            method = "ai"
        else:
            # 降级: 用规则结果
            search_words = [
                r for r in rule_results
                if r.get("type") == "search_word"
            ]
            category_seeds = [
                r for r in rule_results
                if r.get("type") == "category_seed"
            ]
            title_materials = self._rule_materials_to_structured(rule_results)
            noise = [
                {"text": r["word"], "reason": r.get("reason", "")}
                for r in rule_results if r.get("type") == "noise"
            ]
            analysis_note = f"(规则评分降级) 从{parent_keyword}中提取{len(candidates)}个候选词"
            method = "rule"

        # Step 5: 更新词库
        if search_words:
            self.word_lib.add_words(search_words, source=f"phase_b:{parent_keyword}")

        # ★ category_seed 单独存储（强制类型，AI 可能不返回 type 字段）
        if category_seeds:
            for cs in category_seeds:
                cs["type"] = "category_seed"
            self.word_lib.add_words(category_seeds, source=f"phase_b_seed:{parent_keyword}")

        if title_materials:
            self.word_lib.add_title_materials(
                title_materials, source=f"phase_b:{parent_keyword}"
            )

        self.word_lib.save()

        return {
            "parent_keyword": parent_keyword,
            "candidates_extracted": len(candidates),
            "num_found": num_found,
            "search_words": search_words,
            "category_seeds": category_seeds,
            "title_materials": title_materials,
            "noise": noise,
            "analysis_note": analysis_note,
            "method": method,
            "extraction": extraction_method,
        }

    # ── 批量 Phase B ─────────────────────────────────────────────

    def run_phase_b_batch(
        self,
        keyword_data: List[dict],
    ) -> dict:
        """
        对多个父搜索词批量运行 Phase B。

        Args:
            keyword_data: [{keyword, search_items, numFound, ...}, ...]

        Returns:
            批量结果汇总
        """
        all_results = []
        total_pass = 0
        total_watch = 0
        total_pending = 0
        total_materials = 0

        for kw_data in keyword_data:
            kw = kw_data.get("keyword", "")
            items = kw_data.get("search_items", [])
            num_found = kw_data.get("numFound", 0)

            titles = [it.get("title", "") for it in items if it.get("title")]

            # 提取每项的统计
            item_stats = []
            for it in items:
                item_stats.append({
                    "soldCount": it.get("soldCount", it.get("soldCnt", 0)),
                    "price": it.get("price", it.get("priceStr", "")),
                    "wantNum": it.get("wantNum", it.get("wantCount", 0)),
                    "sellerLevel": it.get("sellerLevel", it.get("sellerType", "")),
                    "serviceTags": it.get("serviceTags", it.get("service_ut_params", [])),
                })

            if not titles:
                all_results.append({
                    "parent_keyword": kw,
                    "candidates_extracted": 0,
                    "search_words": [],
                    "title_materials": {},
                    "noise": [],
                    "analysis_note": f"'{kw}' 无搜索结果",
                    "method": "skipped",
                })
                continue

            result = self.run_phase_b(
                parent_keyword=kw,
                search_titles=titles,
                item_stats=item_stats,
                num_found=num_found,
            )
            all_results.append(result)

            for sw in result["search_words"]:
                if sw.get("status") == "pass":
                    total_pass += 1
                elif sw.get("status") == "watch":
                    total_watch += 1
                elif sw.get("status") == "pending_verify":
                    total_pending += 1

            total_materials += sum(
                len(v) for v in result.get("title_materials", {}).values()
            )

        # 保存完整结果
        batch_result = {
            "run_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "summary": {
                "keywords_processed": len(all_results),
                "pass_words": total_pass,
                "watch_words": total_watch,
                "pending_words": total_pending,
                "new_materials": total_materials,
            },
            "word_library_stats": self.word_lib.stats(),
            "results": all_results,
        }

        flywheel_path = self._output_dir / "flywheel_results.json"
        flywheel_path.write_text(
            json.dumps(batch_result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # 自动运行控制机制
        control_report = self.control.run_all_controls()
        batch_result["controls"] = control_report
        if control_report.get("deduplicates"):
            print(f"[控制] 去重: {len(control_report['deduplicates'])} 组")
        if control_report.get("dominance"):
            print(f"[控制] 偏见检测: {control_report['dominance'].get('action','')}")
        if control_report.get("queue_limit", {}).get("action") == "downgrade":
            print(f"[控制] 队列降级: {control_report['queue_limit']['downgraded_count']} 词")
        if control_report.get("expired"):
            print(f"[控制] 过期淘汰: {control_report['expired']}")

        return batch_result

    # ── Phase C: 验证搜索 ────────────────────────────────────────

    def get_pending_words(self) -> List[str]:
        """获取状态为 pending_verify 的词列表"""
        return self.word_lib.get_pending_words()

    def update_word_status(
        self, word: str, status: str, evidence: str = ""
    ):
        """更新词的验证状态"""
        if word in self.word_lib.data["words"]:
            self.word_lib.data["words"][word]["status"] = status
            if evidence:
                self.word_lib.data["words"][word]["evidence"] = evidence
            self.word_lib.data["words"][word]["verified_at"] = \
                datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.word_lib.save()

    # ── Phase C: 验证搜索 ────────────────────────────────────────

    PHASE_C_SYSTEM = """你是闲鱼搜索词验证分析师。根据pending词的1页搜索结果，给出最终判定。

    判定规则：
    - numFound < 50 且商品<$300 → 需求太弱 → discard
    - numFound 50-300 且有售出信号 → watch（观察池）
    - numFound > 300 且有明确的品牌/型号商品 → pass（正式入队）
    - 搜索结果全是低质/广告 → discard

    输出JSON:
    {
      "results": [
        {
          "word": "词",
          "num_found": 300,
          "items_searched": 20,
          "items_with_sold": 5,
          "avg_price_sold": 500,
          "verdict": "pass" | "watch" | "discard",
          "reason": "简短原因",
          "confidence": 0.85
        }
      ],
      "summary": "本轮验证总结"
    }"""

    def run_phase_c(
        self,
        pending_searches: List[dict],
    ) -> dict:
        """
        对 pending_verify 词做验证搜索后的最终判定。

        Args:
            pending_searches: [{
                "word": "瓜车",
                "numFound": 230,
                "search_items": [{title, soldCount, price, ...}, ...],
            }, ...]

        Returns:
            验证结果汇总
        """
        if not pending_searches:
            return {"verified": 0, "results": [], "summary": "无待验证词"}

        # Step 1: 对每个词做规则预判
        rule_verdicts = []
        for ps in pending_searches:
            word = ps["word"]
            num_found = ps.get("numFound", 0)
            items = ps.get("search_items", [])
            n_items = len(items)

            # 统计售出信号
            items_with_sold = 0
            sold_prices = []
            for it in items:
                sc = it.get("soldCount", it.get("soldCnt", 0))
                if sc and int(sc) > 0:
                    items_with_sold += 1
                    p = it.get("price", it.get("priceStr", ""))
                    try:
                        sold_prices.append(float(str(p).replace("¥", "").replace(",", "")))
                    except (ValueError, TypeError):
                        pass

            avg_price = sum(sold_prices) / max(len(sold_prices), 1)

            # 规则判定
            if num_found < 50 and avg_price < 300:
                verdict, reason = "discard", f"numFound={num_found}<50, 均价<¥300, 需求太弱"
            elif num_found > 300 and items_with_sold / max(n_items, 1) > 0.3:
                verdict, reason = "pass", f"numFound={num_found}>300, 售出率{items_with_sold}/{n_items}"
            elif num_found >= 50:
                verdict, reason = "watch", f"numFound={num_found}, 需进一步观察"
            else:
                verdict, reason = "discard", f"numFound={num_found}<50, 无足够信号"

            rule_verdicts.append({
                "word": word,
                "num_found": num_found,
                "items_searched": n_items,
                "items_with_sold": items_with_sold,
                "avg_price_sold": round(avg_price, 0),
                "verdict": verdict,
                "reason": reason,
                "confidence": 0.7,
            })

        # Step 2: 尝试 AI 二次判定
        ai_results = None
        if self.client and self._api_key:
            user_prompt = self._build_phase_c_prompt(pending_searches)
            try:
                ai_result = self.client.chat_json(self.PHASE_C_SYSTEM, user_prompt)
                ai_results = ai_result.get("results", [])
            except Exception as e:
                print(f"[Flywheel Phase C] AI 调用失败，使用规则判定: {e}")

        # Step 3: 合并（AI 优先）+ 更新词库
        final_results = []
        for i, rv in enumerate(rule_verdicts):
            word = rv["word"]
            if ai_results and i < len(ai_results):
                ar = ai_results[i]
                verdict = ar.get("verdict", rv["verdict"])
                reason = ar.get("reason", rv["reason"])
                confidence = ar.get("confidence", 0.85)
            else:
                verdict = rv["verdict"]
                reason = rv["reason"]
                confidence = rv["confidence"]

            # 更新词库状态
            self.update_word_status(word, verdict, reason)
            rv["verdict"] = verdict
            rv["reason"] = reason
            rv["confidence"] = confidence
            rv["method"] = "ai" if ai_results else "rule"
            final_results.append(rv)

        self.word_lib.save()

        pass_count = sum(1 for r in final_results if r["verdict"] == "pass")
        watch_count = sum(1 for r in final_results if r["verdict"] == "watch")
        discard_count = sum(1 for r in final_results if r["verdict"] == "discard")

        summary = (
            f"Phase C 完成: {len(final_results)}词验证 → "
            f"{pass_count}通过, {watch_count}观察, {discard_count}淘汰"
        )

        return {
            "verified": len(final_results),
            "pass_count": pass_count,
            "watch_count": watch_count,
            "discard_count": discard_count,
            "results": final_results,
            "summary": summary,
        }

    def _build_phase_c_prompt(self, pending_searches: List[dict]) -> str:
        """构建 Phase C 验证 prompt"""
        lines = []
        for ps in pending_searches:
            word = ps["word"]
            nf = ps.get("numFound", "?")
            items = ps.get("search_items", [])
            sold_count = sum(
                1 for it in items
                if it.get("soldCount", it.get("soldCnt", 0))
            )
            titles = [it.get("title", "")
                      for it in items[:5] if it.get("title")]
            lines.append(
                f"  【{word}】numFound={nf}, "
                f"{len(items)}条结果, {sold_count}条有售出, "
                f"样本标题: {' | '.join(titles[:3])}"
            )

        return f"""
    验证以下pending词的搜索数据：

    {chr(10).join(lines)}

    请对每个词做最终判定（pass/watch/discard），输出JSON。"""

    # ── 标题素材导出 ──────────────────────────────────────────────

    def export_title_materials(self) -> dict:
        """导出当前所有标题素材"""
        return self.word_lib.data.get("title_materials", {})

    def generate_title(
        self,
        product_type: str = "",
        max_boost_words: int = 3,
    ) -> str:
        """
        从素材库中随机生成标题片段。

        Args:
            product_type: 商品品类
            max_boost_words: 最多使用的增强词数
        """
        materials = self.word_lib.data.get("title_materials", {})
        selected = []

        # 各分类随机取一条
        for category in ["成色", "真实感", "交易"]:
            items = materials.get(category, [])
            if items:
                item = random.choice(items)
                text = item.get("text", "")
                if text and text not in selected:
                    selected.append(text)

        return " + ".join(selected[:max_boost_words])

    # ── 内部工具 ──────────────────────────────────────────────────

    def _rule_materials_to_structured(self, rule_results: List[dict]) -> dict:
        """将规则评分中的 title_boost 转为结构化素材"""
        structured = {
            "真实感": [], "成色": [], "信任": [], "交易": [], "紧迫": [],
        }
        for r in rule_results:
            if r.get("type") == "title_boost":
                category = r.get("category", "真实感")
                if category in structured:
                    structured[category].append({
                        "text": r["word"],
                        "usage": f"提升标题{category}感",
                        "conflict_with": [],
                    })
        return structured


# ── 控制机制 ──────────────────────────────────────────────────────

def _safe_float(v, default=0.0):
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


class FlywheelControl:
    """飞轮控制机制：语义去重 / 偏见检测 / 队列上限 / 过期淘汰"""

    MAX_QUEUE_SIZE = 80       # 词库上限
    EXPIRE_DAYS = 30          # 无产出过期天数
    DOMINANCE_THRESHOLD = 0.50  # 主导品类占比阈值

    def __init__(self, word_lib: WordLibrary):
        self._lib = word_lib

    # ── 品类重叠语义去重 ──

    def deduplicate_overlaps(self, client=None) -> List[dict]:
        """
        检测词库中语义重叠 >70% 的词对，合并为更优的那个。

        Args:
            client: AI 客户端（可选，用于语义判断）

        Returns:
            合并记录列表
        """
        words = self._lib.data.get("words", {})
        word_list = list(words.keys())
        merges = []

        if len(word_list) < 2:
            return merges

        # 规则检测：一个词完全包含另一个
        for i, w1 in enumerate(word_list):
            for j, w2 in enumerate(word_list[i + 1:], i + 1):
                if w1 in w2 or w2 in w1:
                    # 保留更长的（更具体的）
                    if len(w2) > len(w1):
                        keep, remove = w2, w1
                    else:
                        keep, remove = w1, w2

                    if keep in words and remove in words:
                        keep_score = _safe_float(words[keep].get("composite", 0))
                        remove_score = _safe_float(words[remove].get("composite", 0))
                        merged_score = max(keep_score, remove_score)
                        merges.append({
                            "words": [w1, w2],
                            "merged_to": keep,
                            "overlap_pct": 100,
                            "method": "rule_inclusion",
                        })
                        words[keep]["composite"] = merged_score
                        if "merged_from" not in words[keep]:
                            words[keep]["merged_from"] = []
                        words[keep]["merged_from"].append(remove)
                        # 标记被合并的词为 deprecated
                        words[remove]["status"] = "deprecated"
                        words[remove]["merged_to"] = keep

        if merges:
            self._lib.save()
        return merges

    # ── 偏见循环检测 ──

    def check_dominance_bias(self) -> Optional[dict]:
        """
        检测主导品类是否占比 >50%。
        如果是，自动降低该品类评分，提升其他品类权重。

        Returns:
            {dominant_category, dominance_pct, action_taken} 或 None
        """
        words = self._lib.data.get("words", {})
        active_words = {
            w: info for w, info in words.items()
            if info.get("status") in ("pass", "watch", "pending_verify")
        }
        if not active_words:
            return None

        # 按 source 前缀分类（格式: "phase_b:品类名"）
        from collections import Counter
        cat_counter = Counter()
        for w, info in active_words.items():
            source = info.get("source", "")
            if source.startswith("phase_b:"):
                cat = source.replace("phase_b:", "").strip()
                cat_counter[cat] += 1
            else:
                cat_counter["__unknown__"] += 1

        total = sum(cat_counter.values())
        if total == 0:
            return None

        dominant_cat, dominant_count = cat_counter.most_common(1)[0]
        dominance_pct = dominant_count / total

        if dominance_pct > self.DOMINANCE_THRESHOLD:
            # 自动处理：降低主导品类词的评分 10%，提升其他词 5%
            for w, info in active_words.items():
                source = info.get("source", "")
                cat = source.replace("phase_b:", "").strip() if source.startswith("phase_b:") else "__unknown__"
                old_comp = _safe_float(info.get("composite", 0))
                if cat == dominant_cat:
                    info["composite"] = round(old_comp * 0.9, 1)
                else:
                    info["composite"] = round(old_comp * 1.05, 1)

            self._lib.save()
            return {
                "dominant_category": dominant_cat,
                "dominance_pct": round(dominance_pct, 2),
                "action": f"降低'{dominant_cat}'品类词评分10%，提升其他品类5%",
            }
        return None

    # ── 队列上限自动降级 ──

    def enforce_queue_limit(self) -> dict:
        """
        当词库中 pass/watch 词 > MAX_QUEUE_SIZE 时，
        自动将最低分词的 status 降级为 pending_verify。
        """
        words = self._lib.data.get("words", {})
        active = {
            w: info for w, info in words.items()
            if info.get("status") in ("pass", "watch")
        }

        if len(active) <= self.MAX_QUEUE_SIZE:
            return {"action": "none", "count": len(active)}

        # 按 composite 排序，将最低分的降级
        sorted_words = sorted(
            active.items(), key=lambda x: _safe_float(x[1].get("composite", 0)))
        to_downgrade = len(active) - self.MAX_QUEUE_SIZE

        downgraded = []
        for w, info in sorted_words[:to_downgrade]:
            words[w]["status"] = "pending_verify"
            words[w]["downgraded_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            downgraded.append(w)

        self._lib.save()
        return {
            "action": "downgrade",
            "count": len(active),
            "downgraded_count": len(downgraded),
            "downgraded_words": downgraded,
        }

    # ── 30天过期淘汰 ──

    def expire_stale_words(self) -> List[str]:
        """
        淘汰 30 天内状态未从 watch 变为 pass 的词。
        对于 pass 词，检查是否有 30 天无产出记录。
        """
        words = self._lib.data.get("words", {})
        now = datetime.now()
        expired = []

        for w, info in list(words.items()):
            status = info.get("status", "")
            added_str = info.get("added_at", "")

            # 只有 watch 和 pending_verify 才自动过期
            if status not in ("watch", "pending_verify"):
                continue

            try:
                added_at = datetime.strptime(added_str, "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                continue

            if (now - added_at).days >= self.EXPIRE_DAYS:
                info["status"] = "expired"
                info["expired_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
                info["expire_reason"] = f"{self.EXPIRE_DAYS}天无产出"
                expired.append(w)

        if expired:
            self._lib.save()
        return expired

    def run_all_controls(self, client=None) -> dict:
        """运行所有控制机制，返回报告"""
        report = {
            "deduplicates": self.deduplicate_overlaps(client),
            "dominance": self.check_dominance_bias(),
            "queue_limit": self.enforce_queue_limit(),
            "expired": self.expire_stale_words(),
            "run_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        return report

