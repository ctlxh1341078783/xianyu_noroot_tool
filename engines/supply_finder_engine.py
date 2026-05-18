"""货源查找引擎：薄封装 MobileSupplyScheduler，复用原始PDD搜索+AI匹配+利润评估全逻辑"""
import threading
import time
import queue
from pathlib import Path
from typing import Callable, Dict, List, Optional

from utils.log_manager import get_logger

# 从同目录完整实现导入核心类
try:
    from .pdd_supply_finder_v2 import (
        PinduoduoMobileController,
        MobileSupplyScheduler,
        TitleCleanerAI,
        SameProductMatcher,
        ProfitAnalyzer,
        evaluate_supply_quadrant,
        get_ai_cleaner,
        set_ai_api_key,
    )
    HAS_FULL_IMPL = True
except ImportError as e:
    HAS_FULL_IMPL = False
    _IMPORT_ERROR = str(e)


def _safe_float(val) -> float:
    """安全转float，处理 ¥24 / '24.00' / 24 等格式"""
    if isinstance(val, (int, float)):
        return float(val)
    if not val:
        return 0.0
    s = str(val).replace('¥', '').replace('￥', '').replace(',', '').replace(' ', '').strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


class SupplyItem:
    """货源查找输入：闲鱼商品"""
    def __init__(self, data: dict):
        self._raw = data  # 保留原始 enriched dict，供后续传递完整字段
        self.item_id = data.get("itemId", "") or data.get("item_id", "")
        self.title = data.get("商品标题", "") or data.get("title", "")
        self.price = _safe_float(data.get("商品价格", 0) or data.get("price", 0) or 0)
        self.pic_url = data.get("商品图片", "") or data.get("picUrl", "") or data.get("mainPic", "") or data.get("pic_url", "")
        self.sold_count = data.get("已售数量", 0) or data.get("soldCount", 0) or data.get("sold_count", 0)
        self.avg_price = _safe_float(data.get("avgPrice", 0) or data.get("avg_price", 0) or 0)
        self.score = _safe_float(data.get("productScore", 0) or data.get("综合评分", 0))
        self.desc = data.get("商品描述", "") or data.get("desc", "") or data.get("description", "") or data.get("itemDesc", "")
        self.want_count = _safe_float(data.get("想要人数", 0) or data.get("wantCnt", 0) or data.get("want_count", 0))
        self.days_on_sale = _safe_float(data.get("已上架天数", 0) or data.get("daysOnSale", 0) or data.get("days_on_sale", 0))
        self.daily_want = _safe_float(data.get("日均想要数", 0))
        self.xianyu_link = data.get("商品链接", "")
        self.collect_cnt = data.get("收藏数", 0)
        self.browse_cnt = data.get("浏览数", 0)
        self.seller_nick = data.get("卖家昵称", "")
        self.seller_sold = data.get("卖家已售", 0)
        self.seller_good_rate = data.get("卖家好评率", "")
        self.seller_reply_rate = data.get("卖家回复率", "")
        self.grade = data.get("评分等级", "")

    def to_dict(self) -> dict:
        return {
            "item_id": self.item_id, "title": self.title, "price": self.price,
            "pic_url": self.pic_url, "sold_count": self.sold_count,
            "avg_price": self.avg_price, "desc": self.desc,
        }


class SupplyMatch:
    """单条货源匹配结果"""
    def __init__(self):
        self.source_title = ""
        self.source_price = 0.0
        self.source_sales = ""
        self.source_url = ""
        self.match_type = ""
        self.match_reason = ""
        self.sim_score = 0.0
        self.profit = 0.0
        self.profit_rate = 0.0
        self.source = ""


class SupplyResult:
    """单件闲鱼商品的货源查找总结果"""
    def __init__(self, item: SupplyItem):
        self.item = item
        self.title_matches: List[SupplyMatch] = []
        self.image_matches: List[SupplyMatch] = []
        self.best_match: Optional[SupplyMatch] = None
        self.quadrant = ""
        self.quadrant_label = ""
        self.quadrant_emoji = ""
        self.recommendation = ""
        self.final_profit = None
        self.final_price = None
        self.final_source = ""


