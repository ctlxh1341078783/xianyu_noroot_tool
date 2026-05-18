"""线程安全日志管理器：所有模块统一路由，支持GUI回调 + 文件持久化"""
import threading
import queue
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional
from enum import Enum


class LogLevel(Enum):
    DEBUG = 0
    INFO = 1
    WARN = 2
    ERROR = 3


LEVEL_NAMES = {"DEBUG": "🔍", "INFO": "ℹ️", "WARN": "⚠️", "ERROR": "❌"}


class LogManager:
    """线程安全单例日志管理器"""
    _instance: Optional["LogManager"] = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._queue = queue.Queue()
        self._gui_callbacks: "List[Callable]" = []
        self._file_path: Optional[Path] = None
        self._display_level = LogLevel.DEBUG
        self._lock = threading.Lock()

    def setup_file(self, log_dir: Path = None):
        if log_dir is None:
            log_dir = Path.home() / ".xianyu_tool" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now().strftime("%Y-%m-%d")
        self._file_path = log_dir / f"xianyu_{today}.log"

    def add_gui_callback(self, cb: Callable[[str, str, str], None]):
        """注册GUI回调: cb(timestamp, level, message)"""
        self._gui_callbacks.append(cb)

    def set_display_level(self, level: LogLevel):
        self._display_level = level

    def debug(self, msg: str):
        self._log(LogLevel.DEBUG, msg)

    def info(self, msg: str):
        self._log(LogLevel.INFO, msg)

    def warn(self, msg: str):
        self._log(LogLevel.WARN, msg)

    def error(self, msg: str):
        self._log(LogLevel.ERROR, msg)

    def _log(self, level: LogLevel, msg: str):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        level_name = level.name
        formatted = f"{ts} [{level_name}] {msg}"

        # 文件写入
        if self._file_path and level.value >= LogLevel.INFO.value:
            try:
                with open(self._file_path, "a", encoding="utf-8") as f:
                    f.write(formatted + "\n")
            except Exception:
                pass

        # GUI回调（所有级别）
        for cb in self._gui_callbacks:
            try:
                cb(ts, level_name, msg)
            except Exception:
                pass


def get_logger() -> LogManager:
    return LogManager()
