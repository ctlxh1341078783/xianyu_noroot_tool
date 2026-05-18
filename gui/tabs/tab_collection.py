"""数据采集Tab：关键词输入 + 参数配置 + 横向漏斗 + 排序列表 + 开始/停止"""
import re
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path

from gui.theme import SURF, SURF2, FG, FG_M, ACC, SUCC, DANGER, BRD, FONTS
from gui.widgets.funnel_progress import FunnelProgress


class CollectionTab:
    def __init__(self, parent: ttk.Frame, app):
        self.parent = parent
        self.app = app
        self._engine = None
        self._sort_state = {}
        self._market_sort_mode = {}  # "uv" or "trend"
        self._build_ui()

    def set_engine(self, engine):
        self._engine = engine

    def _build_ui(self):
        # 上方：关键词输入 + 参数
        top = tk.Frame(self.parent, bg=SURF)
        top.pack(fill=tk.X, padx=8, pady=(8, 4))

        tk.Label(top, text="关键词:", font=FONTS["ui_bold"]).pack(side=tk.LEFT)
        self._kw_text = tk.Text(top, height=3, width=40, font=FONTS["ui"], wrap=tk.WORD,
                                relief=tk.SOLID, borderwidth=1)
        self._kw_text.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)
        self._kw_text.insert(tk.END, "手机壳\n蓝牙耳机\n机械键盘")

        ttk.Button(top, text="导入", command=self._import_keywords, width=6).pack(side=tk.LEFT, padx=(4, 0))

        # 参数快速设置
        params = tk.Frame(top, bg=SURF)
        params.pack(side=tk.RIGHT, padx=4)

        ttk.Label(params, text="搜索页:", font=FONTS["ui"]).pack(side=tk.LEFT)
        self._pages_var = tk.IntVar(value=10)
        ttk.Spinbox(params, from_=1, to=30, textvariable=self._pages_var, width=4).pack(side=tk.LEFT, padx=(2, 8))

        ttk.Label(params, text="详情数:", font=FONTS["ui"]).pack(side=tk.LEFT)
        self._detail_var = tk.IntVar(value=5)
        ttk.Spinbox(params, from_=0, to=20, textvariable=self._detail_var, width=4).pack(side=tk.LEFT, padx=(2, 8))

        ttk.Label(params, text="评论数:", font=FONTS["ui"]).pack(side=tk.LEFT)
        self._comment_var = tk.IntVar(value=3)
        ttk.Spinbox(params, from_=0, to=10, textvariable=self._comment_var, width=4).pack(side=tk.LEFT, padx=2)

        # 控制按钮
        ctrl = tk.Frame(self.parent, bg=SURF)
        ctrl.pack(fill=tk.X, padx=8, pady=4)

        self._start_btn = ttk.Button(ctrl, text="开始采集", command=self._start)
        self._start_btn.pack(side=tk.LEFT, padx=(0, 8))

        self._stop_btn = ttk.Button(ctrl, text="停止", command=self._stop, state=tk.DISABLED)
        self._stop_btn.pack(side=tk.LEFT)

        self._progress_label = ttk.Label(ctrl, text="就绪", font=FONTS["ui"], foreground=FG_M)
        self._progress_label.pack(side=tk.LEFT, padx=15)

        # 下方：左右并排 — 漏斗(左) + 关键词列表(右)
        bottom = tk.Frame(self.parent, bg=SURF)
        bottom.pack(fill=tk.BOTH, expand=True, padx=8, pady=2)

        # 左：漏斗
        left = tk.Frame(bottom, bg=SURF, width=320)
        left.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 6))
        left.pack_propagate(False)

        self._funnel = FunnelProgress(left)
        self._funnel.pack(fill=tk.X, pady=2)

        # 右：关键词列表
        list_frame = tk.LabelFrame(bottom, text="采集详情", font=FONTS["ui_bold"], padx=4, pady=4)
        list_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        columns = [
            ("关键词", 120, "w"),
            ("搜索数", 55, "center"),
            ("行情(UV/涨跌)", 115, "center"),
            ("详情", 45, "center"),
            ("评论", 45, "center"),
            ("状态", 95, "center"),
        ]
        from gui.widgets.tree_helpers import make_columns
        self._tree = ttk.Treeview(list_frame, columns=[c[0] for c in columns], show="headings")
        make_columns(self._tree, columns)

        # 绑定列头点击排序
        self._setup_sortable()

        vsb = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self._tree.yview)
        hsb = ttk.Scrollbar(list_frame, orient=tk.HORIZONTAL, command=self._tree.xview)
        self._tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self._tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        list_frame.grid_rowconfigure(0, weight=1)
        list_frame.grid_columnconfigure(0, weight=1)

    # ════════════════════════════════════════════════
    # 表头排序
    # ════════════════════════════════════════════════
    def _setup_sortable(self):
        numeric_cols = {"搜索数", "详情", "评论"}
        self._sort_state = {}
        self._market_sort_mode = {}

        def _parse_market(text, mode="uv"):
            """解析行情单元格: '6634UV ↓1' → UV=6634, trend=-1"""
            if not text or text in ("-", "无行情", ""):
                return 0
            uv_match = re.search(r'(\d+)UV', text)
            if mode == "uv":
                return int(uv_match.group(1)) if uv_match else 0
            else:
                trend_match = re.search(r'([↓↑→])\s*(\d+)', text)
                if trend_match:
                    sign = -1 if trend_match.group(1) == "↓" else (1 if trend_match.group(1) == "↑" else 0)
                    return sign * int(trend_match.group(2))
                return 0

        def sort_col(col):
            # 切换行情列的排序模式
            if col == "行情(UV/涨跌)":
                cur = self._market_sort_mode.get(col, "uv")
                self._market_sort_mode[col] = "trend" if cur == "uv" else "uv"

            reverse = self._sort_state.get(col, False)
            self._sort_state[col] = not reverse

            # 读取所有行
            rows = []
            for k in self._tree.get_children(""):
                vals = self._tree.item(k, "values")
                rows.append((k, vals))

            # 获取列索引
            col_names = [c[0] for c in [
                ("关键词",), ("搜索数",), ("行情(UV/涨跌)",), ("详情",), ("评论",), ("状态",)
            ]]
            try:
                col_idx = col_names.index(col)
            except ValueError:
                col_idx = 0

            # 排序键
            def key_fn(row):
                text = str(row[1][col_idx]) if col_idx < len(row[1]) else ""
                if col in numeric_cols:
                    try:
                        return float(text)
                    except ValueError:
                        return -99999
                elif col == "行情(UV/涨跌)":
                    mode = self._market_sort_mode.get(col, "uv")
                    return _parse_market(text, mode)
                else:
                    return text

            rows.sort(key=key_fn, reverse=reverse)

            # 重新排列
            for idx, (k, _) in enumerate(rows):
                self._tree.move(k, "", idx)

            # 更新表头箭头
            for c_name in col_names:
                if c_name == col:
                    arrow = " ▲" if reverse else " ▼"
                    if c_name == "行情(UV/涨跌)":
                        mode = self._market_sort_mode.get(col, "uv")
                        sub = "[UV]" if mode == "uv" else "[趋势]"
                        self._tree.heading(c_name, text=c_name + arrow + sub)
                    else:
                        self._tree.heading(c_name, text=c_name + arrow)
                else:
                    self._tree.heading(c_name, text=c_name)

            # 重新绑定（lambda 闭包需要用默认参数捕获当前值）
            for c_name in col_names:
                self._tree.heading(c_name, command=lambda cn=c_name: sort_col(cn))

        for c_name in [c[0] for c in [
            ("关键词",), ("搜索数",), ("行情(UV/涨跌)",), ("详情",), ("评论",), ("状态",)
        ]]:
            self._tree.heading(c_name, command=lambda cn=c_name: sort_col(cn))

    # ════════════════════════════════════════════════
    # 数据更新
    # ════════════════════════════════════════════════
    def _start(self):
        if not self._engine:
            return
        text = self._kw_text.get("1.0", tk.END).strip()
        keywords = [k.strip() for k in text.split("\n") if k.strip()]
        if not keywords:
            return

        self._start_btn.configure(state=tk.DISABLED)
        self._stop_btn.configure(state=tk.NORMAL)
        self._window = None
        self._funnel.reset()

        # 清空列表
        self._tree.delete(*self._tree.get_children())
        self._sort_state.clear()
        self._market_sort_mode.clear()
        # 恢复表头（清除箭头）
        for c_name in [c[0] for c in [
            ("关键词",), ("搜索数",), ("行情(UV/涨跌)",), ("详情",), ("评论",), ("状态",)
        ]]:
            self._tree.heading(c_name, text=c_name)

        for kw in keywords:
            self._tree.insert("", tk.END, values=(kw, "-", "-", "-", "-", "排队中"))

        # 设置回调
        self._engine.set_callbacks(
            on_stage=self._on_stage_update,
            on_keyword=self._on_kw_update,
            on_product=None,
            on_complete=self._on_done,
        )

        output_dir = Path.home() / ".xianyu_tool" / "collected_data"
        self._engine.start(keywords, output_dir)

    def _stop(self):
        if self._engine:
            self._engine.stop()
        self._on_done()

    def _import_keywords(self):
        path = filedialog.askopenfilename(
            title="导入关键词文件",
            filetypes=[
                ("Excel文件", "*.xlsx *.xls"),
                ("CSV文件", "*.csv"),
                ("文本文件", "*.txt"),
                ("所有文件", "*.*"),
            ],
        )
        if not path:
            return

        keywords = []
        try:
            ext = Path(path).suffix.lower()
            if ext in (".xlsx", ".xls"):
                try:
                    import openpyxl
                    wb = openpyxl.load_workbook(path, read_only=True)
                    ws = wb.active
                    for row in ws.iter_rows(min_col=1, max_col=1, values_only=True):
                        val = str(row[0]).strip() if row[0] is not None else ""
                        if val and val not in keywords:
                            keywords.append(val)
                    wb.close()
                except ImportError:
                    with open(path, "r", encoding="utf-8-sig") as f:
                        for line in f:
                            line = line.strip()
                            if line and line not in keywords:
                                keywords.append(line)
            elif ext == ".csv":
                import csv
                with open(path, "r", encoding="utf-8-sig") as f:
                    reader = csv.reader(f)
                    for row in reader:
                        val = row[0].strip() if row else ""
                        if val and val not in keywords:
                            keywords.append(val)
            else:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and line not in keywords:
                            keywords.append(line)

            if not keywords:
                messagebox.showwarning("导入结果", "文件中未找到有效关键词")
                return

            self._kw_text.delete("1.0", tk.END)
            self._kw_text.insert("1.0", "\n".join(keywords))
            self.app.logger.info(f"[采集] 已导入 {len(keywords)} 个关键词: {Path(path).name}")
        except Exception as e:
            messagebox.showerror("导入失败", f"读取文件出错:\n{e}")

    def _on_stage_update(self, stage: str, info: str, done: int = 1, total: int = 0):
        self.app.root.after(0, lambda t=info: self._progress_label.configure(text=t))
        stage_map = {
            "market": 0,
            "keyword_scoring": 1,
            "keyword_full": 2,
            "product_search": 3,
            "product_detail": 4,
            "product_scoring": 5,
        }
        sidx = stage_map.get(stage)
        if sidx is not None:
            self.app.root.after(0, lambda s=sidx, d=done, t=total: self._funnel.update_stage(s, d, t))

    def _on_kw_update(self, kw: str, idx: int, total: int, status: str,
                       search_cnt: int = 0, has_market: bool = False,
                       detail_cnt: int = 0, comment_cnt: int = 0,
                       market_uv: int = 0, market_price_inc: float = 0):
        def update():
            self._progress_label.configure(text=f"进度: {idx}/{total} — {kw} ({status})")
            for child in self._tree.get_children():
                vals = self._tree.item(child, "values")
                if vals and vals[0] == kw:
                    new_vals = list(vals)
                    if search_cnt > 0:
                        new_vals[1] = str(search_cnt)
                    if has_market and market_uv > 0:
                        trend = f"↑{market_price_inc:.0f}" if market_price_inc > 0 else (f"↓{abs(market_price_inc):.0f}" if market_price_inc < 0 else "→0")
                        new_vals[2] = f"{market_uv}UV {trend}"
                    elif not has_market and new_vals[2] in ("-", ""):
                        new_vals[2] = "无行情"
                    if detail_cnt > 0:
                        new_vals[3] = str(detail_cnt)
                    if comment_cnt > 0:
                        new_vals[4] = str(comment_cnt)
                    new_vals[5] = status
                    self._tree.item(child, values=tuple(new_vals))
                    break
        self.app.root.after(0, update)

    def _on_product_update(self, pd_result: dict):
        def update():
            title = pd_result.get("title", "")[:30]
            grade = pd_result.get("grade", "?")
            total = pd_result.get("total_100", 0)
            self._tree.insert("", tk.END, values=(f"  -> {title}", "-", "-", "-", "-", f"{total}分 {grade}级"))
        self.app.root.after(0, update)

    def _on_done(self, kw_results=None, pd_results=None, supply_pushed=None):
        def update():
            self._start_btn.configure(state=tk.NORMAL)
            self._stop_btn.configure(state=tk.DISABLED)
            a_plus = sum(1 for r in (kw_results or []) if r.get("grade") in ("S", "A"))
            s_a = sum(1 for r in (pd_results or []) if r.get("grade") in ("S", "A"))
            self._progress_label.configure(
                text=f"完成: {len(kw_results or [])}词 {len(pd_results or [])}商品 | A+词:{a_plus} S/A品:{s_a} | 货源:{len(supply_pushed or [])}"
            )
            dash = self.app.get_tab("分析看板")
            if dash and hasattr(dash, 'load_results'):
                dash.load_results(kw_results or [], pd_results or [])
            charts = self.app.get_tab("图表分析")
            if charts and hasattr(charts, 'load_data'):
                charts.load_data(kw_results or [], pd_results or [])
        self.app.root.after(0, update)
