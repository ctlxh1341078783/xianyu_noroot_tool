"""平台工具：ADB路径检测、字体适配、路径处理"""
import sys
import shutil
import os
from pathlib import Path


def find_adb() -> str:
    """检测ADB可执行文件路径，平台自适应"""
    # 1. 检查 PATH
    adb = shutil.which("adb")
    if adb:
        return adb

    # 2. 平台特定路径
    if sys.platform == "win32":
        candidates = [
            Path(os.environ.get("LOCALAPPDATA", "")) / "Android" / "Sdk" / "platform-tools" / "adb.exe",
            Path("C:/platform-tools/adb.exe"),
            Path.home() / "AppData" / "Local" / "Android" / "Sdk" / "platform-tools" / "adb.exe",
        ]
    else:
        candidates = [
            Path.home() / "Library" / "Android" / "sdk" / "platform-tools" / "adb",
            Path("/opt/homebrew/bin/adb"),
            Path("/usr/local/bin/adb"),
        ]

    for c in candidates:
        if c.exists():
            return str(c)

    return "adb"


def is_frozen() -> bool:
    """PyInstaller打包检测"""
    return getattr(sys, 'frozen', False)


def resource_path(relative: str) -> Path:
    """获取资源文件路径（兼容PyInstaller）"""
    if is_frozen():
        base = Path(sys._MEIPASS)
    else:
        base = Path(__file__).parent.parent
    return base / relative


def get_data_dir() -> Path:
    """获取用户数据目录"""
    if is_frozen():
        return Path.home() / ".xianyu_tool"
    return Path(__file__).parent.parent


def is_installed() -> bool:
    """检查是否通过安装程序安装（存在注册表卸载条目）"""
    if sys.platform != "win32":
        return False
    if not is_frozen():
        return False
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\闲鱼数据采集分析工具")
        winreg.CloseKey(key)
        return True
    except OSError:
        pass
    return False


def get_install_dir() -> Path:
    """获取安装目录（仅打包后有效）"""
    if is_frozen():
        return Path(sys.executable).parent
    return Path(__file__).parent.parent
