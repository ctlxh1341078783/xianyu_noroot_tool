"""
自主学习闭环引擎

大模型(DeepSeek)当老师 → 训练本地模型 → 越用越准

循环:
  采集数据 → DeepSeek高质量标注 → 积累训练样本 →
  定期用样本优化本地模型的使用方式(Prompt/规则) →
  本地模型越来越准 → 减少云端调用
"""
from __future__ import annotations
import json
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from engines.ai_client import AIClient


# ── 训练样本格式 ────────────────────────────────────────────────
class TrainingStore:
    """训练样本持久化存储"""

    def __init__(self, path: Path = None):
        self._path = path or Path("collected_data/training_samples.json")
        self._samples: List[dict] = self._load()

    def _load(self) -> list:
        if self._path.exists():
            try:
                return json.loads(self._path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return []

    def save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._samples, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def add(self, sample: dict):
        """添加一条训练样本"""
        sample["recorded_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._samples.append(sample)
        # 只保留最近500条
        if len(self._samples) > 500:
            self._samples = self._samples[-500:]
        self.save()

    def get_recent(self, n: int = 50) -> List[dict]:
        return self._samples[-n:]

    def get_good_examples(self, n: int = 20) -> List[dict]:
        """获取高质量样本（人工确认或高利润）"""
        good = [
            s for s in self._samples
            if s.get("quality") == "good" or (s.get("actual_profit") or 0) > 0
        ]
        return good[-n:]

    def get_bad_examples(self, n: int = 20) -> List[dict]:
        """获取低质量样本（AI误判或低利润）"""
        bad = [
            s for s in self._samples
            if s.get("quality") == "bad" or (
                (s.get("actual_profit") or 999) <= 0 and
                s.get("human_feedback") == "discard"
            )
        ]
        return bad[-n:]

    def stats(self) -> dict:
        good = len(self.get_good_examples(999))
        bad = len(self.get_bad_examples(999))
        return {
            "total": len(self._samples),
            "good": good,
            "bad": bad,
            "unlabeled": len(self._samples) - good - bad,
        }


# ── Prompt 优化器 ───────────────────────────────────────────────

TEACHER_SYSTEM = """你是AI训练专家。根据「好样本」和「坏样本」的对比，
分析当前提取规则的不足之处，给出改进建议。

核心原则:
1. 好样本中的规律要保留和强化
2. 坏样本中的错误模式要识别和规避
3. 改进建议要具体、可执行
4. 不要过度拟合样本，保持泛化能力

输出JSON:
{
  "strengths": ["当前规则做得好的地方"],
  "weaknesses": ["当前规则的不足"],
  "error_patterns": [
    {"pattern": "错误模式描述", "example": "具体例子", "fix": "修正方法"}
  ],
  "improved_rules": ["改进后的规则1", "改进后的规则2"],
  "new_prompt_additions": "可以追加到提取Prompt中的新指令",
  "confidence": 0.8
}"""


class SelfLearner:
    """
    自主学习引擎

    Usage:
        learner = SelfLearner(settings)
        learner.record_sample(title, ai_words, actual_profit=45)

        # 积累足够后触发学习
        if learner.store.stats()["total"] >= 10:
            improvements = learner.learn_from_samples()
            learner.apply_improvements(improvements)
    """

    def __init__(self, settings: dict, storage_dir: Path = None):
        self._settings = settings
        storage_dir = storage_dir or Path("collected_data")
        self.store = TrainingStore(storage_dir / "training_samples.json")
        self._improvements_path = storage_dir / "model_improvements.json"
        self._improvements = self._load_improvements()

    def _load_improvements(self) -> dict:
        if self._improvements_path.exists():
            try:
                return json.loads(self._improvements_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {
            "version": 1,
            "applied_rules": [],
            "error_patterns_learned": [],
            "history": [],
        }

    def save_improvements(self):
        self._improvements_path.write_text(
            json.dumps(self._improvements, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def record_sample(
        self,
        title: str,
        ai_extracted_words: List[str],
        human_feedback: str = None,  # "good" | "bad" | "discard"
        actual_profit: float = None,
        actual_sold: bool = None,
        category: str = "",
    ):
        """记录一条训练样本"""
        quality = None
        if human_feedback == "good" or (actual_profit and actual_profit > 0):
            quality = "good"
        elif human_feedback in ("bad", "discard"):
            quality = "bad"

        self.store.add({
            "title": title,
            "ai_extracted_words": ai_extracted_words,
            "human_feedback": human_feedback,
            "actual_profit": actual_profit,
            "actual_sold": actual_sold,
            "category": category,
            "quality": quality,
        })

    def learn_from_samples(self, cloud_client: AIClient = None) -> dict:
        """
        从积累的样本中学习改进。

        Args:
            cloud_client: 云端大模型客户端（DeepSeek），用于分析

        Returns:
            改进建议
        """
        good = self.store.get_good_examples(20)
        bad = self.store.get_bad_examples(20)

        if not good and not bad:
            return {"note": "样本不足，无法学习"}

        # 没有云端客户端则做简单统计
        if cloud_client is None:
            return self._simple_analysis(good, bad)

        # 用 DeepSeek 分析
        prompt = self._build_teacher_prompt(good, bad)

        try:
            result = cloud_client.chat_json(TEACHER_SYSTEM, prompt)
        except Exception as e:
            print(f"[SelfLearner] 学习分析失败: {e}")
            return self._simple_analysis(good, bad)

        # 记录历史
        self._improvements["history"].append({
            "version": self._improvements["version"],
            "learned_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "sample_stats": self.store.stats(),
            "result": result,
        })
        self._improvements["version"] += 1

        # 提取可应用的规则
        if result.get("error_patterns"):
            self._improvements["error_patterns_learned"].extend(
                result["error_patterns"]
            )

        self.save_improvements()
        return result

    def _build_teacher_prompt(self, good: list, bad: list) -> str:
        """构建教师分析 Prompt"""
        lines = [
            f"好样本 ({len(good)}条) — 这些是正确的提取结果:",
        ]
        for s in good[:10]:
            lines.append(
                f"  标题: {s.get('title', '')[:60]}\n"
                f"  提取: {s.get('ai_extracted_words', [])}"
            )

        lines.append(f"\n坏样本 ({len(bad)}条) — 这些是有问题的提取结果:")
        for s in bad[:10]:
            lines.append(
                f"  标题: {s.get('title', '')[:60]}\n"
                f"  提取: {s.get('ai_extracted_words', [])}\n"
                f"  反馈: {s.get('human_feedback', '')}"
            )

        lines.append("\n请分析并给出改进建议。返回JSON。")
        return "\n".join(lines)

    def _simple_analysis(self, good: list, bad: list) -> dict:
        """简单统计分析（无云端时的降级）"""
        good_words = set()
        for s in good:
            good_words.update(s.get("ai_extracted_words", []))

        bad_words = set()
        for s in bad:
            bad_words.update(s.get("ai_extracted_words", []))

        # 在好样本中出现、坏样本中不出现的 → 好规则
        only_good = good_words - bad_words
        # 在坏样本中出现、好样本中不出现的 → 坏模式
        only_bad = bad_words - good_words

        return {
            "strengths": [f"正确提取了 {len(only_good)} 个独特词"],
            "weaknesses": [f"错误提取了 {len(only_bad)} 个词"],
            "error_patterns": [
                {"pattern": w, "example": w, "fix": f"避免提取'{w}'"}
                for w in list(only_bad)[:5]
            ],
            "improved_rules": [
                f"优先提取: {', '.join(list(only_good)[:10])}" if only_good else "",
                f"避免提取: {', '.join(list(only_bad)[:10])}" if only_bad else "",
            ],
            "new_prompt_additions": "",
            "confidence": 0.3,
            "method": "rule_statistics",
        }

    def get_improved_prompt_additions(self) -> str:
        """
        获取当前学到的 Prompt 改进追加内容。
        可以追加到 AI 提取的 System Prompt 后面。
        """
        additions = []
        for imp in self._improvements.get("history", [])[-3:]:
            additions.append(
                imp.get("result", {}).get("new_prompt_additions", "")
            )
        return "\n".join(a for a in additions if a)

    def get_learned_rules(self) -> List[str]:
        """获取已学习的规则列表"""
        return self._improvements.get("applied_rules", [])

    def get_error_patterns(self) -> List[dict]:
        """获取已识别的错误模式"""
        return self._improvements.get("error_patterns_learned", [])[-20:]

    def stats(self) -> dict:
        """学习状态"""
        return {
            "samples": self.store.stats(),
            "improvements_version": self._improvements.get("version", 1),
            "learned_patterns": len(self._improvements.get("error_patterns_learned", [])),
            "history_count": len(self._improvements.get("history", [])),
        }

    def export_learning_report(self) -> str:
        """导出学习报告（Markdown）"""
        stats = self.stats()
        lines = [
            "# 自主学习报告",
            f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "## 样本统计",
            f"- 总样本: {stats['samples']['total']}",
            f"- 好样本: {stats['samples']['good']}",
            f"- 坏样本: {stats['samples']['bad']}",
            "",
            "## 已学到的规则",
        ]
        for ep in self.get_error_patterns():
            lines.append(
                f"- **{ep.get('pattern', '')}** → {ep.get('fix', '')}"
            )
        return "\n".join(lines)
