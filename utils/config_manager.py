"""配置管理器：settings.json + models_config.json 加载/保存/校验
支持 EXE 打包后的持久化存储（保存到 EXE 同目录，而非临时解压目录）"""
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional
from datetime import datetime


DEFAULT_SETTINGS = {
    "devices": [],
    "api": {
        "zhipu_api_key": "",
        "webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=915cc39b-eb16-41c8-a0b9-34130442b930",
    },
    "collection": {
        "search_pages": 10,
        "hs_pages": 3,
        "detail_max": 5,
        "comment_max": 3,
        "rate_search": [4, 8],
        "rate_detail": [3, 6],
        "rate_comment": [3, 6],
        "rate_market": [3, 5],
        "rate_keyword": [8, 15],
        "kw_push_threshold": 75,
        "pd_push_threshold": 75,
    },
    "scoring": {
        "keyword_grade_thresholds": {"S": 90, "A": 75, "B": 55, "C": 35, "D": 0},
        "product_grade_thresholds": {"S": 90, "A": 75, "B": 55, "C": 40, "D": 0},
        "precheck_min_uv": 200,
        "precheck_max_price_drop": -20,
    },
    "supply_finder": {
        "score_threshold": 75,
        "sim_threshold": 0.8,
        "scroll_pages": 5,
        "max_items": 20,
        "use_img_search": True,
        "delay_between_products": 8,
        "pause_every": 5,
        "pause_duration": 60,
    },
}


def _get_persistent_dir() -> Path:
    """获取持久化配置目录：EXE 模式下为 EXE 所在目录，开发模式下为项目根目录"""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    else:
        return Path(__file__).parent.parent


def _get_bundled_dir() -> Path:
    """获取打包资源目录（EXE 解压后的临时目录）"""
    if getattr(sys, 'frozen', False):
        return Path(sys._MEIPASS)
    else:
        return Path(__file__).parent.parent


class ConfigManager:
    def __init__(self, base_dir: Path = None):
        if base_dir:
            self._persistent_dir = base_dir
            self._bundled_dir = base_dir
        else:
            self._persistent_dir = _get_persistent_dir()
            self._bundled_dir = _get_bundled_dir()

        self._settings_path = self._persistent_dir / "settings.json"
        self._bundled_settings_path = self._bundled_dir / "settings.json"
        self._model_config_path = self._bundled_dir / "models_config.json"
        self._settings: dict = {}
        self._model_config: dict = {}

    def load_all(self):
        # 优先读持久化路径的 settings.json，不存在则读打包资源中的
        if self._settings_path.exists():
            self._settings = self._load_json(self._settings_path, DEFAULT_SETTINGS)
        elif self._bundled_settings_path.exists() and self._bundled_settings_path != self._settings_path:
            self._settings = self._load_json(self._bundled_settings_path, DEFAULT_SETTINGS)
            # 首次启动：把模板配置复制到持久化路径
            self.save_settings()
        else:
            self._settings = self._load_json(self._settings_path, DEFAULT_SETTINGS)

        self._model_config = self._load_json(self._model_config_path, {})

    @property
    def settings(self) -> dict:
        return self._settings

    @property
    def model_config(self) -> dict:
        return self._model_config

    def save_settings(self) -> bool:
        try:
            self._settings_path.parent.mkdir(parents=True, exist_ok=True)
            self._settings_path.write_text(
                json.dumps(self._settings, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return True
        except Exception:
            return False

    def update_settings(self, new_values: dict):
        _deep_merge(self._settings, new_values)

    def get_api_key(self) -> str:
        return self._settings.get("api", {}).get("zhipu_api_key", "")

    def get_webhook_url(self) -> str:
        return self._settings.get("api", {}).get("webhook_url", "")

    def get_collection_params(self) -> dict:
        return self._settings.get("collection", DEFAULT_SETTINGS["collection"])

    def get_device_list(self) -> list:
        return self._settings.get("devices", [])

    @staticmethod
    def _load_json(path: Path, default: dict) -> dict:
        if not path.exists():
            return default
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            result = default.copy()
            _deep_merge(result, data)
            return result
        except (json.JSONDecodeError, IOError):
            return default


def _deep_merge(base: dict, update: dict):
    for k, v in update.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
