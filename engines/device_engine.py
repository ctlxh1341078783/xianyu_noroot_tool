"""设备池引擎：多设备管理，封装 DeviceManager 供 GUI 使用"""
import threading
from typing import Callable, List, Optional
from dataclasses import dataclass

from core.device_mgr import DeviceManager, DeviceInfo, ConnectionState, _find_adb
from utils.log_manager import get_logger


@dataclass
class DeviceState:
    name: str
    adb_addr: str
    type: str
    status: str  # disconnected/connecting/connected/error
    progress: str  # 采集进度
    use_gadget: bool
    gadget_port: int


class DeviceEngine:
    def __init__(self, settings: dict):
        self._mgr = DeviceManager(settings)
        self._logger = get_logger()

    def scan(self) -> List[DeviceInfo]:
        return self._mgr.auto_scan()

    def list_devices(self) -> List[DeviceInfo]:
        return self._mgr.list_devices()

    def connect(self, adb_addr: str) -> ConnectionState:
        self._logger.info(f"正在连接设备 {adb_addr}...")
        result = self._mgr.connect(adb_addr)
        if result.connected:
            self._logger.info(f"设备 {adb_addr} 连接成功")
        else:
            self._logger.error(f"设备 {adb_addr} 连接失败: {result.last_error}")
        return result

    def disconnect(self):
        self._logger.info("断开设备连接")
        self._mgr.disconnect()

    def add_device(self, name: str, adb_addr: str, dev_type: str = "usb") -> DeviceInfo:
        info = self._mgr.add_device(name, adb_addr, dev_type)
        self._logger.info(f"已添加设备: {name} ({adb_addr})")
        return info

    def remove_device(self, adb_addr: str):
        self._mgr.remove_device(adb_addr)
        self._logger.info(f"已移除设备: {adb_addr}")

    def get_active(self) -> Optional[DeviceInfo]:
        return self._mgr.get_active()

    def get_state(self) -> ConnectionState:
        return self._mgr.get_state()

    def set_disconnect_callback(self, cb: Callable):
        self._mgr.set_disconnect_callback(cb)

    def set_reconnect_callback(self, cb: Callable):
        self._mgr.set_reconnect_callback(cb)

    def ensure_services_ready(self) -> bool:
        """确保所有必要服务就绪（LSPatch → 闲鱼App → Frida Gadget）"""
        return self._mgr.ensure_services_ready()

    def pause_health_check(self):
        """货源查找期间暂停健康检查，避免重启闲鱼App干扰PDD"""
        self._mgr._health_suspended = True
        self._logger.info("[设备] 健康检查已暂停（货源查找进行中）")

    def resume_health_check(self):
        self._mgr._health_suspended = False
        self._logger.info("[设备] 健康检查已恢复")

    def save(self):
        self._mgr.save_devices()

    @property
    def manager(self) -> DeviceManager:
        return self._mgr
