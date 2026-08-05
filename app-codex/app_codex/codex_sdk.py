"""Official openai-codex Python SDK adapter."""
from __future__ import annotations

from openai_codex import AsyncCodex, Sandbox

from app_codex.models import CodexSessionResult
from app_codex.redis_limiter import RedisCodexLimiter


class CodexSdkGateway:
    def __init__(
        self,
        api_key: str = "",
        limiter: RedisCodexLimiter | None = None,
    ) -> None:
        self._api_key = api_key.strip()
        self._limiter = limiter

    async def start(self, question: str, project: str) -> CodexSessionResult:
        if self._limiter is None:
            return await self._start_unlimited(question, project)
        async with self._limiter.slot():
            return await self._start_unlimited(question, project)

    async def _start_unlimited(
        self, question: str, project: str
    ) -> CodexSessionResult:
        async with AsyncCodex() as codex:
            if self._api_key:
                await codex.login_api_key(self._api_key)
            thread = await codex.thread_start(
                cwd=project,
                sandbox=Sandbox.workspace_write,
            )
            turn = await thread.run(question)
            return CodexSessionResult(
                thread_id=thread.id,
                response=turn.final_response or "",
                project=project,
            )

