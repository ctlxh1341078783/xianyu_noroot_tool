"""
闲鱼数据采集分析工具 — 安装程序（Windows / macOS）
双击运行，选择安装路径，自动复制文件、创建快捷方式、注册卸载信息
"""
import sys
import os
import json
import shutil
import subprocess
import threading
import tempfile
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

IS_FROZEN = getattr(sys, 'frozen', False)
IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

def get_bundled_app_dir() -> Path:
    """从内嵌的 zip 包解压主程序 .app，返回 .app 路径"""
    if IS_FROZEN:
        meipass = Path(sys._MEIPASS)
        # 查找内嵌的 zip 包
        zip_path = None
        for item in meipass.iterdir():
            if item.name.endswith(".zip"):
                zip_path = item
                break
        if zip_path:
            # 解压到临时目录
            import zipfile
            extract_dir = Path(tempfile.mkdtemp(prefix="xianyu_install_"))
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(extract_dir)
            # 找到解压出的 .app
            for item in extract_dir.iterdir():
                if item.name.endswith(".app") and item.is_dir():
                    return item
            # Fallback: 返回解压目录本身
            return extract_dir
        # 没有 zip，按老方法查找
        for item in meipass.iterdir():
            if item.name.endswith(".app") and item.is_dir():
                return item
        return meipass
    else:
        base = Path(__file__).parent
        app_dir = base / "dist" / "闲鱼数据采集分析工具.app"
        if app_dir.exists():
            return app_dir
        app_dir = base / "dist" / "闲鱼数据采集分析工具"
        if app_dir.exists():
            return app_dir
        return app_dir

def get_default_install_path() -> Path:
    if IS_MAC:
        return Path("/Applications")
    elif IS_WIN:
        import ctypes
        try:
            is_admin = ctypes.windll.shell32.IsUserAnAdmin()
        except Exception:
            is_admin = False
        if is_admin:
            return Path("C:/Program Files/闲鱼数据采集分析工具")
        else:
            local = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
            return Path(local) / "Programs" / "闲鱼数据采集分析工具"
    else:
        return Path.home() / "Applications" / "闲鱼数据采集分析工具"

def get_exe_name() -> str:
    if IS_MAC:
        return "闲鱼数据采集分析工具.app"
    elif IS_WIN:
        return "闲鱼数据采集分析工具.exe"
    else:
        return "闲鱼数据采集分析工具"

def get_uninstaller_name() -> str:
    if IS_MAC:
        return "闲鱼工具卸载程序.app"
    elif IS_WIN:
        return "闲鱼工具卸载程序.exe"
    else:
        return "闲鱼工具卸载程序"

def load_version() -> dict:
    try:
        vp = Path(__file__).parent / "version.json"
        if not vp.exists() and IS_FROZEN:
            vp = Path(sys._MEIPASS) / "version.json"
        if vp.exists():
            return json.loads(vp.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"version": "3.2.0", "build_date": "", "build_number": 1}

