"""
设备管理器：ADB + Frida + App 生命周期管理，断线重连
"""
from __future__ import annotations
import os
import shutil
import subprocess
import sys
import time
import threading
from typing import Dict, List, Optional, Callable
from dataclasses import dataclass, field

from .webhook import WebhookNotifier
from utils.log_manager import get_logger

# Windows 下隐藏 subprocess 弹出的黑窗
_NO_WINDOW = {}
if sys.platform == 'win32':
    _NO_WINDOW = {'creationflags': subprocess.CREATE_NO_WINDOW}

def _run(cmd, **kwargs):
    """subprocess.run 的无黑窗包装"""
    merged = {**_NO_WINDOW, **kwargs}
    return subprocess.run(cmd, **merged)

def _popen(cmd, **kwargs):
    """subprocess.Popen 的无黑窗包装"""
    merged = {**_NO_WINDOW, **kwargs}
    return subprocess.Popen(cmd, **merged)


def _find_adb() -> str:
    """查找 adb 可执行文件路径，优先使用 PATH 中的，否则搜索常见位置"""
    adb = shutil.which("adb")
    if adb:
        return adb
    # 常见位置（macOS / Windows）
    for p in [
        os.path.expanduser("~/Library/Android/sdk/platform-tools/adb"),
        "/opt/homebrew/bin/adb",
        "/usr/local/bin/adb",
        os.path.expanduser("~/android-sdk/platform-tools/adb"),
        "C:\\platform-tools\\adb.exe",
        os.path.expanduser("~\\platform-tools\\adb.exe"),
    ]:
        if os.path.isfile(p):
            return p
    return "adb"


@dataclass
class DeviceInfo:
    name: str
    adb_addr: str
    type: str = "emulator"          # emulator | usb | wifi
    frida_server_path: str = "/data/local/tmp/frida-server-16.7.19-android-x86"
    idlefish_pkg: str = "com.taobao.idlefish"
    use_gadget: bool = False        # 非 root 真机用 Frida Gadget 模式
    gadget_port: int = 27042        # Gadget 监听端口
    android_ver: str = ""
    frida_ok: bool = False
    app_pid: int = 0
    app_running: bool = False


@dataclass
class ConnectionState:
    connected: bool = False
    adb_ok: bool = False
    frida_server_ok: bool = False
    frida_script_ok: bool = False
    app_ok: bool = False
    last_error: str = ""


