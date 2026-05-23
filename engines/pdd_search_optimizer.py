"""
PDD 搜索词优化器 (角色③-3)

从闲鱼标题中 AI 提取核心产品词 → 生成 4 种 PDD 搜索策略 → 评估择优
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional, Tuple

from engines.ai_client import AIClient

# ── AI Prompt ─────────────────────────────────────────────────────

EXTRACT_SYSTEM = """你是电商商品识别专家。从闲鱼标题中提取核心产品信息，用于PDD货源搜索。

规则：
1. 核心产品词 = 品牌 + 型号 + 品类（去掉成色/卖家状态/交易条件）
2. 提取品牌名、型号、关键规格参数、品类词
3. 去除：成色词(99新/仅拆封)、卖家状态(自用/年会奖品)、交易词(包邮/可刀)、营销语

输出JSON:
{
  "brand": "品牌名 or null",
  "model": "型号 or null",
  "category": "品类词",
  "specs": ["规格1", "规格2"],
  "core_product": "品牌+型号+品类 简洁组合",
  "keywords": ["关键词1", "关键词2", "关键词3"],
  "confidence": 0.9
}"""

STRATEGY_SYSTEM = """你是PDD搜索策略专家。根据提取的商品信息，生成4种搜索策略并评估。

策略类型：
- exact: 精确搜索（品牌+型号+品类），匹配度最高但可能漏货
- model: 型号搜索（品牌+型号缩写），平衡匹配度和覆盖面
- keyword: 关键词搜索（核心品类+关键规格），覆盖面广
- broad: 宽泛搜索（纯品类词），覆盖面最大但精准度最低

评估每个策略的预期效果（1-10分）：匹配精度、覆盖面、货源可得性。

输出JSON:
{
  "strategies": [
    {"type": "exact", "query": "...", "precision": 9, "coverage": 3, "availability": 5},
    {"type": "model", "query": "...", "precision": 7, "coverage": 5, "availability": 6},
    {"type": "keyword", "query": "...", "precision": 5, "coverage": 7, "availability": 8},
    {"type": "broad", "query": "...", "precision": 3, "coverage": 9, "availability": 9}
  ],
  "recommended": "exact",
  "note": "首选精确搜索因为..."
}"""


# ── 核心产品词提取 ──────────────────────────────────────────────

def extract_core_product(client: AIClient, xianyu_title: str) -> dict:
    """
    从闲鱼标题中提取核心产品信息。

    Args:
        client: AI 客户端
        xianyu_title: 闲鱼商品标题

    Returns:
        {brand, model, category, specs, core_product, keywords, confidence}
    """
    prompt = f"闲鱼标题: {xianyu_title[:200]}\n\n提取核心产品信息，返回JSON。"

    try:
        result = client.chat_json(EXTRACT_SYSTEM, prompt)
        return result
    except Exception as e:
        print(f"[PDD Optimizer] AI提取失败: {e}")
        # 降级：用规则提取
        return _rule_extract(xianyu_title)


def _rule_extract(title: str) -> dict:
    """规则降级：简单分词提取"""
    import re
    # 去掉括号内容、噪声词
    noise = ["自用", "闲置", "99新", "包邮", "可小刀", "年会奖品",
             "全新", "仅拆封", "几乎全新", "正品", "支持验货", "急出"]
    clean = title
    for n in noise:
        clean = clean.replace(n, " ")
    clean = re.sub(r'\s+', ' ', clean).strip()

    # 按空格和标点分词
    words = re.split(r'[\s,，、。．\t]+', clean)
    words = [w for w in words if len(w) > 1]

    return {
        "brand": None,
        "model": None,
        "category": words[0] if words else title[:10],
        "specs": [],
        "core_product": " ".join(words[:3]) if words else title[:30],
        "keywords": words[:5],
        "confidence": 0.3,
    }


# ── 搜索策略生成 ─────────────────────────────────────────────────

def generate_search_strategies(client: AIClient, product_info: dict) -> dict:
    """
    根据产品信息生成 4 种 PDD 搜索策略。

    Returns:
        {strategies: [...], recommended, note}
    """
    prompt = f"""提取的商品信息：
品牌: {product_info.get('brand', '未知')}
型号: {product_info.get('model', '未知')}
品类: {product_info.get('category', '未知')}
规格: {', '.join(product_info.get('specs', []))}
核心产品: {product_info.get('core_product', '')}

