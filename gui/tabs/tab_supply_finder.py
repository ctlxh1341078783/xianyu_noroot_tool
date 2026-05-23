"""货源查找Tab：完整移植原版SupplyFinderTab全部功能
双路搜索（标题+图搜）+ AI匹配 + 利润评估 + 四象限 + Excel导出
适配项目主题/配置/评分系统，推送评分使用v3 ProductScorerV3
布局：左(控制面板) | 中(结果) | 右(日志)
"""
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import threading
import time
import queue
import os
import webbrowser
import json as _json
from datetime import datetime, timedelta
from typing import Optional, Set, Dict, List

from gui.theme import SURF, SURF2, FG, FG_M, FG_L, ACC, ACC_H, SUCC, DANGER, WARN, BRD, BRD_F, FONTS
from utils.log_manager import get_logger

import sys
from engines.pdd_supply_finder_v2 import (
    PinduoduoMobileController,
    MobileSupplyScheduler,
    TitleCleanerAI,
    SameProductMatcher,
    ProfitAnalyzer,
    evaluate_supply_quadrant,
    get_ai_cleaner,
    set_ai_api_key,
    export_supply_to_excel,
    export_listing_advice_to_excel,
    SourceDetailWindow,
)


def _make_sortable(tree: ttk.Treeview, numeric_cols: set):
    """让Treeview列头可点击排序"""
    tree._sort_state: dict = {}

    def sort_col(col):
        reverse = tree._sort_state.get(col, False)
        data = [(tree.set(k, col), k) for k in tree.get_children('')]

        def key_fn(t):
            v = t[0]
            if col in numeric_cols:
                try:
                    return float(v)
                except (ValueError, TypeError):
                    return -999999
            return v

        data.sort(key=key_fn, reverse=reverse)
        for i, (_, k) in enumerate(data):
            tree.move(k, '', i)

        tree._sort_state[col] = not reverse
        for c in tree['columns']:
            arrow = ''
            if c == col:
                arrow = ' ▼' if reverse else ' ▲'
            tree.heading(c, text=c.rstrip(' ▲▼') + arrow,
                         command=lambda _c=c: sort_col(_c))

    for col in tree['columns']:
        tree.heading(col, text=col, command=lambda c=col: sort_col(c))