class InstallerWindow:
    def __init__(self):
        self.root = tk.Tk()
        ver = load_version().get("version", "3.2.0")
        self.root.title(f"闲鱼数据采集分析工具 v{ver} — 安装程序")
        self.root.geometry("600x460")
        self.root.resizable(True, True)
        self.root.minsize(500, 400)

        self._installing = False
        self._build_ui()

    def _build_ui(self):
        # 标题
        header = tk.Frame(self.root, bg="#667eea", height=60)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(header, text="闲鱼数据采集分析工具", font=("微软雅黑", 16, "bold"),
                 fg="white", bg="#667eea").pack(pady=12)

        body = tk.Frame(self.root, padx=20, pady=15)
        body.pack(fill=tk.BOTH, expand=True)

        # 安装路径
        tk.Label(body, text="安装路径：", font=("微软雅黑", 11)).pack(anchor=tk.W)
        path_frame = tk.Frame(body)
        path_frame.pack(fill=tk.X, pady=(5, 15))
        self.path_var = tk.StringVar(value=str(get_default_install_path()))
        self.path_entry = ttk.Entry(path_frame, textvariable=self.path_var, font=("微软雅黑", 10))
        self.path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(path_frame, text="浏览...", command=self._browse, width=8).pack(side=tk.LEFT, padx=(6, 0))

        # 信息
        ver = load_version()
        info = f"版本: v{ver.get('version', '?')}  |  构建日期: {ver.get('build_date', '?')}"
        tk.Label(body, text=info, font=("微软雅黑", 9), fg="#888").pack(anchor=tk.W)

        # 进度条
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(body, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill=tk.X, pady=(15, 5))

        self.status_var = tk.StringVar(value="准备就绪")
        tk.Label(body, textvariable=self.status_var, font=("微软雅黑", 10)).pack(anchor=tk.W)

        # 按钮
        btn_frame = tk.Frame(body)
        btn_frame.pack(fill=tk.X, pady=(15, 0))
        self.install_btn = tk.Button(btn_frame, text="开始安装", command=self._start_install,
                                     bg="#667eea", fg="white", font=("微软雅黑", 11, "bold"),
                                     relief=tk.FLAT, cursor="hand2", padx=25, pady=6)
        self.install_btn.pack(side=tk.LEFT)
        self.launch_var = tk.BooleanVar(value=True)
        tk.Checkbutton(btn_frame, text="安装完成后启动程序", variable=self.launch_var,
                       font=("微软雅黑", 10)).pack(side=tk.LEFT, padx=15)
        tk.Button(btn_frame, text="取消", command=self.root.destroy,
                  font=("微软雅黑", 10), padx=15).pack(side=tk.RIGHT)

    def _browse(self):
        d = filedialog.askdirectory(title="选择安装目录", initialdir=self.path_var.get())
        if d:
            self.path_var.set(d)

    def _start_install(self):
        if self._installing:
            return
        target = Path(self.path_var.get().strip())
        if not str(target):
            messagebox.showerror("错误", "请选择安装路径")
            return

        try:
            target.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            messagebox.showerror("权限不足", f"无法创建目录:\n{target}\n\n请以管理员身份运行或选择其他路径")
            return

        self._installing = True
        self.install_btn.config(state=tk.DISABLED, text="安装中...")
        t = threading.Thread(target=self._do_install, args=(target,), daemon=True)
        t.start()
        self._poll(t)

    def _do_install(self, target: Path):
        self._report(5, "正在准备...")
        source = get_bundled_app_dir()
        if not source.exists():
            self._report_error(f"未找到安装源目录:\n{source}")
            return

        # 收集文件列表
        files = []
        for f in source.rglob("*"):
            if f.is_file():
                files.append(f)
        total = len(files)

        self._report(10, f"正在安装 ({total} 个文件)...")

        if IS_MAC and source.name.endswith(".app"):
            # macOS .app 包：整体复制到目标目录
            app_dest = target / source.name
            try:
                if app_dest.exists():
                    shutil.rmtree(str(app_dest))
                shutil.copytree(str(source), str(app_dest))
            except PermissionError:
                self._report_error(
                    f"权限不足，无法写入:\n{app_dest}\n\n"
                    "请选择其他目录，或以管理员身份运行"
                )
                return
            except Exception as e:
                self._report_error(f"复制失败:\n{e}")
                return
            self._report(70, "正在创建快捷方式...")
            self._create_shortcuts(target, app_dest)
        else:
            # Windows / 通用：逐文件复制
            for i, src in enumerate(files):
                try:
                    rel = src.relative_to(source)
                    dst = target / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(src), str(dst))
                except Exception as e:
                    self._report_error(f"复制失败: {src.name}\n{e}")
                    return
                pct = 10 + int((i + 1) / total * 60)
                if i % 50 == 0 or pct % 10 == 0:
                    self._report(pct, f"正在安装... ({i+1}/{total})")
            self._report(70, "正在创建快捷方式...")
            self._create_shortcuts(target)

        self._report(85, "正在注册卸载信息...")
        self._register_uninstall(target)

        self._report(95, "正在复制卸载程序...")
        self._copy_uninstaller(target)

        self._report(100, "安装完成！")

    def _create_shortcuts(self, target: Path, app_bundle: Path = None):
        if IS_WIN:
            exe_name = get_exe_name()
            exe = target / exe_name
            if not exe.exists():
                return
            self._create_desktop_shortcut_win(exe)
            self._create_start_menu_win(target, exe)
        elif IS_MAC and app_bundle:
            self._create_desktop_shortcut_mac(app_bundle)

    def _create_desktop_shortcut_win(self, target_exe: Path):
        name = "闲鱼数据采集分析工具"
        ps = f'''
$ws = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath("Desktop")
$lnk = Join-Path $desktop "{name}.lnk"
$sc = $ws.CreateShortcut($lnk)
$sc.TargetPath = "{target_exe}"
$sc.WorkingDirectory = "{target_exe.parent}"
$sc.Description = "闲鱼数据采集分析工具"
$sc.IconLocation = "{target_exe}"
$sc.Save()
'''
        try:
            subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                           "-Command", ps], capture_output=True, timeout=30)
        except Exception:
            pass

    def _create_start_menu_win(self, target: Path, target_exe: Path):
        name = "闲鱼数据采集分析工具"
        uninst_name = get_uninstaller_name()
        try:
            programs = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        except KeyError:
            return
        group = programs / name
        group.mkdir(parents=True, exist_ok=True)

        lnk_main = group / f"{name}.lnk"
        ps = f'''
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut("{lnk_main}")
$sc.TargetPath = "{target_exe}"
$sc.WorkingDirectory = "{target_exe.parent}"
$sc.Description = "闲鱼数据采集分析工具"
$sc.IconLocation = "{target_exe}"
$sc.Save()

$sc2 = $ws.CreateShortcut("{group / '卸载闲鱼数据采集分析工具.lnk'}")
$sc2.TargetPath = "{target / uninst_name}"
$sc2.WorkingDirectory = "{target}"
$sc2.Description = "卸载闲鱼数据采集分析工具"
$sc2.Save()
'''
        try:
            subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                           "-Command", ps], capture_output=True, timeout=30)
        except Exception:
            pass

    def _create_desktop_shortcut_mac(self, app_bundle: Path):
        """macOS: 在桌面创建 .app 的替身(alias)"""
        name = app_bundle.name
        desktop = Path.home() / "Desktop"
        shortcut = desktop / name
        try:
            os.symlink(str(app_bundle), str(shortcut))
        except FileExistsError:
            pass
        except Exception:
            pass

    def _register_uninstall(self, target: Path):
        if sys.platform != "win32":
            return
        import winreg
        name = "闲鱼数据采集分析工具"
        uninstaller = target / "闲鱼工具卸载程序.exe"
        ver = load_version().get("version", "3.2.0")

        values = {
            "DisplayName": name,
            "UninstallString": str(uninstaller),
            "InstallLocation": str(target),
            "DisplayVersion": ver,
            "Publisher": "闲鱼工具",
            "NoModify": 1,
            "NoRepair": 1,
        }
        for hive in [winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER]:
            try:
                key = winreg.CreateKey(hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{name}")
                for k, v in values.items():
                    reg_type = winreg.REG_DWORD if isinstance(v, int) else winreg.REG_SZ
                    winreg.SetValueEx(key, k, 0, reg_type, v)
                winreg.CloseKey(key)
                return
            except (OSError, PermissionError):
                continue

    def _copy_uninstaller(self, target: Path):
        """从内嵌数据中解出卸载程序，复制到安装目录"""
        uninst_name = get_uninstaller_name()
        uninst_dest = target / uninst_name

        if uninst_dest.exists():
            shutil.rmtree(str(uninst_dest))

        # 从 bundled app 的 Resources 中找卸载程序
        bundled = get_bundled_app_dir()
        uninst_src = bundled / "Contents" / "Resources" / uninst_name
        if uninst_src.exists():
            shutil.copytree(str(uninst_src), str(uninst_dest))
            return

        # Fallback: 在 app 根目录或 _MEIPASS 中递归查找
        for search_root in [bundled.parent, bundled]:
            for item in search_root.rglob(uninst_name):
                if item.is_dir() and item != uninst_dest:
                    shutil.copytree(str(item), str(uninst_dest))
                    return

    def _report(self, pct: int, msg: str):
        self.root.after(0, lambda: [
            self.progress_var.set(pct),
            self.status_var.set(msg)
        ])

    def _report_error(self, msg: str):
        self.root.after(0, lambda: [
            self.status_var.set(msg),
            messagebox.showerror("安装失败", msg),
            self.install_btn.config(state=tk.NORMAL, text="重试安装"),
            setattr(self, '_installing', False)
        ])

    def _poll(self, thread):
        if thread.is_alive():
            self.root.after(200, lambda: self._poll(thread))
        else:
            self.install_btn.config(state=tk.NORMAL, text="安装完成 ✓")
            if self.launch_var.get():
                if IS_MAC:
                    installed_app = Path(self.path_var.get()) / get_exe_name()
                    if installed_app.exists():
                        subprocess.Popen(["open", str(installed_app)])
                else:
                    target = Path(self.path_var.get()) / get_exe_name()
                    if target.exists():
                        subprocess.Popen([str(target)], cwd=str(target.parent))

    def run(self):
        self.root.mainloop()

def main():
    app = InstallerWindow()
    app.run()

if __name__ == "__main__":
    main()
