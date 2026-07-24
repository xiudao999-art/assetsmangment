"""文本模型适配器 —— OpenAI 兼容 API(实现 domain.ports.Llm)。不绑死厂商,配 key/model/base_url 即用。"""
from __future__ import annotations
import json
import re
import urllib.request
import urllib.error


class OpenAILlm:
    def __init__(self, api_key: str, model: str = "qwen-plus",
                 base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1") -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")

    def chat_json(self, system: str, user: str) -> dict:
        from app.config import settings
        from app.infrastructure.retry import call_ai

        def _call():
            body = json.dumps({
                "model": self._model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "response_format": {"type": "json_object"},
            }).encode("utf-8")
            req = urllib.request.Request(
                f"{self._base_url}/chat/completions",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._api_key}",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=settings.ai_timeout_s) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                raise RuntimeError(
                    f"LLM 判定失败: HTTP {e.code} {e.read().decode('utf-8', errors='replace')[:500]}"
                ) from e
            choice = (data.get("choices") or [{}])[0]
            content = choice.get("message", {}).get("content", "")
            return content

        return self._parse(call_ai(_call, timeout_s=settings.ai_timeout_s, retries=settings.ai_retries))

    @staticmethod
    def _parse(text: str) -> dict:
        text = (text or "").strip()
        try:
            return json.loads(text)
        except Exception:
            m = re.search(r"\{.*\}", text, re.S)
            try:
                return json.loads(m.group(0)) if m else {}
            except Exception:
                return {}
