"""横向条形漏斗进度组件"""
import tkinter as tk
from tkinter import ttk
from gui.theme import SURF, FG, FG_M, BRD, FONTS


class FunnelProgress(ttk.Frame):
    """六阶段漏斗可视化：词3步(预检/海选/精选) + 品3步(搜索预筛/详情采集/评分推送)"""

    STAGES = [
        {"label": "词·飞轮",     "done": 0, "total": 0, "color": "#10B981", "desc": "进海选词数/词库总量"},
        {"label": "词·海选",     "done": 0, "total": 0, "color": "#3B82F6", "desc": "行情+预检 A+词/已预检"},
        {"label": "词·精选",     "done": 0, "total": 0, "color": "#8B5CF6", "desc": "品类权重+稳定性 精选A+/海选A+"},
        {"label": "品·预筛选",   "done": 0, "total": 0, "color": "#F59E0B", "desc": "预筛通过品数/搜索品数"},
        {"label": "品·详情",     "done": 0, "total": 0, "color": "#EF4444", "desc": "已采详情/预筛通过品数"},
        {"label": "品·货源",     "done": 0, "total": 0, "color": "#EC4899", "desc": "DDK:N 手机:M / 推送品数"},
    ]

    _ROW_H = 32
    _LABEL_W = 84
    _PAD_X = 8
    _BAR_MIN = 4

    def __init__(self, parent):
        super().__init__(parent)
        self._stages = [dict(s) for s in self.STAGES]
        self._build_ui()

    def _build_ui(self):
        self._header = ttk.Label(self, text="采集漏斗（飞轮→词→品→货源）", font=FONTS["heading"])
        self._header.pack(pady=(6, 2))

        h = len(self._stages) * self._ROW_H + 10
        self._canvas = tk.Canvas(self, height=h, bg=SURF, highlightthickness=0)
        self._canvas.pack(fill=tk.X, padx=(10, 4), pady=2)

        self._summary_label = ttk.Label(self, text="待启动", font=FONTS["ui"], foreground=FG_M)
        self._summary_label.pack(pady=(0, 4))

        self._canvas.bind("<Configure>", self._on_resize)
        # 延迟首次绘制，等 layout 完成后 canvas 有正确宽度
        self.after(50, self._draw)

    def _on_resize(self, event=None):
        """窗口大小变化时全量重绘"""
        self._draw()

    def _draw(self, event=None):
        """全量重绘（首次或 resize 时调用）。日常更新走 _refresh()"""
        self._canvas.delete("all")
        w = self._canvas.winfo_width()
        if w < 100:
            return

        n = len(self._stages)
        bar_x0 = self._LABEL_W + self._PAD_X
        bar_x1 = w - self._PAD_X - 100

        if bar_x1 - bar_x0 < 40:
            bar_x1 = bar_x0 + 40

        for i, stage in enumerate(self._stages):
            y0 = 5 + i * self._ROW_H
            y_mid = y0 + self._ROW_H // 2

            # 阶段标签（不变，tag 用于 resize 重建）
            self._canvas.create_text(
                self._LABEL_W - 4, y_mid,
                text=stage["label"], font=FONTS["ui_bold"], fill=FG, anchor="e",
                tags=(f"label_{i}",)
            )

            # 背景条
            self._canvas.create_rectangle(
                bar_x0, y0 + 4, bar_x1, y0 + self._ROW_H - 4,
                fill="#E5E7EB", outline="", width=0,
                tags=(f"bg_{i}",)
            )

            # 进度填充条（先用 0 宽度占位）
            self._canvas.create_rectangle(
                bar_x0, y0 + 4, bar_x0, y0 + self._ROW_H - 4,
                fill=stage["color"], outline="", width=0,
                tags=(f"fill_{i}",)
            )

            # 数值文字
            self._canvas.create_text(
                bar_x1 + 6, y_mid,
                text="-", font=("Microsoft YaHei", 11, "bold"),
                fill=stage["color"], anchor="w",
                tags=(f"value_{i}",)
            )

        # 首次全量绘制后立即刷新数值
        self._refresh()

    def _refresh(self):
        """增量更新：只修改进度条宽度和数值文字，不删除任何元素 → 无闪烁"""
        w = self._canvas.winfo_width()
        if w < 100:
            return

        bar_x0 = self._LABEL_W + self._PAD_X
        bar_x1 = w - self._PAD_X - 100
        if bar_x1 - bar_x0 < 40:
            bar_x1 = bar_x0 + 40

        for i, stage in enumerate(self._stages):
            y0 = 5 + i * self._ROW_H
            y_mid = y0 + self._ROW_H // 2

            done = stage.get("done", 0)
            total = stage.get("total", 0)
            pct = min(done / total, 1.0) if total > 0 else 0

            # 更新进度填充条宽度
            fill_w = max(self._BAR_MIN, (bar_x1 - bar_x0) * pct) if pct > 0 else 0
            self._canvas.coords(
                f"fill_{i}",
                bar_x0, y0 + 4,
                bar_x0 + fill_w, y0 + self._ROW_H - 4
            )

            # 更新数值文字
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
            self._canvas.itemconfigure(f"value_{i}", text=value_text,
                                       font=("Microsoft YaHei", font_size, "bold"))
            self._canvas.coords(f"value_{i}", bar_x1 + 6, y_mid)

    def update_stage(self, stage_idx: int, done: int, total: int = 0):
        """stage_idx: 0=飞轮 1=海选 2=精选 3=预筛选 4=详情 5=货源"""
        if 0 <= stage_idx < len(self._stages):
            self._stages[stage_idx]["done"] = done
            if total > 0:
                self._stages[stage_idx]["total"] = total
        self._refresh()

    def set_summary(self, text: str):
        self._summary_label.configure(text=text)

    def reset(self):
        for s in self._stages:
            s["done"] = 0
            s["total"] = 0
        self._summary_label.configure(text="待启动")
        self._refresh()
