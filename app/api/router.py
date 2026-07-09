"""HTTP 路由 —— 8 大功能的接入层。只依赖 service(+组合根 deps)。"""
from __future__ import annotations
from fastapi import APIRouter, HTTPException
from app.api import deps, schemas
from app.service.material import MaterialNotFound
from app.service.user import InvalidCredentials

router = APIRouter()


# ── 物料管理(F1)──
@router.post("/materials")
def create_material(body: schemas.MaterialCreate):
    m = deps.get_material_service().create(body.type, body.oss_key, b"", body.owner_id)
    deps.get_index_service().index_material(m)  # F4 增量索引
    return {"id": m.id, "type": m.type, "audit_status": m.audit_status}


@router.get("/materials")
def list_materials():
    items = deps.material_repo.list()
    return {"count": len(items), "items": [
        {"id": m.id, "type": m.type, "audit_status": m.audit_status,
         "oss_key": m.oss_key, "description": m.description} for m in items
    ]}


@router.get("/materials/{mid}")
def get_material(mid: str):
    try:
        return {"id": mid, "signed_url": deps.get_material_service().get_signed_url(mid)}
    except MaterialNotFound:
        raise HTTPException(404, "material not found")


@router.delete("/materials/{mid}")
def delete_material(mid: str):
    deps.get_material_service().delete(mid)
    return {"deleted": mid}


# ── 视频反解(F2/F5)──
@router.post("/videos")
def upload_video(body: schemas.VideoUpload):
    vsvc = deps.get_video_service()
    job = vsvc.accept_upload(body.oss_key, body.size_bytes)   # ≤10s 受理
    deps.jobs[job.id] = {"status": "running", "materials": []}
    materials = vsvc.run_job(job)  # 真实为 Celery 异步;此处同步演示
    for m in materials:
        deps.get_index_service().index_material(m)
    deps.jobs[job.id] = {"status": job.status, "materials": [m.id for m in materials]}
    return {"job_id": job.id, "status": job.status, "material_count": len(materials)}


@router.get("/videos/{jid}")
def video_status(jid: str):
    job = deps.jobs.get(jid)
    if job is None:
        raise HTTPException(404, "job not found")
    return {"job_id": jid, **job}


# ── 语义搜索(F3)──
@router.get("/search")
def search(q: str = ""):
    results = deps.get_search_service().search(q)
    return {"count": len(results), "results": [{"id": m.id, "type": m.type} for m in results]}


# ── 用户(F7)──
@router.post("/users/register")
def register(body: schemas.RegisterIn):
    u = deps.get_user_service().register(body.name, body.password)
    return {"id": u.id, "name": u.name}


@router.post("/users/login")
def login(body: schemas.LoginIn):
    try:
        return {"token": deps.get_user_service().login(body.name, body.password)}
    except InvalidCredentials:
        raise HTTPException(401, "invalid credentials")


# ── 权限后台(F8)──
@router.post("/admin/grant")
def grant(body: schemas.GrantIn):
    deps.get_authz_service().grant(body.role, body.permission)
    return {"granted": [body.role, body.permission]}
