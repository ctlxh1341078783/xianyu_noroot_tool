"""分析看板Tab：统一漏斗视图 — 预检→选词评分→商品评分，全部带评分依据"""
import tkinter as tk
from tkinter import ttk
import threading
from pathlib import Path

from gui.theme import SURF, SURF2, FG, FG_M, ACC, SUCC, DANGER, WARN, BRD, GRADE_COLORS, FONTS
from gui.widgets.tree_helpers import make_columns

DIM_NAMES = {
    "demand_scale": "需求规模",
    "deal_efficiency": "成交效率",
    "deal_quality": "成交质量",
    "profit_certainty": "利润确定性",
    "competition": "竞争格局",
    "trend_signal": "趋势信号",
    "demand_signal": "需求信号",
    "price_advantage": "价格优势",
    "seller_verification": "卖家验证",
    "timeliness": "时效性",
    "supply_attribute": "货源属性",
    "item_quality": "商品质量",
}

DIM_MAX = {
    "demand_scale": 20,
    "deal_efficiency": 30,
    "deal_quality": 20,
    "profit_certainty": 25,
    "competition": 15,
    "trend_signal": 10,
    "demand_signal": 20,
    "price_advantage": 25,
    "seller_verification": 25,
    "timeliness": 15,
    "supply_attribute": 15,
    "item_quality": 10,
}


