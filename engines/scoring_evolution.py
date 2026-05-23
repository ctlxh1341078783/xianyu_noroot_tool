"""
品类评分进化引擎 (角色②)

积累预测vs实际利润数据 → AI分析维度相关性 → 自动调整评分模型权重
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from engines.ai_client import AIClient

# ── AI Prompt ─────────────────────────────────────────────────────

EVOLUTION_SYSTEM = """你是数据分析专家。根据品类商品的预测评分与实际利润数据，分析评分维度有效性。

规则：
1. 对比预测高分 vs 实际高利润的相关性
2. 识别哪些维度真正预测了利润（正相关）
3. 识别哪些维度与利润无关甚至负相关
4. 建议新的维度权重分配（总权重=10，各维度按重要性分配）

输出JSON:
{
  "analysis": {
    "sample_size": 10,
    "profit_correlation": {"demand_signal": 0.75, "price_advantage": 0.60, ...},
    "noise_dimensions": ["维度名"],
    "key_findings": "主要发现"
  },
  "weight_adjustments": {
    "demand_signal": {"old": 0.20, "new": 0.30, "reason": "与利润强正相关"},
    "price_advantage": {"old": 0.25, "new": 0.20, "reason": "相关度下降"}
  },
  "recommended_thresholds": {
    "S": 90, "A": 75, "B": 55, "C": 35
  },
  "confidence": 0.85,
  "note": ""
}"""


# ── 数据累积器 ───────────────────────────────────────────────────

class ScoringDataCollector:
    """累积预测 vs 实际利润数据点"""

    def __init__(self, storage_path: Path = None):
        self._path = storage_path or Path("collected_data/scoring_evolution.json")
        self._data: Dict[str, List[dict]] = self._load()

    def _load(self) -> dict:
        if self._path.exists():
            try:
                return json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return {"records": {}, "adjustments": {}}

    def save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def add_record(
        self,
        category: str,
        item_id: str,
        predicted_score: float,
        dimension_scores: Dict[str, float],
        actual_profit: float = None,
        actual_sold: bool = None,
    ):
        """添加一条预测-实际记录"""
        if category not in self._data["records"]:
            self._data["records"][category] = []

        record = {
            "item_id": item_id,
            "predicted_score": predicted_score,
            "dimensions": dimension_scores,
            "actual_profit": actual_profit,
            "actual_sold": actual_sold,
            "recorded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._data["records"][category].append(record)

        # 只保留最近 50 条
        if len(self._data["records"][category]) > 50:
            self._data["records"][category] = \
                self._data["records"][category][-50:]

        self.save()

    def get_records(self, category: str, min_count: int = 5) -> Optional[List[dict]]:
        """获取品类记录，不足 min_count 返回 None"""
        records = self._data["records"].get(category, [])
        if len(records) < min_count:
            return None
        return records

    def get_all_categories(self) -> List[str]:
        return list(self._data["records"].keys())

    def get_adjustments(self, category: str) -> Optional[dict]:
        """获取品类已有的权重调整"""
        return self._data["adjustments"].get(category)

    def apply_adjustments(self, category: str, adjustments: dict):
        """应用权重调整"""
        self._data["adjustments"][category] = {
            "applied_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "weights": adjustments.get("weights", {}),
            "thresholds": adjustments.get("thresholds", {}),
            "note": adjustments.get("note", ""),
        }
        self.save()


# ── 评分进化引擎 ─────────────────────────────────────────────────

class ScoringEvolution:
    """
    品类评分进化引擎 — 数据积累 → AI分析 → 权重自动调整。

    Usage:
        evo = ScoringEvolution(client, storage_path)
        evo.add_record("蓝牙耳机", "item123", 85.0, {...}, actual_profit=45)
        if evo.should_evolve("蓝牙耳机"):
            new_weights = evo.evolve("蓝牙耳机")
    """

    def __init__(self, client: AIClient, storage_path: Path = None):
        self._client = client
        self._collector = ScoringDataCollector(storage_path)

    def add_record(
        self,
        category: str,
        item_id: str,
        predicted_score: float,
        dimension_scores: Dict[str, float],
        actual_profit: float = None,
        actual_sold: bool = None,
    ):
        """记录一条预测结果"""
        self._collector.add_record(
            category, item_id, predicted_score,
            dimension_scores, actual_profit, actual_sold,
        )

    def should_evolve(self, category: str) -> bool:
        """检查是否应该触发进化（>=5条新记录）"""
        records = self._collector.get_records(category, min_count=5)
        if not records:
            return False

        # 检查是否有未分析的记录（无 adjustment 或 adjustment 时间早于最新记录）
        adj = self._collector.get_adjustments(category)
        if not adj:
            return True

        try:
            adj_time = datetime.strptime(adj["applied_at"], "%Y-%m-%d %H:%M:%S")
            latest_record_time = max(
                datetime.strptime(r["recorded_at"], "%Y-%m-%d %H:%M:%S")
                for r in records
            )
            return latest_record_time > adj_time
        except (ValueError, KeyError):
            return True

    def evolve(self, category: str) -> Optional[dict]:
        """
        触发 AI 分析，生成新的权重建议。

        Returns:
            {weights: {dim: weight, ...}, thresholds: {...}, note: ...}
            或 None（如果数据不足）
        """
        records = self._collector.get_records(category, min_count=3)
        if not records:
            return None

        # 只取有实际利润数据的记录
        valid_records = [r for r in records if r.get("actual_profit") is not None]
        if len(valid_records) < 3:
            print(f"[进化] {category}: 有效数据不足 ({len(valid_records)}条有利润)")
            return None

        # 构建分析 prompt
        data_summary = []
        for r in valid_records[-15:]:  # 最多取15条
            dims = r.get("dimensions", {})
            dim_str = ", ".join(f"{k}={v:.0f}" for k, v in dims.items())
            data_summary.append(
                f"  预测={r['predicted_score']:.1f}, 实际利润=¥{r.get('actual_profit', 0):.0f}, "
                f"维度: {dim_str}"
            )

        prompt = f"""品类: {category}
