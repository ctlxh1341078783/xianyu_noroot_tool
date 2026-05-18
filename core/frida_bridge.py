"""
Frida 桥接层：脚本加载、RPC 通信、断线自动重连
"""
from __future__ import annotations
import frida
import json
import sys
import time
import os
import threading
from pathlib import Path
from typing import Dict, Optional, Any, Callable

from .device_mgr import DeviceManager
from utils.log_manager import get_logger


def _get_base_dir() -> str:
    """获取项目根目录，兼容 PyInstaller 打包"""
    if getattr(sys, 'frozen', False):
        return sys._MEIPASS
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FridaBridge:
    """Frida 脚本加载与 RPC 通信，带心跳+自动重连"""

    def __init__(self, device_mgr: DeviceManager, script_path: str = "batch_collect.js"):
        self._device_mgr = device_mgr
        self._script_path = os.path.join(_get_base_dir(), script_path)
        self._bridge_loader_path = os.path.join(_get_base_dir(), "bridge_loader.js")
        self._log = get_logger()
        self._session: Optional[frida.core.Session] = None
        self._script: Optional[frida.core.Script] = None
        self._loaded = False
        self._lock = threading.Lock()
        self._msg_callback: Optional[Callable[[str], None]] = None
        self._progress_callback: Optional[Callable[[str, str, int, int], None]] = None
        self._rpc_timeout = 35
        # Frida bridge 代码目录（Gadget模式需要）
        self._bridges_dir = Path(os.path.dirname(frida.__file__)) / ".." / "frida_tools" / "bridges"
        if not self._bridges_dir.is_dir():
            # 尝试从 frida_tools 包路径推导
            try:
                import frida_tools
                self._bridges_dir = Path(os.path.dirname(frida_tools.__file__)) / "bridges"
            except ImportError:
                pass

    @property
    def loaded(self) -> bool:
        return self._loaded

    def set_message_callback(self, cb: Callable[[str], None]):
        self._msg_callback = cb

    def set_progress_callback(self, cb: Callable[[str, str, int, int], None]):
        """设置闭环采集进度回调 (stage, info, done, total)"""
        self._progress_callback = cb

    def load(self) -> bool:
        """加载 Frida 脚本到目标进程"""
        with self._lock:
            return self._do_load()

    def _do_load(self) -> bool:
        device_info = self._device_mgr.get_active()
        if not device_info or not device_info.app_pid:
            self._log.error("[Frida] 无活动设备或 App 未运行")
            return False

        # 通过 ADB 端口转发连接 frida-server (127.0.0.1:27042)
        try:
            device = frida.get_device_manager().add_remote_device("127.0.0.1:27042")
        except Exception:
            try:
                device = frida.get_usb_device()
            except Exception as e:
                self._log.error(f"[Frida] 获取设备失败: {e}")
                self._loaded = False
                return False

        try:
            self._session = device.attach(device_info.app_pid)
        except Exception as e:
            self._log.error(f"[Frida] attach 失败 (PID {device_info.app_pid}): {e}")
            self._loaded = False
            return False

        if not os.path.exists(self._script_path):
            self._log.error(f"[Frida] 脚本文件不存在: {self._script_path}")
            self._loaded = False
            return False

        # 拼接 bridge_loader.js + 采集脚本（Gadget模式需要bridge_loader来加载Java bridge）
        js_parts = []
        if os.path.exists(self._bridge_loader_path):
            js_parts.append(Path(self._bridge_loader_path).read_text(encoding="utf-8"))
        js_parts.append(Path(self._script_path).read_text(encoding="utf-8"))
        js_code = "\n// === bridge_loader ===\n" + js_parts[0] + "\n// === collector ===\n" + js_parts[1] if len(js_parts) > 1 else js_parts[0]

        try:
            self._script = self._session.create_script(js_code)
            self._script.on("message", self._on_frida_message)
            self._script.load()

            # 等待 JS 端 Java bridge 初始化完成（bridge_loader.js会触发frida:load-bridge协议）
            for i in range(20):
                time.sleep(1.5)
                try:
                    r = self._script.exports_sync.status()
                    data = json.loads(r)
                    if data.get("ready"):
                        self._loaded = True
                        self._log.info("[Frida] 脚本加载成功")
                        return True
                except Exception:
                    pass
                if i == 0:
                    self._log.info("[Frida] 等待 Java Bridge 初始化...")
                elif i == 5:
                    self._log.info("[Frida] Java Bridge 仍在加载中...")

            self._log.error("[Frida] 脚本 Java Bridge 初始化超时")
            self._loaded = False
            return False
        except Exception as e:
            self._log.error(f"[Frida] 脚本加载失败: {e}")
            self._loaded = False
            return False

    def unload(self):
        with self._lock:
            self._do_unload()

    def _do_unload(self):
        try:
            if self._script:
                self._script.unload()
        except:
            pass
        try:
            if self._session:
                self._session.detach()
        except:
            pass
        self._script = None
        self._session = None
        self._loaded = False

    def _on_frida_message(self, message, data):
        if message["type"] == "send":
            payload = message.get("payload", {})
            # 处理 Gadget bridge loading 协议
            if isinstance(payload, dict) and payload.get("type") == "frida:load-bridge":
                stem = payload["name"].lower()
                bridge_path = self._bridges_dir / f"{stem}.js"
                if bridge_path.exists():
                    bridge_src = bridge_path.read_text(encoding="utf-8")
                    self._script.post({
                        "type": "frida:bridge-loaded",
                        "filename": f"{stem}.js",
                        "source": bridge_src,
                    })
                else:
                    self._log.error(f"[Frida] Bridge文件不存在: {bridge_path}")
                return
            # 处理闭环采集进度消息
            if isinstance(payload, dict) and payload.get("type") == "progress":
                if self._progress_callback:
                    stage = payload.get("stage", "")
                    done = payload.get("done", 0)
                    total = payload.get("total", 0)
                    kw = payload.get("kw", "")
                    self._progress_callback(stage, kw, done, total)
                return
            if self._msg_callback:
                self._msg_callback(str(payload) if not isinstance(payload, str) else payload)
            if "[BC]" in str(payload):
                self._log.debug(f"[Frida] {payload}")
        elif message["type"] == "error":
            self._log.warn(f"[Frida] Error: {message}")

    def _reload_if_needed(self) -> bool:
        if self._loaded and self._script:
            return True
        self._log.warn("[Frida] 脚本未加载，尝试重新加载...")
        return self._do_load()

    # ── RPC 方法封装 ──

    def _rpc(self, method: str, *args, timeout: int = None) -> Any:
        timeout = timeout or self._rpc_timeout
        if not self._reload_if_needed():
            return {"error": "Frida 脚本未加载"}
        try:
            result = getattr(self._script.exports_sync, method)(*args)
            return json.loads(result)
        except AttributeError:
            self._log.warn(f"[RPC] 方法 {method} 不存在，重新加载脚本")
            self._loaded = False
            return {"error": f"unable to find method '{method}'"}
        except Exception as e:
            self._log.error(f"[RPC] {method} 失败: {e}")
            return {"error": str(e)}

    def heartbeat(self) -> bool:
        result = self._rpc("status", timeout=10)
        return result.get("ok", False) if isinstance(result, dict) else False

    # ── 搜索 ──
    def search(self, keyword: str, page: int = 1, page_size: int = 20) -> dict:
        return self._rpc("search", keyword, page, page_size)

    # ── 行情 ──
    def search_for_market(self, keyword: str) -> dict:
        """仅触发搜索以获取 spuId，不记录数据"""
        return self._rpc("search", "__market_anchor__", 1, 20)

    def get_market_tabs(self, keyword: str) -> dict:
        return self._rpc("get_market_tabs", keyword)

    def get_market_topbar(self, keyword: str, spu_id: str, category_id: str,
                          spu_name: str = "", category_name: str = "") -> dict:
        return self._rpc("get_market_topbar", keyword, spu_id, category_id, spu_name, category_name)

    def get_market_history_sale(self, keyword: str, spu_id: str, category_id: str,
                                spu_name: str = "", category_name: str = "",
                                page: int = 1) -> dict:
        return self._rpc("get_market_history_sale", keyword, spu_id, category_id, spu_name, category_name, page)

    def get_market_price_trend(self, keyword: str, spu_id: str, category_id: str,
                               spu_name: str = "", category_name: str = "") -> dict:
        return self._rpc("get_market_price_trend", keyword, spu_id, category_id, spu_name, category_name)

    # ── 详情/评论 ──
    def get_detail(self, item_id: str) -> dict:
        return self._rpc("get_detail", item_id)

    def get_comments(self, item_id: str) -> dict:
        return self._rpc("get_comments", item_id)

    # ── 闭环采集 ──
    def collect_keyword(self, kw: str, max_pages: int = 5, detail_max: int = 5,
                        comment_max: int = 3) -> dict:
        """闭环采集：搜索翻页+详情+评论，一次 RPC 完成，JS 侧通过 send() 推送进度"""
        return self._rpc("collect_keyword", kw, max_pages, detail_max, comment_max, timeout=180)

    def collect_details(self, item_ids: list) -> dict:
        """精准详情采集：只采指定 itemId 列表的详情（Python 预筛选后调用）"""
        import json
        return self._rpc("collect_details", json.dumps(item_ids), timeout=120)

    def collect_comments(self, item_ids: list) -> dict:
        """精准评论采集：只采指定 itemId 列表的评论"""
        import json
        return self._rpc("collect_comments", json.dumps(item_ids), timeout=120)

    def collect_market(self, kw: str, hs_pages: int = 3) -> dict:
        """闭环行情采集：tabs + topbar + historySale翻页 + pricetrend，一次 RPC 完成"""
        return self._rpc("collect_market", kw, hs_pages, timeout=120)

    # ── 工具 ──
    def clear_cache(self) -> dict:
        return self._rpc("clear")
