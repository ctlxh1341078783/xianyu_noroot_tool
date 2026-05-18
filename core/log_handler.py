"""
统一日志处理器：Tkinter Text widget + 文件双写，支持级别过滤
"""
from __future__ import annotations
import tkinter as tk
from datetime import datetime
from typing import Callable, Optional
import logging
import os


class LogHandler(logging.Handler):
    """Python logging Handler，输出到 Tkinter Text widget 和文件"""

    LEVELS = {"DEBUG": "#7f8c8d", "INFO": "#2ecc71", "WARN": "#f39c12", "ERROR": "#e74c3c"}

    def __init__(self, text_widget: Optional[tk.Text] = None, log_dir: str = "./logs"):
        super().__init__()
        self.widget = text_widget
        self.log_dir = log_dir
        self._callbacks: list[Callable[[str, str], None]] = []

        os.makedirs(log_dir, exist_ok=True)
        date_str = datetime.now().strftime("%Y%m%d")
        file_path = os.path.join(log_dir, f"xianyu_tool_{date_str}.log")
        self._file_handler = logging.FileHandler(file_path, encoding="utf-8")
        self._file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        self.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

        self._logger = logging.getLogger("XianyuTool")
        self._logger.setLevel(logging.DEBUG)
        self._logger.addHandler(self)
        self._logger.addHandler(self._file_handler)
        self._display_level = "INFO"

    def set_widget(self, widget: tk.Text):
        self.widget = widget

    def set_display_level(self, level: str):
        self._display_level = level

    def add_callback(self, cb: Callable[[str, str], None]):
        self._callbacks.append(cb)

    def emit(self, record: logging.LogRecord):
        msg = self.format(record)
        level = record.levelname
        self._write_to_ui(msg, level)
        for cb in self._callbacks:
            try:
                cb(msg, level)
            except:
                pass

    def _write_to_ui(self, msg: str, level: str):
        if not self.widget:
            return
        if self._display_level == "DEBUG":
            pass
        elif self._display_level == "INFO" and level == "DEBUG":
            return
        elif self._display_level == "WARN" and level in ("DEBUG", "INFO"):
            return
        elif self._display_level == "ERROR" and level in ("DEBUG", "INFO", "WARN"):
            return

        color = self.LEVELS.get(level, "#ffffff")
        self.widget.after(0, lambda: self._append_text(msg, color))

    def _append_text(self, msg: str, color: str):
        try:
            self.widget.configure(state="normal")
            tag = f"tag_{color.replace('#','')}"
            self.widget.tag_configure(tag, foreground=color)
            self.widget.insert(tk.END, msg + "\n", tag)
            self.widget.see(tk.END)
            self.widget.configure(state="disabled")
        except:
            pass

    def debug(self, msg: str):
        self._logger.debug(msg)

    def info(self, msg: str):
        self._logger.info(msg)

    def warn(self, msg: str):
        self._logger.warning(msg)

    def error(self, msg: str):
        self._logger.error(msg)


# 全局日志实例
_log_handler: Optional[LogHandler] = None


def get_log_handler(log_dir: str = None) -> LogHandler:
    global _log_handler
    if _log_handler is None:
        _log_handler = LogHandler(log_dir=log_dir or "./logs")
    elif log_dir and not _log_handler.log_dir:
        # 补充设置 log_dir（首次由 GUI 调用时设置）
        _log_handler.log_dir = log_dir
    return _log_handler


def set_log_widget(widget: tk.Text):
    handler = get_log_handler()
    handler.set_widget(widget)
