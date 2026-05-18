"""顶部状态栏：设备连接状态 + 采集进度 + 最后更新（全中文）"""
import tkinter as tk
from tkinter import ttk
from datetime import datetime
from gui.theme import SURF, FG, FG_M, ACC, FONTS

STATUS_LIGHT_COLORS = {
    "已连接": "#10B981",
    "连接中...": "#F59E0B",
    "连接失败": "#EF4444",
    "采集中": ACC,          # 橘色主题
    "未连接": "#9CA3AF",
    "空闲": "#9CA3AF",
}


class StatusBar(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.configure(style="Status.TFrame")

        self._status_light = tk.Canvas(self, width=10, height=10, highlightthickness=0)
        self._status_light.pack(side=tk.LEFT, padx=(10, 4))
        self._light_id = self._status_light.create_oval(1, 1, 9, 9, fill="#9CA3AF", outline="")

        self._device_label = ttk.Label(self, text="无设备 | 未连接", font=FONTS["ui"])
        self._device_label.pack(side=tk.LEFT, padx=(2, 15))

        ttk.Separator(self, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=5, pady=2)

        self._progress_label = ttk.Label(self, text="就绪", font=FONTS["ui"])
        self._progress_label.pack(side=tk.LEFT, padx=10)

        ttk.Separator(self, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=5, pady=2)

        self._last_update_label = ttk.Label(self, text="", font=FONTS["ui"], foreground=FG_M)
        self._last_update_label.pack(side=tk.RIGHT, padx=10)

    def set_device(self, name: str = "", status: str = "未连接"):
        color = STATUS_LIGHT_COLORS.get(status, "#9CA3AF")
        self._status_light.itemconfig(self._light_id, fill=color)
        if name:
            self._device_label.configure(text=f"{name} | {status}")
        else:
            self._device_label.configure(text=f"无设备 | {status}")

    def set_progress(self, text: str):
        self._progress_label.configure(text=text)

    def set_last_update(self):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._last_update_label.configure(text=f"最后更新: {ts}")