class SupplyFinderTab:
    """货源查找Tab — 完整移植原版SupplyFinderTab全部功能"""

    def __init__(self, parent: ttk.Frame, app):
        self.parent = parent
        self.app = app
        self._engine = None
        self._logger = get_logger()

        self.scheduler: Optional[MobileSupplyScheduler] = None
        self.task_queue: queue.Queue = queue.Queue()
        self._result_records: list = []
        self.controller: Optional[PinduoduoMobileController] = None

        self.use_ai_var = tk.BooleanVar(value=True)
        self.api_key_var = tk.StringVar()
        self._api_testing = False
        self._pushed_ids: set = set()

        self._scheduled_push_thread: Optional[threading.Thread] = None
        self._scheduled_push_running = False

        # Webhook（企业微信通知：风控告警 + 正利润推送）
        self._wechat_webhook: str = ""

        self._build_ui()
        self._load_api_key()
        self._load_webhook_url()

    def set_engine(self, engine):
        self._engine = engine

    # ── API Key 管理 ──

    def _load_api_key(self):
        settings = self.app.config.settings
        api_key = settings.get("api", {}).get("zhipu_api_key", "")
        if api_key:
            self.api_key_var.set(api_key)
            set_ai_api_key(api_key, self._log)
            self._log("已加载保存的 API Key")
            self._update_api_status(True)

    def _save_api_key(self):
        api_key = self.api_key_var.get()
        self.app.config.settings.setdefault("api", {})["zhipu_api_key"] = api_key
        return self.app.config.save_settings()

    def _load_webhook_url(self):
        webhook_url = self.app.config.settings.get("api", {}).get("webhook_url", "")
        if webhook_url:
            self._wechat_webhook = webhook_url
            self._log("已加载企业微信 Webhook")
            self._update_webhook_status(True)
            # 同步给调度器
            if self.scheduler:
                self.scheduler._wechat_webhook = webhook_url
        else:
            self._update_webhook_status(False)

    def reload_config(self):
        """设置保存后由主窗口调用，同步最新 API Key 和 Webhook URL"""
        self._load_api_key()
        self._load_webhook_url()

    def _update_webhook_status(self, configured: bool):
        try:
            if configured:
                self.webhook_status_lbl.config(text="已配置 ✓", fg=SUCC)
            else:
                self.webhook_status_lbl.config(text="未配置（风控告警将跳过）", fg=WARN)
        except Exception:
            pass

    # ── UI组件工厂 ──

    def _btn(self, p, text, cmd, bg=None, width=None, **kw):
        return tk.Button(p, text=text, command=cmd,
                         bg=bg or SURF2, fg=FG,
                         activebackground=BRD, relief=tk.FLAT, cursor="hand2",
                         font=FONTS["ui"], padx=10, pady=4,
                         borderwidth=0, width=width, **kw)

    def _entry(self, p, width=8, default=''):
        e = tk.Entry(p, width=width, bg=SURF, fg=FG,
                     insertbackground=ACC, relief=tk.FLAT,
                     highlightbackground=BRD, highlightthickness=1,
                     font=FONTS["ui"])
        if default:
            e.insert(0, default)
        return e

    def _section(self, parent, title):
        """创建统一的LabelFrame卡片"""
        frame = tk.LabelFrame(parent, text=title, bg=SURF, fg=FG_M,
                              font=FONTS["ui"], relief=tk.FLAT,
                              highlightbackground=BRD, highlightthickness=1)
        return frame

    # ── 日志（线程安全，统一走全局Logger → 右侧日志面板） ──

    def _log(self, msg: str):
        """所有日志统一走全局 Logger → 右侧全局日志面板"""
        self._logger.info(f"[货源] {msg}")

    def _get_redmi_serial(self) -> str:
        """货源查找固定走红米，从配置中读取其ADB序列号"""
        devices = self.app.config.settings.get("devices", [])
        for d in devices:
            if "redmi" in d.get("name", "").lower() or d.get("use_gadget"):
                return d.get("adb_addr", "")
        return devices[0].get("adb_addr", "") if devices else ""

    def _update_countdown(self, remaining: int):
        def _u():
            if remaining > 0:
                self.countdown_label.config(text=f"倒计时: {remaining}s", fg=DANGER)
            else:
                self.countdown_label.config(text="等待中", fg=FG_M)
        try:
            self.parent.after(0, _u)
        except Exception:
            pass

    # ══════════════════════════════════════════════════════════════
    # UI构建：左(控制面板) | 中(结果) | 右(日志)
    # ══════════════════════════════════════════════════════════════

    def _build_ui(self):
        # 外层用PanedWindow实现左右两栏，可拖动分隔条
        # 日志已统一到全局右侧日志面板，此处不再单独设置
        main = tk.PanedWindow(self.parent, orient=tk.HORIZONTAL, bg=SURF2,
                              sashwidth=4, sashrelief=tk.FLAT)
        main.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        # ═══ 左栏：可滚动控制面板 (380px) ═══
        self._build_left_panel(main)

        # ═══ 中栏：结果 (自适应，填满右侧) ═══
        self._build_center_panel(main)

        # 定时刷新API统计
        self._update_api_stats_display()

    def _build_left_panel(self, main):
        """左侧控制面板：可滚动，包含统计/AI/设备/参数/定时推送/操作"""
        left = tk.Frame(main, bg=SURF2, width=380)
        main.add(left, minsize=320, width=380)

        left_outer = tk.Frame(left, bg=SURF2)
        left_outer.pack(fill=tk.BOTH, expand=True)

        canvas = tk.Canvas(left_outer, bg=SURF2, highlightthickness=0)
        scrollbar = ttk.Scrollbar(left_outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        inner = tk.Frame(canvas, bg=SURF2)
        canvas_window = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _cfg_inner(event):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfig(canvas_window, width=event.width)
        def _cfg_canvas(event):
            canvas.itemconfig(canvas_window, width=event.width)

        inner.bind("<Configure>", _cfg_inner)
        canvas.bind("<Configure>", _cfg_canvas)

        def _wheel(e):
            canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        def _wheel_up(e):
            canvas.yview_scroll(-1, "units")
        def _wheel_down(e):
            canvas.yview_scroll(1, "units")

        for w in (canvas, inner):
            w.bind("<MouseWheel>", _wheel)
            w.bind("<Button-4>", _wheel_up)
            w.bind("<Button-5>", _wheel_down)

        self._left_canvas = canvas
        self._left_inner = inner

        # ── 统计卡片 ──
        stats_frame = self._section(inner, "实时统计")
        stats_frame.pack(fill=tk.X, padx=8, pady=(6, 4))

        stats_row = tk.Frame(stats_frame, bg=SURF)
        stats_row.pack(fill=tk.X, padx=8, pady=6)

        def stat_block(p, label, init='0', color=ACC):
            blk = tk.Frame(p, bg=SURF)
            blk.pack(side=tk.LEFT, expand=True)
            num = tk.Label(blk, text=init, bg=SURF, fg=color,
                          font=("Microsoft YaHei", 18, "bold"))
            num.pack()
            tk.Label(blk, text=label, bg=SURF, fg=FG_M, font=FONTS["ui"]).pack()
            return num

        self._stat_queue = stat_block(stats_row, "队列待处理", "0", DANGER)
        self._stat_done = stat_block(stats_row, "已处理", "0", SUCC)
        self._stat_profit = stat_block(stats_row, "有利润", "0", ACC)

        info_row = tk.Frame(stats_frame, bg=SURF)
        info_row.pack(fill=tk.X, padx=8, pady=(0, 6))
        self.countdown_label = tk.Label(info_row, text="等待中", bg=SURF, fg=FG_M, font=FONTS["ui"])
        self.countdown_label.pack(side=tk.LEFT)
        self.stats_label = tk.Label(info_row, text="最高利润率: — | 模型: 加载中",
                                     bg=SURF, fg=FG_M, font=FONTS["ui"])
        self.stats_label.pack(side=tk.RIGHT)

        # ── AI设置卡片 ──
        ai_frame = self._section(inner, "AI 标题清洗（智谱 GLM-4-Flash）")
        ai_frame.pack(fill=tk.X, padx=8, pady=(4, 4))

        ai_inner = tk.Frame(ai_frame, bg=SURF)
        ai_inner.pack(fill=tk.X, padx=6, pady=6)

        tk.Checkbutton(ai_inner, text="启用 AI 清洗（API 失效时自动降级）",
                       variable=self.use_ai_var, bg=SURF, fg=FG,
                       selectcolor=SURF, activebackground=SURF).pack(anchor="w", pady=(0, 5))

        api_row = tk.Frame(ai_inner, bg=SURF)
        api_row.pack(fill=tk.X, pady=3)
        tk.Label(api_row, text="API Key:", bg=SURF, fg=FG,
                 width=8, anchor='w').pack(side=tk.LEFT)
        self.api_entry = tk.Entry(api_row, bg=SURF2, fg=FG,
                                   show="*", relief=tk.FLAT,
                                   highlightbackground=BRD, highlightthickness=1,
                                   font=FONTS["ui"])
        self.api_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
        self.api_entry.bind('<KeyRelease>', lambda e: self.api_key_var.set(self.api_entry.get()))

        self.show_pwd = False
        self.toggle_pwd_btn = self._btn(api_row, "显示", self._toggle_password, bg=SURF2, width=4)
        self.toggle_pwd_btn.pack(side=tk.RIGHT)

        btn_row = tk.Frame(ai_inner, bg=SURF)
        btn_row.pack(fill=tk.X, pady=5)
        self.test_api_btn = self._btn(btn_row, "测试连接", self._test_api, width=9)
        self.test_api_btn.pack(side=tk.LEFT, padx=(0, 5))
        self.save_api_btn = self._btn(btn_row, "保存 Key", self._save_api_key_ui, width=9)
        self.save_api_btn.pack(side=tk.LEFT)

        self.api_status = tk.Label(ai_inner, text="未配置 API Key", bg=SURF, fg=FG_M, font=FONTS["ui"])
        self.api_status.pack(anchor="w", pady=(5, 0))
        self.api_stats_label = tk.Label(ai_inner, text="API 调用: 0次 | 成功: 0次", bg=SURF,
                                         fg=FG_M, font=FONTS["ui"])
        self.api_stats_label.pack(anchor="w", pady=(2, 0))

        # ── 设备卡片 ──
        device_frame = self._section(inner, "设备")
        device_frame.pack(fill=tk.X, padx=8, pady=(4, 4))

        dev_row = tk.Frame(device_frame, bg=SURF)
        dev_row.pack(fill=tk.X, padx=6, pady=4)
        self.launch_btn = self._btn(dev_row, "启动拼多多", self._launch_app, state=tk.DISABLED, width=12)
        self.launch_btn.pack(side=tk.LEFT)
        self.device_status = tk.Label(dev_row, text="自动连接中...", bg=SURF, fg=FG_M, font=FONTS["ui"])
        self.device_status.pack(side=tk.LEFT, padx=(10, 0))

        dev_row2 = tk.Frame(device_frame, bg=SURF)
        dev_row2.pack(fill=tk.X, padx=6, pady=(0, 6))
        self._btn(dev_row2, "重连设备", self._reconnect_device, width=10).pack(side=tk.LEFT)
        self._btn(dev_row2, "重载模型", self._reload_model, width=10).pack(side=tk.LEFT, padx=(6, 0))

        # ── 通用设置（DDK API + 手机兜底都适用）──
        sf_settings = self.app.config.settings.get("supply_finder", {})

        common_frame = self._section(inner, "通用设置（DDK API + 手机兜底）")
        common_frame.pack(fill=tk.X, padx=8, pady=(4, 4))

        common_grid = tk.Frame(common_frame, bg=SURF)
        common_grid.pack(fill=tk.X, padx=6, pady=6)

        common_params = [
            ("相似阈值:", "sim_thresh_e", str(sf_settings.get("sim_threshold", 0.8)), 6),
            ("标题翻页:", "scroll_pages_e", str(sf_settings.get("scroll_pages", 5)), 6),
            ("最多采集:", "max_items_e", str(sf_settings.get("max_items", 20)), 6),
            ("推送评分:", "score_thresh_e", str(sf_settings.get("score_threshold", 75)), 6),
        ]

        for i, (label, attr, default, width) in enumerate(common_params):
            row, col = i // 2, i % 2
            f = tk.Frame(common_grid, bg=SURF)
            f.grid(row=row, column=col, sticky="ew", padx=4, pady=3)
            tk.Label(f, text=label, bg=SURF, fg=FG,
                     font=FONTS["ui"], width=9, anchor='w').pack(side=tk.LEFT)
            entry = self._entry(f, width=width, default=default)
            entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
            setattr(self, attr, entry)
        common_grid.columnconfigure(0, weight=1)
        common_grid.columnconfigure(1, weight=1)

        # AI同款比对开关（通用）
        self.use_ai_compare_var = tk.BooleanVar(value=sf_settings.get("use_ai_compare", True))
        toggle_row_c = tk.Frame(common_frame, bg=SURF)
        toggle_row_c.pack(fill=tk.X, padx=6, pady=(2, 4))
        tk.Checkbutton(toggle_row_c, text="AI批量同款比对（DDK+图搜都适用）",
                       variable=self.use_ai_compare_var,
                       bg=SURF, fg=FG, font=FONTS["ui"],
                       selectcolor=SURF).pack(side=tk.LEFT)

        # ── 手机兜底设置（仅 uiautomator2 触发）──
        fallback_frame = self._section(inner, "手机兜底设置（仅DDK无结果时触发）")
        fallback_frame.pack(fill=tk.X, padx=8, pady=(4, 4))

        fallback_grid = tk.Frame(fallback_frame, bg=SURF)
        fallback_grid.pack(fill=tk.X, padx=6, pady=6)

        fallback_params = [
            ("图搜翻页:", "img_scroll_pages_e", str(sf_settings.get("img_scroll_pages", 3)), 6),
            ("间隔(秒):", "delay_between_e", str(sf_settings.get("delay_between_products", 8)), 6),
            ("每N件休息:", "pause_every_e", str(sf_settings.get("pause_every", 5)), 6),
            ("休息(秒):", "pause_dur_e", str(sf_settings.get("pause_duration", 60)), 6),
        ]

        for i, (label, attr, default, width) in enumerate(fallback_params):
            row, col = i // 2, i % 2
            f = tk.Frame(fallback_grid, bg=SURF)
            f.grid(row=row, column=col, sticky="ew", padx=4, pady=3)
            tk.Label(f, text=label, bg=SURF, fg=FG,
                     font=FONTS["ui"], width=9, anchor='w').pack(side=tk.LEFT)
            entry = self._entry(f, width=width, default=default)
            entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
            setattr(self, attr, entry)
        fallback_grid.columnconfigure(0, weight=1)
        fallback_grid.columnconfigure(1, weight=1)

        # 图搜开关（兜底专属）
        self.use_img_search_var = tk.BooleanVar(value=sf_settings.get("use_img_search", True))
        toggle_row_f = tk.Frame(fallback_frame, bg=SURF)
        toggle_row_f.pack(fill=tk.X, padx=6, pady=(2, 4))
        tk.Checkbutton(toggle_row_f, text="启用以图搜款（标题搜0结果时自动触发）",
                       variable=self.use_img_search_var,
                       bg=SURF, fg=FG, font=FONTS["ui"],
                       selectcolor=SURF).pack(side=tk.LEFT)

        update_btn_frame = tk.Frame(fallback_frame, bg=SURF)
        update_btn_frame.pack(fill=tk.X, padx=6, pady=(0, 6))
        self.update_settings_btn = self._btn(update_btn_frame, "更新设置",
                                              self._update_scheduler_settings, bg=ACC, width=12)
        self.update_settings_btn.config(fg="white")
        self.update_settings_btn.pack(side=tk.RIGHT)
        tk.Label(update_btn_frame, text="修改参数后点击更新，立即生效", bg=SURF, fg=FG_M,
                 font=FONTS["ui"]).pack(side=tk.LEFT)

        # ── 定时推送（通用）──
        sched_frame = self._section(inner, "定时推送（通用）")
        sched_frame.pack(fill=tk.X, padx=8, pady=(4, 4))

        sched_inner = tk.Frame(sched_frame, bg=SURF)
        sched_inner.pack(fill=tk.X, padx=6, pady=6)

        r_sched = tk.Frame(sched_inner, bg=SURF)
        r_sched.pack(fill=tk.X, pady=2)
        self.enable_scheduled_push_var = tk.BooleanVar(value=True)
        tk.Checkbutton(r_sched, text="每天", variable=self.enable_scheduled_push_var,
                       bg=SURF, fg=FG, font=FONTS["ui"],
                       selectcolor=SURF).pack(side=tk.LEFT)
        self.sched_hour_e = self._entry(r_sched, width=4, default="8")
        self.sched_hour_e.pack(side=tk.LEFT, padx=(2, 2))
        tk.Label(r_sched, text="时", bg=SURF, fg=FG, font=FONTS["ui"]).pack(side=tk.LEFT)
        self.sched_min_e = self._entry(r_sched, width=4, default="0")
        self.sched_min_e.pack(side=tk.LEFT, padx=(2, 2))
        tk.Label(r_sched, text="分 自动推送正利润货源", bg=SURF, fg=FG_M,
                 font=FONTS["ui"]).pack(side=tk.LEFT, padx=(2, 0))

        self.sched_status_lbl = tk.Label(sched_inner, text="定时推送线程待启动",
                                          bg=SURF, fg=FG_M, font=FONTS["ui"])
        self.sched_status_lbl.pack(anchor="w", pady=(2, 4))

        r_webhook = tk.Frame(sched_inner, bg=SURF)
        r_webhook.pack(fill=tk.X, pady=(4, 2))
        tk.Label(r_webhook, text="企业微信通知:", bg=SURF, fg=FG, font=FONTS["ui"]).pack(side=tk.LEFT)
        self.webhook_status_lbl = tk.Label(r_webhook, text="加载中...", bg=SURF, fg=FG_M,
                                            font=FONTS["ui"])
        self.webhook_status_lbl.pack(side=tk.LEFT, padx=(6, 0))
        tk.Label(r_webhook, text="（风控告警 + 正利润推送 + Excel文件）", bg=SURF, fg=FG_M,
                 font=FONTS["ui"]).pack(side=tk.LEFT, padx=(2, 0))

        # ── 手机兜底保护（仅兜底方案）──
        protect_frame = self._section(inner, "手机兜底保护（仅兜底方案触发）")
        protect_frame.pack(fill=tk.X, padx=8, pady=(4, 4))

        protect_inner = tk.Frame(protect_frame, bg=SURF)
        protect_inner.pack(fill=tk.X, padx=6, pady=6)

        r_empty = tk.Frame(protect_inner, bg=SURF)
        r_empty.pack(fill=tk.X, pady=2)
        tk.Label(r_empty, text="连续", bg=SURF, fg=FG, font=FONTS["ui"]).pack(side=tk.LEFT)
        self.empty_threshold_e = self._entry(r_empty, width=4, default="3")
        self.empty_threshold_e.pack(side=tk.LEFT, padx=(4, 4))
        tk.Label(r_empty, text="次采集到0个商品 → 触发风控告警+截图+暂停",
                 bg=SURF, fg=FG_M, font=FONTS["ui"]).pack(side=tk.LEFT)

        r_cache = tk.Frame(protect_inner, bg=SURF)
        r_cache.pack(fill=tk.X, pady=2)
        tk.Label(r_cache, text="运行超过", bg=SURF, fg=FG, font=FONTS["ui"]).pack(side=tk.LEFT)
        self.cache_clear_interval_e = self._entry(r_cache, width=4, default="120")
        self.cache_clear_interval_e.pack(side=tk.LEFT, padx=(4, 4))
        tk.Label(r_cache, text="分钟 → 清理后台并重启PDD", bg=SURF, fg=FG_M,
                 font=FONTS["ui"]).pack(side=tk.LEFT)

        # ── 操作按钮 ──
        action_frame = self._section(inner, "操作")
        action_frame.pack(fill=tk.X, padx=8, pady=(4, 4))

        row1 = tk.Frame(action_frame, bg=SURF)
        row1.pack(fill=tk.X, padx=6, pady=(4, 2))
        self.auto_push_var = tk.BooleanVar(value=True)
        tk.Checkbutton(row1, text="自动推送高分商品", variable=self.auto_push_var,
                       bg=SURF, fg=FG, font=FONTS["ui"]).pack(side=tk.LEFT)
        self._btn(row1, "推送当前商品", self._push_current_items, width=12).pack(side=tk.RIGHT)

        row2 = tk.Frame(action_frame, bg=SURF)
        row2.pack(fill=tk.X, padx=6, pady=(2, 4))
        self.start_btn = self._btn(row2, "开始处理", self._start_processing,
                                   bg=SUCC, state=tk.DISABLED, width=10)
        self.start_btn.config(fg="white")
        self.start_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.stop_btn = self._btn(row2, "停止", self._stop_processing,
                                  bg=DANGER, state=tk.DISABLED, width=6)
        self.stop_btn.config(fg="white")
        self.stop_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.resume_btn = self._btn(row2, "继续处理", self._resume_processing,
                                    bg=ACC, state=tk.DISABLED, width=10)
        self.resume_btn.config(fg="white")
        self.resume_btn.pack(side=tk.LEFT, padx=(0, 4))

        export_frame = tk.Frame(row2, bg=SURF)
        export_frame.pack(side=tk.RIGHT)
        self._btn(export_frame, "导出全部", self._export_all, width=9).pack(side=tk.LEFT, padx=(0, 2))
        self._btn(export_frame, "导出正利润", self._export_profit_only, width=10).pack(side=tk.LEFT, padx=(0, 2))
        self._btn(export_frame, "导出上架建议", self._export_listing_advice, width=11).pack(side=tk.LEFT)

    def _build_center_panel(self, main):
        """中间结果面板：主结果树 + 标题/图搜详情树 + 媒体面板"""
        center = tk.Frame(main, bg=SURF2)
        main.add(center, minsize=400)

        right_pane = tk.PanedWindow(center, orient=tk.VERTICAL, bg=SURF2,
                                    sashwidth=4, sashrelief=tk.FLAT)
        right_pane.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        # 主结果树
        t1_frame = tk.Frame(right_pane, bg=SURF2)
        right_pane.add(t1_frame, minsize=180)
        tk.Label(t1_frame, text="货源结果（双击商品行查看详情）",
                 bg=SURF2, fg=FG_M, font=FONTS["ui"]).pack(anchor='w')

        t1_cols = ('闲鱼标题', '售价', '闲鱼评分', '象限', '综合利润', '标题同款利润', '标题平替利润',
                   '图搜同款利润', '图搜平替利润', '最优货源', '进价', '利润率%', '建议', '闲鱼链接', '图片链接')
        t1_widths = (120, 46, 50, 72, 62, 72, 72, 72, 72, 150, 46, 56, 80, 180, 160)
        self.tree = self._make_tree(t1_frame, t1_cols, t1_widths, height=10)
        self.tree.tag_configure('q1', background='#E8F8EF')
        self.tree.tag_configure('q2', background='#E8F0FB')
        self.tree.tag_configure('q3', background='#FFF9E6')
        self.tree.tag_configure('q4', background='#FDECEC')
        self.tree.tag_configure('best', background='#FFF3CD')
        self.tree.tag_configure('profit', background='#EAF7EF')
        _make_sortable(self.tree, {'售价', '闲鱼评分', '进价', '综合利润', '标题同款利润',
                                    '标题平替利润', '图搜同款利润', '图搜平替利润', '利润率%'})
        self.tree.bind('<<TreeviewSelect>>', self._on_select)
        self.tree.bind('<Double-Button-1>', self._on_tree_double_click)

        # 下半部分：标题详情 + 图搜详情 + 媒体
        t2_frame = tk.Frame(right_pane, bg=SURF2)
        right_pane.add(t2_frame, minsize=240)

        tk.Label(t2_frame, text="标题搜索同款/平替（双击商品行查看详情）",
                 bg=SURF2, fg=FG_M, font=FONTS["ui"]).pack(anchor='w')
        t2_cols = ('#', '货源商品名', '匹配类型', '进价', '利润', '利润率%', '货源评分', '销量', '相似度', '比对', '推荐', '理由', '来源', '佣金', '货源链接')
        t2_widths = (28, 150, 70, 52, 58, 62, 58, 76, 56, 50, 56, 180, 50, 52, 200)
        self.detail_tree = self._make_tree(t2_frame, t2_cols, t2_widths, height=4)
        self.detail_tree.tag_configure('best_src', background='#FFF3CD')
        self.detail_tree.tag_configure('good_src', background='#EAF7EF')
        _make_sortable(self.detail_tree, {'进价', '利润', '利润率%', '货源评分', '相似度'})
        self.detail_tree.bind('<Double-Button-1>', self._on_detail_double_click)

        tk.Label(t2_frame, text="以图搜款同款/平替（双击商品行查看详情）",
                 bg=SURF2, fg=FG_M, font=FONTS["ui"]).pack(anchor='w', pady=(6, 0))
        self.img_detail_tree = self._make_tree(t2_frame, t2_cols, t2_widths, height=4)
        self.img_detail_tree.tag_configure('best_src', background='#E8F0FB')
        self.img_detail_tree.tag_configure('good_src', background='#E8F8EF')
        _make_sortable(self.img_detail_tree, {'进价', '利润', '利润率%', '货源评分', '相似度'})
        self.img_detail_tree.bind('<Double-Button-1>', self._on_img_detail_double_click)

        # 媒体信息面板
        media_frame = tk.LabelFrame(t2_frame, text="闲鱼商品媒体信息",
                                     bg=SURF, fg=FG_M,
                                     font=FONTS["ui"], relief=tk.FLAT,
                                     highlightbackground=BRD, highlightthickness=1)
        media_frame.pack(fill=tk.BOTH, expand=True, padx=2, pady=(4, 2))

        media_canvas = tk.Canvas(media_frame, bg=SURF, highlightthickness=0)
        media_scroll = ttk.Scrollbar(media_frame, orient="vertical", command=media_canvas.yview)
        media_canvas.configure(yscrollcommand=media_scroll.set)
        media_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        media_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._media_inner = tk.Frame(media_canvas, bg=SURF)
        self._media_window = media_canvas.create_window((0, 0), window=self._media_inner, anchor="nw")

        def _on_media_configure(event):
            media_canvas.configure(scrollregion=media_canvas.bbox("all"))
            media_canvas.itemconfig(self._media_window, width=event.width)

        media_canvas.bind("<Configure>", _on_media_configure)
        self._media_inner.bind("<Configure>", lambda e: media_canvas.configure(
            scrollregion=media_canvas.bbox("all")))

        def _media_wheel(e):
            media_canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        media_canvas.bind("<MouseWheel>", _media_wheel)
        media_canvas.bind("<Button-4>", lambda e: media_canvas.yview_scroll(-1, "units"))
        media_canvas.bind("<Button-5>", lambda e: media_canvas.yview_scroll(1, "units"))
        self._media_canvas = media_canvas

        self._media_placeholder = tk.Label(self._media_inner,
            text="← 点击左侧货源行，查看闲鱼商品链接和图片",
            bg=SURF, fg=FG_M, font=FONTS["ui"])
        self._media_placeholder.pack(pady=10)

    def _make_tree(self, parent, cols, widths, height=8) -> ttk.Treeview:
        tf = tk.Frame(parent, bg=SURF, highlightbackground=BRD, highlightthickness=1)
        tf.pack(fill=tk.BOTH, expand=True)
        vsb = ttk.Scrollbar(tf, orient="vertical")
        hsb = ttk.Scrollbar(tf, orient="horizontal")
        tree = ttk.Treeview(tf, columns=cols, show='headings', height=height,
                            yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.config(command=tree.yview)
        hsb.config(command=tree.xview)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        hsb.pack(side=tk.BOTTOM, fill=tk.X)
        tree.pack(fill=tk.BOTH, expand=True)
        for col, w in zip(cols, widths):
            tree.heading(col, text=col)
            tree.column(col, width=w, minwidth=25, anchor='w')
        def _wheel(e):
            tree.yview_scroll(int(-1 * (e.delta / 120)), "units")
        def _wheel_up(e):
            tree.yview_scroll(-1, "units")
        def _wheel_down(e):
            tree.yview_scroll(1, "units")
        tree.bind("<MouseWheel>", _wheel)
        tree.bind("<Button-4>", _wheel_up)
        tree.bind("<Button-5>", _wheel_down)
        def _hwheel(e):
            tree.xview_scroll(int(-1 * (e.delta / 120)), "units")
        tree.bind("<Shift-MouseWheel>", _hwheel)
        return tree

    # ── AI设置 ──

    def _toggle_password(self):
        self.show_pwd = not self.show_pwd
        if self.show_pwd:
            self.api_entry.config(show="")
            self.toggle_pwd_btn.config(text="隐藏")
        else:
            self.api_entry.config(show="*")
            self.toggle_pwd_btn.config(text="显示")

    def _test_api(self):
        if self._api_testing:
            return

        api_key = self.api_entry.get().strip()
        if not api_key:
            messagebox.showwarning("提示", "请先输入 API Key")
            return

        self._api_testing = True
        self.test_api_btn.config(state=tk.DISABLED, text="测试中...")
        self._log("正在测试 API 连接...")

        def do_test():
            try:
                cleaner = TitleCleanerAI(api_key=api_key, log_cb=self._log)
                success, msg = cleaner.test_connection()

                def update_ui():
                    if success:
                        self._log("API 连接成功！")
                        self.api_status.config(text="API 可用", fg=SUCC)
                        set_ai_api_key(api_key, self._log)
                        self.api_key_var.set(api_key)
                    else:
                        self._log(f"API 连接失败: {msg}")
                        self.api_status.config(text=f"{msg[:30]}", fg=DANGER)
                    self.test_api_btn.config(state=tk.NORMAL, text="测试连接")
                    self._api_testing = False

                self.parent.after(0, update_ui)
            except Exception as e:
                self.parent.after(0, lambda: self._log(f"测试异常: {e}"))
                self.parent.after(0, lambda: self.test_api_btn.config(state=tk.NORMAL, text="测试连接"))
                self.parent.after(0, lambda: setattr(self, '_api_testing', False))

        threading.Thread(target=do_test, daemon=True).start()

    def _save_api_key_ui(self):
        api_key = self.api_entry.get().strip()
        if not api_key:
            messagebox.showwarning("提示", "API Key 不能为空")
            return

        if self._save_api_key():
            set_ai_api_key(api_key, self._log)
            self.api_key_var.set(api_key)
            self._log("API Key 已保存")
            self._update_api_status(True)
            messagebox.showinfo("成功", "API Key 已保存")
        else:
            messagebox.showerror("错误", "保存失败")

    def _update_api_status(self, is_valid: bool = None):
        if is_valid is None:
            is_valid = bool(self.api_key_var.get())
        if is_valid:
            self.api_status.config(text="API 已配置", fg=SUCC)
        else:
            self.api_status.config(text="未配置 API Key（将使用本地清洗）", fg=FG_M)

    def _update_api_stats_display(self):
        try:
            cleaner = get_ai_cleaner()
            stats = cleaner.get_stats()
            clean_calls = stats.get('clean_calls', 0)
            clean_ok = stats.get('clean_ok', 0)
            compare_calls = stats.get('compare_calls', 0)
            compare_ok = stats.get('compare_ok', 0)
            self.api_stats_label.config(
                text=(f"清洗: {clean_ok}/{clean_calls}次 | 比对: {compare_ok}/{compare_calls}次")
            )
        except Exception:
            pass
        try:
            self.parent.after(2000, self._update_api_stats_display)
        except Exception:
            pass

    # ── 参数 ──

    def _get_params(self):
        try:
            return {
                'score_threshold': int(self.score_thresh_e.get() or 75),
                'sim_threshold': float(self.sim_thresh_e.get() or 0.8),
                'scroll_pages': int(self.scroll_pages_e.get() or 5),
                'max_items': int(self.max_items_e.get() or 20),
                'img_scroll_pages': int(self.img_scroll_pages_e.get() or 3),
                'delay_between_products': int(self.delay_between_e.get() or 8),
                'pause_every': int(self.pause_every_e.get() or 5),
                'pause_duration': int(self.pause_dur_e.get() or 60),
                'cache_clear_interval_min': int(self.cache_clear_interval_e.get() or 120),
                'empty_threshold': int(self.empty_threshold_e.get() or 3),
                'use_img_search': self.use_img_search_var.get(),
                'use_ai_compare': self.use_ai_compare_var.get(),
            }
        except Exception as ex:
            messagebox.showerror("参数错误", str(ex))
            return None

    def _update_scheduler_settings(self):
        if not self.scheduler:
            self._log("调度器未启动，请先点击「开始处理」")
            return

        params = self._get_params()
        if not params:
            return

        empty_threshold = max(1, min(20, params['empty_threshold']))
        cache_clear_interval = max(1, min(360, params['cache_clear_interval_min']))

        self.scheduler.score_threshold = params['score_threshold']
        self.scheduler.sim_threshold = params['sim_threshold']
        self.scheduler.scroll_pages = params['scroll_pages']
        self.scheduler.max_items = params['max_items']
        self.scheduler.img_scroll_pages = params['img_scroll_pages']
        self.scheduler.use_img_search = params['use_img_search']
        self.scheduler.use_ai_compare = params['use_ai_compare']
        self.scheduler.delay_between_products = params['delay_between_products']
        self.scheduler.pause_every = params['pause_every']
        self.scheduler.pause_duration = params['pause_duration']
        self.scheduler.cache_clear_interval_min = cache_clear_interval
        self.scheduler._empty_threshold = empty_threshold
        self.scheduler.use_ai_clean = self.use_ai_var.get()

        if hasattr(self.scheduler, 'matcher'):
            self.scheduler.matcher.threshold = params['sim_threshold']

        api_key = self.api_key_var.get()
        if api_key and self.use_ai_var.get():
            set_ai_api_key(api_key, self._log)

        # 持久化参数
        self.app.config.settings["supply_finder"] = {
            "score_threshold": params['score_threshold'],
            "sim_threshold": params['sim_threshold'],
            "scroll_pages": params['scroll_pages'],
            "max_items": params['max_items'],
            "img_scroll_pages": params['img_scroll_pages'],
            "use_img_search": params['use_img_search'],
            "use_ai_compare": params['use_ai_compare'],
            "delay_between_products": params['delay_between_products'],
            "pause_every": params['pause_every'],
            "pause_duration": params['pause_duration'],
        }
        self.app.config.save_settings()

        self._log("=" * 50)
        self._log("设置更新完成：")
        self._log(f"  相似阈值:{self.scheduler.sim_threshold} 标题翻页:{self.scheduler.scroll_pages} 最多采集:{self.scheduler.max_items}")
        self._log(f"  图搜:{self.scheduler.use_img_search} AI比对:{self.scheduler.use_ai_compare}")
        self._log(f"  间隔:{self.scheduler.delay_between_products}s 评分:{self.scheduler.score_threshold}")
        self._log("=" * 50)

    # ── 设备操作 ──

    def _launch_app(self):
        if not self.controller:
            messagebox.showwarning("提示", "手机未连接")
            return

        def do_launch():
            if self.controller.launch_pinduoduo():
                try:
                    self.start_btn.config(state=tk.NORMAL)
                except Exception:
                    pass

        threading.Thread(target=do_launch, daemon=True).start()

    def _reconnect_device(self):
        self._log("重新连接手机...")
        self.device_status.config(text="连接中...", fg=FG_M)

        redmi_serial = self._get_redmi_serial()

        def do_reconnect():
            try:
                ctrl = PinduoduoMobileController(self._log)
                if ctrl.connect(serial=redmi_serial):
                    self.controller = ctrl
                    if self.scheduler:
                        self.scheduler.controller = ctrl
                    self.parent.after(0, lambda: self.device_status.config(text="已连接", fg=SUCC))
                    self.parent.after(0, lambda: self.launch_btn.config(state=tk.NORMAL))
                    self._log("设备重连成功")
                else:
                    self.parent.after(0, lambda: self.device_status.config(text="连接失败", fg=DANGER))
            except Exception as e:
                self._log(f"重连异常: {e}")
                self.parent.after(0, lambda: self.device_status.config(text="连接失败", fg=DANGER))

        threading.Thread(target=do_reconnect, daemon=True).start()

    def _reload_model(self):
        self._log("重新加载语义模型...")
        SameProductMatcher._loaded = False
        SameProductMatcher._model = None
        SameProductMatcher._error = None

        def do_reload():
            ok = SameProductMatcher.load_model(self._log)
            def upd():
                try:
                    self.stats_label.config(text=f"最高利润率: — | 模型: {'已加载' if ok else '降级模式'}")
                except Exception:
                    pass
            self.parent.after(0, upd)

        threading.Thread(target=do_reload, daemon=True).start()

    # ── 处理流程 ──

    def _resume_processing(self):
        # 优先恢复引擎内部调度器（采集流程自动启动时）
        engine_scheduler = None
        if self._engine and hasattr(self._engine, '_scheduler') and self._engine._scheduler:
            engine_scheduler = self._engine._scheduler

        # 先尝试恢复引擎调度器
        if engine_scheduler and engine_scheduler.is_paused():
            engine_scheduler.resume()
            self._log("已恢复引擎调度器，队列继续处理")
            self.resume_btn.config(state=tk.DISABLED)
            self.stop_btn.config(state=tk.NORMAL)
            return

        # 再尝试恢复Tab自己的调度器
        if self.scheduler and self.scheduler.is_paused():
            self.scheduler.resume()
            self._log("已发送恢复指令，队列继续处理")
            self.resume_btn.config(state=tk.DISABLED)
            self.stop_btn.config(state=tk.NORMAL)
            return

        if not self.scheduler and not engine_scheduler:
            self._log("调度器未启动，请先点击「开始处理」")
        else:
            self._log("队列未处于暂停状态，无需恢复")

    def _start_processing(self):
        if not self.controller:
            # 尝试自动连接
            self._log("手机未连接，尝试自动连接...")
            self._reconnect_device()
            time.sleep(3)
            if not self.controller:
                messagebox.showerror("错误", "手机未连接，请点击「重连设备」")
                return

        # 确保 PDD 在前台
        if not self.controller.is_connected():
            self._log("PDD 未连接，尝试重新连接...")
            self._reconnect_device()
            time.sleep(2)

        if self.controller.is_connected():
            self._log("启动拼多多到前台...")
            self.controller.launch_pinduoduo()
            time.sleep(3)

        params = self._get_params()
        if not params:
            return

        empty_threshold = max(1, min(20, params['empty_threshold']))

        api_key = self.api_key_var.get()
        if api_key and self.use_ai_var.get():
            set_ai_api_key(api_key, self._log)

        if self.scheduler and self.scheduler._running:
            self.scheduler.stop()
            time.sleep(0.5)

        self.scheduler = MobileSupplyScheduler(
            task_queue=self.task_queue,
            result_cb=self._on_result,
            log_cb=self._log,
            countdown_cb=self._update_countdown,
            use_ai_clean=self.use_ai_var.get(),
            use_ai_compare=params['use_ai_compare'],
            use_img_search=params['use_img_search'],
            score_threshold=params['score_threshold'],
            sim_threshold=params['sim_threshold'],
            scroll_pages=params['scroll_pages'],
            max_items=params['max_items'],
            img_scroll_pages=params['img_scroll_pages'],
            delay_between_products=params['delay_between_products'],
            pause_every=params['pause_every'],
            pause_duration=params['pause_duration'],
            cache_clear_interval_min=params['cache_clear_interval_min'],
            empty_threshold=empty_threshold,
        )
        self.scheduler.controller = self.controller

        # 传递 webhook URL 给调度器（风控告警 + 正利润推送）
        if self._wechat_webhook:
            self.scheduler._wechat_webhook = self._wechat_webhook
            self._log("Webhook 已绑定到调度器（风控告警+正利润推送已启用）")

        self.scheduler.start_processing()
        self.start_btn.config(text="处理中...", state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.resume_btn.config(state=tk.NORMAL)
        self._log("货源查找队列已启动（标题搜索 + 以图搜款双路径）")

    def _stop_processing(self):
        if self.scheduler:
            self.scheduler.stop()
        # 同时停止引擎内部调度器（采集流程自动启动时）
        if self._engine:
            self._engine.stop()
        self.stop_btn.config(state=tk.DISABLED)
        self.start_btn.config(state=tk.NORMAL)
        self.resume_btn.config(state=tk.DISABLED)
        self._update_countdown(0)

    # ── 推送 ──

    def _push_current_items(self):
        all_data = getattr(self.app, '_data', {}).get('all_items', [])
        if not all_data:
            analysis_tab = self.app.get_tab("分析看板")
            if analysis_tab and hasattr(analysis_tab, '_pd_results'):
                all_data = analysis_tab._pd_results

        thresh = int(self.score_thresh_e.get() or 75)
        pushed = 0
        for item in all_data:
            try:
                score = item.get('total_score', 0) or item.get('综合评分', 0)
                if int(score) >= thresh:
                    self.task_queue.put(item)
                    pushed += 1
            except Exception:
                pass
        self._log(f"推送 {pushed} 件，队列深度: {self.task_queue.qsize()}")
        self._update_stats()

    def notify_new_item(self, item: dict):
        """接收采集引擎推送的已评分商品（v3 ProductScorerV3评分）"""
        if not self.auto_push_var.get():
            return

        try:
            score_thresh = int(self.score_thresh_e.get() or 75)
        except:
            score_thresh = 75

        try:
            score = (item.get('total_100', 0) or item.get('total_score', 0)
                     or item.get('product_score', 0) or item.get('综合评分', 0)
                     or item.get('productScore', 0))
            if int(score) >= score_thresh:
                self.task_queue.put(item)
                title = item.get('title', '') or item.get('商品标题', '')
                self._log(f"自动推送: {title[:30]} ({score}分)")
                self._update_stats()
        except Exception as e:
            self._log(f"自动推送失败: {e}")

    def push_product(self, item_data: dict):
        """兼容旧接口"""
        self.notify_new_item(item_data)

    # ── 结果处理 ──

    def _on_result(self, record: dict):
        self._result_records.append(record)

        def _u():
            title_items = record.get('pdd_items', [])
            img_items = record.get('img_pdd_items', [])
            title_best = title_items[0] if title_items else None
            img_best = img_items[0] if img_items else None

            q = record.get('quadrant', '')
            tag_map = {'Q1': 'q1', 'Q2': 'q2', 'Q3': 'q3', 'Q4': 'q4', 'Q5': 'q4'}
            tag = tag_map.get(q, 'profit' if record.get('final_profit', 0) and record['final_profit'] > 0 else '')

            pics_raw = record.get('xianyu_pics', '')
            first_pic = pics_raw.split(',')[0] if pics_raw else ''

            fp = record.get('final_profit')
            fnp = record.get('final_price')
            tsp = record.get('title_same_profit')
            tap = record.get('title_alt_profit')
            isp = record.get('img_same_profit')
            iap = record.get('img_alt_profit')

            rate_str = ''
            if fp is not None and fnp and fnp > 0:
                rate_str = f"{fp/fnp*100:.1f}%"

            self.tree.insert('', 0, values=(
                record['source_title'][:18],
                record['xianyu_price'],
                record.get('xianyu_score', ''),
                f"{record.get('quadrant_emoji', '')} {q} {record.get('quadrant_label', '')}",
                f"¥{fp:.1f}" if fp is not None else '—',
                f"¥{tsp:.1f}" if tsp is not None else '—',
                f"¥{tap:.1f}" if tap is not None else '—',
                f"¥{isp:.1f}" if isp is not None else '—',
                f"¥{iap:.1f}" if iap is not None else '—',
                (title_best['goods_name'][:22] if title_best else
                 img_best['goods_name'][:22] if img_best else ''),
                f"¥{fnp:.1f}" if fnp is not None else '—',
                rate_str,
                record.get('recommendation', '')[:30],
                record.get('xianyu_link', ''),
                first_pic,
            ), tags=(tag,))
            self._update_stats()

        self.parent.after(0, _u)

    def _on_select(self, _event):
        sel = self.tree.selection()
        if not sel:
            return
        vals = self.tree.item(sel[0], 'values')
        title_prefix = vals[0]

        record = next((r for r in self._result_records
                       if r['source_title'][:18] == title_prefix), None)
        if not record:
            return

        for row in self.detail_tree.get_children():
            self.detail_tree.delete(row)
        for row in self.img_detail_tree.get_children():
            self.img_detail_tree.delete(row)

        def _fill_tree(tree, items, color_best, color_good):
            for rank, it in enumerate(items, 1):
                tag = color_best if rank == 1 else (
                    color_good if (it.get('货源评分', 0) or 0) >= 50 else '')
                match_type = it.get('匹配类型', '')
                match_type_display = match_type
                if match_type == '同款':
                    match_type_display = '同款'
                elif match_type == '平替':
                    match_type_display = '平替'
                elif match_type == '部分匹配':
                    match_type_display = '部分'

                src = it.get('source', '手机')
                src_display = 'API' if src == 'ddk_api' else '手机'
                comm = it.get('commission', 0)
                comm_pct = it.get('promotion_rate', 0)
                comm_str = f'¥{comm:.2f}({comm_pct}%)' if comm else '—'
                tree.insert('', 'end', values=(
                    f"#{rank}",
                    it.get('goods_name', '')[:35],
                    match_type_display,
                    it.get('拼多多进价(元)', ''),
                    it.get('预估利润(元)', ''),
                    it.get('利润率(%)', ''),
                    it.get('货源评分', ''),
                    it.get('sales_tip', ''),
                    it.get('sim_score', 0),
                    it.get('match_src', ''),
                    it.get('是否推荐货源', ''),
                    it.get('匹配说明', '')[:80],
                    src_display,
                    comm_str,
                    it.get('goods_url', ''),
                ), tags=(tag,))

        _fill_tree(self.detail_tree, record.get('pdd_items', []), 'best_src', 'good_src')
        _fill_tree(self.img_detail_tree, record.get('img_pdd_items', []), 'best_src', 'good_src')
        self._rebuild_media_panel(record)

    def _on_tree_double_click(self, event):
        sel = self.tree.selection()
        if not sel:
            return
        vals = self.tree.item(sel[0], 'values')
        title_prefix = vals[0]

        record = next((r for r in self._result_records
                       if r['source_title'][:18] == title_prefix), None)
        if not record:
            return

        title_items = record.get('pdd_items', [])
        img_items = record.get('img_pdd_items', [])
        if title_items or img_items:
            self._show_record_detail(record)

    def _on_detail_double_click(self, event):
        sel = self.detail_tree.selection()
        if not sel:
            return
        values = self.detail_tree.item(sel[0], 'values')
        if not values:
            return
        tree_sel = self.tree.selection()
        if not tree_sel:
            return
        vals = self.tree.item(tree_sel[0], 'values')
        title_prefix = vals[0]
        record = next((r for r in self._result_records
                       if r['source_title'][:18] == title_prefix), None)
        if not record:
            return
        title_items = record.get('pdd_items', [])
        rank_str = values[0]
        try:
            rank = int(rank_str.replace('#', ''))
            if 1 <= rank <= len(title_items):
                SourceDetailWindow(self.parent, f"标题搜索货源详情", [title_items[rank - 1]], "标题搜索")
        except (ValueError, IndexError):
            pass

    def _on_img_detail_double_click(self, event):
        sel = self.img_detail_tree.selection()
        if not sel:
            return
        values = self.img_detail_tree.item(sel[0], 'values')
        if not values:
            return
        tree_sel = self.tree.selection()
        if not tree_sel:
            return
        vals = self.tree.item(tree_sel[0], 'values')
        title_prefix = vals[0]
        record = next((r for r in self._result_records
                       if r['source_title'][:18] == title_prefix), None)
        if not record:
            return
        img_items = record.get('img_pdd_items', [])
        rank_str = values[0]
        try:
            rank = int(rank_str.replace('#', ''))
            if 1 <= rank <= len(img_items):
                SourceDetailWindow(self.parent, f"以图搜款货源详情", [img_items[rank - 1]], "以图搜款")
        except (ValueError, IndexError):
            pass

    def _show_record_detail(self, record: dict):
        """双击货源行弹出纵向阅读卡片详情窗口"""
        title_items = record.get('pdd_items', [])
        img_items = record.get('img_pdd_items', [])

        win = tk.Toplevel(self.parent)
        win.title(f"货源详情 — {record['source_title'][:35]}")
        win.geometry("780x680")
        win.configure(bg=SURF2)
        win.transient(self.parent)

        header = tk.Frame(win, bg=ACC)
        header.pack(fill=tk.X)
        tk.Label(header, text="货源详情", bg=ACC, fg="white",
                 font=("Microsoft YaHei", 11, "bold")).pack(side=tk.LEFT, padx=14, pady=10)

        q_emoji = record.get('quadrant_emoji', '')
        q_label = record.get('quadrant_label', '')
        fp = record.get('final_profit')
        fp_str = f"综合利润 ¥{fp:.1f}" if fp is not None else "无利润数据"
        tk.Label(header, text=f"{q_emoji} {q_label}  {fp_str}",
                 bg=ACC, fg="white", font=("Microsoft YaHei", 9)).pack(side=tk.RIGHT, padx=14)

        summary_f = tk.Frame(win, bg=SURF, highlightbackground=BRD, highlightthickness=1)
        summary_f.pack(fill=tk.X, padx=10, pady=(8, 4))
        tk.Label(summary_f,
                 text=f"  闲鱼标题：{record['source_title']}",
                 bg=SURF, fg=FG, font=("Microsoft YaHei", 9, "bold"),
                 anchor='w', wraplength=740).pack(fill=tk.X, padx=6, pady=(6, 2))
        tk.Label(summary_f,
                 text=f"  售价 ¥{record['xianyu_price']}  |  搜索词：{record.get('search_keyword','')}  |  {record.get('recommendation','')}",
                 bg=SURF, fg=FG_M, font=FONTS["ui"],
                 anchor='w', wraplength=740).pack(fill=tk.X, padx=6, pady=(0, 6))

        content_frame = tk.Frame(win, bg=SURF2)
        content_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)

        canvas = tk.Canvas(content_frame, bg=SURF2, highlightthickness=0)
        vsb = ttk.Scrollbar(content_frame, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        inner = tk.Frame(canvas, bg=SURF2)
        inner_win = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _on_inner_cfg(e):
            canvas.configure(scrollregion=canvas.bbox("all"))
        def _on_canvas_cfg(e):
            canvas.itemconfig(inner_win, width=e.width)
        inner.bind("<Configure>", _on_inner_cfg)
        canvas.bind("<Configure>", _on_canvas_cfg)

        def _wheel(e):
            canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        canvas.bind("<MouseWheel>", _wheel)
        canvas.bind("<Button-4>", lambda e: canvas.yview_scroll(-1, "units"))
        canvas.bind("<Button-5>", lambda e: canvas.yview_scroll(1, "units"))
        inner.bind("<MouseWheel>", _wheel)

        COLORS = {
            '同款': ('#E8F8EF', '#27AE60', '同款'),
            '平替': ('#E8F0FB', '#185FA5', '平替'),
        }
        FONT_LG = ("Microsoft YaHei", 10, "bold")
        FONT_MD = ("Microsoft YaHei", 9)

        def _section_title(parent, text, icon=""):
            row = tk.Frame(parent, bg=SURF2)
            row.pack(fill=tk.X, pady=(10, 4))
            tk.Label(row, text=f"{icon} {text}", bg=SURF2, fg=ACC,
                     font=("Microsoft YaHei", 10, "bold")).pack(side=tk.LEFT, padx=2)
            tk.Frame(row, bg=BRD, height=1).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 0))

        def _render_items(parent, items, source_tag):
            if not items:
                tk.Label(parent, text=f"  {source_tag} 无货源数据",
                         bg=SURF2, fg=FG_M, font=FONTS["ui"]).pack(anchor='w', padx=4)
                return

            for rank, it in enumerate(items, 1):
                match_type = it.get('匹配类型', '')
                bg_c, acc_c, badge = COLORS.get(match_type, ('#F8F9FD', '#8A90A8', '未知'))
                profit = it.get('预估利润(元)', 0) or 0
                if profit < 0:
                    bg_c, acc_c = '#FEF0F0', '#E74C3C'

                card = tk.Frame(parent, bg=SURF, highlightbackground=BRD, highlightthickness=1)
                card.pack(fill=tk.X, padx=4, pady=3)
                bar = tk.Frame(card, bg=acc_c, width=4)
                bar.pack(side=tk.LEFT, fill=tk.Y)
                body = tk.Frame(card, bg=SURF)
                body.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 8), pady=6)

                row1 = tk.Frame(body, bg=SURF)
                row1.pack(fill=tk.X)
                tk.Label(row1, text=f"#{rank}", bg=SURF, fg=FG_M,
                         font=FONTS["ui"], width=3, anchor='w').pack(side=tk.LEFT)
                tk.Label(row1, text=it.get('goods_name', ''),
                         bg=SURF, fg=FG, font=FONT_LG, anchor='w', wraplength=500).pack(
                         side=tk.LEFT, fill=tk.X, expand=True)
                tk.Label(row1, text=badge, bg=bg_c, fg=acc_c,
                         font=FONTS["ui"], padx=6, pady=2).pack(side=tk.RIGHT, padx=(4, 0))

                row2 = tk.Frame(body, bg=SURF)
                row2.pack(fill=tk.X, pady=(3, 0))
                price_v = it.get('拼多多进价(元)', 0) or 0
                rate_v = it.get('利润率(%)', 0) or 0
                score_v = it.get('货源评分', 0) or 0
                profit_color = "#27AE60" if profit > 0 else "#E74C3C"
                tk.Label(row2, text=f"¥{profit:.2f}", bg=SURF, fg=profit_color,
                         font=("Microsoft YaHei", 14, "bold")).pack(side=tk.LEFT)
                tk.Label(row2, text=" 利润", bg=SURF, fg=FG_M, font=FONTS["ui"]).pack(side=tk.LEFT)
                tk.Label(row2, text=f"  进价 ¥{price_v:.2f}", bg=SURF, fg=FG, font=FONT_MD).pack(
                         side=tk.LEFT, padx=(12, 0))
                tk.Label(row2, text=f"  利润率 {rate_v:.1f}%", bg=SURF, fg=FG, font=FONT_MD).pack(
                         side=tk.LEFT, padx=(8, 0))
                tk.Label(row2, text=f"  评分 {score_v}", bg=SURF, fg=FG, font=FONT_MD).pack(
                         side=tk.LEFT, padx=(8, 0))
                rec = it.get('是否推荐货源', '')
                if rec:
                    tk.Label(row2, text=rec, bg=SURF, fg=FG_M, font=FONTS["ui"]).pack(side=tk.RIGHT, padx=4)

                row3 = tk.Frame(body, bg=SURF)
                row3.pack(fill=tk.X, pady=(2, 0))
                sales = it.get('sales_tip', '') or ''
                sim = it.get('sim_score', 0) or 0
                src = it.get('match_src', '')
                meta = []
                if sales: meta.append(f"销量: {sales}")
                if src: meta.append(f"比对: {src}")
                meta.append(f"相似度: {sim:.0%}")
                tk.Label(row3, text="  ".join(meta), bg=SURF, fg=FG_M, font=FONTS["ui"]).pack(side=tk.LEFT)

                reason = it.get('匹配说明', '') or it.get('reason', '')
                if reason:
                    row4 = tk.Frame(body, bg="#F8F9FD",
                                    highlightbackground=BRD, highlightthickness=1)
                    row4.pack(fill=tk.X, pady=(5, 0))
                    tk.Label(row4, text=f"  {reason}",
                             bg="#F8F9FD", fg="#5C6073",
                             font=FONTS["ui"], wraplength=660, justify=tk.LEFT,
                             anchor='w').pack(fill=tk.X, padx=8, pady=4)

        _section_title(inner, f"标题搜索货源（{len(title_items)} 件）", "")
        _render_items(inner, title_items, "标题搜索")
        _section_title(inner, f"以图搜款货源（{len(img_items)} 件）", "")
        _render_items(inner, img_items, "以图搜款")
        tk.Frame(inner, bg=SURF2, height=20).pack()

        btn_frame = tk.Frame(win, bg=SURF2)
        btn_frame.pack(fill=tk.X, pady=(0, 10))
        tk.Button(btn_frame, text="关闭", command=win.destroy,
                  bg=SURF2, fg=FG, padx=24, pady=6,
                  font=FONTS["ui"], relief=tk.FLAT, cursor="hand2").pack()

    def _rebuild_media_panel(self, record: dict):
        for w in self._media_inner.winfo_children():
            w.destroy()

        link = record.get('xianyu_link', '')
        pics = record.get('xianyu_pics', '')
        video = record.get('xianyu_video', '')

        def open_url(url):
            if url and url != '—':
                webbrowser.open(url)

        FONT_LBL = ("Microsoft YaHei", 8, "bold")

        row_link = tk.Frame(self._media_inner, bg=SURF)
        row_link.pack(fill=tk.X, padx=6, pady=2)
        tk.Label(row_link, text="商品链接:", bg=SURF, fg=FG,
                 font=FONT_LBL, width=9, anchor='w').pack(side=tk.LEFT)
        if link:
            lbl = tk.Label(row_link, text=link[:80] + ('...' if len(link) > 80 else ''),
                           bg=SURF, fg="#0055CC", font=FONTS["ui"],
                           cursor="hand2", anchor='w')
            lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)
            lbl.bind("<Button-1>", lambda e, u=link: open_url(u))
            tk.Label(row_link, text="点击打开", bg=SURF, fg=FG_M,
                     font=FONTS["ui"]).pack(side=tk.LEFT, padx=(4, 0))
        else:
            tk.Label(row_link, text="—", bg=SURF, fg=FG_M, font=FONTS["ui"]).pack(side=tk.LEFT)

        pic_list = [p.strip() for p in pics.split(',') if p.strip()] if pics else []
        hdr_row = tk.Frame(self._media_inner, bg=SURF)
        hdr_row.pack(fill=tk.X, padx=6, pady=(4, 0))
        tk.Label(hdr_row, text=f"图片（共{len(pic_list)}张）:", bg=SURF, fg=FG, font=FONT_LBL).pack(anchor='w')

        for i, pic_url in enumerate(pic_list, 1):
            pic_row = tk.Frame(self._media_inner, bg=SURF)
            pic_row.pack(fill=tk.X, padx=6, pady=1)
            tk.Label(pic_row, text=f"  {i}.", bg=SURF, fg=FG_M,
                     font=FONTS["ui"], width=3).pack(side=tk.LEFT)
            lbl_pic = tk.Label(pic_row,
                               text=pic_url[:100] + ('...' if len(pic_url) > 100 else ''),
                               bg=SURF, fg="#0055CC", font=FONTS["ui"],
                               cursor="hand2", anchor='w')
            lbl_pic.pack(side=tk.LEFT, fill=tk.X, expand=True)
            lbl_pic.bind("<Button-1>", lambda e, u=pic_url: open_url(u))

        if not pic_list:
            tk.Label(self._media_inner, text="  （无图片数据）", bg=SURF,
                     fg=FG_M, font=FONTS["ui"]).pack(anchor='w', padx=6)

        if video:
            row_vid = tk.Frame(self._media_inner, bg=SURF)
            row_vid.pack(fill=tk.X, padx=6, pady=2)
            tk.Label(row_vid, text="视频:", bg=SURF, fg=FG,
                     font=FONT_LBL, width=9, anchor='w').pack(side=tk.LEFT)
            lbl_vid = tk.Label(row_vid,
                               text=video[:80] + ('...' if len(video) > 80 else ''),
                               bg=SURF, fg="#0055CC", font=FONTS["ui"],
                               cursor="hand2", anchor='w')
            lbl_vid.pack(side=tk.LEFT, fill=tk.X, expand=True)
            lbl_vid.bind("<Button-1>", lambda e, u=video: open_url(u))

    def _update_stats(self):
        def _u():
            # 统计Tab自己的队列 + 引擎内部队列（采集流程自动触发时）
            q = self.task_queue.qsize()
            if self._engine and hasattr(self._engine, '_task_queue'):
                try:
                    q += self._engine._task_queue.qsize()
                except Exception:
                    pass
            done = len(self._result_records)
            has_profit = sum(1 for r in self._result_records
                             if (r.get('final_profit') or 0) > 0)
            rates = []
            for r in self._result_records:
                fp = r.get('final_profit')
                fnp = r.get('final_price')
                if fp is not None and fnp and fnp > 0:
                    rates.append(fp / fnp * 100)
            mr = f"{max(rates):.1f}%" if rates else "—"
            model_txt = "已加载" if SameProductMatcher._loaded else "加载中"
            try:
                self._stat_queue.config(text=str(q))
                self._stat_done.config(text=str(done))
                self._stat_profit.config(text=str(has_profit))
                self.stats_label.config(text=f"最高利润率: {mr}  |  模型: {model_txt}")
            except Exception:
                pass
        try:
            self.parent.after(0, _u)
        except Exception:
            pass

    # ── Webhook 通知（企业微信） ──

    def _send_file_to_wechat(self, file_path: str, file_type: str = "file") -> bool:
        """上传文件到企业微信并发送"""
        if not self._wechat_webhook or not os.path.exists(file_path):
            return False
        try:
            import requests
            upload_url = self._wechat_webhook.replace('/send?', '/upload_media?') + f'&type={file_type}'
            with open(file_path, 'rb') as f:
                files = {'media': (os.path.basename(file_path), f)}
                resp = requests.post(upload_url, files=files, timeout=30)
            if resp.status_code == 200:
                result = resp.json()
                if result.get('errcode') == 0:
                    media_id = result.get('media_id')
                    file_data = {"msgtype": "file", "file": {"media_id": media_id}}
                    requests.post(self._wechat_webhook, json=file_data, timeout=10)
                    return True
                else:
                    self._log(f"  ⚠️ 上传失败: {result}")
            return False
        except Exception as e:
            self._log(f"  ⚠️ 上传文件异常: {e}")
            return False

    def _send_profit_summary_via_wechat(self, records: list, excel_path: str):
        """发送正利润货源简报到企业微信"""
        if not self._wechat_webhook:
            return
        try:
            import requests
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            profit_0_10 = sum(1 for r in records if 0 < (r.get('final_profit') or 0) < 10)
            profit_10_30 = sum(1 for r in records if 10 <= (r.get('final_profit') or 0) < 30)
            profit_30_plus = sum(1 for r in records if (r.get('final_profit') or 0) >= 30)
            sorted_records = sorted(records, key=lambda x: x.get('final_profit', 0), reverse=True)
            top5_lines = []
            for r in sorted_records[:5]:
                title = r.get('source_title', '')[:25]
                profit = r.get('final_profit', 0)
                quadrant = r.get('quadrant_label', '')
                top5_lines.append(f"- {title} → ¥{profit:.1f} ({quadrant})")
            content = f"""## 💰 正利润货源简报

**时间：** {timestamp}
**新增正利润数：** {len(records)}

### 📊 利润分布
- ¥30以上：{profit_30_plus} 件
- ¥10~30：{profit_10_30} 件
- ¥0~10：{profit_0_10} 件

### 🏆 利润前5
{chr(10).join(top5_lines)}

**附件：** 完整Excel文件
"""
            data = {"msgtype": "markdown", "markdown": {"content": content}}
            requests.post(self._wechat_webhook, json=data, timeout=10)
            self._log(f"  📤 正利润简报已发送（{len(records)}条）")
            if excel_path and os.path.exists(excel_path):
                self._send_file_to_wechat(excel_path, "file")
                self._log("  📁 正利润Excel已发送")
        except Exception as e:
            self._log(f"  ⚠️ 发送正利润简报失败: {e}")

    def _send_listing_advice_summary_via_wechat(self, records: list, excel_path: str):
        """发送上架建议简报到企业微信"""
        if not self._wechat_webhook:
            return
        try:
            import requests
            from engines.pdd_supply_finder_v2 import _calc_listing_priority
            stats = {'S级': 0, 'A级': 0, 'B级': 0, 'C级': 0, 'D级': 0}
            for r in records:
                priority = _calc_listing_priority(r)
                stats[priority] = stats.get(priority, 0) + 1
            top_items = []
            for r in records:
                priority = _calc_listing_priority(r)
                if priority in ('S级', 'A级'):
                    title = r.get('source_title', '')[:28]
                    profit = r.get('final_profit')
                    profit_str = f"¥{profit:.1f}" if profit else "待计算"
                    top_items.append(f"- {priority} {title} → {profit_str}")
            top_preview = "\n".join(top_items[:8])
            if len(top_items) > 8:
                top_preview += f"\n... 共{len(top_items)}条"
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            total = len(records)
            s_cnt = stats.get('S级', 0)
            a_cnt = stats.get('A级', 0)
            recommend_rate = f"{(s_cnt + a_cnt) / total * 100:.0f}%" if total > 0 else "0%"
            content = f"""## 📋 上架建议简报

**时间：** {timestamp}
**本次新增货源数：** {total}

### 📊 上架优先级分布
- 🏆 **S级**（强烈推荐）：{s_cnt} 件
- ✅ **A级**（建议上架）：{a_cnt} 件
- 🔄 **B级**（可尝试）：{stats.get('B级', 0)} 件
- ⚠️ **C级**（谨慎）：{stats.get('C级', 0)} 件
- ❌ **D级**（不建议）：{stats.get('D级', 0)} 件

**推荐上架率：** {recommend_rate}（S+A级占比）

### 🎯 S/A级商品预览
{top_preview if top_preview else '（无S/A级商品）'}

---
💡 **提示：** 优先处理S/A级商品，利润空间大、需求旺盛

**附件：** 完整上架建议表
"""
            data = {"msgtype": "markdown", "markdown": {"content": content}}
            requests.post(self._wechat_webhook, json=data, timeout=10)
            self._log(f"  📤 上架建议简报已发送（S级{s_cnt}/A级{a_cnt}）")
            if excel_path and os.path.exists(excel_path):
                self._send_file_to_wechat(excel_path, "file")
                self._log("  📁 上架建议Excel已发送")
        except Exception as e:
            self._log(f"  ⚠️ 发送上架建议简报失败: {e}")

    # ── 导出 ──

    def _export_all(self):
        if not self._result_records:
            messagebox.showwarning("提示", "暂无数据")
            return
        now = datetime.now()
        path = os.path.expanduser(
            f"~/Desktop/全部货源_{now.strftime('%Y%m%d_%H%M%S')}_{len(self._result_records)}件.xlsx")
        export_supply_to_excel(self._result_records, path, self._log, profit_only=False)
        self._log(f"已导出全部结果: {path}")
        # Webhook 推送正利润Excel
        profit_records = [r for r in self._result_records if r.get('final_profit', 0) > 0]
        if profit_records and self._wechat_webhook:
            self._send_profit_summary_via_wechat(profit_records, path)
        messagebox.showinfo("完成", f"已导出全部结果:\n{path}")

    def _export_profit_only(self):
        if not self._result_records:
            messagebox.showwarning("提示", "暂无数据")
            return
        profit_records = [r for r in self._result_records if r.get('final_profit', 0) > 0]
        if not profit_records:
            messagebox.showwarning("提示", "暂无正利润货源")
            return
        now = datetime.now()
        path = os.path.expanduser(
            f"~/Desktop/正利润货源_{now.strftime('%Y%m%d_%H%M%S')}_{len(profit_records)}件.xlsx")
        export_supply_to_excel(profit_records, path, self._log, profit_only=True)
        self._log(f"已导出正利润货源: {path} 共{len(profit_records)}件")
        # Webhook 推送正利润报告
        if self._wechat_webhook:
            self._send_profit_summary_via_wechat(profit_records, path)
        messagebox.showinfo("完成", f"已导出正利润货源:\n{path} 共{len(profit_records)}件")

    def _export_listing_advice(self):
        if not self._result_records:
            messagebox.showwarning("提示", "暂无数据")
            return
        now = datetime.now()
        path = os.path.expanduser(
            f"~/Desktop/上架建议_{now.strftime('%Y%m%d_%H%M%S')}_{len(self._result_records)}件.xlsx")
        export_listing_advice_to_excel(self._result_records, path, self._log)
        self._log(f"上架建议表已导出: {path}")
        # Webhook 推送上架建议
        if self._wechat_webhook:
            self._send_listing_advice_summary_via_wechat(self._result_records, path)
        messagebox.showinfo("完成", f"上架建议表已导出:\n{path}")

    # ── 定时推送 ──

    def _start_scheduled_push_thread(self):
        if self._scheduled_push_running:
            return
        self._scheduled_push_running = True
        self._scheduled_push_thread = threading.Thread(
            target=self._scheduled_push_loop, daemon=True)
        self._scheduled_push_thread.start()

    def _scheduled_push_loop(self):
        last_push_date = None
        while self._scheduled_push_running:
            time.sleep(30)
            try:
                try:
                    enabled = self.enable_scheduled_push_var.get()
                    target_h = int(self.sched_hour_e.get() or 8)
                    target_m = int(self.sched_min_e.get() or 0)
                except Exception:
                    continue

                if not enabled:
                    continue

                now = datetime.now()
                if (now.hour == target_h and now.minute == target_m and last_push_date != now.date()):
                    self._log(f"定时推送触发：{now.strftime('%H:%M')}")
                    self._do_scheduled_push()
                    last_push_date = now.date()
                    try:
                        self.sched_status_lbl.config(
                            text=f"上次推送: {now.strftime('%Y-%m-%d %H:%M')}", fg=SUCC)
                    except Exception:
                        pass
                else:
                    try:
                        next_dt = now.replace(hour=target_h, minute=target_m, second=0, microsecond=0)
                        if next_dt <= now:
                            next_dt += timedelta(days=1)
                        diff_min = int((next_dt - now).total_seconds() / 60)
                        self.sched_status_lbl.config(
                            text=f"下次推送: {next_dt.strftime('%m-%d %H:%M')}（约{diff_min}分后）", fg=FG_M)
                    except Exception:
                        pass
            except Exception:
                pass

    def _do_scheduled_push(self):
        """定时推送：导出已积累的正利润货源 + 上架建议 + Webhook 通知"""
        self._export_profit_and_listing()

    def _export_profit_and_listing(self):
        """导出正利润货源 + 上架建议 Excel + Webhook 通知"""
        if not self._result_records:
            return
        # 只导出未推送过的记录
        new_records = [r for r in self._result_records if id(r) not in self._pushed_ids]
        profit_records = [r for r in new_records if r.get('final_profit', 0) > 0]
        if not profit_records:
            return
        now = datetime.now()
        profit_path = os.path.expanduser(
            f"~/Desktop/正利润货源_{now.strftime('%Y%m%d_%H%M%S')}_{len(profit_records)}件.xlsx")
        export_supply_to_excel(profit_records, profit_path, self._log, profit_only=True)
        self._log(f"定时推送：正利润Excel已导出 {profit_path}")
        listing_path = os.path.expanduser(
            f"~/Desktop/上架建议_{now.strftime('%Y%m%d_%H%M%S')}_{len(profit_records)}件.xlsx")
        export_listing_advice_to_excel(profit_records, listing_path, self._log)
        self._log(f"定时推送：上架建议Excel已导出 {listing_path}")
        if self._wechat_webhook:
            self._send_profit_summary_via_wechat(profit_records, profit_path)
            self._send_listing_advice_summary_via_wechat(profit_records, listing_path)
        for r in profit_records:
            self._pushed_ids.add(id(r))

    # ── 初始化 & Tab挂载 ──

    def auto_init(self):
        threading.Thread(target=self._auto_init, daemon=True).start()

    def _auto_init(self):
        SameProductMatcher.load_model(self._log)

        def update_model_status():
            try:
                if SameProductMatcher._loaded:
                    self.stats_label.config(text="最高利润率: — | 模型: 已加载")
                    self._log("语义模型已就绪")
                elif SameProductMatcher._error:
                    self.stats_label.config(text="最高利润率: — | 模型: 降级模式")
                    self._log(f"语义模型不可用: {SameProductMatcher._error}")
                else:
                    self.stats_label.config(text="最高利润率: — | 模型: 加载中")
            except:
                pass

        self.parent.after(0, update_model_status)

        import random
        time.sleep(random.uniform(0.5, 1.0))

        # 货源查找固定走红米，从配置中读取ADB序列号
        redmi_serial = self._get_redmi_serial()
        self._log(f"[货源] 固定设备: {redmi_serial}")

        ctrl = PinduoduoMobileController(self._log)
        if ctrl.connect(serial=redmi_serial):
            self.controller = ctrl
            try:
                self.parent.after(0, lambda: self.device_status.config(text="已连接", fg=SUCC))
                self.parent.after(0, lambda: self.launch_btn.config(state=tk.NORMAL))
                self._start_processing()
            except Exception as e:
                self._log(f"自动启动失败: {e}")

    def _bind_global_mousewheel(self):
        root = self.parent.winfo_toplevel()

        def _find_scrollable(widget, horizontal=False):
            w = widget
            while w:
                try:
                    if horizontal:
                        if hasattr(w, 'xview') and isinstance(
                                w, (ttk.Treeview, tk.Canvas, tk.Text, scrolledtext.ScrolledText)):
                            return w
                    else:
                        if hasattr(w, 'yview') and isinstance(
                                w, (ttk.Treeview, tk.Canvas, tk.Text, scrolledtext.ScrolledText)):
                            return w
                except Exception:
                    pass
                try:
                    w = w.master
                except Exception:
                    break
            return None

        def _on_global_wheel(event):
            try:
                widget = event.widget
            except Exception:
                return
            target = _find_scrollable(widget, horizontal=False)
            if target:
                target.yview_scroll(int(-1 * (event.delta / 120)), "units")

        def _on_global_wheel_linux_up(event):
            try:
                widget = event.widget
            except Exception:
                return
            target = _find_scrollable(widget, horizontal=False)
            if target:
                target.yview_scroll(-1, "units")

        def _on_global_wheel_linux_down(event):
            try:
                widget = event.widget
            except Exception:
                return
            target = _find_scrollable(widget, horizontal=False)
            if target:
                target.yview_scroll(1, "units")

        def _on_global_hwheel(event):
            try:
                widget = event.widget
            except Exception:
                return
            target = _find_scrollable(widget, horizontal=True)
            if target:
                target.xview_scroll(int(-1 * (event.delta / 120)), "units")

        root.bind_all("<MouseWheel>", _on_global_wheel, add="+")
        root.bind_all("<Button-4>", _on_global_wheel_linux_up, add="+")
        root.bind_all("<Button-5>", _on_global_wheel_linux_down, add="+")
        root.bind_all("<Shift-MouseWheel>", _on_global_hwheel, add="+")

    def on_mount(self):
        """Tab挂载到Notebook后调用"""
        self.auto_init()
        self.parent.after(500, self._start_scheduled_push_thread)
        self.parent.after(600, self._bind_global_mousewheel)
