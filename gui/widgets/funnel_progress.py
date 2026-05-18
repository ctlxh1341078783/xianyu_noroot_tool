"""横向条形漏斗进度组件"""
import tkinter as tk
from tkinter import ttk
from gui.theme import SURF, FG, FG_M, BRD, FONTS


class FunnelProgress(ttk.Frame):
    """六阶段漏斗可视化：词3步(预检/海选/精选) + 品3步(搜索预筛/详情采集/评分推送)"""

    STAGES = [
        {"label": "词·预筛选",   "done": 0, "total": 0, "color": "#3B82F6", "desc": "预筛通过词数/总词数"},
        {"label": "词·海选",     "done": 0, "total": 0, "color": "#8B5CF6", "desc": "海选通过词数/预检通过词数"},
        {"label": "词·精选",     "done": 0, "total": 0, "color": "#A78BFA", "desc": "推选品词数/海选通过词数"},
        {"label": "品·预筛选",   "done": 0, "total": 0, "color": "#F59E0B", "desc": "预筛通过品数/总搜索品数"},
        {"label": "品·详情采集", "done": 0, "total": 0, "color": "#EF4444", "desc": "已采集详情数/预筛通过品数"},
        {"label": "品·货源推送", "done": 0, "total": 0, "color": "#EC4899", "desc": "已评分品数/已采集详情数"},
    ]

    _ROW_H = 32
    _LABEL_W = 92
    _PAD_X = 10
    _BAR_MIN = 4

    def __init__(self, parent):
        super().__init__(parent)
        self._stages = [dict(s) for s in self.STAGES]
        self._build_ui()

    def _build_ui(self):
        self._header = ttk.Label(self, text="采集漏斗（词3步 → 品3步）", font=FONTS["heading"])
        self._header.pack(pady=(6, 2))

        h = len(self._stages) * self._ROW_H + 10
        self._canvas = tk.Canvas(self, height=h, bg=SURF, highlightthickness=0)
        self._canvas.pack(fill=tk.X, padx=(10, 4), pady=2)

        self._summary_label = ttk.Label(self, text="待启动", font=FONTS["ui"], foreground=FG_M)
        self._summary_label.pack(pady=(0, 4))

        self._canvas.bind("<Configure>", self._draw)

    def _draw(self, event=None):
        self._canvas.delete("all")
        w = self._canvas.winfo_width()
        if w < 100:
            return

        n = len(self._stages)
        bar_x0 = self._LABEL_W + self._PAD_X
        bar_x1 = w - self._PAD_X - 100  # 右侧留100px给数值文本

        if bar_x1 - bar_x0 < 40:
            bar_x1 = bar_x0 + 40

        for i, stage in enumerate(self._stages):
            y0 = 5 + i * self._ROW_H
            y_mid = y0 + self._ROW_H // 2

            # 阶段标签 (右对齐)
            self._canvas.create_text(
                self._LABEL_W - 4, y_mid,
                text=stage["label"], font=FONTS["ui_bold"], fill=FG, anchor="e"
            )

            # 背景条
            self._canvas.create_rectangle(
                bar_x0, y0 + 4, bar_x1, y0 + self._ROW_H - 4,
                fill="#E5E7EB", outline="", width=0
            )

            # 进度条
            done = stage.get("done", 0)
            total = stage.get("total", 0)
            pct = min(done / total, 1.0) if total > 0 else 0
            if pct > 0:
                fill_w = max(self._BAR_MIN, (bar_x1 - bar_x0) * pct)
                self._canvas.create_rectangle(
                    bar_x0, y0 + 4, bar_x0 + fill_w, y0 + self._ROW_H - 4,
                    fill=stage["color"], outline="", width=0
                )

            # 数值 + 百分比
            if total > 0:
                text = f"{done}/{total}"
                pct_str = f"({pct*100:.0f}%)" if pct > 0 else ""
            elif done > 0:
                text = str(done)
                pct_str = ""
            else:
                text = "-"
                pct_str = ""

            value_text = f"{text} {pct_str}" if pct_str else text
            font_size = 9 if len(value_text) >= 12 else (10 if len(value_text) >= 8 else 11)
            self._canvas.create_text(
                bar_x1 + 6, y_mid,
                text=value_text, font=("Microsoft YaHei", font_size, "bold"),
                fill=stage["color"], anchor="w"
            )

    def update_stage(self, stage_idx: int, done: int, total: int = 0):
        """stage_idx: 0=预检 1=海选 2=精选 3=搜索+预筛 4=详情采集 5=评分+推送"""
        if 0 <= stage_idx < len(self._stages):
            self._stages[stage_idx]["done"] = done
            if total > 0:
                self._stages[stage_idx]["total"] = total
        self._draw()

    def set_summary(self, text: str):
        self._summary_label.configure(text=text)

    def reset(self):
        for s in self._stages:
            s["done"] = 0
            s["total"] = 0
        self._summary_label.configure(text="待启动")
        self._draw()
