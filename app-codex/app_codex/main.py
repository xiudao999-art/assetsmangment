"""FastAPI entry point for the Codex execution microservice."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request

from app_codex.auth import TokenVerifier
from app_codex.codex_sdk import CodexSdkGateway
from app_codex.config import settings
from app_codex.models import CodexSessionIn, CodexSessionOut
from app_codex.redis_limiter import RedisCodexLimiter
from app_codex.service import (
    CodexSessionFailed,
    CodexSessionService,
    CodexSessionUnavailable,
    InvalidCodexRequest,
)


limiter = RedisCodexLimiter(
    redis_url=settings.redis_url,
    limit=settings.max_concurrency,
)
gateway = CodexSdkGateway(settings.api_key, limiter=limiter)
session_service = CodexSessionService(
    gateway,
    project_root=settings.project_root,
    timeout_s=settings.timeout_s,
)
token_verifier = TokenVerifier(settings.token_secret)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        yield
    finally:
        await limiter.close()


app = FastAPI(title="Codex Execution Service", version="0.1.0", lifespan=lifespan)
app.state.session_service = session_service


def require_user(authorization: str | None = Header(default=None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "请先登录")
    user_id = token_verifier.verify(authorization[7:])
    if user_id is None:
        raise HTTPException(401, "登录凭证无效或已过期")
    return user_id


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/codex/sessions", response_model=CodexSessionOut)
async def create_codex_session(
    body: CodexSessionIn,
    request: Request,
    _user_id: str = Depends(require_user),
) -> CodexSessionOut:
    service: CodexSessionService = request.app.state.session_service
    try:
        result = await service.create(body.question, body.project)
    except InvalidCodexRequest as exc:
        raise HTTPException(400, str(exc)) from exc
    except CodexSessionUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except CodexSessionFailed as exc:
        raise HTTPException(502, str(exc)) from exc
    return CodexSessionOut(
        thread_id=result.thread_id,
        response=result.response,
        project=result.project,
    )

