"""AI Client — supports DeepSeek (cloud) / 智谱 / Ollama (local) via OpenAI-compatible API"""
from __future__ import annotations
import json
import os
from typing import Optional


def _check_ollama() -> Optional[str]:
    """检测本地 Ollama 是否运行，返回可用模型名或 None"""
    try:
        import urllib.request
        resp = urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2)
        data = json.loads(resp.read())
        models = [m.get("name", "") for m in data.get("models", [])]
        # 优先选小模型（快），没有就用第一个
        for pref in ["qwen2.5:3b", "qwen2.5:1.5b", "qwen3:4b", "llama3.2:3b", "gemma3:4b"]:
            for m in models:
                if m.startswith(pref.replace(":", ":") if ":" in pref else pref):
                    return pref if ":" in pref else m
        return models[0] if models else None
    except Exception:
        return None


class AIClient:
    """Multi-provider AI client — 本地Ollama + 云端DeepSeek双通道"""

    PROVIDERS = {
        "deepseek": {
            "base_url": "https://api.deepseek.com",
            "default_model": "deepseek-chat",
            "models": ["deepseek-chat", "deepseek-reasoner"],
        },
        "zhipu": {
            "base_url": "https://open.bigmodel.cn/api/paas/v4",
            "default_model": "glm-4-flash",
            "models": ["glm-4-flash", "glm-4", "glm-4-plus"],
        },
        "ollama": {
            "base_url": "http://localhost:11434/v1",
            "default_model": "qwen2.5:3b",
            "models": [],
        },
    }

    def __init__(self, api_key: str, provider: str = "deepseek",
                 model: str = None, proxy: str = None):
        self._api_key = api_key
        self._provider = provider
        cfg = self.PROVIDERS[provider]
        self._base_url = cfg["base_url"]
        self._model = model or cfg["default_model"]
        self._proxy = proxy

    @classmethod
    def auto_best(cls, settings: dict, task: str = "default") -> "AIClient":
        """
        自动选择最佳客户端：本地 Ollama > DeepSeek > 智谱
        task: "simple"(分类/提取) | "complex"(打分/分析) | "default"
        """
        api_cfg = settings.get("api", {})

        # 简单任务优先本地模型
        if task == "simple":
            local_model = _check_ollama()
            if local_model:
                return cls("ollama", provider="ollama", model=local_model)

        # 复杂任务用 DeepSeek
        dk = api_cfg.get("deepseek_api_key", "")
        if dk:
            return cls(dk, provider="deepseek")

        # 兜底
        zp = api_cfg.get("zhipu_api_key", "")
        if zp:
            return cls(zp, provider="zhipu")

        # 最后尝试本地
        local_model = _check_ollama()
        if local_model:
            return cls("ollama", provider="ollama", model=local_model)

        return None

    @staticmethod
    def _sanitize(text: str) -> str:
        """清洗字符串确保 UTF-8 兼容（修复 openai 0.28.x latin-1 编码bug）"""
        return text.encode('utf-8', errors='replace').decode('utf-8')

    def _call_api(self, system: str, user: str,
                  temperature: float = 0.3, max_tokens: int = 4096) -> str:
        """调用 API（支持 openai >= 1.0 和 0.x，兼容 Ollama）"""
        system = self._sanitize(system)
        user = self._sanitize(user)
        try:
            # openai >= 1.0
            from openai import OpenAI
            import httpx
            kwargs = dict(api_key=self._api_key or "ollama", base_url=self._base_url)
            if self._proxy:
                kwargs["http_client"] = httpx.Client(proxy=self._proxy)
            client = OpenAI(**kwargs)
            response = client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return response.choices[0].message.content
        except ImportError:
            pass

        # openai 0.28.x 有 latin-1 编码bug → 改直连 HTTP
        import requests as _r
        headers = {
            "Authorization": f"Bearer {self._api_key or 'ollama'}",
            "Content-Type": "application/json; charset=utf-8",
        }
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        s = _r.Session()
        s.trust_env = False
        resp = s.post(
            f"{self._base_url}/chat/completions",
            headers=headers,
            json=body,
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    def chat(self, system: str, user: str, temperature: float = 0.3,
             max_tokens: int = 16384) -> str:
        """Send a chat completion request and return the text response."""
        saved_proxy = self._proxy
        self._proxy = None
        try:
            return self._call_api(system, user, temperature, max_tokens)
        except Exception:
            if not saved_proxy:
                raise
        self._proxy = saved_proxy
        return self._call_api(system, user, temperature, max_tokens)

    def chat_json(self, system: str, user: str, temperature: float = 0.2,
                  max_tokens: int = 4096) -> dict:
        """Send a chat request and parse the response as JSON."""
        text = self.chat(system, user, temperature, max_tokens)
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]
        text = text.strip()
        errors = []
        for attempt in range(3):
            try:
                return json.loads(text)
            except json.JSONDecodeError as e:
                errors.append(str(e))
                if attempt == 0:
                    last_complete = max(text.rfind("}]"), text.rfind('"]'), text.rfind("}"))
                    if last_complete > 0:
                        text = text[:last_complete + 2] if text[last_complete:last_complete+2] == "}]" else text[:last_complete + 1]
                elif attempt == 1:
                    import ast
                    try:
                        return ast.literal_eval(text)
                    except Exception:
                        pass
        raise ValueError(f"JSON 解析失败: {'; '.join(errors)}")

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def model(self) -> str:
        return self._model

    @property
    def is_local(self) -> bool:
        return self._provider == "ollama"
