"""
品类差异化评分配置 — 每品类独立权重/阈值/预检规则

不同品类选词和选品的逻辑完全不同:
  数码电子 → 品牌型号驱动，价格敏感
  文玩收藏 → 材质产地驱动，稀缺性溢价
  游戏账号 → 角色等级驱动，安全风险优先
  虚拟服务 → 技能质量驱动，评价导向
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Dict, Optional


# ── 默认配置（通用品类回退）─────────────────────────────────────
DEFAULT_PROFILE = {
    "keyword_scorer": {
        "weights": {
            "demand_scale": 0.20,
            "deal_efficiency": 0.30,
            "deal_quality": 0.20,
            "profit_certainty": 0.15,
            "competition": 0.10,
            "trend_signal": 0.05,
        },
        "grade_thresholds": {"S": 90, "A": 75, "B": 55, "C": 35, "D": 0},
    },
    "product_scorer": {
        "weights": {
            "demand_signal": 0.20,
            "price_advantage": 0.25,
            "seller_quality": 0.15,
            "item_condition": 0.15,
            "market_fit": 0.15,
            "logistics": 0.10,
        },
        "grade_thresholds": {"S": 90, "A": 75, "B": 55, "C": 40, "D": 0},
    },
    "precheck": {
        "min_uv": 200,
        "max_price_drop": -20,
    },
    "_note": "默认配置，适用于未专门配置的品类",
}


# ── 各品类差异化配置 ────────────────────────────────────────────
CATEGORY_PROFILES = {
    # ═══ 数码电子 ═══
    "consumer_electronics": {
        "keyword_scorer": {
            "weights": {
                "demand_scale": 0.15,       # 需求反而不那么重要（数码更新快）
                "deal_efficiency": 0.25,     # 成交效率最重要（快速周转）
                "deal_quality": 0.15,
                "profit_certainty": 0.25,    # 利润确定性（信息差大）
                "competition": 0.15,         # 竞争预估（品牌机竞争大）
                "trend_signal": 0.05,
            },
            "grade_thresholds": {"S": 88, "A": 72, "B": 52, "C": 32, "D": 0},
        },
        "product_scorer": {
            "weights": {
                "demand_signal": 0.15,
                "price_advantage": 0.35,     # 价格优势权重最高（比价容易）
                "seller_quality": 0.15,
                "item_condition": 0.10,
                "market_fit": 0.15,
                "logistics": 0.10,
            },
            "grade_thresholds": {"S": 88, "A": 72, "B": 52, "C": 40, "D": 0},
        },
        "precheck": {
            "min_uv": 500,                   # 数码品类流量大，UV门槛高
            "max_price_drop": -15,            # 降价幅度容忍度低（贬值快）
        },
        "_note": "数码电子: 品牌型号驱动，价格敏感，快周转。选品重价格优势，选词重利润确定性",
    },

    # ═══ 文玩收藏 ═══
    "collectibles": {
        "keyword_scorer": {
            "weights": {
                "demand_scale": 0.10,
                "deal_efficiency": 0.15,     # 成交效率低（小众）
                "deal_quality": 0.30,        # 成交质量最重要（真伪/品相）
                "profit_certainty": 0.25,    # 利润确定性（鉴定溢价）
                "competition": 0.05,         # 竞争不重要（稀缺品）
                "trend_signal": 0.15,        # 趋势信号（市场热度）
            },
            "grade_thresholds": {"S": 85, "A": 70, "B": 50, "C": 30, "D": 0},
        },
        "product_scorer": {
            "weights": {
                "demand_signal": 0.10,
                "price_advantage": 0.15,     # 价格不是核心（稀缺性更关键）
                "seller_quality": 0.30,      # 卖家质量最高（真伪鉴定）
                "item_condition": 0.25,       # 品相很重要
                "market_fit": 0.10,
                "logistics": 0.10,
            },
            "grade_thresholds": {"S": 85, "A": 70, "B": 50, "C": 40, "D": 0},
        },
        "precheck": {
            "min_uv": 50,                    # 文玩收藏UV低，门槛低
            "max_price_drop": -30,            # 涨价都正常
        },
        "_note": "文玩收藏: 材质产地驱动，稀缺性溢价。选品重卖家质量+品相，选词重成交质量",
    },

    # ═══ 游戏账号 ═══
    "game_accounts": {
        "keyword_scorer": {
            "weights": {
                "demand_scale": 0.25,        # 需求最重要（热度决定）
                "deal_efficiency": 0.20,
                "deal_quality": 0.20,        # 成交质量（大号/高价值）
                "profit_certainty": 0.15,
                "competition": 0.10,
                "trend_signal": 0.10,
            },
            "grade_thresholds": {"S": 88, "A": 72, "B": 52, "C": 32, "D": 0},
        },
        "product_scorer": {
            "weights": {
                "demand_signal": 0.20,
                "price_advantage": 0.25,
                "seller_quality": 0.30,      # 卖家质量最高（找回风险）
                "item_condition": 0.05,      # 品相不重要（虚拟商品）
                "market_fit": 0.10,
                "logistics": 0.10,           # 交付安全性
            },
            "grade_thresholds": {"S": 88, "A": 72, "B": 52, "C": 40, "D": 0},
        },
        "precheck": {
            "min_uv": 300,
            "max_price_drop": -25,
        },
        "_note": "游戏账号: 角色等级驱动，安全风险优先。选品重卖家质量(防找回)，选词重需求规模",
    },

    # ═══ 虚拟服务 ═══
    "virtual_services": {
        "keyword_scorer": {
            "weights": {
                "demand_scale": 0.25,
                "deal_efficiency": 0.20,
                "deal_quality": 0.30,        # 服务质量最重要（好评率）
                "profit_certainty": 0.10,
                "competition": 0.10,
                "trend_signal": 0.05,
            },
            "grade_thresholds": {"S": 85, "A": 70, "B": 50, "C": 30, "D": 0},
        },
        "product_scorer": {
            "weights": {
                "demand_signal": 0.15,
                "price_advantage": 0.20,
                "seller_quality": 0.35,      # 卖家质量最高（评价导向）
                "item_condition": 0.05,      # 品相不重要（服务）
                "market_fit": 0.10,
                "logistics": 0.15,           # 交付速度
            },
            "grade_thresholds": {"S": 85, "A": 70, "B": 50, "C": 40, "D": 0},
        },
        "precheck": {
            "min_uv": 100,
            "max_price_drop": -30,
        },
        "_note": "虚拟服务: 技能质量驱动，评价导向。选品重卖家质量，选词重服务质量",
    },

    # ═══ 知识付费 ═══
    "knowledge_products": {
        "keyword_scorer": {
            "weights": {
                "demand_scale": 0.20,
                "deal_efficiency": 0.15,
                "deal_quality": 0.30,        # 质量最重要（作者/时效性）
                "profit_certainty": 0.10,
                "competition": 0.15,
                "trend_signal": 0.10,
            },
            "grade_thresholds": {"S": 85, "A": 70, "B": 50, "C": 30, "D": 0},
        },
        "product_scorer": {
            "weights": {
                "demand_signal": 0.20,
                "price_advantage": 0.15,     # 知识付费价格不重要
                "seller_quality": 0.25,
                "item_condition": 0.10,
                "market_fit": 0.20,          # 市场匹配（考纲/年份）
                "logistics": 0.10,
            },
            "grade_thresholds": {"S": 85, "A": 70, "B": 50, "C": 40, "D": 0},
        },
        "precheck": {
            "min_uv": 50,
            "max_price_drop": -50,           # 知识付费定价灵活
        },
        "_note": "知识付费: 作者时效驱动，市场匹配优先。选品重市场匹配，选词重质量",
    },
}


class CategoryProfileManager:
    """品类差异化配置管理器 — 与评分进化联动"""

    def __init__(self, storage_path: Path = None):
        self._path = storage_path or Path("collected_data/category_profiles.json")
        self._profiles: Dict[str, dict] = {}
        self._load()

    def _load(self):
        """加载配置：默认先取内置配置，再从持久化文件覆盖"""
        self._profiles = dict(CATEGORY_PROFILES)
        if self._path.exists():
            try:
                saved = json.loads(self._path.read_text(encoding="utf-8"))
                for cat, profile in saved.items():
                    self._profiles[cat] = profile
            except Exception:
                pass

    def save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._profiles, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get(self, product_type: str = None) -> dict:
        """获取品类配置，找不到返回默认"""
        if product_type and product_type in self._profiles:
            return self._profiles[product_type]
        return DEFAULT_PROFILE

    def get_kw_weights(self, product_type: str = None) -> dict:
        return self.get(product_type)["keyword_scorer"]["weights"]

    def get_kw_thresholds(self, product_type: str = None) -> dict:
        return self.get(product_type)["keyword_scorer"]["grade_thresholds"]

    def get_pd_weights(self, product_type: str = None) -> dict:
        return self.get(product_type)["product_scorer"]["weights"]

    def get_pd_thresholds(self, product_type: str = None) -> dict:
        return self.get(product_type)["product_scorer"]["grade_thresholds"]

    def get_precheck(self, product_type: str = None) -> dict:
        return self.get(product_type)["precheck"]

    def evolve_weights(self, product_type: str, scorer: str,
                       new_weights: dict, source: str = "scoring_evolution"):
        """
        品类评分进化回调：ScoringEvolution 发现新权重时调用
        scorer: "keyword_scorer" | "product_scorer"
        """
        if product_type not in self._profiles:
            self._profiles[product_type] = json.loads(json.dumps(DEFAULT_PROFILE))

        old = self._profiles[product_type][scorer]["weights"]
        self._profiles[product_type][scorer]["weights"] = new_weights
        self._profiles[product_type]["_evolved_at"] = __import__('datetime').datetime.now().isoformat()
        self._profiles[product_type]["_evolution_source"] = source
        self.save()
        return {"old": old, "new": new_weights}

    def list_categories(self) -> list:
        return list(self._profiles.keys())

    def get_summary(self) -> dict:
        """所有品类的配置摘要"""
        return {
            cat: {
                "kw_weights": p.get("keyword_scorer", {}).get("weights", {}),
                "pd_weights": p.get("product_scorer", {}).get("weights", {}),
                "precheck": p.get("precheck", {}),
                "evolved": p.get("_evolved_at", False),
                "note": p.get("_note", ""),
            }
            for cat, p in self._profiles.items()
        }
