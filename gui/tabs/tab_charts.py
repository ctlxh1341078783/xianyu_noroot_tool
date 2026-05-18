"""图表分析Tab：等级分布饼图 + 维度对比柱状图 + 价格散点图 + 利润分布"""
import tkinter as tk
from tkinter import ttk

from gui.theme import SURF, SURF2, FG, FG_M, ACC, GRADE_COLORS, FONTS


class ChartsTab:
    def __init__(self, parent: ttk.Frame, app):
        self.parent = parent
        self.app = app
        self._kw_results = []
        self._pd_results = []
        self._fig = None
        self._canvas = None
        self._build_ui()

    def _build_ui(self):
        # 控制栏
        ctrl = tk.Frame(self.parent, bg=SURF)
        ctrl.pack(fill=tk.X, padx=8, pady=(8, 4))

        ttk.Label(ctrl, text="图表类型:", font=FONTS["ui"]).pack(side=tk.LEFT)
        self._chart_var = tk.StringVar(value="等级分布")
        cb = ttk.Combobox(ctrl, textvariable=self._chart_var,
                          values=["等级分布", "维度对比", "价格散点", "利润分布"],
                          state="readonly", width=12)
        cb.pack(side=tk.LEFT, padx=4)
        cb.bind("<<ComboboxSelected>>", lambda e: self._draw())

        ttk.Button(ctrl, text="刷新图表", command=self._draw).pack(side=tk.LEFT, padx=10)

        self._summary_label = ttk.Label(ctrl, text="", font=FONTS["ui"], foreground=FG_M)
        self._summary_label.pack(side=tk.RIGHT, padx=8)

        # 图表区域
        self._chart_frame = tk.Frame(self.parent, bg=SURF)
        self._chart_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        self._placeholder = ttk.Label(self._chart_frame, text="暂无数据 — 采集完成后自动加载，或从分析看板导入数据",
                                      font=FONTS["heading"], foreground=FG_M)
        self._placeholder.pack(expand=True)

        self._data_label = ttk.Label(ctrl, text="", font=FONTS["ui"], foreground=ACC)
        self._data_label.pack(side=tk.RIGHT, padx=8)

    def load_data(self, kw_results: list, pd_results: list):
        self._kw_results = kw_results or []
        self._pd_results = pd_results or []
        self._data_label.configure(text=f"{len(self._kw_results)}词 / {len(self._pd_results)}商品")
        self._draw()

    def _draw(self):
        if not self._kw_results and not self._pd_results:
            self._placeholder.configure(text="暂无数据 — 采集完成后自动加载，或从分析看板导入数据")
            self._placeholder.pack(expand=True)
            return

        chart_type = self._chart_var.get()
        self._placeholder.pack_forget()

        try:
            import matplotlib
            matplotlib.use("TkAgg")
            from matplotlib.figure import Figure
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

            self._fig = Figure(figsize=(8, 5), dpi=100)
            self._fig.set_facecolor(SURF)

            if chart_type == "等级分布":
                self._draw_grade_pie()
            elif chart_type == "维度对比":
                self._draw_dimension_bar()
            elif chart_type == "价格散点":
                self._draw_price_scatter()
            elif chart_type == "利润分布":
                self._draw_profit_bar()

            if self._canvas:
                self._canvas.get_tk_widget().destroy()
            self._canvas = FigureCanvasTkAgg(self._fig, self._chart_frame)
            self._canvas.draw()
            self._canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        except ImportError:
            self._placeholder.configure(text="matplotlib 未安装，请 pip install matplotlib")
            self._placeholder.pack(expand=True)

    def _draw_grade_pie(self):
        ax = self._fig.add_subplot(111)
        grades = {}
        for r in self._kw_results:
            g = r.get("grade", "N/A")
            grades[g] = grades.get(g, 0) + 1

        labels = list(grades.keys())
        sizes = list(grades.values())
        colors_list = [GRADE_COLORS.get(l, ("#F3F4F6", "#6B7280"))[0] for l in labels]
        explode = [0.05] * len(labels)

        ax.pie(sizes, explode=explode, labels=labels, colors=colors_list,
               autopct="%1.1f%%", shadow=False, startangle=90)
        ax.set_title("关键词等级分布", fontproperties="Microsoft YaHei", fontsize=13)
        total = len(self._kw_results)
        self._summary_label.configure(text=f"共 {total} 个关键词")

    def _draw_dimension_bar(self):
        if not self._kw_results:
            return
        dims = ["demand_scale", "deal_efficiency", "deal_quality", "profit_certainty", "competition", "trend_signal"]
        dim_names = ["需求规模", "成交效率", "成交质量", "利润确定性", "竞争格局", "趋势信号"]
        maxes = [20, 30, 20, 25, 15, 10]
        n = len(self._kw_results)
        avgs = []
        for i, d in enumerate(dims):
            total = sum(r.get("scores", {}).get(d, 0) for r in self._kw_results)
            avgs.append(total / n / maxes[i] * 100 if n > 0 else 0)

        ax = self._fig.add_subplot(111)
        colors_list = ["#3B82F6", "#8B5CF6", "#10B981", "#F59E0B", "#EF4444", "#EC4899"]
        bars = ax.bar(dim_names, avgs, color=colors_list)
        ax.set_ylabel("得分率 (%)")
        ax.set_title("各维度平均得分率", fontproperties="Microsoft YaHei", fontsize=13)
        ax.set_ylim(0, 105)
        for bar, val in zip(bars, avgs):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 1, f"{val:.0f}%",
                    ha="center", fontsize=9)

    def _draw_price_scatter(self):
        if not self._pd_results:
            return
        ax = self._fig.add_subplot(111)
        prices = [r.get("price", 0) for r in self._pd_results]
        scores = [r.get("total_100", 0) for r in self._pd_results]
        grades = [r.get("grade", "C") for r in self._pd_results]
        color_map = {"S": "#FFD700", "A": "#10B981", "B": "#3B82F6", "C": "#F59E0B", "D": "#EF4444"}
        colors = [color_map.get(g, "#9CA3AF") for g in grades]
        ax.scatter(prices, scores, c=colors, alpha=0.7, s=60)
        ax.set_xlabel("价格 (¥)")
        ax.set_ylabel("商品评分")
        ax.set_title("价格 vs 评分 散点图", fontproperties="Microsoft YaHei", fontsize=13)
        for grade, color in color_map.items():
            ax.scatter([], [], c=color, label=f"等级{grade}")
        ax.legend(loc="upper right")

    def _draw_profit_bar(self):
        """货源利润分布（从评分数据统计各象限商品数）"""
        ax = self._fig.add_subplot(111)
        quadrants = ["Q1 双正利润", "Q2 图搜优先", "Q3 标题参考", "Q4 不推荐"]
        colors_list = ["#10B981", "#3B82F6", "#F59E0B", "#EF4444"]

        # 从商品结果中统计 supply_attr 维度（货源属性）的分布
        values = [0, 0, 0, 0]
        for r in self._pd_results:
            sa = r.get("scores", {}).get("supply_attr", 0)
            if sa >= 20:
                values[0] += 1  # Q1
            elif sa >= 15:
                values[1] += 1  # Q2
            elif sa >= 10:
                values[2] += 1  # Q3
            else:
                values[3] += 1  # Q4
        if sum(values) == 0:
            values = [0, 0, 0, 0]  # 全是0时也不显示假数据

        ax.bar(quadrants, values, color=colors_list)
        ax.set_ylabel("商品数量")
        ax.set_title("货源四象限分布（基于supply_attr维度）", fontproperties="Microsoft YaHei", fontsize=13)
        for i, (bar, val) in enumerate(zip(ax.patches, values)):
            if val > 0:
                ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.3,
                        str(val), ha="center", fontsize=10)