class DeviceManager:
    """ADB + Frida 设备全生命周期管理"""

    _LSPATCH_PKG = "org.lsposed.lspatch"  # LSPatch 加载器（非Root方案必须）

    def __init__(self, settings: dict, webhook: Optional[WebhookNotifier] = None):
        self._settings = settings
        self._webhook = webhook
        self._log = get_logger()
        self._devices: Dict[str, DeviceInfo] = {}
        self._active_device: Optional[DeviceInfo] = None
        self._state = ConnectionState()
        self._health_thread: Optional[threading.Thread] = None
        self._health_running = False
        self._health_suspended = False  # 货源查找期间暂停
        self._on_disconnect: Optional[Callable] = None
        self._on_reconnect: Optional[Callable] = None
        self._health_interval = settings.get("frida", {}).get("health_check_interval", 10)

        self._load_devices()

    # ── 设备列表管理 ──

    def _load_devices(self):
        for dev_data in self._settings.get("devices", []):
            info = DeviceInfo(**{k: v for k, v in dev_data.items() if k in DeviceInfo.__dataclass_fields__})
            addr = info.adb_addr or ("usb" if info.type == "usb" else "auto")
            self._devices[addr] = info
        # 自动扫描 ADB，发现配置中未列出的设备
        self.auto_scan()

    def save_devices(self):
        self._settings["devices"] = [{
            "name": d.name, "adb_addr": d.adb_addr, "type": d.type,
            "frida_server_path": d.frida_server_path, "idlefish_pkg": d.idlefish_pkg,
            "use_gadget": d.use_gadget, "gadget_port": d.gadget_port,
        } for d in self._devices.values()]

    def list_devices(self) -> List[DeviceInfo]:
        return list(self._devices.values())

    def auto_scan(self) -> List[DeviceInfo]:
        """扫描 ADB 已连接设备，自动添加未在配置中的设备"""
        adb = _find_adb()
        try:
            r = _run([adb, "devices", "-l"], capture_output=True, text=True,
                             timeout=8, encoding='utf-8', errors='replace')
            new_devices = []
            for line in r.stdout.strip().split('\n')[1:]:
                if not line.strip() or 'offline' in line:
                    continue
                parts = line.strip().split()
                if len(parts) < 2:
                    continue
                addr = parts[0]
                if addr not in self._devices:
                    # 解析 transport_id 判断连接方式
                    transport = 'emulator' if addr.startswith('127.0.0.1') or addr.startswith('localhost') else 'usb'
                    info = DeviceInfo(name=addr, adb_addr=addr, type=transport)
                    self._devices[addr] = info
                    new_devices.append(info)
                    self._log.info(f"[设备] 自动发现新设备: {addr} ({transport})")
            if new_devices:
                self.save_devices()
            return new_devices
        except Exception as e:
            self._log.debug(f"[设备] 自动扫描失败: {e}")
            return []

    def add_device(self, name: str, adb_addr: str, dev_type: str = "emulator") -> DeviceInfo:
        info = DeviceInfo(name=name, adb_addr=adb_addr, type=dev_type)
        self._devices[adb_addr] = info
        self.save_devices()
        return info

    def remove_device(self, adb_addr: str):
        self._devices.pop(adb_addr, None)
        self.save_devices()

    def get_active(self) -> Optional[DeviceInfo]:
        return self._active_device

    def get_state(self) -> ConnectionState:
        return self._state

    # ── 连接管理 ──

    def connect(self, adb_addr: str) -> ConnectionState:
        """连接到指定设备，返回连接状态"""
        info = self._devices.get(adb_addr)
        if not info:
            info = DeviceInfo(name=adb_addr, adb_addr=adb_addr)
            self._devices[adb_addr] = info

        self._active_device = info
        self._state = ConnectionState()
        self._log.info(f"[设备] 正在连接 {info.name} ({adb_addr})...")

        # Step 1: ADB 连接
        self._state.adb_ok = self._check_adb(info)
        if not self._state.adb_ok:
            self._state.last_error = "ADB 连接失败"
            return self._state

        # Step 2: 获取设备信息 + 自动识别模拟器/真机
        self._detect_device(info)

        # Step 3: 根据模式走不同流程
        if info.use_gadget:
            return self._connect_gadget(info)
        else:
            return self._connect_frida_server(info)

    def _connect_frida_server(self, info: DeviceInfo) -> ConnectionState:
        """frida-server 模式（模拟器/root 设备）"""

        # Step 1: 尝试 root
        self._try_adb_root(info)

        # Step 2: 端口转发 (Frida)，adb root 后需重新转发
        self._setup_frida_forward(info)

        # Step 3: 检查 Frida server
        self._state.frida_server_ok = self._check_frida_server(info)
        if not self._state.frida_server_ok:
            self._log.warn(f"[设备] Frida server 未运行，尝试启动...")
            if not self._start_frida_server(info):
                self._state.last_error = "Frida server 启动失败"
                return self._state
            self._state.frida_server_ok = True

        # Step 4: 检查 App
        self._state.app_ok = self._check_app(info)
        if not self._state.app_ok:
            self._log.info(f"[设备] 闲鱼 App 未运行，尝试启动...")
            self._start_app(info)
            time.sleep(5)
            self._state.app_ok = self._check_app(info)

        self._state.connected = self._state.adb_ok and self._state.frida_server_ok and self._state.app_ok

        if self._state.connected:
            self._log.info(f"[设备] {info.name} 连接成功 (Android {info.android_ver}, PID {info.app_pid})")
            self._start_health_monitor()
        else:
            self._log.error(f"[设备] 连接失败: {self._state.last_error}")

        return self._state

    def _connect_gadget(self, info: DeviceInfo) -> ConnectionState:
        """Frida Gadget 模式（非 root 真机）"""

        # Step 1: 端口转发 (gadget 在设备内监听 127.0.0.1:27042)
        self._setup_gadget_forward(info)

        # Step 2: 确保 LSPatch 加载器正在运行（非Root方案，App依赖它加载模块）
        self._ensure_lspatch(info)

        # Step 3: 确保 App 已启动（gadget 随 App 启动）
        self._state.app_ok = self._check_app(info)
        if not self._state.app_ok:
            self._log.info(f"[Gadget] 闲鱼 App 未运行，尝试启动...")
            self._start_app(info)
            time.sleep(8)
            self._state.app_ok = self._check_app(info)

        if not self._state.app_ok:
            self._state.last_error = "App 未运行（请确认已安装带 Gadget 的闲鱼 APK）"
            self._log.error(f"[Gadget] {self._state.last_error}")
            return self._state

        # Step 3: 等待 Gadget 就绪（gadget 随 App 启动需要时间初始化）
        self._state.frida_server_ok = self._wait_for_gadget(info)
        if not self._state.frida_server_ok:
            self._state.last_error = "Gadget 连接超时，请确认 App 内嵌了 Frida Gadget"
            self._log.error(f"[Gadget] {self._state.last_error}")
            return self._state

        self._state.connected = self._state.adb_ok and self._state.frida_server_ok and self._state.app_ok

        if self._state.connected:
            self._log.info(f"[Gadget] {info.name} 连接成功 (Android {info.android_ver}, PID {info.app_pid})")
            self._start_health_monitor()
        else:
            self._log.error(f"[设备] 连接失败: {self._state.last_error}")

        return self._state

    def disconnect(self):
        self._stop_health_monitor()
        if self._active_device:
            self._log.info(f"[设备] 已断开 {self._active_device.name}")
        self._active_device = None
        self._state = ConnectionState()

    def reconnect(self) -> ConnectionState:
        if not self._active_device:
            return ConnectionState()
        self._log.info(f"[设备] 尝试重连 {self._active_device.name}...")
        return self.connect(self._active_device.adb_addr)

    # ── 设备检测 ──

    # 已知模拟器特征
    _EMULATOR_HARDWARE = {'goldfish', 'ranchu', 'vbox86', 'nox', 'ttvm', 'qcom'}
    _EMULATOR_MANUFACTURER = {'Genymotion', 'unknown'}
    _EMULATOR_FINGERPRINT_KEYWORDS = {'generic', 'sdk', 'emulator', 'vbox', 'nox', 'mumu'}

    def _detect_device(self, info: DeviceInfo):
        """自动识别设备类型、Android 版本、品牌型号，并更新 DeviceInfo"""
        adb = _find_adb()
        props = {}

        # 批量获取关键属性
        prop_keys = [
            'ro.build.version.release',
            'ro.product.manufacturer',
            'ro.product.model',
            'ro.product.brand',
            'ro.hardware',
            'ro.build.characteristics',
            'ro.kernel.qemu',
        ]
        for key in prop_keys:
            try:
                r = _run(
                    [adb, "-s", info.adb_addr, "shell", f"getprop {key}"],
                    capture_output=True, text=True, timeout=5,
                    encoding='utf-8', errors='replace'
                )
                props[key] = r.stdout.strip()
            except:
                props[key] = ""

        # Android 版本
        info.android_ver = props.get('ro.build.version.release', '') or ''

        # 判断是否为模拟器
        hw = (props.get('ro.hardware', '') or '').lower()
        mfr = (props.get('ro.product.manufacturer', '') or '').lower()
        brand = (props.get('ro.product.brand', '') or '').lower()
        model = (props.get('ro.product.model', '') or '').lower()
        chars = (props.get('ro.build.characteristics', '') or '').lower()
        qemu = props.get('ro.kernel.qemu', '') or ''

        is_emu = False
        emu_reason = ""

        if qemu == '1':
            is_emu = True
            emu_reason = "qemu内核"
        elif hw in self._EMULATOR_HARDWARE:
            is_emu = True
            emu_reason = f"硬件={hw}"
        elif 'emulator' in chars:
            is_emu = True
            emu_reason = "build.characteristics=emulator"
        elif any(kw in brand for kw in self._EMULATOR_FINGERPRINT_KEYWORDS):
            is_emu = True
            emu_reason = f"brand={brand}"
        elif any(kw in model for kw in self._EMULATOR_FINGERPRINT_KEYWORDS):
            is_emu = True
            emu_reason = f"model={model}"
        elif mfr in self._EMULATOR_MANUFACTURER:
            is_emu = True
            emu_reason = f"manufacturer={mfr}"

        # 修正连接方式判断：127.0.0.1 表示模拟器，其他IP或USB表示真机
        if not is_emu and info.adb_addr.startswith('127.0.0.1'):
            # localhost 连接也可能是远程真机，但大概率是模拟器
            is_emu = True
            emu_reason = "本地回环地址"

        if is_emu:
            info.type = "emulator"
            self._log.info(f"[设备] 识别为模拟器 ({emu_reason}) → {brand}/{model}")
        else:
            info.type = "usb"
            self._log.info(f"[设备] 识别为真机 → {brand} {model}")
            # 真机默认启用 gadget 模式（非 root 无法运行 frida-server）
            if not info.use_gadget:
                info.use_gadget = True
                self._log.info("[设备] 真机自动启用 Frida Gadget 模式")

        self.save_devices()

    def _adb_exec(self, *args, timeout=10):
        cmd = [_find_adb()]
        if self._active_device:
            cmd += ["-s", self._active_device.adb_addr]
        cmd += list(args)
        try:
            return _run(cmd, capture_output=True, text=True, timeout=timeout,
                                encoding='utf-8', errors='replace')
        except subprocess.TimeoutExpired:
            self._log.warn(f"[ADB] 命令超时 ({timeout}s): {' '.join(cmd)}")
            raise

    def _check_adb(self, info: DeviceInfo) -> bool:
        try:
            adb = _find_adb()
            result = _run([adb, "devices"], capture_output=True, text=True, timeout=5,
                                    encoding='utf-8', errors='replace')
            for line in result.stdout.strip().split("\n")[1:]:
                if info.adb_addr in line and "device" in line:
                    return True
            # 尝试连接
            _run([adb, "connect", info.adb_addr], capture_output=True, text=True, timeout=5,
                          encoding='utf-8', errors='replace')
            time.sleep(1)
            result = _run([adb, "devices"], capture_output=True, text=True, timeout=5,
                                   encoding='utf-8', errors='replace')
            return info.adb_addr in result.stdout
        except Exception as e:
            self._log.error(f"[ADB] 检查失败: {e}")
            return False

    def _check_frida_server(self, info: DeviceInfo) -> bool:
        try:
            # 先用 frida 远程连接检测（最可靠，不依赖 ps 格式）
            try:
                import frida
                device = frida.get_device_manager().add_remote_device("127.0.0.1:27042")
                device.get_process("com.taobao.idlefish", timeout=3)
                return True
            except:
                pass
            # 回退：ps 命令检测（兼容不同 Android 版本）
            for cmd in ["ps -A | grep frida", "ps | grep frida", "ps -e | grep frida"]:
                result = self._adb_exec("shell", cmd, timeout=10)
                if "frida" in result.stdout.lower():
                    return True
            return False
        except:
            return False

    def _check_gadget(self, info: DeviceInfo) -> bool:
        """检查 Frida Gadget 是否在设备内监听（用ADB检测，避免与FridaBridge冲突）"""
        port = getattr(info, 'gadget_port', 27042)
        # 方法1: ADB pidof 确认 App 进程存活
        try:
            adb = _find_adb()
            r = _run(
                [adb, "-s", info.adb_addr, "shell", f"pidof {info.idlefish_pkg}"],
                capture_output=True, text=True, timeout=5,
                encoding='utf-8', errors='replace'
            )
            if not r.stdout.strip():
                return False
            # 更新 PID（防止进程重启后 PID 变化）
            info.app_pid = int(r.stdout.strip().split()[0])
        except Exception:
            return False
        # 方法2: 确认端口转发仍在
        try:
            r2 = _run(
                [adb, "-s", info.adb_addr, "forward", "--list"],
                capture_output=True, text=True, timeout=5,
                encoding='utf-8', errors='replace'
            )
            if f"tcp:{port}" not in r2.stdout:
                # 重新设置转发
                self._setup_gadget_forward(info)
        except Exception:
            pass
        return True

    def _setup_frida_forward(self, info: DeviceInfo):
        """设置 ADB 端口转发，使宿主机可以连接模拟器内的 frida-server"""
        try:
            adb = _find_adb()
            _run(
                [adb, "-s", info.adb_addr, "forward", "tcp:27042", "tcp:27042"],
                capture_output=True, text=True, timeout=5,
                encoding='utf-8', errors='replace'
            )
        except Exception:
            pass

    def _setup_gadget_forward(self, info: DeviceInfo):
        """设置 ADB 端口转发，使宿主机可以连接设备内的 Frida Gadget"""
        port = getattr(info, 'gadget_port', 27042)
        try:
            adb = _find_adb()
            _run(
                [adb, "-s", info.adb_addr, "forward", f"tcp:{port}", f"tcp:{port}"],
                capture_output=True, text=True, timeout=5,
                encoding='utf-8', errors='replace'
            )
            self._log.info(f"[Gadget] ADB 端口转发: tcp:{port} → tcp:{port}")
        except Exception as e:
            self._log.warn(f"[Gadget] 端口转发失败: {e}")

    def _wait_for_gadget(self, info: DeviceInfo, max_wait: int = 30) -> bool:
        """等待 Gadget 在设备内启动并就绪（listen 模式需要 App 完全启动后 gadget 才监听）
        使用 ADB pidof 检查，不占用 frida 连接，避免与后续 FridaBridge 冲突"""
        port = getattr(info, 'gadget_port', 27042)
        self._log.info(f"[Gadget] 等待 Gadget 就绪 (端口 {port})...")
        adb = _find_adb()
        for i in range(max_wait):
            time.sleep(1)
            try:
                # 方法1: pidof 直接获取进程 PID
                result = _run(
                    [adb, "-s", info.adb_addr, "shell", f"pidof {info.idlefish_pkg}"],
                    capture_output=True, text=True, timeout=5,
                    encoding='utf-8', errors='replace'
                )
                if result.stdout.strip():
                    pid = int(result.stdout.strip().split()[0])
                    info.app_pid = pid
                    self._log.info(f"[Gadget] 已就绪 (PID {pid})")
                    return True

                # 方法2: ps + grep 兜底
                result = _run(
                    [adb, "-s", info.adb_addr, "shell", f"ps -A | grep {info.idlefish_pkg}"],
                    capture_output=True, text=True, timeout=5,
                    encoding='utf-8', errors='replace'
                )
                for line in result.stdout.strip().split("\n"):
                    if info.idlefish_pkg in line:
                        parts = line.strip().split()
                        if len(parts) >= 2:
                            try:
                                info.app_pid = int(parts[1])
                            except ValueError:
                                info.app_pid = int(parts[0])
                            self._log.info(f"[Gadget] 已就绪 (PID {info.app_pid})")
                            return True
            except Exception:
                pass
            if i % 5 == 4:
                self._log.info(f"[Gadget] 仍在等待... ({i+1}/{max_wait}s)")
        return False

    def _try_adb_root(self, info: DeviceInfo):
        """尝试 adb root，模拟器通常支持，root 后 frida 才能 attach 到 app 进程"""
        try:
            adb = _find_adb()
            result = _run(
                [adb, "-s", info.adb_addr, "root"],
                capture_output=True, text=True, timeout=5,
                encoding='utf-8', errors='replace'
            )
            if "already running as root" in result.stdout or "restarting" in result.stdout:
                self._log.info("[设备] ADB root 权限已获取")
                time.sleep(1)  # 等 adbd 重启
        except Exception:
            pass

    def _start_frida_server(self, info: DeviceInfo) -> bool:
        try:
            adb = _find_adb()
            # 先杀掉旧进程（可能以 shell 用户运行，root 后 attach 失败）
            frida_bin = info.frida_server_path.split("/")[-1]
            _run(
                [adb, "-s", info.adb_addr, "shell", f"killall {frida_bin}"],
                capture_output=True, text=True, timeout=5,
                encoding='utf-8', errors='replace'
            )
            time.sleep(1)

            # 重新转发（adb root 后旧转发会失效）
            self._setup_frida_forward(info)

            # 方法1: 用 nohup + setsid 启动，兼容 Android 7.x
            cmd1 = f"nohup {info.frida_server_path} -l 0.0.0.0:27042 > /dev/null 2>&1 &"
            _popen(
                [adb, "-s", info.adb_addr, "shell", cmd1],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            time.sleep(3)
            if self._check_frida_server(info):
                self._log.info("[设备] Frida server 启动成功")
                return True

            # 方法2: 写入启动脚本再执行（兼容旧版 Android 7.x）
            try:
                script = f"#!/system/bin/sh\n{info.frida_server_path} -l 0.0.0.0:27042 &\n"
                _run(
                    [adb, "-s", info.adb_addr, "push", "-", "/data/local/tmp/start_frida.sh"],
                    input=script, capture_output=True, text=True, timeout=5,
                    encoding='utf-8', errors='replace'
                )
                _run(
                    [adb, "-s", info.adb_addr, "shell", "chmod 755 /data/local/tmp/start_frida.sh && /data/local/tmp/start_frida.sh"],
                    capture_output=True, text=True, timeout=5,
                    encoding='utf-8', errors='replace'
                )
                time.sleep(3)
                if self._check_frida_server(info):
                    self._log.info("[设备] Frida server 启动成功 (脚本方式)")
                    return True
            except:
                pass

            # 方法3: su -c 方式
            cmd3 = f"su -c 'nohup {info.frida_server_path} -l 0.0.0.0:27042 > /dev/null 2>&1 &'"
            _popen(
                [adb, "-s", info.adb_addr, "shell", cmd3],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            time.sleep(3)
            return self._check_frida_server(info)
        except Exception as e:
            self._log.error(f"[设备] Frida 启动异常: {e}")
            return False

    def _check_app(self, info: DeviceInfo) -> bool:
        try:
            # 用 frida 检测（如果 frida 已连接），最可靠
            try:
                import frida
                device = frida.get_device_manager().add_remote_device("127.0.0.1:27042")
                for p in device.enumerate_processes():
                    if info.idlefish_pkg in p.name:
                        info.app_pid = p.pid
                        info.app_running = True
                        return True
            except:
                pass
            # 回退：ps 命令检测（兼容不同 Android 版本）
            for cmd in [f"ps | grep '{info.idlefish_pkg}'",
                         f"ps -A | grep '{info.idlefish_pkg}'",
                         f"ps -e | grep '{info.idlefish_pkg}'"]:
                result = self._adb_exec("shell", cmd, timeout=10)
                for line in result.stdout.strip().split("\n"):
                    if info.idlefish_pkg in line:
                        parts = line.strip().split()
                        if len(parts) >= 2:
                            try:
                                info.app_pid = int(parts[1])
                            except:
                                try:
                                    info.app_pid = int(parts[0])
                                except:
                                    pass
                            info.app_running = True
                            return True
            return False
        except:
            return False

    def _check_lspatch(self) -> bool:
        """检查 LSPatch 加载器进程是否在运行"""
        for cmd in [f"ps -A | grep '{self._LSPATCH_PKG}'",
                     f"ps | grep '{self._LSPATCH_PKG}'",
                     f"ps -e | grep '{self._LSPATCH_PKG}'"]:
            try:
                result = self._adb_exec("shell", cmd, timeout=8)
                if self._LSPATCH_PKG in result.stdout:
                    return True
            except Exception:
                pass
        return False

    def _start_lspatch(self):
        """启动 LSPatch 加载器"""
        self._log.info("[LSPatch] 启动加载器...")
        try:
            self._adb_exec("shell", f"monkey -p {self._LSPATCH_PKG} -c android.intent.category.LAUNCHER 1", timeout=10)
            time.sleep(3)
            if self._check_lspatch():
                self._log.info("[LSPatch] 加载器已启动")
                return True
            else:
                self._log.warn("[LSPatch] 加载器可能未正常启动")
                return False
        except Exception as e:
            self._log.error(f"[LSPatch] 启动失败: {e}")
            return False

    def _ensure_lspatch(self, info: DeviceInfo):
        """确保 LSPatch 加载器在后台运行，不在则启动"""
        if not self._check_lspatch():
            self._log.warn("[LSPatch] 加载器未运行，正在启动...")
            self._start_lspatch()
        else:
            self._log.info("[LSPatch] 加载器运行中")

    def ensure_services_ready(self) -> bool:
        """确保所有必要服务就绪：ADB → LSPatch → 闲鱼App → Frida Gadget
        Returns True 表示可以开始采集
        """
        if not self._active_device:
            self._log.error("[守护] 无活动设备")
            return False

        info = self._active_device

        # 1. ADB
        if not self._check_adb(info):
            self._log.error("[守护] ADB 连接断开")
            return False

        # 2. LSPatch (Gadget模式)
        if info.use_gadget:
            if not self._check_lspatch():
                self._log.warn("[守护] LSPatch 不在运行，尝试启动...")
                self._start_lspatch()
                time.sleep(2)

        # 3. 闲鱼 App
        if not self._check_app(info):
            self._log.warn("[守护] 闲鱼 App 不在运行，尝试启动...")
            self._start_app(info)
            time.sleep(8)

        app_ok = self._check_app(info)
        if not app_ok:
            self._log.error("[守护] 闲鱼 App 启动失败（请检查 LSPatch 是否正常）")
            return False

        # 4. Frida Gadget
        if info.use_gadget:
            self._state.frida_server_ok = self._wait_for_gadget(info, max_wait=15)
        else:
            self._state.frida_server_ok = self._check_frida_server(info)

        if not self._state.frida_server_ok:
            self._log.error("[守护] Frida 连接未就绪")
            return False

        self._state.app_ok = app_ok
        self._state.adb_ok = True
        self._state.connected = True
        self._log.info("[守护] 所有服务就绪，可以开始采集")
        return True

    def _start_app(self, info: DeviceInfo):
        try:
            self._adb_exec("shell", f"monkey -p {info.idlefish_pkg} -c android.intent.category.LAUNCHER 1", timeout=10)
        except Exception as e:
            self._log.error(f"[设备] 启动 App 失败: {e}")

    def restart_app(self, info: Optional[DeviceInfo] = None):
        info = info or self._active_device
        if not info:
            return
        self._log.info("[设备] 重启闲鱼 App...")
        self._adb_exec("shell", f"am force-stop {info.idlefish_pkg}", timeout=5)
        time.sleep(2)
        self._start_app(info)
        time.sleep(8)

    def get_app_pid(self) -> int:
        if self._active_device:
            self._check_app(self._active_device)
            return self._active_device.app_pid
        return 0

    # ── 健康监控 ──

    def set_disconnect_callback(self, cb: Callable):
        self._on_disconnect = cb

    def set_reconnect_callback(self, cb: Callable):
        self._on_reconnect = cb

    def _start_health_monitor(self):
        if self._health_running:
            return
        self._health_running = True
        self._health_thread = threading.Thread(target=self._health_loop, daemon=True)
        self._health_thread.start()

    def _stop_health_monitor(self):
        self._health_running = False
        if self._health_thread:
            self._health_thread.join(timeout=5)
            self._health_thread = None

    def _health_loop(self):
        while self._health_running:
            time.sleep(self._health_interval)
            if self._health_suspended or not self._active_device:
                continue

            dev = self._active_device

            # 检查 ADB
            if not self._check_adb(dev):
                self._log.warn(f"[健康检查] ADB 断开: {dev.name}")
                self._state.adb_ok = False
                self._state.connected = False
                if self._webhook:
                    self._webhook.notify_disconnect(dev.name)
                self._attempt_reconnect()
                continue

            # 检查 LSPatch（Gadget模式必须）
            if dev.use_gadget and not self._check_lspatch():
                self._log.warn("[健康检查] LSPatch 加载器不在运行，尝试启动...")
                self._start_lspatch()
                time.sleep(2)

            # 检查 App 进程
            if not self._check_app(dev):
                self._log.warn(f"[健康检查] 闲鱼 App 未运行")
                self._state.app_ok = False
                self._state.connected = False
                # App崩了先确保LSPatch在
                if dev.use_gadget:
                    self._ensure_lspatch(dev)
                self._start_app(dev)
                time.sleep(8)
                if self._check_app(dev):
                    self._state.app_ok = True
                    self._state.connected = True
                    self._log.info("[健康检查] App 已自动重启")
                continue

            # 根据模式检查 Frida 连接
            if dev.use_gadget:
                # Gadget 模式：gadget 随 App 运行，无需重启 frida-server
                if not self._check_gadget(dev):
                    self._log.warn("[健康检查] Gadget 连接断开，等待 App 恢复...")
                    self._state.frida_server_ok = False
                    self._state.connected = False
                    # Gadget 无法单独重启，只能等 App 重启后自动恢复
                    time.sleep(5)
                    if self._check_gadget(dev):
                        self._state.frida_server_ok = True
                        self._state.connected = True
                        self._log.info("[健康检查] Gadget 已恢复")
            else:
                # Frida-server 模式
                if not self._check_frida_server(dev):
                    self._log.warn("[健康检查] Frida server 断开，尝试重启...")
                    self._state.frida_server_ok = False
                    self._state.connected = False
                    self._start_frida_server(dev)
                    time.sleep(2)
                    if self._check_frida_server(dev):
                        self._state.frida_server_ok = True
                        self._state.connected = True
                        self._log.info("[健康检查] Frida server 已自动重启")

            self._state.connected = self._state.adb_ok and self._state.frida_server_ok and self._state.app_ok

    def _attempt_reconnect(self):
        max_retries = self._settings.get("frida", {}).get("reconnect_max_retries", 3)
        delay = self._settings.get("frida", {}).get("reconnect_delay", 5)

        for i in range(max_retries):
            self._log.info(f"[重连] 第 {i+1}/{max_retries} 次尝试...")
            time.sleep(delay)
            state = self.connect(self._active_device.adb_addr if self._active_device else "")
            if state.connected:
                self._log.info("[重连] 成功！")
                if self._webhook:
                    self._webhook.notify_reconnect(self._active_device.name if self._active_device else "", True)
                if self._on_reconnect:
                    self._on_reconnect()
                return

        self._log.error("[重连] 所有尝试失败")
        if self._webhook:
            self._webhook.notify_reconnect(self._active_device.name if self._active_device else "", False)
        if self._on_disconnect:
            self._on_disconnect()
