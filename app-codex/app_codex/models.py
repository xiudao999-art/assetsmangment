"""Request, response, and domain models for Codex sessions."""
from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel


class CodexSessionIn(BaseModel):
    question: str
    project: str


class CodexSessionOut(BaseModel):
    thread_id: str
    response: str
    project: str


@dataclass(frozen=True)
class CodexSessionResult:
    thread_id: str
    response: str
    project: str = ""

