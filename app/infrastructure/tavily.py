"""Tavily 联网搜索(实现 WebSearcher 端口)。
音乐物料按歌名联网搜「表达的情绪 / 适合的场景 / 曲风」,把 answer + 结果摘要拼成简报文本喂大模型合成档案。
失败(网络/超时/异常/空查询)→ 返回 "",绝不阻塞审核/入库。走已装的 httpx,不引新 SDK。"""
from __future__ import annotations
import httpx


class TavilySearch:
    def __init__(self, api_key: str, base_url: str = "https://api.tavily.com") -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")

    def search(self, query: str) -> str:
        q = (query or "").strip()
        if not q:
            return ""
        from app.config import settings
        from app.infrastructure.retry import call_ai

        def _call():
            r = httpx.post(
                f"{self._base_url}/search",
                json={"api_key": self._api_key, "query": q, "search_depth": "basic",
                      "max_results": 5, "include_answer": True},
                timeout=settings.ai_timeout_s)
            r.raise_for_status()
            return r.json()

        try:
            data = call_ai(_call, timeout_s=settings.ai_timeout_s, retries=settings.ai_retries)
        except Exception:
            return ""   # 联网失败不阻塞:音乐物料回退 qwen 文本档案
        return self._brief(data)

    @staticmethod
    def _brief(data) -> str:
        """把 Tavily 返回拼成一段简报文本:概述(answer)+ 各结果的标题:内容。"""
        if not isinstance(data, dict):
            return ""
        lines: list[str] = []
        answer = str(data.get("answer") or "").strip()
        if answer:
            lines.append(f"概述:{answer}")
        for r in (data.get("results") or [])[:5]:
            if not isinstance(r, dict):
                continue
            title = str(r.get("title") or "").strip()
            content = str(r.get("content") or "").strip()
            if content:
                lines.append((f"- {title}:{content}" if title else f"- {content}")[:400])
        return "\n".join(lines)[:4000]