class SupplyFinderEngine:
    """货源查找引擎：复用 MobileSupplyScheduler 的完整逻辑"""

    def __init__(self, settings: dict):
        self._settings = settings
        self._logger = get_logger()
        self._running = False
        self._results: List[SupplyResult] = []
        self._supply_items: List[SupplyItem] = []
        # 货源查找固定走红米，从配置中读取其ADB序列号
        self._device_serial = self._get_redmi_serial()

        self._score_threshold = 75
        self._sim_threshold = 0.8
        self._scroll_pages = 5
        self._max_items = 20
        self._img_scroll_pages = 3
        self._use_img_search = True
        self._use_ai_compare = True
        self._delay_between = 8
        self._pause_every = 5
        self._pause_duration = 60
        self._load_params(settings)

        # 回调
        self._on_progress: Optional[Callable[[str], None]] = None
        self._on_result: Optional[Callable[[SupplyResult], None]] = None
        self._on_complete: Optional[Callable[[], None]] = None
        self._on_ui_result: Optional[Callable[[dict], None]] = None  # 直接传原始record给Tab更新UI

        # 内部组件（延迟初始化）
        self._task_queue: queue.Queue = queue.Queue()
        self._scheduler: Optional[MobileSupplyScheduler] = None
        self._controller: Optional[PinduoduoMobileController] = None
        self._thread: Optional[threading.Thread] = None
        self._device_engine = None  # 用于暂停/恢复健康检查

    @property
    def running(self) -> bool:
        return self._running

    @property
    def results(self) -> List[SupplyResult]:
        return self._results

    def _load_params(self, settings: dict):
        sf = settings.get("supply_finder", {})
        self._score_threshold = sf.get("score_threshold", 75)
        self._sim_threshold = sf.get("sim_threshold", 0.8)
        self._scroll_pages = sf.get("scroll_pages", 5)
        self._max_items = sf.get("max_items", 20)
        self._img_scroll_pages = sf.get("img_scroll_pages", 3)
        self._use_img_search = sf.get("use_img_search", True)
        self._use_ai_compare = sf.get("use_ai_compare", True)
        self._delay_between = sf.get("delay_between_products", 8)
        self._pause_every = sf.get("pause_every", 5)
        self._pause_duration = sf.get("pause_duration", 60)

    def update_params(self, settings: dict):
        self._load_params(settings)
        # 如果调度器已在运行，实时更新参数
        if self._scheduler:
            self._scheduler.score_threshold = self._score_threshold
            self._scheduler.sim_threshold = self._sim_threshold
            self._scheduler.scroll_pages = self._scroll_pages
            self._scheduler.max_items = self._max_items
            self._scheduler.img_scroll_pages = self._img_scroll_pages
            self._scheduler.use_img_search = self._use_img_search
            self._scheduler.use_ai_compare = self._use_ai_compare
            self._scheduler.delay_between_products = self._delay_between
            self._scheduler.pause_every = self._pause_every
            self._scheduler.pause_duration = self._pause_duration

    def _get_redmi_serial(self) -> str:
        """货源查找固定走红米，从配置中读取其ADB序列号"""
        devices = self._settings.get("devices", [])
        for d in devices:
            if "redmi" in d.get("name", "").lower() or d.get("use_gadget"):
                return d.get("adb_addr", "")
        # 没有找到红米设备，返回第一台或空
        return devices[0].get("adb_addr", "") if devices else ""

    def set_device_engine(self, device_engine):
        """注入设备引擎，用于暂停/恢复健康检查"""
        self._device_engine = device_engine

    def set_callbacks(self, on_progress=None, on_result=None, on_complete=None, on_ui_result=None):
        self._on_progress = on_progress
        self._on_result = on_result
        self._on_complete = on_complete
        self._on_ui_result = on_ui_result

    def start(self, items: List[dict]):
        if self._running:
            self._logger.warn("货源查找已在运行中")
            return

        if not HAS_FULL_IMPL:
            self._logger.warn(f"完整货源查找模块未加载: {_IMPORT_ERROR}")
            return

        if not items:
            self._logger.warn("货源查找：无商品输入")
            return

        self._running = True
        self._results = []
        self._supply_items = [SupplyItem(d) for d in items]

        # 暂停全局健康检查（货源查找全生命周期内禁止重启闲鱼）
        if self._device_engine:
            self._device_engine.pause_health_check()
            self._logger.info("[设备] 健康检查已暂停（货源查找进行中）")

        # 后台线程：初始化 + 推队列 + 启动调度
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._scheduler:
            self._scheduler.stop()
        self._logger.info("货源查找已停止")
        # 恢复全局健康检查（只在彻底停止时恢复）
        if self._device_engine:
            self._device_engine.resume_health_check()
            self._logger.info("[设备] 健康检查已恢复")

    # ── 内部 ──

    def _init_components(self) -> bool:
        """初始化PDD控制器 + AI + 调度器，完全复用原版逻辑"""
        # 1. 设置AI API Key（全局单例，MobileSupplyScheduler内部用 get_ai_cleaner() 获取）
        api_key = self._settings.get("api", {}).get("zhipu_api_key", "")
        if api_key and self._use_ai_compare:
            set_ai_api_key(api_key, self._logger.info)
            self._logger.info("[货源] AI API Key 已配置")

        # 1.5 加载语义模型（text2vec，失败则自动降级到字符相似度）
        SameProductMatcher.load_model(self._logger.info)

        # 2. 连接PDD控制器（完全沿用原版 connect() 自动检测USB设备）
        if not self._controller:
            self._controller = PinduoduoMobileController(log_cb=self._logger.info)
            try:
                if not self._controller.connect(serial=self._device_serial):
                    self._logger.error("[货源] PDD设备连接失败（请确认手机USB已连接，PDD已安装）")
                    return False
            except Exception as e:
                self._logger.error(f"[货源] PDD设备连接异常: {e}")
                return False

        # 3. 确保PDD在前台并处于首页（原版GUI有独立"启动APP"按钮，引擎里自动处理）
        if not self._controller.launch_pinduoduo():
            self._logger.warn("[货源] 拼多多启动失败，尝试继续")
        import time
        time.sleep(2)  # 额外等待页面稳定

        # 4. 创建调度器（复用MobileSupplyScheduler的_process_item全逻辑）
        self._task_queue = queue.Queue()
        self._scheduler = MobileSupplyScheduler(
            task_queue=self._task_queue,
            result_cb=self._on_scheduler_result,
            log_cb=self._logger.info,
            score_threshold=self._score_threshold,
            sim_threshold=self._sim_threshold,
            scroll_pages=self._scroll_pages,
            max_items=self._max_items,
            img_scroll_pages=self._img_scroll_pages,
            use_img_search=self._use_img_search,
            use_ai_compare=self._use_ai_compare,
            delay_between_products=self._delay_between,
            pause_every=self._pause_every,
            pause_duration=self._pause_duration,
        )
        self._scheduler.controller = self._controller

        # 传递 webhook URL 给调度器（风控告警 + 正利润推送）
        webhook_url = self._settings.get("api", {}).get("webhook_url", "")
        if webhook_url:
            self._scheduler._wechat_webhook = webhook_url
            self._logger.info("[货源] Webhook 已配置，风控告警+正利润推送已启用")

        self._logger.info("[货源] 所有组件初始化完成")
        return True

    def _run(self):
        """后台主流程：初始化 → 推队列 → 启动 → 等待完成"""
        if not self._init_components():
            # 初始化失败，所有商品标记Q5
            for item in self._supply_items:
                result = SupplyResult(item)
                result.quadrant = "Q5"
                result.quadrant_label = "无PDD设备"
                result.recommendation = "PDD设备未就绪，无法采集"
                self._results.append(result)
                if self._on_result:
                    self._on_result(result)
            if self._on_complete:
                self._on_complete()
            self.stop()
            return

        # 推送所有商品到队列
        for item in self._supply_items:
            task = dict(item._raw)  # 保留原始 enriched 全部字段
            # 用 SupplyItem 解析后的值覆盖关键字段（确保类型正确）
            task.update({
                '商品标题': item.title,
                '商品描述': item.desc,
                '商品价格': item.price or item.avg_price or 1,
                '商品图片': item.pic_url,
                '商品链接': item.xianyu_link or task.get('商品链接', ''),
                '综合评分': item.score or item.avg_price or 0,
                '已上架天数': item.days_on_sale,
                '想要人数': item.want_count,
                '日均想要数': item.daily_want or (item.want_count / max(item.days_on_sale, 1) if item.days_on_sale > 0 else 0),
                '已售数量': item.sold_count,
                '卖家昵称': item.seller_nick or task.get('卖家昵称', ''),
                '卖家已售': item.seller_sold or task.get('卖家已售', 0),
                '卖家好评率': item.seller_good_rate or task.get('卖家好评率', ''),
                '卖家回复率': item.seller_reply_rate or task.get('卖家回复率', ''),
                '收藏数': item.collect_cnt or task.get('收藏数', 0),
                '浏览数': item.browse_cnt or task.get('浏览数', 0),
            })
            self._task_queue.put(task)
        self._logger.info(f"[货源] 已推送 {len(self._supply_items)} 件商品到处理队列")

        # 启动调度器（auto_stop_when_empty=True，队列空后自动停止）
        self._scheduler.start_processing(auto_stop_when_empty=True)

        # 等待调度器处理完成（轮询 scheduler._running）
        while self._scheduler._running and self._running:
            time.sleep(1)

        self._logger.info(f"[货源] 查找完成: {len(self._results)} 件结果")
        self.stop()
        if self._on_complete:
            self._on_complete()

    def _on_scheduler_result(self, record: dict):
        """MobileSupplyScheduler 的结果回调 → 转为 SupplyResult"""
        item = None
        for si in self._supply_items:
            if si.title == record.get('source_title', ''):
                item = si
                break
        if item is None:
            item = SupplyItem({'title': record.get('source_title', '')})

        result = SupplyResult(item)

        def _build_matches(pdd_items: list) -> List[SupplyMatch]:
            matches = []
            for p in pdd_items:
                m = SupplyMatch()
                m.source_title = p.get('goods_name', '')
                m.source_price = p.get('pdd_price_yuan', 0)
                m.source_sales = p.get('sales_tip', '')
                m.match_type = p.get('match_level', '')
                m.match_reason = p.get('match_reason', '')
                m.sim_score = p.get('sim_score', 0)
                m.profit = p.get('预估利润(元)', 0) or 0
                m.profit_rate = p.get('利润率(%)', 0) or 0
                m.source = p.get('platform', '')
                matches.append(m)
            return matches

        result.title_matches = _build_matches(record.get('pdd_items', []))
        result.image_matches = _build_matches(record.get('img_pdd_items', []))
        result.quadrant = record.get('quadrant', 'Q5')
        result.quadrant_label = record.get('quadrant_label', '')
        result.quadrant_emoji = record.get('quadrant_emoji', '')
        result.recommendation = record.get('recommendation', '')
        result.final_profit = record.get('final_profit')
        result.final_price = record.get('final_price')
        result.final_source = record.get('final_source', '')

        # 找最优匹配
        all_items = record.get('pdd_items', []) + record.get('img_pdd_items', [])
        if all_items:
            best = all_items[0]  # analyzer已按评分降序
            result.best_match = SupplyMatch()
            result.best_match.source_title = best.get('goods_name', '')
            result.best_match.source_price = best.get('pdd_price_yuan', 0)
            result.best_match.source_sales = best.get('sales_tip', '')
            result.best_match.match_type = best.get('match_level', '')
            result.best_match.match_reason = best.get('match_reason', '')
            result.best_match.sim_score = best.get('sim_score', 0)
            result.best_match.profit = best.get('预估利润(元)', 0) or 0
            result.best_match.profit_rate = best.get('利润率(%)', 0) or 0
            result.best_match.source = best.get('platform', '')

        self._logger.info(
            f"  {result.quadrant_emoji} {result.quadrant} {result.quadrant_label} | "
            f"标题{len(result.title_matches)}件 图搜{len(result.image_matches)}件 | "
            f"利润¥{result.final_profit or 0:.1f}"
        )

        self._results.append(result)
        if self._on_result:
            self._on_result(result)
        if self._on_ui_result:
            self._on_ui_result(record)  # 将原始record传给Tab更新UI树
