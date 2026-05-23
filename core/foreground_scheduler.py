"""前台令牌调度器：协调闲鱼(飞轮C)和拼多多(货源手机兜底)的前台占用"""
import time
import threading
from typing import Optional, Callable


class ForegroundToken:
    def __init__(self, app: str, deadline: float):
        self.app = app
        self.deadline = deadline
        self.renewal_signal = False


class ForegroundScheduler:
    """
    不强制抢占——租约到期前15秒发renewal_signal，
    持有者完成当前项后自行释放。

    规则:
      1. 飞轮C攒够N个待验证词再申请
      2. 货源先走DDK API（不用手机），无结果才申请
      3. 租约到期前 → renewal_signal → 持有者收尾保存→释放
      4. 货源排队期间DDK有新结果→取消手机申请
      5. 货源排队超过2分钟→飞轮C当前批次后让出
    """

    def __init__(self, device_mgr=None, renewal_warning_sec: float = 15,
                 pdd_max_wait_sec: float = 120, log_cb: Optional[Callable] = None):
        self._device_mgr = device_mgr
        self._renewal_warning = renewal_warning_sec
        self._pdd_max_wait = pdd_max_wait_sec
        self._log = log_cb or (lambda msg: None)
        self._lock = threading.Lock()
        self._current_token: Optional[ForegroundToken] = None
        self._pdd_queue_start: Optional[float] = None  # PDD开始排队的时间

    def request(self, app: str, duration_sec: int) -> Optional[ForegroundToken]:
        """申请前台令牌。返回Token或None（已被占用）。"""
        with self._lock:
            if self._current_token is not None:
                # 检查货源是否排队太久
                if app == "xianyu" and self._pdd_queue_start is not None:
                    waited = time.time() - self._pdd_queue_start
                    if waited > self._pdd_max_wait:
                        self._log(f"[前台] 货源已排队{waited:.0f}s，飞轮C让出")
                        # 信号当前持有者尽快释放
                        self._current_token.renewal_signal = True
                        return None
                return None

            deadline = time.time() + duration_sec
            token = ForegroundToken(app, deadline)
            self._current_token = token
            self._log(f"[前台] {app} 获得令牌, 租约 {duration_sec}s")
            return token

    def release(self, token: ForegroundToken):
        """释放前台令牌"""
        with self._lock:
            if self._current_token is token:
                self._log(f"[前台] {token.app} 释放令牌")
                self._current_token = None
                self._pdd_queue_start = None

    def cancel_request(self, app: str):
        """DDK有结果了→取消手机申请"""
        with self._lock:
            if app == "pdd":
                self._pdd_queue_start = None
                self._log("[前台] PDD手机申请已取消（DDK有结果）")

    def notify_pdd_waiting(self):
        """货源开始等待手机令牌"""
        with self._lock:
            if self._pdd_queue_start is None:
                self._pdd_queue_start = time.time()

    def check_renewal(self, token: ForegroundToken) -> bool:
        """检查是否需要收尾。True=需要尽快释放。"""
        if token.renewal_signal:
            return True
        remaining = token.deadline - time.time()
        if remaining <= self._renewal_warning:
            token.renewal_signal = True
            self._log(f"[前台] {token.app} 租约还剩{remaining:.0f}s，发送收尾信号")
            return True
        return False

    def is_held(self) -> bool:
        with self._lock:
            return self._current_token is not None

    def holder_app(self) -> Optional[str]:
        with self._lock:
            return self._current_token.app if self._current_token else None
