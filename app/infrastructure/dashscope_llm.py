"""大模型判定适配器 —— 百炼 qwen-plus(实现 domain.ports.Llm)。JSON 强制输出。infra→domain。"""
from __future__ import annotations
import json
import re


class DashScopeLlm:
    def __init__(self, api_key: str, model: str = "qwen-plus") -> None:
        import dashscope  # 延迟导入,校验依赖
        self._dashscope = dashscope
        self._api_key = api_key
        self._model = model

    def chat_json(self, system: str, user: str) -> dict:
        from dashscope import Generation
        from http import HTTPStatus
        resp = Generation.call(
            api_key=self._api_key,
            model=self._model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            result_format="message",
            response_format={"type": "json_object"},  # 提示词里已含「json」字样
        )
        if getattr(resp, "status_code", None) != HTTPStatus.OK:
            raise RuntimeError(f"qwen 判定失败: {getattr(resp, 'status_code', '?')} "
                               f"{getattr(resp, 'code', '')} {getattr(resp, 'message', '')}")
        text = resp.output.choices[0].message.content
        if isinstance(text, list):  # 兼容多模态返回结构
            text = text[0].get("text", "") if text else ""
        return self._parse(text)

    @staticmethod
    def _parse(text: str) -> dict:
        text = (text or "").strip()
        try:
            return json.loads(text)
        except Exception:
            m = re.search(r"\{.*\}", text, re.S)  # 剥离偶发 ```json 包裹
            try:
                return json.loads(m.group(0)) if m else {}
            except Exception:
                return {}
