"""可折叠日志面板：线程安全，实时展示所有模块日志
horizontal模式：底部横条（旧版兼容）
vertical模式：右侧竖栏，增量不删历史，深色终端风格
"""
import tkinter as tk
from tkinter import ttk, scrolledtext
from gui.theme import SURF, SURF2, FG, FG_M, FG_L, ACC, ACC_H, SUCC, DANGER, WARN, BRD, LEVEL_EMOJI, LOG_COLORS, FONTS


class LogPanel(ttk.Frame):
    MAX_LINES = 2000  # vertical模式上限，远高于旧版500

    def __init__(self, parent, vertical=False):
        super().__init__(parent)
        self._vertical = vertical
        self._collapsed = False
        self._line_count = 0
        self._build_ui()

    def _build_ui(self):
        if self._vertical:
            self._build_vertical()
        else:
            self._build_horizontal()

    # ═══ 竖直模式（右侧日志栏） ═══

    def _build_vertical(self):
        self.configure(style="Status.TFrame")

        # 标题栏
        header = tk.Frame(self, bg="#2D2D3F")
        header.pack(fill=tk.X)

        # 折叠按钮
        self._toggle_btn = tk.Label(header, text="◀", bg="#2D2D3F", fg="#CDD6F4",
                                     font=FONTS["ui_bold"], cursor="hand2")
        self._toggle_btn.pack(side=tk.LEFT, padx=4, pady=3)
        self._toggle_btn.bind("<Button-1>", self._toggle)

        tk.Label(header, text="实时日志", bg="#2D2D3F", fg="#CDD6F4",
                 font=("Consolas", 10, "bold")).pack(side=tk.LEFT, padx=2)

        self._count_label = tk.Label(header, text="0 行", bg="#2D2D3F", fg="#6C7086",
                                      font=("Consolas", 8))
        self._count_label.pack(side=tk.RIGHT, padx=8)

        # 日志文本框：深色主题，ScrolledText 自带滚动条
        self._text_frame = tk.Frame(self, bg="#1E1E2E")
        self._text_frame.pack(fill=tk.BOTH, expand=True)

        self._text = scrolledtext.ScrolledText(
            self._text_frame,
            wrap=tk.WORD,
            state=tk.DISABLED,
            font=("Consolas", 9),
            bg="#1E1E2E",
            fg="#CDD6F4",
            relief=tk.FLAT,
            padx=6,
            pady=4,
            insertbackground="white",
        )
        self._text.pack(fill=tk.BOTH, expand=True)

        # 日志级别颜色
        self._text.tag_config("DEBUG", foreground="#6C7086")
        self._text.tag_config("INFO", foreground="#CDD6F4")
        self._text.tag_config("WARN", foreground="#F9E2AF")
        self._text.tag_config("ERROR", foreground="#F38BA8")

        # 底部控制栏
        footer = tk.Frame(self, bg="#2D2D3F")
        footer.pack(fill=tk.X)
        tk.Button(footer, text="清空", command=self.clear,
                  bg="#3D3D5F", fg="#CDD6F4",
                  font=FONTS["ui"], relief=tk.FLAT, cursor="hand2",
                  padx=10, pady=2, borderwidth=0).pack(side=tk.RIGHT, padx=4, pady=3)
        tk.Label(footer, text="增量模式，不删历史", bg="#2D2D3F", fg="#6C7086",
                 font=("Consolas", 7)).pack(side=tk.LEFT, padx=6)

    # ═══ 水平模式（底部横条，旧版兼容） ═══

    def _build_horizontal(self):
        # 标题栏
        self._header = tk.Frame(self, bg=SURF, cursor="hand2")
        self._header.pack(fill=tk.X, side=tk.TOP)
        self._header.bind("<Button-1>", self._toggle)

        self._toggle_btn = ttk.Label(self._header, text="▼", font=FONTS["ui"], foreground=ACC)
        self._toggle_btn.pack(side=tk.LEFT, padx=10, pady=2)

        self._title_label = ttk.Label(self._header, text="日志面板", font=FONTS["ui_bold"])
        self._title_label.pack(side=tk.LEFT, pady=2)

        self._count_label = ttk.Label(self._header, text="", font=FONTS["ui"], foreground=FG_L)
        self._count_label.pack(side=tk.RIGHT, padx=10, pady=2)

        ttk.Separator(self, orient=tk.HORIZONTAL).pack(fill=tk.X, side=tk.TOP)

        # 日志文本框
        self._text_frame = tk.Frame(self, bg=SURF)
        self._text_frame.pack(fill=tk.BOTH, expand=True, side=tk.TOP)

        self._text = tk.Text(
            self._text_frame,
            wrap=tk.WORD,
            state=tk.DISABLED,
            font=FONTS["mono"],
            bg=SURF,
            fg=FG,
            relief=tk.FLAT,
            padx=8,
            pady=4,
            height=8,
        )
        scrollbar = ttk.Scrollbar(self._text_frame, orient=tk.VERTICAL, command=self._text.yview)
        self._text.configure(yscrollcommand=scrollbar.set)
        self._text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # 日志级别颜色
        self._text.tag_config("DEBUG", foreground=LOG_COLORS.get("DEBUG", "#6B7280"))
        self._text.tag_config("INFO", foreground=LOG_COLORS.get("INFO", "#1F2937"))
        self._text.tag_config("WARN", foreground=LOG_COLORS.get("WARN", "#D97706"))
        self._text.tag_config("ERROR", foreground=LOG_COLORS.get("ERROR", "#DC2626"))

    # ═══ 通用方法 ═══

    def _toggle(self, event=None):
        if self._collapsed:
            self._text_frame.pack(fill=tk.BOTH, expand=True)
            self._toggle_btn.configure(text="◀" if self._vertical else "▼")
            self._collapsed = False
        else:
            self._text_frame.pack_forget()
            self._toggle_btn.configure(text="▶" if self._vertical else "▶")
            self._collapsed = True

    def add_log(self, timestamp: str, level: str, msg: str):
        def _append():
            self._text.configure(state=tk.NORMAL)
            line = f"{timestamp} [{level}] {msg}\n"
            self._text.insert(tk.END, line, level)

            self._line_count += 1
            if self._vertical:
                # 竖直模式：只在上限时裁旧，正常增量不删
                if self._line_count > self.MAX_LINES:
                    self._text.delete("1.0", "2.0")
                    self._line_count -= 1
            else:
                # 水平模式：保持旧行为
                if self._line_count > self.MAX_LINES:
                    self._text.delete("1.0", "2.0")
                    self._line_count -= 1

            self._text.see(tk.END)
            self._text.configure(state=tk.DISABLED)
            self._count_label.configure(text=f"{self._line_count} 行")

        try:
            self.after(0, _append)
        except Exception:
            pass

    def clear(self):
        self._text.configure(state=tk.NORMAL)
        self._text.delete("1.0", tk.END)
        self._text.configure(state=tk.DISABLED)
        self._line_count = 0
        self._count_label.configure(text="0 行")