样本数: {len(valid_records)}

预测 vs 实际数据：
{chr(10).join(data_summary)}

分析评分维度有效性，建议新权重。返回JSON。"""

        try:
            result = self._client.chat_json(EVOLUTION_SYSTEM, prompt)
        except Exception as e:
            print(f"[进化] AI 分析失败: {e}")
            return None

        # 提取权重调整
        weight_adjustments = result.get("weight_adjustments", {})
        new_weights = {}
        for dim, adj in weight_adjustments.items():
            new_weights[dim] = adj.get("new", adj.get("old", 0.15))

        thresholds = result.get("recommended_thresholds", {})

        adjustment = {
            "weights": new_weights,
            "thresholds": thresholds,
            "note": result.get("note", result.get("analysis", {}).get("key_findings", "")),
            "confidence": result.get("confidence", 0.5),
            "analyzed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "sample_size": len(valid_records),
        }

        # 应用调整
        self._collector.apply_adjustments(category, adjustment)
        return adjustment

    def get_weights_for(self, category: str, default_weights: dict = None) -> dict:
        """获取品类定制权重（回退到默认）"""
        adj = self._collector.get_adjustments(category)
        if adj and adj.get("weights"):
            return adj["weights"]
        return default_weights or {}

    def get_thresholds_for(self, category: str, default_thresholds: dict = None) -> dict:
        """获取品类定制阈值"""
        adj = self._collector.get_adjustments(category)
        if adj and adj.get("thresholds"):
            return adj["thresholds"]
        return default_thresholds or {}

    def stats(self) -> dict:
        """进化统计"""
        records = self._collector._data.get("records", {})
        adjustments = self._collector._data.get("adjustments", {})
        return {
            "categories_with_data": len(records),
            "total_records": sum(len(v) for v in records.values()),
            "categories_evolved": len(adjustments),
            "details": {
                cat: {
                    "records": len(recs),
                    "has_evolution": cat in adjustments,
                    "evolved_at": adjustments.get(cat, {}).get("applied_at"),
                }
                for cat, recs in records.items()
            },
        }

    def connect_category_profiles(self, profile_manager):
        """
        连接品类配置管理器，让进化结果自动更新品类评分配置。

        Usage:
            evo = ScoringEvolution(client)
            profiles = CategoryProfileManager()
            evo.connect_category_profiles(profiles)
            # 之后每次 evolve() 都会自动更新品类配置
        """
        self._profile_manager = profile_manager

    def evolve_and_apply(self, category: str, product_type: str = None) -> Optional[dict]:
        """
        进化 + 自动应用到品类配置。
        product_type: 品类类型标识（如 'consumer_electronics'）
        """
        result = self.evolve(category)
        if result and hasattr(self, '_profile_manager'):
            pt = product_type or category
            new_weights = result.get("weights", {})
            if new_weights:
                self._profile_manager.evolve_weights(
                    pt, "keyword_scorer", new_weights,
                    source="scoring_evolution"
                )
        return result

    def export_report(self) -> str:
        """导出进化报告（Markdown）"""
        stats = self.stats()
        lines = [
            "# 品类评分进化报告",
            f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            f"## 概览",
            f"- 有数据品类: {stats['categories_with_data']}",
            f"- 总记录数: {stats['total_records']}",
            f"- 已进化品类: {stats['categories_evolved']}",
            "",
            "## 品类详情",
        ]

        for cat, detail in stats["details"].items():
            lines.append(f"\n### {cat}")
            lines.append(f"- 记录数: {detail['records']}")
            if detail["has_evolution"]:
                adj = self._collector.get_adjustments(cat)
                lines.append(f"- 进化时间: {detail['evolved_at']}")
                lines.append(f"- 新权重: {json.dumps(adj.get('weights', {}), ensure_ascii=False)}")
                lines.append(f"- 新阈值: {json.dumps(adj.get('thresholds', {}), ensure_ascii=False)}")
                lines.append(f"- 说明: {adj.get('note', '')}")
            else:
                lines.append("- 状态: 数据不足，未进化")

        return "\n".join(lines)
