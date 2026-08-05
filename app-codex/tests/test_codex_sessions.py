from __future__ import annotations

from contextlib import asynccontextmanager
import hashlib
import hmac
from pathlib import Path
import time

import pytest
from fastapi.testclient import TestClient

from app_codex.codex_sdk import CodexSdkGateway
from app_codex.main import app
from app_codex.models import CodexSessionResult
from app_codex.service import CodexSessionService, InvalidCodexRequest


client = TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def close_test_client():
    yield
    client.close()


class _FakeCodexGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def start(self, question: str, project: str) -> CodexSessionResult:
        self.calls.append((question, project))
        return CodexSessionResult(thread_id="thread-123", response="处理完成")


def _token(secret: str = "dev-insecure-token-secret-change-me") -> str:
    message = f"user01.{int(time.time()) + 60}"
    signature = hmac.new(
        secret.encode(), message.encode(), hashlib.sha256
    ).hexdigest()
    return f"{message}.{signature}"


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token()}"}


@pytest.mark.asyncio
async def test_sdk_gateway_starts_thread_with_project_as_cwd(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class _Turn:
        final_response = "SDK 回答"

    class _Thread:
        id = "sdk-thread"

        async def run(self, question: str):
            calls["question"] = question
            return _Turn()

    class _Codex:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def thread_start(self, **kwargs):
            calls["thread_start"] = kwargs
            return _Thread()

    monkeypatch.setattr("app_codex.codex_sdk.AsyncCodex", _Codex)

    result = await CodexSdkGateway().start("检查代码", "D:/projects/demo")

    assert result.thread_id == "sdk-thread"
    assert result.response == "SDK 回答"
    assert calls["question"] == "检查代码"
    assert calls["thread_start"]["cwd"] == "D:/projects/demo"


@pytest.mark.asyncio
async def test_sdk_gateway_holds_redis_slot_while_codex_runs(monkeypatch) -> None:
    events: list[str] = []

    class _Limiter:
        active = False

        @asynccontextmanager
        async def slot(self):
            self.active = True
            events.append("slot_acquired")
            try:
                yield
            finally:
                self.active = False
                events.append("slot_released")

    limiter = _Limiter()

    class _Turn:
        final_response = "done"

    class _Thread:
        id = "limited-thread"

        async def run(self, _question: str):
            assert limiter.active is True
            events.append("sdk_run")
            return _Turn()

    class _Codex:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def thread_start(self, **_kwargs):
            assert limiter.active is True
            return _Thread()

    monkeypatch.setattr("app_codex.codex_sdk.AsyncCodex", _Codex)

    await CodexSdkGateway(limiter=limiter).start("question", "D:/projects/demo")

    assert events == ["slot_acquired", "sdk_run", "slot_released"]


@pytest.mark.asyncio
async def test_service_starts_new_thread_in_resolved_project(tmp_path: Path) -> None:
    project = tmp_path / "demo"
    project.mkdir()
    gateway = _FakeCodexGateway()
    service = CodexSessionService(gateway, project_root=str(tmp_path))

    result = await service.create("  修复失败测试  ", "demo")

    assert result == CodexSessionResult(
        thread_id="thread-123",
        response="处理完成",
        project=str(project.resolve()),
    )
    assert gateway.calls == [("修复失败测试", str(project.resolve()))]


@pytest.mark.asyncio
@pytest.mark.parametrize("question, project", [("", "demo"), ("问题", "")])
async def test_service_rejects_blank_input(
    tmp_path: Path, question: str, project: str
) -> None:
    service = CodexSessionService(_FakeCodexGateway(), project_root=str(tmp_path))

    with pytest.raises(InvalidCodexRequest):
        await service.create(question, project)


@pytest.mark.asyncio
async def test_service_rejects_project_outside_configured_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    service = CodexSessionService(_FakeCodexGateway(), project_root=str(root))

    with pytest.raises(InvalidCodexRequest, match="允许的项目根目录"):
        await service.create("问题", str(outside))


def test_health() -> None:
    assert client.get("/health").json() == {"status": "ok"}


def test_api_requires_valid_bearer_token() -> None:
    body = {"question": "问题", "project": "."}

    assert client.post("/codex/sessions", json=body).status_code == 401
    assert client.post(
        "/codex/sessions",
        json=body,
        headers={"Authorization": "Bearer forged"},
    ).status_code == 401


def test_api_creates_session_and_returns_thread_id(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "demo"
    project.mkdir()
    gateway = _FakeCodexGateway()
    service = CodexSessionService(gateway, project_root=str(tmp_path))
    monkeypatch.setattr(app.state, "session_service", service)

    response = client.post(
        "/codex/sessions",
        json={"question": "解释项目结构", "project": "demo"},
        headers=_headers(),
    )

    assert response.status_code == 200
    assert response.json() == {
        "thread_id": "thread-123",
        "response": "处理完成",
        "project": str(project.resolve()),
    }