为PDD搜索生成4种策略并评估。返回JSON。"""

    try:
        result = client.chat_json(STRATEGY_SYSTEM, prompt)
        return result
    except Exception as e:
        print(f"[PDD Optimizer] 策略生成失败: {e}")
        return _rule_strategies(product_info)


def _rule_strategies(product_info: dict) -> dict:
    """规则降级：生成简单策略"""
    brand = product_info.get("brand") or ""
    model = product_info.get("model") or ""
    category = product_info.get("category", "")
    core = product_info.get("core_product", "")
    keywords = product_info.get("keywords", [])

    # 短关键词（1-2个核心词，DDK搜索不要太长）
    short_kw = " ".join(keywords[:2]) if len(keywords) >= 2 else (keywords[0] if keywords else category)
    model_str = f"{brand} {model}".strip() if (brand or model) else ""

    return {
        "strategies": [
            {"type": "exact", "query": model_str or short_kw,
             "precision": 9, "coverage": 3, "availability": 5},
            {"type": "model", "query": model_str or short_kw,
             "precision": 7, "coverage": 5, "availability": 6},
            {"type": "keyword", "query": short_kw,
             "precision": 5, "coverage": 7, "availability": 8},
            {"type": "broad", "query": category,
             "precision": 3, "coverage": 9, "availability": 9},
        ],
        "recommended": "model" if model_str else "keyword",
        "note": "规则降级生成",
    }


# ── 策略评估与择优 ───────────────────────────────────────────────

def evaluate_strategies(
    client: AIClient,
    strategies: List[dict],
    pdd_results: Dict[str, dict],
) -> Tuple[str, dict]:
    """
    根据 PDD 实际搜索结果评估各策略效果。

    Args:
        client: AI 客户端
        strategies: 策略列表
        pdd_results: {strategy_type: {match_count, avg_price, ...}}

    Returns:
        (best_strategy_type, evaluation_report)
    """
    eval_prompt = f"""4种策略的PDD搜索结果：

    {json.dumps(pdd_results, ensure_ascii=False, indent=2)}

    评估哪种策略最优（匹配精度×货源可得性×价格竞争力）。返回JSON：
    {{
      "best_strategy": "exact|model|keyword|broad",
      "rankings": [{{"type": "...", "score": 8.5, "reason": "..."}}],
      "note": "评估总结"
    }}"""

    try:
        result = client.chat_json("你是PDD搜索效果评估专家。", eval_prompt)
        return result.get("best_strategy", "keyword"), result
    except Exception as e:
        print(f"[PDD Optimizer] 评估失败: {e}")
        # 规则降级优先用 model（品牌+型号精准），其次keyword，最后broad
        has_exact = any(s["type"] == "exact" and s.get("query") for s in strategies)
        has_model = any(s["type"] == "model" and s.get("query") for s in strategies)
        fallback = "model" if has_model else ("exact" if has_exact else "keyword")
        return fallback, {"best_strategy": fallback, "note": f"规则降级: {e}"}


# ── 优化器主类 ───────────────────────────────────────────────────

class PDDSearchOptimizer:
    """
    PDD 搜索词优化器 — 从闲鱼标题到最优PDD搜索策略。
    支持本地模型做简单提取，云端做复杂评估。

    Usage:
        opt = PDDSearchOptimizer(client)  # 或 PDDSearchOptimizer.auto(settings)
        product = opt.extract("索尼WH-1000XM4降噪耳机 99新 包邮")
        strategies = opt.generate_strategies(product)
        best = opt.evaluate(strategies, pdd_results)
    """

    def __init__(self, client: AIClient):
        self._client = client
        # 策略历史：记录每种商品类型的最佳策略（本地学习积累）
        self._history: Dict[str, str] = {}
        self._history_path = None

    @classmethod
    def auto(cls, settings: dict) -> "PDDSearchOptimizer":
        """自动选择最佳模型构建优化器"""
        from engines.ai_client import AIClient
        client = AIClient.auto_best(settings, task="simple")
        if client is None:
            api_cfg = settings.get("api", {})
            key = api_cfg.get("deepseek_api_key", "")
            if key:
                client = AIClient(key, provider="deepseek")
        opt = cls(client) if client else cls(None)
        return opt

    def load_history(self, path):
        """加载策略历史（本地学习积累）"""
        self._history_path = path
        try:
            with open(path) as f:
                self._history = json.load(f)
        except Exception:
            pass

    def save_history(self):
        """保存策略历史"""
        if self._history_path:
            try:
                import json
                with open(self._history_path, 'w') as f:
                    json.dump(self._history, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

    def extract(self, xianyu_title: str) -> dict:
        """Step 1: 提取核心产品词"""
        return extract_core_product(self._client, xianyu_title)

    def generate_strategies(self, product_info: dict) -> dict:
        """Step 2: 生成 4 种搜索策略"""
        return generate_search_strategies(self._client, product_info)

    def evaluate(
        self,
        strategies: List[dict],
        pdd_results: Dict[str, dict],
        category: str = "",
    ) -> Tuple[str, dict]:
        """Step 3: 评估策略 + 记录最佳策略"""
        best, report = evaluate_strategies(
            self._client, strategies, pdd_results)

        # 记录历史
        if category:
            self._history[category] = best

        return best, report

    def get_best_strategy_for(self, category: str) -> Optional[str]:
        """获取品类历史上最佳策略"""
        return self._history.get(category)

    def optimize_title_for_pdd(
        self, xianyu_title: str, category: str = ""
    ) -> dict:
        """
        一键优化：从闲鱼标题到PDD搜索词。

        Returns:
            {
                "original_title": "...",
                "core_product": {...},
                "strategies": [...],
                "recommended": "exact",
                "recommended_query": "..."
            }
        """
        product = self.extract(xianyu_title)
        strategies_result = self.generate_strategies(product)

        # 如果有品类历史，优先使用历史最佳策略类型
        best_type = strategies_result.get("recommended", "keyword")
        if category and category in self._history:
            historical_best = self._history[category]
            # 验证历史最佳类型在本次策略中
            types = [s["type"] for s in strategies_result.get("strategies", [])]
            if historical_best in types:
                best_type = historical_best

        # 找对应 query
        recommended_query = ""
        for s in strategies_result.get("strategies", []):
            if s["type"] == best_type:
                recommended_query = s["query"]
                break

        return {
            "original_title": xianyu_title,
            "core_product": product,
            "strategies": strategies_result.get("strategies", []),
            "recommended": best_type,
            "recommended_query": recommended_query,
            "note": strategies_result.get("note", ""),
        }

    def batch_optimize(
        self,
        titles: List[str],
        category: str = "",
    ) -> List[dict]:
        """批量优化多个标题"""
        results = []
        for title in titles:
            try:
                result = self.optimize_title_for_pdd(title, category)
                results.append(result)
            except Exception as e:
                results.append({
                    "original_title": title,
                    "error": str(e),
                })
        return results
