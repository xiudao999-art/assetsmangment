"""FastAPI 入口 —— 物料管理系统。分层:api > service > domain > infrastructure。"""
import os
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from app.api.router import router

app = FastAPI(title="物料管理系统", version="0.1.0")


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
