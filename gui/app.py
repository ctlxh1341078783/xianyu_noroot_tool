"""闲鱼数据采集分析工具 — 主应用窗口"""
import json
import shutil
import threading
import zipfile
import tempfile
import urllib.request
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path

from gui.theme import BG, SURF, FG, FG_M, ACC, ACC_H, SUCC, FONTS
from gui.widgets.status_bar import StatusBar
from gui.widgets.log_panel import LogPanel
from gui.dialogs.settings_dialog import SettingsDialog
from utils.config_manager import ConfigManager
from utils.log_manager import get_logger
from utils.platform_utils import is_frozen, resource_path, is_installed, get_install_dir


class XianyuApp:
    def __init__(self):
        self.root = tk.Tk()
        self._version_info = self._load_version()
        ver = self._version_info.get("version", "3.2.0")
        self.root.title(f"闲鱼数据采集分析工具 v{ver}")
        self.root.geometry("1280x840")
        self.root.configure(bg=BG)
        self.root.minsize(1024, 680)

        self._set_app_icon()
        self._setup_style()

        # 配置
        self.config = ConfigManager()
        self.config.load_all()

        # 中控数据总线
        self._data = {}
        self._tabs = {}  # name -> controller instance

        # 日志
        self.logger = get_logger()
        self.logger.setup_file()

        # 引擎（延迟初始化）
        self._engines = {}

        # 构建UI：左右分栏（Notebook | 日志面板）
        self._build_menu()
        self._build_top_bar()
        self._build_main_area()  # 替代 _build_notebook + _build_log_panel

        # 注册日志回调
        self.logger.add_gui_callback(self._on_log)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # 延迟初始化引擎：窗口先渲染，引擎后台加载，避免启动白屏
        self.status_bar.set_progress("正在初始化引擎...")
        self.root.after(100, self._deferred_init)

    def _setup_style(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=FG, font=FONTS["ui"])
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", padding=[16, 6], font=FONTS["ui_bold"])
        style.map("TNotebook.Tab",
                  background=[("selected", ACC)],     # 选中Tab橘色背景
                  foreground=[("selected", "white")])  # 选中Tab白色文字
        style.configure("Status.TFrame", background=SURF)
        style.configure("TLabelframe", background=BG)
        style.configure("TLabelframe.Label", background=BG, font=FONTS["ui_bold"])
        # 按钮橘色主题
        style.configure("TButton", font=FONTS["ui"])
        style.map("TButton",
                  background=[("active", ACC), ("!active", SURF)],
                  foreground=[("active", "white")])

    def _load_version(self) -> dict:
        try:
            vp = resource_path("version.json")
            if vp.exists():
                return json.loads(vp.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {"version": "3.2.0", "build_date": "", "build_number": 1, "changelog": []}

    def _set_app_icon(self):
        try:
            ico = resource_path("assets/app_icon.ico")
            if ico.exists():
                self.root.iconbitmap(str(ico))
        except Exception:
            pass

    def _build_menu(self):
        menubar = tk.Menu(self.root, font=FONTS["ui"])
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="设置", command=self._open_settings)
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self._on_close)
        menubar.add_cascade(label="文件", menu=file_menu)
        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="关于", command=self._show_about)
        menubar.add_cascade(label="帮助", menu=help_menu)
        self.root.config(menu=menubar)

    def _build_top_bar(self):
        self.status_bar = StatusBar(self.root)
        self.status_bar.pack(fill=tk.X, side=tk.TOP)
        ttk.Separator(self.root, orient=tk.HORIZONTAL).pack(fill=tk.X, side=tk.TOP)

    def _build_main_area(self):
        """左右分栏：左侧Notebook(5个Tab) | 右侧日志面板"""
        main = tk.PanedWindow(self.root, orient=tk.HORIZONTAL, bg=BG,
                              sashwidth=4, sashrelief=tk.FLAT)
        main.pack(fill=tk.BOTH, expand=True, side=tk.TOP, padx=4, pady=(0, 4))

        # 左侧：Notebook
        self.notebook = ttk.Notebook(main)
        main.add(self.notebook, minsize=700)

        from gui.tabs.tab_device import DeviceTab
        from gui.tabs.tab_collection import CollectionTab
        from gui.tabs.tab_dashboard import DashboardTab
        from gui.tabs.tab_supply_finder import SupplyFinderTab
        from gui.tabs.tab_charts import ChartsTab

        tab_specs = [
            ("设备管理", DeviceTab),
            ("数据采集", CollectionTab),
            ("分析看板", DashboardTab),
            ("货源查找", SupplyFinderTab),
            ("图表分析", ChartsTab),
        ]

        for name, cls in tab_specs:
            frame = ttk.Frame(self.notebook)
            self.notebook.add(frame, text=name)
            tab = cls(frame, self)
            self._tabs[name] = tab

        # 右侧：日志面板（竖直，全高）
        self.log_panel = LogPanel(main, vertical=True)
        main.add(self.log_panel, minsize=280, width=320)

    def _init_engines(self):
        from engines.device_engine import DeviceEngine
        from engines.collection_engine import CollectionEngine
        from engines.supply_finder_engine import SupplyFinderEngine
        from engines.keyword_scorer_v3 import KeywordScorerV3
        from engines.product_scorer_v3 import ProductScorerV3

        # 设备引擎
        dev_engine = DeviceEngine(self.config.settings)
        self._engines["device"] = dev_engine

        # 评分引擎（传入配置以读取预检阈值等参数）
        kw_scorer = KeywordScorerV3(self.config.settings)
        pd_scorer = ProductScorerV3(self.config.settings)
        self._engines["kw_scorer"] = kw_scorer
        self._engines["pd_scorer"] = pd_scorer

        # 货源引擎
        sf_engine = SupplyFinderEngine(self.config.settings)
        self._engines["supply"] = sf_engine

        # 采集引擎（注入评分和货源引擎）
        col_engine = CollectionEngine(dev_engine, self.config.settings)
        col_engine.set_scorers(kw_scorer, pd_scorer)
        col_engine.set_supply_engine(sf_engine)
        self._engines["collection"] = col_engine

        # 注入Tab
        self._tabs["设备管理"].set_engine(dev_engine)
        self._tabs["数据采集"].set_engine(col_engine)
        self._tabs["分析看板"].set_scorers(kw_scorer, pd_scorer)
        self._tabs["货源查找"].set_engine(sf_engine)
        # 注入设备引擎（用于货源查找期间暂停全局健康检查）
        sf_engine.set_device_engine(dev_engine)
        # 货源引擎结果直接推送到Tab UI（采集流程中引擎自动启动时，结果也能显示在界面上）
        sf_engine.set_callbacks(on_ui_result=self._tabs["货源查找"]._on_result)

    def _deferred_init(self):
        """延迟初始化：窗口渲染后再加载引擎，避免启动白屏"""
        self._init_engines()
        self.status_bar.set_progress("就绪")
        # 货源Tab重型初始化（text2vec模型 + PDD连接）再延迟2秒
        self.root.after(2000, self._deferred_supply_init)

    def _deferred_supply_init(self):
        sf_tab = self._tabs.get("货源查找")
        if sf_tab and hasattr(sf_tab, 'on_mount'):
            sf_tab.on_mount()

    def _on_log(self, timestamp: str, level: str, msg: str):
        self.root.after(0, lambda: self.log_panel.add_log(timestamp, level, msg))

    def _open_settings(self):
        SettingsDialog(self.root, self.config)
        # 设置保存后，同步所有参数到各引擎
        settings = self.config.settings
        col_engine = self._engines.get("collection")
        if col_engine:
            col_engine.update_params(settings)
        kw_scorer = self._engines.get("kw_scorer")
        if kw_scorer:
            kw_scorer.update_params(settings)
        pd_scorer = self._engines.get("pd_scorer")
        if pd_scorer:
            pd_scorer.update_params(settings)
        sf_engine = self._engines.get("supply")
        if sf_engine:
            sf_engine.update_params(settings)
        # 同步货源Tab的API Key和Webhook URL
        sf_tab = self._tabs.get("货源查找")
        if sf_tab and hasattr(sf_tab, 'reload_config'):
            sf_tab.reload_config()

    def _show_about(self):
        ver = self._version_info.get("version", "3.2.0")
        build_date = self._version_info.get("build_date", "")
        build_num = self._version_info.get("build_number", 1)
        changelog = self._version_info.get("changelog", [])

        win = tk.Toplevel(self.root, bg=SURF)
        win.title("关于")
        win.geometry("420x380")
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()

        header = tk.Frame(win, bg=ACC)
        header.pack(fill=tk.X)
        tk.Label(header, text=f"  闲鱼数据采集分析工具 v{ver}", bg=ACC, fg="white",
                 font=("Microsoft YaHei", 13, "bold")).pack(side=tk.LEFT, padx=14, pady=12)

        body = tk.Frame(win, bg=SURF)
        body.pack(fill=tk.BOTH, expand=True, padx=20, pady=12)

        lines = [
            f"版本: {ver} (构建 {build_num})",
            f"构建日期: {build_date}",
            "基于 Frida Gadget 真机方案",
            "三阶段漏斗：预检 → 海选 → 精选+选品",
            "全自动货源查找 + AI匹配",
        ]
        for line in lines:
            tk.Label(body, text=line, bg=SURF, fg=FG,
                     font=FONTS["ui"]).pack(anchor="w", pady=1)

        if changelog:
            tk.Label(body, text="更新日志:", bg=SURF, fg=FG_M,
                     font=("Microsoft YaHei", 9, "bold")).pack(anchor="w", pady=(10, 2))
            for entry in changelog[:4]:
                tk.Label(body, text=f"  · {entry}", bg=SURF, fg="#666",
                         font=("Microsoft YaHei", 9)).pack(anchor="w")

        tk.Label(body, text="", bg=SURF).pack()  # spacer
        tk.Label(body, text="© 2026 闲鱼数据采集分析工具", bg=SURF, fg=FG_M,
                 font=FONTS["ui"]).pack(anchor="w", pady=(8, 0))

        btn_frame = tk.Frame(win, bg=SURF)
        btn_frame.pack(fill=tk.X, padx=20, pady=(0, 15))
        tk.Button(btn_frame, text="检查更新", command=self._check_for_updates,
                  bg=SUCC, fg="white", font=FONTS["ui_bold"],
                  relief=tk.FLAT, cursor="hand2", padx=16, pady=6).pack(side=tk.LEFT)
        tk.Button(btn_frame, text="关闭", command=win.destroy,
                  bg=ACC, fg="white", font=FONTS["ui_bold"],
                  relief=tk.FLAT, cursor="hand2", padx=24, pady=6).pack(side=tk.RIGHT)

    def _check_for_updates(self):
        """检查更新：从 GitHub Release 自动获取最新版本"""
        if not is_frozen():
            messagebox.showinfo("开发模式", "当前为开发模式，无需更新。\n\n打包安装后可通过 GitHub 自动检查更新。")
            return

        repo = self._version_info.get("github_repo", "")
        if not repo:
            # 手动选择文件夹作为备用
            self._check_updates_manual()
            return

        threading.Thread(target=self._check_updates_github, args=(repo,), daemon=True).start()

    def _check_updates_github(self, repo: str):
        """后台从 GitHub API 查询最新 Release"""
        api_url = f"https://api.github.com/repos/{repo}/releases/latest"
        try:
            req = urllib.request.Request(api_url)
            req.add_header("Accept", "application/vnd.github+json")
            req.add_header("User-Agent", "XianyuTool-Update")
            with urllib.request.urlopen(req, timeout=15) as resp:
                release = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            self.root.after(0, lambda: [
                messagebox.showwarning("检查失败",
                    f"无法连接到 GitHub，请检查网络。\n\n错误: {e}\n\n将切换到手动选择模式。"),
                self._check_updates_manual()
            ])
            return

        tag = release.get("tag_name", "")
        body = release.get("body", "")
        assets = release.get("assets", [])

        if not tag:
            self.root.after(0, lambda: messagebox.showinfo("已是最新", "当前已是最新版本"))
            return

        # 找 zip 下载链接
        download_url = None
        for a in assets:
            name = a.get("name", "")
            if name.endswith(".zip"):
                download_url = a.get("browser_download_url")
                break
        if not download_url and assets:
            download_url = assets[0].get("browser_download_url")

        cur_ver = self._version_info.get("version", "0")
        new_ver = tag.lstrip("v")

        if new_ver <= cur_ver:
            self.root.after(0, lambda: messagebox.showinfo("已是最新",
                f"当前 v{cur_ver} (构建 {self._version_info.get('build_number')}) 已是最新版本"))
            return

        self.root.after(0, lambda: self._confirm_github_update(
            new_ver, download_url, body, cur_ver))

    def _confirm_github_update(self, new_ver: str, download_url: str, body: str, cur_ver: str):
        """弹窗确认：是否从 GitHub 下载更新"""
        changelog = body[:500] if body else ""
        ok = messagebox.askyesno("发现新版本",
            f"当前: v{cur_ver}\n"
            f"新版: v{new_ver}\n\n"
            f"更新内容:\n{changelog}\n\n"
            f"是否立即下载并更新？")
        if not ok:
            return
        if not download_url:
            messagebox.showwarning("无法更新", "未找到下载链接，请手动更新")
            return
        self._download_and_update(download_url)

    def _check_updates_manual(self):
        """备用：手动选择文件夹更新"""
        src_dir = filedialog.askdirectory(title="选择新版本文件夹")
        if not src_dir:
            return
        src = Path(src_dir)
        # 尝试找 version.json
        src_ver = src / "version.json"
        if not src_ver.exists():
            src_ver = src / "_internal" / "version.json"
        if src_ver.exists():
            try:
                new_info = json.loads(src_ver.read_text(encoding="utf-8"))
                new_build = new_info.get("build_number", 0)
                if new_build <= self._version_info.get("build_number", 0):
                    messagebox.showinfo("已是最新", "当前已是最新版本")
                    return
                ok = messagebox.askyesno("发现新版本",
                    f"新版: v{new_info.get('version')} (构建 {new_build})\n\n是否更新？")
                if not ok:
                    return
                self._do_update_files(src if src_ver.parent.name != "_internal" else src_ver.parent.parent)
                return
            except Exception:
                pass
        # 没有 version.json，直接用选中的目录
        ok = messagebox.askyesno("确认更新", f"将用选中目录的内容更新当前程序\n\n{src}\n\n是否继续？")
        if not ok:
            return
        self._do_update_files(src)

    def _download_and_update(self, url: str):
        """后台下载 zip 并更新"""
        pw = tk.Toplevel(self.root, bg=SURF)
        pw.title("正在下载更新...")
        pw.geometry("400x150")
        pw.resizable(False, False)
        pw.transient(self.root)
        pw.grab_set()

        title_var = tk.StringVar(value="正在连接 GitHub...")
        tk.Label(pw, textvariable=title_var, bg=SURF, fg=FG,
                 font=FONTS["ui"]).pack(pady=(12, 8))
        bar = ttk.Progressbar(pw, mode="indeterminate")
        bar.pack(fill=tk.X, padx=20, pady=(0, 8))
        bar.start()
        status_var = tk.StringVar(value="下载中...")
        tk.Label(pw, textvariable=status_var, bg=SURF, fg=FG_M,
                 font=("Microsoft YaHei", 9)).pack()

        def run_download():
            tmpdir = Path(tempfile.mkdtemp(prefix="xianyu_update_"))
            zip_path = tmpdir / "update.zip"

            try:
                # 下载
                pw.after(0, lambda: title_var.set("正在下载更新包..."))
                req = urllib.request.Request(url)
                req.add_header("User-Agent", "XianyuTool-Update")
                with urllib.request.urlopen(req, timeout=300) as resp:
                    total_size = int(resp.headers.get("Content-Length", 0))
                    downloaded = 0
                    with open(zip_path, "wb") as f:
                        while True:
                            chunk = resp.read(8192)
                            if not chunk:
                                break
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total_size > 0:
                                pct = min(int(downloaded / total_size * 100), 100)
                                pw.after(0, lambda pct=pct: [
                                    status_var.set(f"{pct}%  "
                                        f"({downloaded // 1024 // 1024}MB / {total_size // 1024 // 1024}MB)"),
                                    bar.config(mode="determinate", maximum=100, value=pct)
                                ])
                            else:
                                pw.after(0, lambda d=downloaded: status_var.set(
                                    f"已下载 {d // 1024 // 1024} MB..."))

                # 解压
                pw.after(0, lambda: [
                    title_var.set("正在解压..."),
                    bar.config(mode="indeterminate"),
                    bar.start()
                ])
                extract_dir = tmpdir / "extracted"
                extract_dir.mkdir(exist_ok=True)
                with zipfile.ZipFile(zip_path, "r") as zf:
                    zf.extractall(extract_dir)

                # 找根目录（可能是嵌套的）
                contents = list(extract_dir.iterdir())
                if len(contents) == 1 and contents[0].is_dir():
                    src = contents[0]
                else:
                    src = extract_dir

                # 复制文件
                pw.after(0, lambda: [
                    title_var.set("正在安装更新..."),
                    bar.config(mode="indeterminate")
                ])
                self._do_update_files(src, silent=True)

                pw.after(0, lambda: [
                    pw.destroy(),
                    messagebox.showinfo("更新完成", "更新已完成，请重启程序以应用更改。\n\n程序即将退出。"),
                    self.root.destroy()
                ])

            except Exception as e:
                pw.after(0, lambda: [
                    pw.destroy(),
                    messagebox.showerror("更新失败", f"下载或安装失败:\n\n{e}")
                ])
            finally:
                try:
                    shutil.rmtree(tmpdir, ignore_errors=True)
                except Exception:
                    pass

        threading.Thread(target=run_download, daemon=True).start()

    def _do_update_files(self, src_dir: Path, silent: bool = False):
        """复制文件到安装目录"""
        target = get_install_dir() if is_frozen() else Path(__file__).parent.parent

        files_to_copy = []
        for f in src_dir.rglob("*"):
            if f.is_file():
                try:
                    rel = f.relative_to(src_dir)
                    files_to_copy.append((f, target / rel))
                except ValueError:
                    continue

        if not files_to_copy:
            if not silent:
                messagebox.showwarning("更新失败", "源目录中没有找到文件")
            return

        # 进度窗口
        pw = tk.Toplevel(self.root, bg=SURF)
        pw.title("正在更新...")
        pw.geometry("380x130")
        pw.resizable(False, False)
        pw.transient(self.root)
        pw.grab_set()

        total = len(files_to_copy)
        title_var = tk.StringVar(value=f"正在更新 (0/{total})...")
        tk.Label(pw, textvariable=title_var, bg=SURF, fg=FG,
                 font=FONTS["ui"]).pack(pady=(12, 8))
        bar = ttk.Progressbar(pw, mode="determinate", maximum=total)
        bar.pack(fill=tk.X, padx=20, pady=(0, 8))
        status_var = tk.StringVar(value="准备中...")
        tk.Label(pw, textvariable=status_var, bg=SURF, fg=FG_M,
                 font=("Microsoft YaHei", 9)).pack()

        def run_update():
            for i, (src, dst) in enumerate(files_to_copy):
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(src), str(dst))
                except Exception as e:
                    pw.after(0, lambda: [
                        status_var.set(f"复制失败: {src.name}"),
                        messagebox.showerror("更新失败", f"复制 {src.name} 失败:\n{e}")
                    ])
                    return
                pct = int((i + 1) / total * 100)
                pw.after(0, lambda i=i, pct=pct: [
                    bar.config(value=i + 1),
                    status_var.set(f"{pct}%  ({i+1}/{total})"),
                    title_var.set(f"正在更新 ({i+1}/{total})...")
                ])
            pw.after(0, lambda: [
                pw.destroy(),
                messagebox.showinfo("更新完成", "更新已完成，请重启程序以应用更改。")
            ])

        threading.Thread(target=run_update, daemon=True).start()

    def _on_close(self):
        self.root.destroy()

    def run(self):
        self.root.mainloop()

    def get_tab(self, name: str):
        return self._tabs.get(name)

    def get_engine(self, name: str):
        return self._engines.get(name)

    @property
    def data(self):
        return self._data

    def set_data(self, key: str, value):
        self._data[key] = value
