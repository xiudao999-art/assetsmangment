"""Ports used by the Codex application service."""
from __future__ import annotations

from typing import Protocol

from app_codex.models import CodexSessionResult


class CodexSessionGateway(Protocol):
    async def start(self, question: str, project: str) -> CodexSessionResult: ...