class DashboardTab:
    def __init__(self, parent: ttk.Frame, app):
        self.parent = parent
        self.app = app
        self._kw_scorer = None
        self._pd_scorer = None
        self._kw_results = []
        self._pd_results = []
        self._build_ui()

    def set_scorers(self, kw_scorer, pd_scorer):
        self._kw_scorer = kw_scorer
        self._pd_scorer = pd_scorer

    def _build_ui(self):
        # 控制栏
        ctrl = tk.Frame(self.parent, bg=SURF)
        ctrl.pack(fill=tk.X, padx=8, pady=(8, 4))

        ttk.Label(ctrl, text="搜索关键词:", font=FONTS["ui"]).pack(side=tk.LEFT)
        self._filter_var = tk.StringVar()
        filter_entry = ttk.Entry(ctrl, textvariable=self._filter_var, width=16, font=FONTS["ui"])
        filter_entry.pack(side=tk.LEFT, padx=4)
        filter_entry.bind("<Return>", lambda e: self._refresh())
        ttk.Button(ctrl, text="搜索", command=self._refresh, width=5).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(ctrl, text="清除", command=self._clear_filter, width=5).pack(side=tk.LEFT)

        ttk.Label(ctrl, text="等级:", font=FONTS["ui"]).pack(side=tk.LEFT, padx=(8, 2))
        self._grade_var = tk.StringVar(value="全部")
        grade_combo = ttk.Combobox(ctrl, textvariable=self._grade_var, values=["全部", "S", "A", "B", "C", "D"],
                     width=5, state="readonly")
        grade_combo.pack(side=tk.LEFT)
        grade_combo.bind("<<ComboboxSelected>>", lambda e: self._refresh())

        ttk.Button(ctrl, text="导入数据", command=self._import_data).pack(side=tk.LEFT, padx=8)
        ttk.Button(ctrl, text="导出Excel", command=self._export).pack(side=tk.LEFT)

        self._data_source_label = tk.Label(ctrl, text="", font=FONTS["ui"], fg=ACC, bg=SURF)
        self._data_source_label.pack(side=tk.LEFT, padx=15)

        # 漏斗概览
        self._funnel_frame = tk.LabelFrame(self.parent, text="漏斗概览", font=FONTS["ui_bold"], padx=6, pady=4)
        self._funnel_frame.pack(fill=tk.X, padx=8, pady=4)

        self._funnel_text = tk.Label(self._funnel_frame, text="暂无数据", font=FONTS["ui"], fg=FG_M)
        self._funnel_text.pack(pady=4)

        self._funnel_canvas = tk.Canvas(self._funnel_frame, height=30, bg=SURF, highlightthickness=0)
        self._funnel_canvas.pack(fill=tk.X, padx=10, pady=(0, 6))

        # 选词结果
        kw_frame = tk.LabelFrame(self.parent, text="选词结果（点击展开评分依据）", font=FONTS["ui_bold"], padx=4, pady=4)
        kw_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=2)

        kw_cols = [
            ("关键词", 120, "w"),
            ("总分", 50, "center"),
            ("等级", 40, "center"),
            ("需求", 40, "center"),
            ("效率", 40, "center"),
            ("质量", 40, "center"),
            ("利润", 40, "center"),
            ("竞争", 40, "center"),
            ("趋势", 40, "center"),
        ]
        self._kw_tree = ttk.Treeview(kw_frame, columns=[c[0] for c in kw_cols], show="headings")
        make_columns(self._kw_tree, kw_cols)
        self._kw_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        kw_sb = ttk.Scrollbar(kw_frame, orient=tk.VERTICAL, command=self._kw_tree.yview)
        self._kw_tree.configure(yscrollcommand=kw_sb.set)
        kw_sb.pack(side=tk.RIGHT, fill=tk.Y)

        self._kw_tree.bind("<<TreeviewSelect>>", self._on_kw_select)

        # 选词详情
        kw_detail = tk.LabelFrame(self.parent, text="评分依据", font=FONTS["ui_bold"], padx=4, pady=4)
        kw_detail.pack(fill=tk.X, padx=8, pady=2)

        self._kw_detail_text = tk.Text(kw_detail, height=5, font=FONTS["mono"], wrap=tk.WORD,
                                       bg=SURF2, fg=FG, relief=tk.FLAT)
        self._kw_detail_text.pack(fill=tk.BOTH)

        # 商品结果
        pd_frame = tk.LabelFrame(self.parent, text="商品结果（全部已评分商品）", font=FONTS["ui_bold"], padx=4, pady=4)
        pd_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=2)

        pd_cols = [
            ("商品标题", 200, "w"),
            ("总分", 50, "center"),
            ("等级", 40, "center"),
            ("需求", 40, "center"),
            ("价格", 40, "center"),
            ("卖家", 40, "center"),
            ("时效", 40, "center"),
            ("货源", 40, "center"),
            ("质量", 40, "center"),
        ]
        self._pd_tree = ttk.Treeview(pd_frame, columns=[c[0] for c in pd_cols], show="headings")
        make_columns(self._pd_tree, pd_cols)
        self._pd_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        pd_sb = ttk.Scrollbar(pd_frame, orient=tk.VERTICAL, command=self._pd_tree.yview)
        self._pd_tree.configure(yscrollcommand=pd_sb.set)
        pd_sb.pack(side=tk.RIGHT, fill=tk.Y)

        self._pd_tree.bind("<<TreeviewSelect>>", self._on_pd_select)

        # 商品详情
        pd_detail = tk.LabelFrame(self.parent, text="商品评分依据", font=FONTS["ui_bold"], padx=4, pady=4)
        pd_detail.pack(fill=tk.X, padx=8, pady=2)

        self._pd_detail_text = tk.Text(pd_detail, height=5, font=FONTS["mono"], wrap=tk.WORD,
                                       bg=SURF2, fg=FG, relief=tk.FLAT)
        self._pd_detail_text.pack(fill=tk.BOTH)

        # 等级标签颜色
        for grade, (bg, fg) in GRADE_COLORS.items():
            self._kw_tree.tag_configure(f"g_{grade}", background=bg, foreground=fg)
            self._pd_tree.tag_configure(f"g_{grade}", background=bg, foreground=fg)

    def load_results(self, kw_results: list, pd_results: list = None):
        """加载评分结果到看板（采集完成后自动调用，包含全部数据）"""
        self._kw_results = kw_results or []
        self._pd_results = pd_results or []
        self._data_source_label.configure(
            text=f"共 {len(self._kw_results)} 词 / {len(self._pd_results)} 商品（全部采集数据）")
        self._refresh()
        # 自动同步到图表分析
        charts = self.app.get_tab("图表分析")
        if charts and hasattr(charts, 'load_data'):
            charts.load_data(self._kw_results, self._pd_results)

    def _refresh(self):
        self._draw_funnel()
        self._populate_kw_tree()
        self._populate_pd_tree()

    def _clear_filter(self):
        self._filter_var.set("")
        self._grade_var.set("全部")
        self._refresh()

    def _draw_funnel(self):
        kw = self._kw_results
        total = len(kw)
        if total == 0:
            self._funnel_text.configure(text="暂无数据 — 采集完成后自动加载，或点击「导入数据」加载历史结果")
            self._funnel_canvas.delete("all")
            return

        passed = sum(1 for r in kw if r.get("total_100", 0) >= 75)
        s_top = sum(1 for r in kw if r.get("grade") == "S")
        a_top = sum(1 for r in kw if r.get("grade") == "A")
        n_a = sum(1 for r in kw if r.get("grade") == "N/A")

        self._funnel_text.configure(
            text=f"全部 {total} 词 | S级 {s_top} 个 | A级 {a_top} 个 | A+共 {passed} 个 | 淘汰 {n_a} 个"
        )

        w = self._funnel_canvas.winfo_width()
        if w < 50:
            return
        self._funnel_canvas.delete("all")

        bar_w = int(w * 0.8)
        bar_x = int(w * 0.1)
        self._funnel_canvas.create_rectangle(bar_x, 5, bar_x + bar_w, 25, fill="#F3F4F6", outline=BRD)
        fill_w = int(bar_w * passed / total) if total > 0 else 0
        if fill_w > 0:
            self._funnel_canvas.create_rectangle(bar_x, 5, bar_x + fill_w, 25, fill=SUCC, outline="")
        self._funnel_canvas.create_text(w / 2, 15, text=f"A+占比 {passed}/{total} ({passed/total*100:.0f}%)" if total > 0 else "", font=FONTS["ui"])

    def _populate_kw_tree(self):
        self._kw_tree.delete(*self._kw_tree.get_children())
        grade_filter = self._grade_var.get()
        kw_filter = self._filter_var.get().strip().lower()

        for r in self._kw_results:
            grade = r.get("grade", "N/A")
            if grade_filter != "全部" and grade != grade_filter:
                continue
            kw_name = r.get("keyword", "")
            if kw_filter and kw_filter not in kw_name.lower():
                continue

            scores = r.get("scores", {})
            vals = (
                kw_name,
                r.get("total_100", 0),
                grade,
                int(scores.get("demand_scale", 0)),
                int(scores.get("deal_efficiency", 0)),
                int(scores.get("deal_quality", 0)),
                int(scores.get("profit_certainty", 0)),
                int(scores.get("competition", 0)),
                int(scores.get("trend_signal", 0)),
            )
            self._kw_tree.insert("", tk.END, values=vals, tags=(f"g_{grade}",))

    def _populate_pd_tree(self):
        self._pd_tree.delete(*self._pd_tree.get_children())
        for r in self._pd_results:
            grade = r.get("grade", "N/A")
            scores = r.get("scores", {})
            vals = (
                r.get("title", "")[:40],
                r.get("total_100", 0),
                grade,
                int(scores.get("demand_signal", 0)),
                int(scores.get("price_advantage", 0)),
                int(scores.get("seller_verification", 0)),
                int(scores.get("timeliness", 0)),
                int(scores.get("supply_attribute", 0)),
                int(scores.get("item_quality", 0)),
            )
            self._pd_tree.insert("", tk.END, values=vals, tags=(f"g_{grade}",))

    def _on_kw_select(self, event):
        sel = self._kw_tree.selection()
        if not sel:
            return
        vals = self._kw_tree.item(sel[0], "values")
        kw_name = vals[0]

        r = next((r for r in self._kw_results if r.get("keyword") == kw_name), None)
        if not r:
            return

        lines = [f"关键词: {kw_name}  总分: {r.get('total_100', 0)}  等级: {r.get('grade', 'N/A')}", ""]
        scores = r.get("scores", {})
        for dim_key, dim_name in DIM_NAMES.items():
            val = scores.get(dim_key, 0)
            mx = DIM_MAX.get(dim_key, 1)
            pct = val / mx * 100 if mx > 0 else 0
            bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
            lines.append(f"  {dim_name}: {val}/{mx}  [{bar}]")

        if r.get("avg_price"):
            lines.extend(["", f"  均价: ¥{r['avg_price']:.1f}  "
                         f"不砍价率: {r.get('no_bargain_rate', 0):.1%}  "
                         f"当天成交率: {r.get('same_day_rate', 0):.1%}"])

        self._kw_detail_text.configure(state=tk.NORMAL)
        self._kw_detail_text.delete("1.0", tk.END)
        self._kw_detail_text.insert(tk.END, "\n".join(lines))
        self._kw_detail_text.configure(state=tk.DISABLED)

    def _on_pd_select(self, event):
        sel = self._pd_tree.selection()
        if not sel:
            return
        title = self._pd_tree.item(sel[0], "values")[0]
        r = next((r for r in self._pd_results if r.get("title", "")[:40] == title), None)
        if not r:
            return

        lines = [f"商品: {r.get('title', '')[:60]}", f"总分: {r.get('total_100', 0)}  等级: {r.get('grade', 'N/A')}", ""]
        scores = r.get("scores", {})
        for dim_key in ["demand_signal", "price_advantage", "seller_verification", "timeliness", "supply_attribute", "item_quality"]:
            val = scores.get(dim_key, 0)
            mx = DIM_MAX.get(dim_key, 1)
            pct = val / mx * 100 if mx > 0 else 0
            bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
            dim_name = DIM_NAMES.get(dim_key, dim_key)
            lines.append(f"  {dim_name}: {val} [{bar}]")

        seller = r.get("seller", {}) or {}
        if seller:
            lines.extend(["", f"  卖家: 已售{seller.get('hasSoldNum', '?')} "
                         f"好评{seller.get('goodRatio', '?')} "
                         f"回复率{seller.get('reply24h', '?')}"])

        self._pd_detail_text.configure(state=tk.NORMAL)
        self._pd_detail_text.delete("1.0", tk.END)
        self._pd_detail_text.insert(tk.END, "\n".join(lines))
        self._pd_detail_text.configure(state=tk.DISABLED)

    def _import_data(self):
        """从采集数据目录导入并重新评分"""
        from tkinter import filedialog, messagebox
        import json

        default_dir = Path.home() / ".xianyu_tool" / "collected_data"

        # 默认目录存在且有JSON文件，直接导入；否则打开文件选择
        json_files = list(default_dir.glob("*.json")) if default_dir.exists() else []
        if json_files:
            # 直接使用默认目录
            self.app.logger.info(f"从 {default_dir} 导入数据并评分 ({len(json_files)} 个文件)...")
            self._data_source_label.configure(text=f"正在导入 {len(json_files)} 个文件...")
            threading.Thread(target=self._do_import_and_score, args=(default_dir,), daemon=True).start()
            return

        # 默认目录没有文件，弹出文件选择
        dir_path = filedialog.askdirectory(
            title="选择采集数据目录（JSON文件所在目录）",
            initialdir=str(default_dir.parent) if default_dir.parent.exists() else str(Path.home()),
        )
        if not dir_path:
            return

        self.app.logger.info(f"从 {dir_path} 导入数据并评分...")
        threading.Thread(target=self._do_import_and_score, args=(Path(dir_path),), daemon=True).start()

    def _do_import_and_score(self, data_dir: Path):
        import json
        kw_results = []
        pd_results = []

        # 优先读取汇总文件
        summary_file = data_dir / "_pipeline_summary.json"
        if summary_file.exists():
            try:
                data = json.loads(summary_file.read_text(encoding="utf-8"))
                kw_results = data.get("keywords", [])
                pd_results = data.get("products", [])
                self.app.logger.info(f"[看板] 从汇总文件加载: {len(kw_results)}词 {len(pd_results)}商品")
            except Exception as e:
                self.app.logger.error(f"[看板] 汇总文件读取失败: {e}")

        # 汇总文件不存在或为空，遍历单词JSON
        if not kw_results:
            for f in sorted(data_dir.glob("*.json")):
                if f.name.startswith("_"):
                    continue
                try:
                    item = json.loads(f.read_text(encoding="utf-8"))
                except Exception:
                    continue
                kw = item.get("keyword", f.stem)
                if kw and "total_100" in item:
                    kw_results.append(item)

        self.app.root.after(0, lambda: self.load_results(kw_results, pd_results))
        self.app.logger.info(f"[看板] 导入完成: {len(kw_results)} 词 / {len(pd_results)} 商品")

    def _export(self):
        from tkinter import filedialog, messagebox
        file_path = filedialog.asksaveasfilename(
            title="导出Excel",
            defaultextension=".xlsx",
            filetypes=[("Excel文件", "*.xlsx")],
        )
        if not file_path:
            return
        try:
            from exporter.excel_exporter import ExcelExporter
            ExcelExporter.export_dashboard(file_path, self._kw_results, self._pd_results)
            messagebox.showinfo("成功", f"已导出到 {file_path}")
        except ImportError:
            messagebox.showerror("失败", "Excel导出模块暂不可用")
