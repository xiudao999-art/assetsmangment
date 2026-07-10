"""FastAPI 入口 —— 物料管理系统。分层:api > service > domain > infrastructure。"""
import os
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from app.api.router import router

app = FastAPI(title="物料管理系统", version="0.1.0")


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
