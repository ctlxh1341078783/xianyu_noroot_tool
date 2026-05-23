"""
闲鱼数据采集分析工具 — 卸载程序（Windows / macOS）
彻底清除：文件、快捷方式、开始菜单、注册表
自清洁：复制自身到 %TEMP%，从外部删除整个安装目录
"""
import sys
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
APP_NAME = "闲鱼数据采集分析工具"


def run_cleanup(install_dir: str):
    """在 %TEMP% 中运行，等待原进程退出后删除整个安装目录"""
    time.sleep(2)
    try:
        shutil.rmtree(install_dir, ignore_errors=True)
    except Exception:
        pass


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--cleanup":
        run_cleanup(sys.argv[2])
        sys.exit(0)

    app = UninstallerWindow()
    app.run()


class UninstallerWindow:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} — 卸载")
        self.root.geometry("500x360")
        self.root.resizable(True, True)
        self.root.minsize(460, 300)
        self._install_dir = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
        self._build_ui()

    def _build_ui(self):
        header = tk.Frame(self.root, bg="#e74c3c", height=50)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(header, text=f"⚠ 卸载 {APP_NAME}", font=("微软雅黑", 14, "bold"),
                 fg="white", bg="#e74c3c").pack(pady=10)

        body = tk.Frame(self.root, padx=20, pady=15)
        body.pack(fill=tk.BOTH, expand=True)

        if IS_MAC:
            # 查找已安装的主程序
            main_app = self._install_dir / f"{APP_NAME}.app"
            warnings = [
                "以下操作将不可撤销：",
                f"  删除主程序: {main_app}",
                f"  删除卸载程序自身",
                "  删除桌面快捷方式",
            ]
        elif IS_WIN:
            warnings = [
                "以下操作将不可撤销：",
                f"  删除安装目录: {self._install_dir}",
                "  删除桌面快捷方式",
                "  删除开始菜单文件夹",
                "  清除注册表记录",
            ]
        else:
            warnings = [
                "以下操作将不可撤销：",
                f"  删除安装目录: {self._install_dir}",
                "  删除桌面快捷方式",
            ]
        for w in warnings:
            fg = "#e74c3c" if "不可撤销" in w else "#333"
            tk.Label(body, text=w, font=("微软雅黑", 10), fg=fg,
                    anchor=tk.W, wraplength=440).pack(anchor=tk.W)

        self.confirm_var = tk.BooleanVar(value=False)
        tk.Checkbutton(body, text="我确认要彻底卸载此程序", variable=self.confirm_var,
                       font=("微软雅黑", 10, "bold")).pack(pady=(15, 5))

        self.status_var = tk.StringVar(value="")
        tk.Label(body, textvariable=self.status_var, font=("微软雅黑", 10), fg="#888",
                wraplength=440).pack()

        btn_frame = tk.Frame(body)
        btn_frame.pack(fill=tk.X, pady=(15, 0))
        tk.Button(btn_frame, text="确认卸载", command=self._do_uninstall,
                  bg="#e74c3c", fg="white", font=("微软雅黑", 11, "bold"),
                  relief=tk.FLAT, cursor="hand2", padx=25, pady=8).pack(side=tk.LEFT)
        tk.Button(btn_frame, text="取消", command=self.root.destroy,
                  font=("微软雅黑", 10), padx=15, pady=6).pack(side=tk.RIGHT)

    def _do_uninstall(self):
        if not self.confirm_var.get():
            messagebox.showwarning("提示", "请先勾选确认框")
            return

        self.root.config(cursor="watch")
        self.root.update()

        self.status_var.set("正在删除桌面快捷方式...")
        self.root.update()
        self._remove_shortcuts()

        if IS_WIN:
            self.status_var.set("正在清除注册表...")
            self.root.update()
            self._remove_registry()

        if IS_MAC:
            self.status_var.set("正在删除主程序...")
            self.root.update()
            self._remove_main_app()

        self.status_var.set("正在卸载...")
        self.root.update()
        self._self_destruct()

    def _remove_shortcuts(self):
        if IS_WIN:
            try:
                desktop = Path(os.environ.get("USERPROFILE", "")) / "Desktop"
                for name in [f"{APP_NAME}.lnk", APP_NAME]:
                    lnk = desktop / name
                    if lnk.exists():
                        os.remove(lnk)
            except Exception:
                pass
            try:
                programs = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
                group = programs / APP_NAME
                if group.exists():
                    shutil.rmtree(group)
            except Exception:
                pass
        elif IS_MAC:
            try:
                desktop = Path.home() / "Desktop"
                # 移除桌面替身/快捷方式
                for name in [f"{APP_NAME}.app", f"{APP_NAME}.command"]:
                    shortcut = desktop / name
                    if shortcut.exists():
                        if shortcut.is_symlink() or shortcut.is_file():
                            shortcut.unlink()
            except Exception:
                pass

    def _remove_main_app(self):
        """macOS: 删除已安装的主程序 .app"""
        main_app = self._install_dir / f"{APP_NAME}.app"
        if main_app.exists():
            try:
                shutil.rmtree(str(main_app))
            except Exception:
                pass

    def _remove_registry(self):
        import winreg
        for hive in [winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER]:
            try:
                key_path = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{APP_NAME}"
                winreg.DeleteKey(hive, key_path)
            except OSError:
                pass

    def _self_destruct(self):
        install_dir = str(self._install_dir)
        frozen = getattr(sys, 'frozen', False)
        my_exe = str(sys.executable) if frozen else __file__

        if IS_WIN:
            tmp_exe = Path(tempfile.gettempdir()) / "_xianyu_uninst_cleanup.exe"
            try:
                shutil.copy2(my_exe, str(tmp_exe))
                subprocess.Popen(
                    [str(tmp_exe), "--cleanup", install_dir],
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    close_fds=True,
                )
            except Exception:
                pass
        elif IS_MAC:
            # macOS: 删除主程序 .app 和卸载程序自身
            main_app = str(self._install_dir / f"{APP_NAME}.app")
            uninst_app = str(self._install_dir / "闲鱼工具卸载程序.app")
            sh_path = Path(tempfile.gettempdir()) / "_xianyu_uninst.sh"
            script = f'''#!/bin/bash
sleep 2
rm -rf "{main_app}"
rm -rf "{uninst_app}"
rm -f "$0"
'''
            sh_path.write_text(script)
            os.chmod(sh_path, 0o755)
            try:
                subprocess.Popen(["nohup", "bash", str(sh_path)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               close_fds=True)
            except Exception:
                pass
        else:
            sh_path = Path(tempfile.gettempdir()) / "_xianyu_uninst.sh"
            script = f'''#!/bin/bash
sleep 2
rm -rf "{install_dir}"
rm -f "$0"
'''
            sh_path.write_text(script)
            os.chmod(sh_path, 0o755)
            try:
                subprocess.Popen(["nohup", "bash", str(sh_path)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               close_fds=True)
            except Exception:
                pass

        self.root.destroy()
        sys.exit(0)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    main()
