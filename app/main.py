"""FastAPI 入口 —— 物料管理系统。分层:api > service > domain > infrastructure。"""
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from app.api.router import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: recover stuck tasks + begin periodic scanning.
    Shutdown: signal the background thread to exit."""
    from app.api.deps import task_janitor   # lazy import — deps 模块初始化较重
    task_janitor.start()
    yield
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
    uvicorn.run("app.main:app", host="0.0.0.0", port=8099, reload=True)
