"""设置弹出窗口：设备/API/评分/采集参数配置"""
import tkinter as tk
from tkinter import ttk, messagebox
from gui.theme import SURF, SURF2, FG, FG_M, ACC, ACC_H, BRD, FONTS


class SettingsDialog(tk.Toplevel):
    def __init__(self, parent, config_manager):
        super().__init__(parent)
        self._config = config_manager
        self._vars = {}
        self.title("设置")
        self.geometry("700x500")
        self.resizable(True, True)
        self.configure(bg=SURF)

        self._build_ui()
        self._load_values()

        self.transient(parent)
        self.grab_set()

    def _build_ui(self):
        # 顶部标题
        header = tk.Frame(self, bg=ACC)
        header.pack(fill=tk.X)
        ttk.Label(header, text="  全局设置", font=FONTS["heading"],
                  background=ACC, foreground="white").pack(side=tk.LEFT, padx=14, pady=10)

        # Notebook 分类
        self._notebook = ttk.Notebook(self)
        self._notebook.pack(fill=tk.BOTH, expand=True, padx=15, pady=5)

        outer1 = self._make_scrollable()
        self._notebook.add(outer1, text="API与通知")
        self._api_frame = outer1._content

        outer2 = self._make_scrollable()
        self._notebook.add(outer2, text="评分参数")
        self._scoring_frame = outer2._content

        outer3 = self._make_scrollable()
        self._notebook.add(outer3, text="采集参数")
        self._collection_frame = outer3._content

        outer4 = self._make_scrollable()
        self._notebook.add(outer4, text="货源查找")
        self._supply_frame = outer4._content

        # 底部按钮
        btn_frame = tk.Frame(self, bg=SURF)
        btn_frame.pack(fill=tk.X, padx=15, pady=15)
        ttk.Button(btn_frame, text="保存", command=self._on_save).pack(side=tk.RIGHT, padx=5)
        ttk.Button(btn_frame, text="取消", command=self.destroy).pack(side=tk.RIGHT, padx=5)

    def _make_scrollable(self):
        outer = tk.Frame(self._notebook, bg=SURF)
        canvas = tk.Canvas(outer, bg=SURF, highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=canvas.yview)
        inner = tk.Frame(canvas, bg=SURF)

        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        win_id = canvas.create_window((0, 0), window=inner, anchor=tk.NW)
        # 让内嵌Frame宽度跟随Canvas扩展，否则内容挤在1px不可见
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(win_id, width=e.width))
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # 鼠标滚轮支持
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _on_mousewheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

        # 存储inner引用供后续添加组件
        outer._content = inner
        return outer

    def _section(self, parent, title: str):
        frame = tk.LabelFrame(parent, text=title, bg=SURF, fg=FG,
                              font=FONTS["ui_bold"], padx=10, pady=5)
        frame.pack(fill=tk.X, padx=5, pady=5)
        return frame

    def _row(self, parent, label: str, var: tk.Variable = None, width: int = 15):
        f = tk.Frame(parent, bg=SURF)
        f.pack(fill=tk.X, padx=5, pady=2)
        ttk.Label(f, text=label, font=FONTS["ui"], width=18, anchor=tk.W).pack(side=tk.LEFT)
        entry = ttk.Entry(f, textvariable=var, width=width, font=FONTS["ui"])
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        return var

    def _row_with_test(self, parent, label: str, var: tk.Variable, width: int, test_cmd, status_var: tk.StringVar):
        """带测试按钮的行"""
        f = tk.Frame(parent, bg=SURF)
        f.pack(fill=tk.X, padx=5, pady=2)
        ttk.Label(f, text=label, font=FONTS["ui"], width=18, anchor=tk.W).pack(side=tk.LEFT)
        entry = ttk.Entry(f, textvariable=var, width=width, font=FONTS["ui"])
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        ttk.Button(f, text="测试", command=test_cmd, width=6).pack(side=tk.LEFT, padx=2)
        ttk.Label(f, textvariable=status_var, font=FONTS["ui"], foreground=FG_M, width=12).pack(side=tk.LEFT)
        return var

    def _load_values(self):
        s = self._config.settings

        # API
        self._vars["zhipu_api_key"] = tk.StringVar(value=s.get("api", {}).get("zhipu_api_key", ""), master=self)
        self._vars["webhook_url"] = tk.StringVar(value=s.get("api", {}).get("webhook_url", ""), master=self)
        self._vars["api_status"] = tk.StringVar(value="", master=self)
        self._vars["webhook_status"] = tk.StringVar(value="", master=self)

        # Collection
        c = s.get("collection", {})
        self._vars["search_pages"] = tk.IntVar(value=c.get("search_pages", 10))
        self._vars["hs_pages"] = tk.IntVar(value=c.get("hs_pages", 3))
        self._vars["detail_max"] = tk.IntVar(value=c.get("detail_max", 5))
        self._vars["comment_max"] = tk.IntVar(value=c.get("comment_max", 3))
        self._vars["rate_search_low"] = tk.IntVar(value=c.get("rate_search", [4, 8])[0])
        self._vars["rate_search_high"] = tk.IntVar(value=c.get("rate_search", [4, 8])[1])
        self._vars["rate_detail_low"] = tk.IntVar(value=c.get("rate_detail", [3, 6])[0])
        self._vars["rate_detail_high"] = tk.IntVar(value=c.get("rate_detail", [3, 6])[1])
        self._vars["rate_comment_low"] = tk.IntVar(value=c.get("rate_comment", [3, 6])[0])
        self._vars["rate_comment_high"] = tk.IntVar(value=c.get("rate_comment", [3, 6])[1])
        self._vars["rate_market_low"] = tk.IntVar(value=c.get("rate_market", [3, 5])[0])
        self._vars["rate_market_high"] = tk.IntVar(value=c.get("rate_market", [3, 5])[1])
        self._vars["rate_keyword_low"] = tk.IntVar(value=c.get("rate_keyword", [8, 15])[0])
        self._vars["rate_keyword_high"] = tk.IntVar(value=c.get("rate_keyword", [8, 15])[1])
        self._vars["kw_push_threshold"] = tk.IntVar(value=c.get("kw_push_threshold", 75))
        self._vars["pd_push_threshold"] = tk.IntVar(value=c.get("pd_push_threshold", 75))

        # Scoring
        sc = s.get("scoring", {})
        kt = sc.get("keyword_grade_thresholds", {})
        self._vars["kw_S"] = tk.IntVar(value=kt.get("S", 90))
        self._vars["kw_A"] = tk.IntVar(value=kt.get("A", 75))
        self._vars["kw_B"] = tk.IntVar(value=kt.get("B", 55))
        self._vars["kw_C"] = tk.IntVar(value=kt.get("C", 35))
        self._vars["kw_D"] = tk.IntVar(value=kt.get("D", 0))

        pt = sc.get("product_grade_thresholds", {})
        self._vars["pd_S"] = tk.IntVar(value=pt.get("S", 90))
        self._vars["pd_A"] = tk.IntVar(value=pt.get("A", 75))
        self._vars["pd_B"] = tk.IntVar(value=pt.get("B", 55))
        self._vars["pd_C"] = tk.IntVar(value=pt.get("C", 40))
        self._vars["pd_D"] = tk.IntVar(value=pt.get("D", 0))

        self._vars["precheck_min_uv"] = tk.IntVar(value=sc.get("precheck_min_uv", 200))
        self._vars["precheck_max_price_drop"] = tk.IntVar(value=sc.get("precheck_max_price_drop", -20))

        # Supply
        sf = s.get("supply_finder", {})
        self._vars["sf_score_threshold"] = tk.IntVar(value=sf.get("score_threshold", 75))
        self._vars["sf_sim_threshold"] = tk.DoubleVar(value=sf.get("sim_threshold", 0.8))
        self._vars["sf_scroll_pages"] = tk.IntVar(value=sf.get("scroll_pages", 5))
        self._vars["sf_max_items"] = tk.IntVar(value=sf.get("max_items", 20))
        self._vars["sf_use_img_search"] = tk.BooleanVar(value=sf.get("use_img_search", True))
        self._vars["sf_delay_products"] = tk.IntVar(value=sf.get("delay_between_products", 8))
        self._vars["sf_pause_every"] = tk.IntVar(value=sf.get("pause_every", 5))
        self._vars["sf_pause_duration"] = tk.IntVar(value=sf.get("pause_duration", 60))

        self._build_api_section()
        self._build_collection_section()
        self._build_scoring_section()
        self._build_supply_section()

    def _build_api_section(self):
        p = self._api_frame
        self._section(p, "API密钥")
        self._row_with_test(p, "智谱API Key", self._vars["zhipu_api_key"], 40,
                           self._test_zhipu_api, self._vars["api_status"])
        self._row_with_test(p, "企业微信Webhook", self._vars["webhook_url"], 40,
                           self._test_webhook, self._vars["webhook_status"])

    def _build_collection_section(self):
        p = self._collection_frame

        sec = self._section(p, "翻页设置")
        self._row(sec, "搜索翻页数", self._vars["search_pages"])
        self._row(sec, "历史销售翻页数", self._vars["hs_pages"])
        self._row(sec, "详情最大数", self._vars["detail_max"])
        self._row(sec, "评论最大数", self._vars["comment_max"])

        sec_push = self._section(p, "推送阈值")
        self._row(sec_push, "词推选品最低分", self._vars["kw_push_threshold"])
        self._row(sec_push, "品推货源最低分", self._vars["pd_push_threshold"])

        sec2 = self._section(p, "限速 (秒/次)")
        r = tk.Frame(sec2, bg=SURF)
        r.pack(fill=tk.X, padx=5, pady=2)
        ttk.Label(r, text="搜索间隔", font=FONTS["ui"], width=18, anchor=tk.W).pack(side=tk.LEFT)
        ttk.Entry(r, textvariable=self._vars["rate_search_low"], width=6, font=FONTS["ui"]).pack(side=tk.LEFT)
        ttk.Label(r, text="~", font=FONTS["ui"]).pack(side=tk.LEFT)
        ttk.Entry(r, textvariable=self._vars["rate_search_high"], width=6, font=FONTS["ui"]).pack(side=tk.LEFT)

        for label, vlow, vhigh in [
            ("详情间隔", "rate_detail_low", "rate_detail_high"),
            ("评论间隔", "rate_comment_low", "rate_comment_high"),
            ("行情间隔", "rate_market_low", "rate_market_high"),
            ("词间间隔", "rate_keyword_low", "rate_keyword_high"),
        ]:
            r = tk.Frame(sec2, bg=SURF)
            r.pack(fill=tk.X, padx=5, pady=2)
            ttk.Label(r, text=label, font=FONTS["ui"], width=18, anchor=tk.W).pack(side=tk.LEFT)
            ttk.Entry(r, textvariable=self._vars[vlow], width=6, font=FONTS["ui"]).pack(side=tk.LEFT)
            ttk.Label(r, text="~", font=FONTS["ui"]).pack(side=tk.LEFT)
            ttk.Entry(r, textvariable=self._vars[vhigh], width=6, font=FONTS["ui"]).pack(side=tk.LEFT)

    def _build_scoring_section(self):
        p = self._scoring_frame

        sec = self._section(p, "选词等级阈值")
        r = tk.Frame(sec, bg=SURF)
        r.pack(fill=tk.X, padx=5, pady=2)
        for grade in ["S", "A", "B", "C", "D"]:
            ttk.Label(r, text=f"{grade}级≥", font=FONTS["ui"]).pack(side=tk.LEFT, padx=2)
            ttk.Entry(r, textvariable=self._vars[f"kw_{grade}"], width=5, font=FONTS["ui"]).pack(side=tk.LEFT, padx=(0, 8))

        sec2 = self._section(p, "选品等级阈值")
        r2 = tk.Frame(sec2, bg=SURF)
        r2.pack(fill=tk.X, padx=5, pady=2)
        for grade in ["S", "A", "B", "C", "D"]:
            ttk.Label(r2, text=f"{grade}级≥", font=FONTS["ui"]).pack(side=tk.LEFT, padx=2)
            ttk.Entry(r2, textvariable=self._vars[f"pd_{grade}"], width=5, font=FONTS["ui"]).pack(side=tk.LEFT, padx=(0, 8))

        sec3 = self._section(p, "预检参数")
        self._row(sec3, "最低24hUV(日)", self._vars["precheck_min_uv"])
        self._row(sec3, "最大价格跌幅(元)", self._vars["precheck_max_price_drop"])

    def _build_supply_section(self):
        p = self._supply_frame

        sec = self._section(p, "货源筛选")
        self._row(sec, "评分阈值", self._vars["sf_score_threshold"])
        self._row(sec, "相似度阈值", self._vars["sf_sim_threshold"])
        self._row(sec, "滚动页数", self._vars["sf_scroll_pages"])
        self._row(sec, "最大结果数", self._vars["sf_max_items"])

        sec2 = self._section(p, "选项")
        r = tk.Frame(sec2, bg=SURF)
        r.pack(fill=tk.X, padx=5, pady=5)
        ttk.Checkbutton(r, text="启用图搜", variable=self._vars["sf_use_img_search"]).pack(side=tk.LEFT)

        sec3 = self._section(p, "限速")
        self._row(sec3, "商品间延迟(秒)", self._vars["sf_delay_products"])
        self._row(sec3, "暂停间隔(个)", self._vars["sf_pause_every"])
        self._row(sec3, "暂停时长(秒)", self._vars["sf_pause_duration"])

    def _test_zhipu_api(self):
        api_key = self._vars["zhipu_api_key"].get().strip()
        if not api_key:
            self._vars["api_status"].set("请先输入Key")
            return
        self._vars["api_status"].set("测试中...")
        import threading
        def _do():
            try:
                import requests, json
                resp = requests.post(
                    "https://open.bigmodel.cn/api/paas/v4/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={"model": "glm-4-flash", "messages": [{"role": "user", "content": "hi"}]},
                    timeout=15
                )
                if resp.status_code == 200:
                    self._vars["api_status"].set("✅ 连接成功")
                else:
                    self._vars["api_status"].set(f"❌ HTTP {resp.status_code}")
            except Exception as e:
                self._vars["api_status"].set(f"❌ {str(e)[:20]}")
        threading.Thread(target=_do, daemon=True).start()

    def _test_webhook(self):
        url = self._vars["webhook_url"].get().strip()
        if not url:
            self._vars["webhook_status"].set("请先输入URL")
            return
        self._vars["webhook_status"].set("测试中...")
        import threading
        def _do():
            try:
                import requests, json
                payload = {"msgtype": "text", "text": {"content": "✅ 闲鱼采集工具 Webhook 测试成功"}}
                resp = requests.post(url, json=payload, timeout=15)
                if resp.status_code == 200:
                    self._vars["webhook_status"].set("✅ 发送成功")
                else:
                    self._vars["webhook_status"].set(f"❌ HTTP {resp.status_code}")
            except Exception as e:
                self._vars["webhook_status"].set(f"❌ {str(e)[:20]}")
        threading.Thread(target=_do, daemon=True).start()

    def _on_save(self):
        try:
            new_values = {
                "api": {
                    "zhipu_api_key": self._vars["zhipu_api_key"].get(),
                    "webhook_url": self._vars["webhook_url"].get(),
                },
                "collection": {
                    "search_pages": self._vars["search_pages"].get(),
                    "hs_pages": self._vars["hs_pages"].get(),
                    "detail_max": self._vars["detail_max"].get(),
                    "comment_max": self._vars["comment_max"].get(),
                    "rate_search": [self._vars["rate_search_low"].get(), self._vars["rate_search_high"].get()],
                    "rate_detail": [self._vars["rate_detail_low"].get(), self._vars["rate_detail_high"].get()],
                    "rate_comment": [self._vars["rate_comment_low"].get(), self._vars["rate_comment_high"].get()],
                    "rate_market": [self._vars["rate_market_low"].get(), self._vars["rate_market_high"].get()],
                    "rate_keyword": [self._vars["rate_keyword_low"].get(), self._vars["rate_keyword_high"].get()],
                    "kw_push_threshold": self._vars["kw_push_threshold"].get(),
                    "pd_push_threshold": self._vars["pd_push_threshold"].get(),
                },
                "scoring": {
                    "keyword_grade_thresholds": {
                        "S": self._vars["kw_S"].get(), "A": self._vars["kw_A"].get(),
                        "B": self._vars["kw_B"].get(), "C": self._vars["kw_C"].get(), "D": self._vars["kw_D"].get(),
                    },
                    "product_grade_thresholds": {
                        "S": self._vars["pd_S"].get(), "A": self._vars["pd_A"].get(),
                        "B": self._vars["pd_B"].get(), "C": self._vars["pd_C"].get(), "D": self._vars["pd_D"].get(),
                    },
                    "precheck_min_uv": self._vars["precheck_min_uv"].get(),
                    "precheck_max_price_drop": self._vars["precheck_max_price_drop"].get(),
                },
                "supply_finder": {
                    "score_threshold": self._vars["sf_score_threshold"].get(),
                    "sim_threshold": self._vars["sf_sim_threshold"].get(),
                    "scroll_pages": self._vars["sf_scroll_pages"].get(),
                    "max_items": self._vars["sf_max_items"].get(),
                    "use_img_search": self._vars["sf_use_img_search"].get(),
                    "delay_between_products": self._vars["sf_delay_products"].get(),
                    "pause_every": self._vars["sf_pause_every"].get(),
                    "pause_duration": self._vars["sf_pause_duration"].get(),
                },
            }
            self._config.update_settings(new_values)
            self._config.save_settings()
            messagebox.showinfo("成功", "设置已保存")
            self.destroy()
        except Exception as e:
            messagebox.showerror("保存失败", str(e))

