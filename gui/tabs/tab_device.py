"""设备管理Tab：多设备列表 + 连接/断开 + 进度显示（全中文）"""
import tkinter as tk
from tkinter import ttk
import threading

from gui.theme import SURF, FG, FG_M, ACC, SUCC, DANGER, WARN, BRD, FONTS
from gui.widgets.tree_helpers import make_columns

STATUS_MAP = {
    "idle": "空闲",
    "connecting": "连接中...",
    "connected": "已连接",
    "collecting": "采集中",
    "error": "连接失败",
    "disconnected": "未连接",
}

STATUS_COLORS = {
    "空闲": "#9CA3AF",
    "连接中...": "#F59E0B",
    "已连接": "#10B981",
    "采集中": "#3B82F6",
    "连接失败": "#EF4444",
    "未连接": "#9CA3AF",
}


class DeviceTab:
    def __init__(self, parent: ttk.Frame, app):
        self.parent = parent
        self.app = app
        self._engine = None
        self._status_cache = {}
        self._build_ui()

    def set_engine(self, engine):
        self._engine = engine
        self._refresh_list()

    def _build_ui(self):
        # 工具栏
        toolbar = tk.Frame(self.parent, bg=SURF)
        toolbar.pack(fill=tk.X, padx=8, pady=(8, 4))

        ttk.Button(toolbar, text="扫描设备", command=self._scan).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="连接选中", command=self._connect).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="断开连接", command=self._disconnect).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="全部连接", command=self._connect_all).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="全部断开", command=self._disconnect_all).pack(side=tk.LEFT, padx=2)

        # 添加设备区域
        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=10, pady=2)

        ttk.Label(toolbar, text="名称:", font=FONTS["ui"]).pack(side=tk.LEFT, padx=(2, 2))
        self._name_entry = ttk.Entry(toolbar, width=10, font=FONTS["ui"])
        self._name_entry.pack(side=tk.LEFT, padx=2)
        self._name_entry.insert(0, "Redmi")

        ttk.Label(toolbar, text="地址:", font=FONTS["ui"]).pack(side=tk.LEFT, padx=2)
        self._addr_entry = ttk.Entry(toolbar, width=14, font=FONTS["ui"])
        self._addr_entry.pack(side=tk.LEFT, padx=2)
        self._addr_entry.insert(0, "127.0.0.1:27042")

        ttk.Button(toolbar, text="添加设备", command=self._add_device).pack(side=tk.LEFT, padx=4)

        # 设备列表
        tree_frame = tk.Frame(self.parent, bg=SURF)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        columns = [
            ("设备名称", 100, "w"),
            ("连接地址", 160, "w"),
            ("设备类型", 60, "center"),
            ("Android版本", 70, "center"),
            ("Gadget模式", 70, "center"),
            ("连接状态", 80, "center"),
            ("采集进度", 120, "w"),
        ]
        self._tree = ttk.Treeview(tree_frame, columns=[c[0] for c in columns], show="headings", selectmode="browse")
        make_columns(self._tree, columns)

        scrollbar = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self._tree.yview)
        self._tree.configure(yscrollcommand=scrollbar.set)
        self._tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # 状态标签颜色
        for status, color in STATUS_COLORS.items():
            self._tree.tag_configure(f"s_{status}", foreground=color)

        # 设备详情 + 使用说明
        detail_frame = tk.LabelFrame(self.parent, text="设备详情", font=FONTS["ui_bold"], padx=8, pady=4)
        detail_frame.pack(fill=tk.X, padx=8, pady=4)

        self._detail_text = tk.Text(detail_frame, height=5, font=FONTS["mono"], wrap=tk.WORD,
                                    bg=SURF, fg=FG, relief=tk.FLAT)
        self._detail_text.pack(fill=tk.BOTH)

        self._show_help()

    def _show_help(self):
        self._detail_text.configure(state=tk.NORMAL)
        self._detail_text.delete("1.0", tk.END)
        self._detail_text.insert(tk.END, "使用说明:\n\n"
            "1. 确保手机已通过USB连接电脑，并开启了USB调试\n"
            "2. 确认手机上已安装带Frida Gadget的闲鱼App\n"
            "3. 点击【扫描设备】自动发现ADB设备\n"
            "4. 选中设备后点击【连接选中】或直接【全部连接】\n"
            "5. 连接成功后状态栏会显示绿色'已连接'\n"
            "6. 如果使用Gadget模式，App需要先手动打开，连接时会自动等待Gadget就绪\n\n"
            "常见问题:\n"
            "- 连接失败: 检查手机USB调试是否已授权\n"
            "- Gadget未就绪: 确保已安装带Gadget的闲鱼APK，并已打开App\n"
            "- 端口被占用: 检查是否有其他frida进程在使用27042端口")
        self._detail_text.configure(state=tk.DISABLED)

    def _scan(self):
        if not self._engine:
            self._detail_text.configure(state=tk.NORMAL)
            self._detail_text.delete("1.0", tk.END)
            self._detail_text.insert(tk.END, "设备引擎未初始化，请检查配置")
            self._detail_text.configure(state=tk.DISABLED)
            return

        self._detail_text.configure(state=tk.NORMAL)
        self._detail_text.delete("1.0", tk.END)
        self._detail_text.insert(tk.END, "正在扫描ADB设备...\n")
        self._detail_text.configure(state=tk.DISABLED)

        found = self._engine.scan()
        self._refresh_list()

        self._detail_text.configure(state=tk.NORMAL)
        if found:
            self._detail_text.insert(tk.END, f"发现 {len(found)} 个新设备\n")
            for d in found:
                self._detail_text.insert(tk.END, f"  {d.name} ({d.adb_addr})\n")
        else:
            self._detail_text.insert(tk.END, "未发现新设备（已全部在列表中）\n")
        self._detail_text.see(tk.END)
        self._detail_text.configure(state=tk.DISABLED)

    def _connect(self):
        sel = self._tree.selection()
        if not sel:
            self._detail_text.configure(state=tk.NORMAL)
            self._detail_text.delete("1.0", tk.END)
            self._detail_text.insert(tk.END, "请先在列表中选中一个设备")
            self._detail_text.configure(state=tk.DISABLED)
            return
        if not self._engine:
            return
        addr = self._tree.item(sel[0], "values")[1]
        self._update_status(addr, "连接中...")

        self._detail_text.configure(state=tk.NORMAL)
        self._detail_text.insert(tk.END, f"\n正在连接 {addr}...\n")
        self._detail_text.see(tk.END)
        self._detail_text.configure(state=tk.DISABLED)

        threading.Thread(target=self._do_connect, args=(addr,), daemon=True).start()

    def _do_connect(self, addr: str):
        state = self._engine.connect(addr)
        status = "已连接" if state.connected else "连接失败"
        self._update_status(addr, status)

        self.app.root.after(0, self._refresh_list)

        detail = ""
        if state.connected:
            dev = self._engine.get_active()
            detail = f"\n连接成功! 设备: {dev.name}, Android {dev.android_ver}, PID {dev.app_pid}\n"
        else:
            detail = f"\n连接失败: {state.last_error}\n"
            detail += "请检查:\n"
            detail += "  1. 手机USB调试是否已开启\n"
            detail += "  2. 带Frida Gadget的闲鱼App是否已安装并打开\n"
            detail += "  3. ADB端口转发是否正常\n"

        self.app.root.after(0, lambda: self._append_detail(detail))
        self.app.root.after(0, lambda: self.app.status_bar.set_device(
            self._engine.get_active().name if self._engine.get_active() else "",
            status
        ))

    def _disconnect(self):
        if not self._engine:
            return
        active = self._engine.get_active()
        self._engine.disconnect()
        self._refresh_list()
        self.app.root.after(0, lambda: self.app.status_bar.set_device(""))
        if active:
            self._append_detail(f"\n已断开 {active.name}\n")

    def _connect_all(self):
        if not self._engine:
            return
        devs = self._engine.list_devices()
        if not devs:
            self._append_detail("\n没有可连接的设备，请先【扫描设备】\n")
            return
        self._append_detail(f"\n开始连接全部 {len(devs)} 个设备...\n")
        for dev in devs:
            self._update_status(dev.adb_addr, "连接中...")
            threading.Thread(target=self._do_connect, args=(dev.adb_addr,), daemon=True).start()

    def _disconnect_all(self):
        if not self._engine:
            return
        self._engine.disconnect()
        self._refresh_list()
        self.app.root.after(0, lambda: self.app.status_bar.set_device(""))
        self._append_detail("\n已断开所有设备\n")

    def _add_device(self):
        name = self._name_entry.get().strip()
        addr = self._addr_entry.get().strip()
        if not name or not addr:
            self._append_detail("\n请输入设备名称和连接地址\n")
            return
        if not self._engine:
            return

        self._engine.add_device(name, addr)
        self._name_entry.delete(0, tk.END)
        self._refresh_list()
        self._append_detail(f"\n已添加设备: {name} ({addr})\n")

    def _update_status(self, addr: str, status: str):
        self._status_cache[addr] = status
        self.app.root.after(0, lambda: self._apply_status(addr, status))

    def _apply_status(self, addr: str, status: str):
        for item in self._tree.get_children():
            if self._tree.item(item, "values")[1] == addr:
                vals = list(self._tree.item(item, "values"))
                vals[5] = status
                self._tree.item(item, values=vals, tags=(f"s_{status}",))
                return

    def _refresh_list(self):
        self._tree.delete(*self._tree.get_children())
        if not self._engine:
            return
        active = self._engine.get_active()
        for dev in self._engine.list_devices():
            is_active = active and active.adb_addr == dev.adb_addr
            state = self._engine.get_state()
            if is_active and state.connected:
                status = "已连接"
            elif is_active and not state.connected:
                status = self._status_cache.get(dev.adb_addr, "未连接")
            else:
                status = self._status_cache.get(dev.adb_addr, "未连接")

            self._tree.insert("", tk.END, values=(
                dev.name,
                dev.adb_addr,
                "真机" if dev.type in ("usb", "wifi") else "模拟器",
                dev.android_ver or "未知",
                "已启用" if dev.use_gadget else "未启用",
                status,
                "",
            ), tags=(f"s_{status}",))

    def _append_detail(self, text: str):
        self._detail_text.configure(state=tk.NORMAL)
        self._detail_text.insert(tk.END, text)
        self._detail_text.see(tk.END)
        self._detail_text.configure(state=tk.DISABLED)
