"""
全自动采集管线管理器

闭环: 词库pass词 → 采集 → 飞轮Phase B/C → 新词入库 → 下一轮自动包含
"""
from __future__ import annotations
import json, time, threading
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional


class PipelineManager:
    """
    全自动管线: 词库驱动 → 批量采集 → 飞轮膨胀 → 循环

    Usage:
        mgr = PipelineManager(settings, output_dir)
        keywords = mgr.get_all_keywords()  # 种子词 + pass词 去重
        mgr.run_cycle(keywords)            # 跑一轮采集+飞轮
        # mgr.schedule_cycle()             # 定时循环（未来）
    """

    def __init__(self, settings: dict, output_dir: Path = None):
        self._settings = settings
        self._output_dir = output_dir or Path("collected_data")
        self._output_dir.mkdir(parents=True, exist_ok=True)

        # 加载种子词
        self._seed_keywords = self._load_seed_keywords()
        # 加载词库 pass 词
        self._pass_words = self._load_pass_words()

    def _load_seed_keywords(self) -> List[str]:
        """加载种子词库"""
        seed_path = Path(__file__).parent / "seed_keywords.json"
        try:
            with open(seed_path) as f:
                seeds = json.load(f)
            return seeds.get("flat_keywords", [])
        except Exception:
            return []

    def _load_pass_words(self) -> List[str]:
        """加载词库中 status=pass 的词"""
        wl_path = self._output_dir / "word_library.json"
        if not wl_path.exists():
            return []
        try:
            with open(wl_path) as f:
                wl = json.load(f)
            words = wl.get("words", {})
            return [
                w for w, info in words.items()
                if info.get("status") == "pass"
            ]
        except Exception:
            return []

    def get_all_keywords(self, min_composite: float = 0) -> List[str]:
        """
        获取所有待搜索关键词: 种子词 + pass词，去重。

        Args:
            min_composite: 最低综合分（筛选高质量pass词）

        Returns:
            去重后的关键词列表
        """
        # 重新加载 pass 词（可能是新跑出来的）
        self._pass_words = self._load_pass_words()

        # 如果有分数要求，过滤低分 pass 词
        if min_composite > 0:
            wl_path = self._output_dir / "word_library.json"
            filtered = []
            try:
                with open(wl_path) as f:
                    wl = json.load(f)
                words = wl.get("words", {})
                for w in self._pass_words:
                    comp = float(str(words[w].get("composite", 0)))
                    if comp >= min_composite:
                        filtered.append(w)
                self._pass_words = filtered
            except Exception:
                pass

        # 合并去重（pass词优先，种子词补充）
        all_words = list(dict.fromkeys(self._pass_words + self._seed_keywords))
        return all_words

    def get_category_distribution(self) -> dict:
        """关键词品类分布统计"""
        from collections import Counter
        seed_path = Path(__file__).parent / "seed_keywords.json"
        cat_map = {}
        try:
            with open(seed_path) as f:
                seeds = json.load(f)
            for cat_name, cat_data in seeds.get("categories", {}).items():
                for kw in cat_data.get("keywords", []):
                    cat_map[kw] = cat_name
        except Exception:
            pass

        dist = Counter()
        for kw in self.get_all_keywords():
            cat = cat_map.get(kw, "飞轮产出")
            dist[cat] += 1

        return dict(dist)

    def get_pipeline_stats(self) -> dict:
        """管线统计"""
        wl_path = self._output_dir / "word_library.json"
        wl_stats = {}
        if wl_path.exists():
            try:
                with open(wl_path) as f:
                    wl = json.load(f)
                words = wl.get("words", {})
                wl_stats = {
                    "total": len(words),
                    "pass": sum(1 for i in words.values() if i.get("status") == "pass"),
                    "watch": sum(1 for i in words.values() if i.get("status") == "watch"),
                    "pending": sum(1 for i in words.values() if i.get("status") == "pending_verify"),
                    "discard": sum(1 for i in words.values() if i.get("status") == "discard"),
                }
            except Exception:
                pass

        all_kw = self.get_all_keywords()
        return {
            "seed_keywords": len(self._seed_keywords),
            "pass_words": len(self._pass_words),
            "total_search_keywords": len(all_kw),
            "word_library": wl_stats,
            "category_distribution": self.get_category_distribution(),
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    def export_keyword_list(self) -> Path:
        """导出当前搜索关键词列表到文件"""
        keywords = self.get_all_keywords()
        path = self._output_dir / "_search_keywords.json"
        data = {
            "total": len(keywords),
            "seeds": self._seed_keywords,
            "pass_words": self._pass_words,
            "all_keywords": keywords,
            "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        return path

    def run_cycle(
        self,
        keywords: List[str] = None,
        search_pages: int = 3,
        detail_max: int = 3,
        run_flywheel: bool = True,
    ) -> dict:
        """
        跑一轮采集 + 飞轮。

        Args:
            keywords: 关键词列表（None则自动获取全部）
            search_pages: 每词搜索页数
            detail_max: 每词详情数
            run_flywheel: 是否跑飞轮

        Returns:
            采集结果 + 飞轮结果
        """
        if keywords is None:
            keywords = self.get_all_keywords()

        # 动态修改 settings
        run_settings = json.loads(json.dumps(self._settings))
        run_settings["collection"]["search_pages"] = search_pages
        run_settings["collection"]["detail_max"] = detail_max
        run_settings["collection"]["kw_push_threshold"] = 30
        run_settings["collection"]["pd_push_threshold"] = 30

        from engines.device_engine import DeviceEngine
        from engines.collection_engine import CollectionEngine
        from engines.keyword_scorer_v3 import KeywordScorerV3
        from engines.product_scorer_v3 import ProductScorerV3

        # 连接设备
        dev_engine = DeviceEngine(self._settings)
        devices = dev_engine.list_devices()
        if not devices:
            return {"error": "无设备"}

        target = devices[0]
        state = dev_engine.connect(target.adb_addr)
        if not state.connected:
            return {"error": "设备连接失败"}

        # 评分引擎
        scorer_cfg = {
            "keyword_model": {"grades": {"S": 90, "A": 30, "B": 20, "C": 10, "D": 0}},
            "product_model": {"grades": {"S": 90, "A": 30, "B": 20, "C": 10, "D": 0}},
        }
        kw_scorer = KeywordScorerV3(scorer_cfg)
        pd_scorer = ProductScorerV3(scorer_cfg)

        # 采集
        engine = CollectionEngine(dev_engine, run_settings)
        engine.set_scorers(kw_scorer, pd_scorer)

        done_event = threading.Event()
        cycle_result = {}

        def on_complete(kw_results, pd_results, supply_pushed):
            cycle_result["kw_results"] = kw_results
            cycle_result["pd_results"] = pd_results
            cycle_result["supply_pushed"] = supply_pushed
            done_event.set()

        engine.set_callbacks(on_complete=on_complete)
        engine.start(keywords, output_dir=self._output_dir)
        done_event.wait(timeout=7200)

        result = {
            "keywords_total": len(keywords),
            "keywords_processed": len(cycle_result.get("kw_results", [])),
            "products_scored": len(cycle_result.get("pd_results", [])),
            "supply_pushed": len(cycle_result.get("supply_pushed", [])),
            "collected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        # 飞轮
        if run_flywheel:
            result["flywheel"] = self._run_flywheel()

        return result

    def _run_flywheel(self) -> dict:
        """运行飞轮 Phase B + C"""
        from engines.flywheel_engine import FlywheelEngine

        fw = FlywheelEngine(self._settings, output_dir=self._output_dir)

        # 收集所有有搜索数据的关键词
        keyword_data = []
        for f in sorted(self._output_dir.glob("*.json")):
            name = f.name
            if name.startswith("_"): continue
            skip = ("flywheel_results.json", "word_library.json")
            if name in skip: continue
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

        if not keyword_data:
            return {"error": "无搜索数据"}

        result = fw.run_phase_b_batch(keyword_data)
        s = result["summary"]
        return {
            "keywords_processed": s.get("keywords_processed", 0),
            "pass_words": s.get("pass_words", 0),
            "watch_words": s.get("watch_words", 0),
            "pending_words": s.get("pending_words", 0),
            "new_materials": s.get("new_materials", 0),
            "word_library_stats": result.get("word_library_stats", {}),
        }
