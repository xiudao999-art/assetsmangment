"""FastAPI 入口 —— 物料管理系统。分层:api > service > domain > infrastructure。"""
import logging
import os
from contextlib import asynccontextmanager
from logging.handlers import TimedRotatingFileHandler
from fastapi import FastAPI

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


class _DailySizeRotatingHandler(TimedRotatingFileHandler):
    """按天切分 + 单文件超 10MB 也切，保留 30 天。"""

    def __init__(self, filename: str, max_bytes: int = 10 * 1024 * 1024, **kw):
        super().__init__(filename, when="midnight", interval=1, backupCount=30, encoding="utf-8", **kw)
        self.max_bytes = max_bytes

    def shouldRollover(self, record: logging.LogRecord) -> int:
        if self.stream is not None and self.max_bytes > 0:
            try:
                if os.path.getsize(self.baseFilename) >= self.max_bytes:
                    return 1
            except OSError:
                pass
        return super().shouldRollover(record)


# 持久化日志文件（容器内 /logs → 宿主机 logs/assetsmangment）
from app.config import settings as _cfg

_log_dir = _cfg.log_dir
if _log_dir:
    os.makedirs(_log_dir, exist_ok=True)
    _fmt = logging.getLogger().handlers[0].formatter

    # 全量日志（INFO+），按天 + 10MB 切分，保留 30 天
    _fh_all = _DailySizeRotatingHandler(os.path.join(_log_dir, "app.log"))
    _fh_all.setFormatter(_fmt)
    logging.getLogger().addHandler(_fh_all)

    # 错误日志（ERROR+），按天 + 10MB 切分，保留 30 天
    _fh_err = _DailySizeRotatingHandler(os.path.join(_log_dir, "app.error.log"))
    _fh_err.setLevel(logging.ERROR)
    _fh_err.setFormatter(_fmt)
    logging.getLogger().addHandler(_fh_err)
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from app.api.router import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: recover stuck tasks + begin periodic scanning.
    Shutdown: signal the background thread to exit."""
    from app.api.deps import task_janitor, submission_trash_janitor  # lazy import — deps 模块初始化较重
    from app.config import settings
    task_janitor.start()
    if settings.submission_trash_cleanup_enabled:
        submission_trash_janitor.start()
    yield
    if settings.submission_trash_cleanup_enabled:
        submission_trash_janitor.stop()
    task_janitor.stop()


app = FastAPI(title="物料管理系统", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def _no_cache_ui(request, call_next):
    """前端不缓存 —— 避免浏览器拿到旧版页面(部署后无需硬刷新)。"""
    resp = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/ui"):
        resp.headers["Cache-Control"] = "no-store, max-age=0, must-revalidate"
    return resp


@app.get("/health")
def health() -> dict:
    """健康检查(ACK 存活探针用)。"""
    return {"status": "ok"}



app.include_router(router)

# 后台前端(静态站,同源调用 API)
_frontend = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")
if os.path.isdir(_frontend):
    @app.get("/")
    def _root():
        return RedirectResponse("/ui/")
    app.mount("/ui", StaticFiles(directory=_frontend, html=True), name="ui")

if __name__ == "__main__":
    import uvicorn
    # 只监控业务代码与前端；日志、临时文件变化不应触发无限热重载。
    uvicorn.run(
        "app.main:app", host="0.0.0.0", port=8099, reload=True,
        reload_dirs=["app", "frontend"],
    )
