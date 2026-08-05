"""Validate project boundaries and execute a new Codex thread."""
from __future__ import annotations

import asyncio
from pathlib import Path

from app_codex.models import CodexSessionResult
from app_codex.ports import CodexSessionGateway


class InvalidCodexRequest(ValueError):
    pass


class CodexSessionUnavailable(RuntimeError):
    pass


class CodexSessionFailed(RuntimeError):
    pass


class CodexSessionService:
    def __init__(
        self,
        gateway: CodexSessionGateway,
        project_root: str = ".",
        timeout_s: int = 600,
    ) -> None:
        self._gateway = gateway
        self._project_root = project_root
        self._timeout_s = max(1, timeout_s)

    async def create(self, question: str, project: str) -> CodexSessionResult:
        question = (question or "").strip()
        project = (project or "").strip()
        if not question:
            raise InvalidCodexRequest("问题不能为空。")
        if not project:
            raise InvalidCodexRequest("项目不能为空。")

        root = self._resolve_root()
        requested = Path(project).expanduser()
        if not requested.is_absolute():
            requested = root / requested
        try:
            resolved = requested.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise InvalidCodexRequest("项目目录不存在或不可访问。") from exc
        if not resolved.is_dir():
            raise InvalidCodexRequest("项目必须是一个目录。")
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise InvalidCodexRequest("项目不在允许的项目根目录内。") from exc

        try:
            result = await asyncio.wait_for(
                self._gateway.start(question, str(resolved)),
                timeout=self._timeout_s,
            )
        except TimeoutError as exc:
            raise CodexSessionFailed("Codex 执行超时。") from exc
        except CodexSessionUnavailable:
            raise
        except Exception as exc:
            raise CodexSessionFailed("Codex 会话执行失败。") from exc
        return CodexSessionResult(
            thread_id=result.thread_id,
            response=result.response,
            project=str(resolved),
        )

    def _resolve_root(self) -> Path:
        try:
            root = Path(self._project_root).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise CodexSessionUnavailable("Codex 项目根目录配置无效。") from exc
        if not root.is_dir():
            raise CodexSessionUnavailable("Codex 项目根目录配置无效。")
        return root

