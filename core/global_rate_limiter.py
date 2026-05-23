"""全局限速器：所有Frida RPC调用入口，保证最小间隔，防闲鱼风控"""
import time
import threading
from typing import Optional, Callable


class GlobalRateLimiter:
    """所有Worker线程在调用FridaBridge RPC前必须先acquire()"""

    def __init__(self, min_interval_sec: float = 3.0, max_interval_sec: float = 10.0,
                 log_cb: Optional[Callable] = None):
        self._min_interval = min_interval_sec
        self._max_interval = max_interval_sec
        self._current_interval = min_interval_sec
        self._last_call = 0.0
        self._lock = threading.Lock()
        self._log = log_cb or (lambda msg: None)
        self._total_calls = 0
        self._total_waits = 0
        self._total_wait_ms = 0.0

    def acquire(self, caller_id: str = "") -> bool:
        """获取调用许可，必要时sleep等待。返回True。"""
        with self._lock:
            elapsed = time.time() - self._last_call
            wait = max(0, self._current_interval - elapsed)
        if wait > 0:
            self._total_waits += 1
            self._total_wait_ms += wait * 1000
            if wait > self._current_interval * 2:
                self._log(f"[限速] {caller_id} 等待 {wait:.1f}s (队列堆积)")
            time.sleep(wait)
        with self._lock:
            self._last_call = time.time()
            self._total_calls += 1
        return True

    def release(self):
        """调完释放（当前实现无需操作，预留给未来扩展）"""
        pass

    def adjust_interval(self, new_sec: float):
        """风控时动态加大间隔"""
        self._current_interval = min(max(new_sec, self._min_interval), self._max_interval)
        self._log(f"[限速] 间隔调整为 {self._current_interval:.1f}s")

    def reset_interval(self):
        self._current_interval = self._min_interval

    def get_queue_depth(self) -> int:
        """估计当前排队数（基于上次调用时间）"""
        elapsed = time.time() - self._last_call
        if elapsed >= self._current_interval:
            return 0
        return 1  # 简化：有人正在等=深度1

    def stats(self) -> dict:
        return {
            "calls": self._total_calls,
            "waits": self._total_waits,
            "avg_wait_ms": round(self._total_wait_ms / max(1, self._total_waits), 1),
            "current_interval": round(self._current_interval, 1),
        }
