"""
闲鱼数据采集分析工具 — 卸载程序（Windows / macOS）
彻底清除：文件、快捷方式、开始菜单、注册表
"""
import sys
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

class UninstallerWindow:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("闲鱼数据采集分析工具 — 卸载")
        self.root.geometry("500x360")
        self.root.resizable(True, True)
        self.root.minsize(460, 300)
        self._install_dir = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
        self._build_ui()

    def _build_ui(self):
        header = tk.Frame(self.root, bg="#e74c3c", height=50)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(header, text="⚠ 卸载闲鱼数据采集分析工具", font=("微软雅黑", 14, "bold"),
                 fg="white", bg="#e74c3c").pack(pady=10)

        body = tk.Frame(self.root, padx=20, pady=15)
        body.pack(fill=tk.BOTH, expand=True)

        if IS_WIN:
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
            tk.Label(body, text=w, font=("微软雅黑", 10), fg=fg, anchor=tk.W).pack(anchor=tk.W)

        self.confirm_var = tk.BooleanVar(value=False)
        tk.Checkbutton(body, text="我确认要彻底卸载此程序", variable=self.confirm_var,
                       font=("微软雅黑", 10, "bold")).pack(pady=(15, 5))

        self.status_var = tk.StringVar(value="")
        tk.Label(body, textvariable=self.status_var, font=("微软雅黑", 10), fg="#888").pack()

        btn_frame = tk.Frame(body)
        btn_frame.pack(fill=tk.X, pady=(15, 0))
        tk.Button(btn_frame, text="确认卸载", command=self._do_uninstall,
                  bg="#e74c3c", fg="white", font=("微软雅黑", 11, "bold"),
                  relief=tk.FLAT, cursor="hand2", padx=25, pady=6).pack(side=tk.LEFT)
        tk.Button(btn_frame, text="取消", command=self.root.destroy,
                  font=("微软雅黑", 10), padx=15).pack(side=tk.RIGHT)

    def _do_uninstall(self):
        if not self.confirm_var.get():
            messagebox.showwarning("提示", "请先勾选确认框")
            return

        self.status_var.set("正在卸载...")

        self._remove_shortcuts()

        if IS_WIN:
            self._remove_registry()

        self._self_destruct()

    def _remove_shortcuts(self):
        if IS_WIN:
            try:
                desktop = Path(os.environ.get("USERPROFILE", "")) / "Desktop"
                for name in ["闲鱼数据采集分析工具.lnk", "闲鱼数据采集分析工具"]:
                    lnk = desktop / name
                    if lnk.exists():
                        os.remove(lnk)
            except Exception:
                pass
            try:
                programs = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
                group = programs / "闲鱼数据采集分析工具"
                if group.exists():
                    shutil.rmtree(group)
            except Exception:
                pass
        elif IS_MAC:
            try:
                desktop = Path.home() / "Desktop"
                shortcut = desktop / "闲鱼数据采集分析工具.command"
                if shortcut.exists():
                    os.remove(shortcut)
            except Exception:
                pass

    def _remove_registry(self):
        import winreg
        for hive in [winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER]:
            try:
                key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\闲鱼数据采集分析工具"
                winreg.DeleteKey(hive, key_path)
            except OSError:
                pass

    def _self_destruct(self):
        install_dir = str(self._install_dir)
        self.status_var.set("正在清理文件...")

        if IS_WIN:
            ps_cmd = (
                f'Start-Sleep -Seconds 3; '
                f'Remove-Item -Path "{install_dir}" -Recurse -Force -ErrorAction SilentlyContinue'
            )
            subprocess.Popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd],
                creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.DETACHED_PROCESS,
                close_fds=True,
            )
        else:
            script = f'''#!/bin/bash
sleep 2
rm -rf "{install_dir}"
rm -f "$0"
'''
            tmpsh = Path(tempfile.gettempdir()) / "_xianyu_uninst.sh"
            tmpsh.write_text(script)
            os.chmod(tmpsh, 0o755)
            subprocess.Popen(["nohup", "bash", str(tmpsh)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           close_fds=True)

        self.root.destroy()
        sys.exit(0)

    def run(self):
        self.root.mainloop()

def main():
    app = UninstallerWindow()
    app.run()

if __name__ == "__main__":
    main()
