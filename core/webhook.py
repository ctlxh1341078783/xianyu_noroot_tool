"""
企业微信 Webhook 通知模块
"""
from __future__ import annotations
import requests
import json
from datetime import datetime


class WebhookNotifier:
    """企业微信机器人通知"""

    def __init__(self, webhook_url: str = ""):
        self.webhook_url = webhook_url
        self._enabled = bool(webhook_url)

    def set_url(self, url: str):
        self.webhook_url = url
        self._enabled = bool(url)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _send(self, content: str, msg_type: str = "markdown") -> bool:
        if not self._enabled or not self.webhook_url:
            return False
        try:
            data = {"msgtype": msg_type, msg_type: {"content": content}}
            resp = requests.post(self.webhook_url, json=data, timeout=10)
            return resp.status_code == 200 and resp.json().get("errcode") == 0
        except:
            return False

    def notify_event(self, title: str, body: str = "", level: str = "info"):
        """发送事件通知"""
        icons = {"info": "✅", "warn": "⚠️", "error": "❌"}
        icon = icons.get(level, "📌")
        ts = datetime.now().strftime("%m-%d %H:%M:%S")
        content = f"{icon} **{title}**\n> {ts}\n{body}"
        self._send(content)

    def notify_collect_done(self, task_type: str, keyword: str, count: int):
        """采集完成通知"""
        self.notify_event(f"{task_type}采集完成", f"关键词: {keyword}\n采集数量: {count} 条")

    def notify_error(self, title: str, detail: str = ""):
        """错误通知"""
        self.notify_event(title, detail, level="error")

    def notify_disconnect(self, device_name: str):
        """设备断开通知"""
        self.notify_event(f"{device_name} 连接断开", "正在尝试自动重连...", level="warn")

    def notify_reconnect(self, device_name: str, success: bool):
        """重连结果通知"""
        if success:
            self.notify_event(f"{device_name} 重连成功", level="info")
        else:
            self.notify_event(f"{device_name} 重连失败", "请检查设备和 Frida 服务", level="error")

    def notify_model_done(self, model_type: str, results: dict):
        """模型评分完成通知"""
        total = results.get("total", 0)
        s_count = results.get("S", 0)
        a_count = results.get("A", 0)
        content = f"共评分: {total} 项\nS级: {s_count} | A级: {a_count}"
        self.notify_event(f"{model_type}评分完成", content)

    def notify_supply_done(self, item_title: str, same_count: int, alt_count: int):
        """货源查找完成通知"""
        content = f"商品: {item_title[:30]}\n同款: {same_count} | 平替: {alt_count}"
        self.notify_event("货源查找完成", content)
