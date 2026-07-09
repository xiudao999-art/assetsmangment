"""HTTP 路由 —— 8 大功能的接入层。只依赖 service(+组合根 deps)。"""
from __future__ import annotations
import uuid
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from app.api import deps, schemas
from app.domain.models import MaterialType, AuditStatus
from app.service.material import MaterialNotFound
from app.service.user import InvalidCredentials

router = APIRouter()


def _mat_out(m):
    return {"id": m.id, "type": m.type, "audit_status": m.audit_status,
            "oss_key": m.oss_key, "thumb": m.thumb, "description": m.description,
            "source_timecode": m.source_timecode}


# ── 物料管理(F1)──
@router.post("/materials")
def create_material(body: schemas.MaterialCreate):
    m = deps.get_material_service().create(body.type, body.oss_key, b"", body.owner_id)
    deps.get_index_service().index_material(m)
    return _mat_out(m)


@router.post("/materials/upload")
async def upload_material(file: UploadFile = File(...), type: str = Form("image")):
    """真文件上传:存 OSS + 落库(F1)。"""
    data = await file.read()
    key = f"materials/{uuid.uuid4().hex}-{file.filename}"
    m = deps.get_material_service().create(MaterialType(type), key, data, "u1")
    deps.get_index_service().index_material(m)
    return _mat_out(m)


@router.get("/materials")
def list_materials(type: str | None = None, status: str | None = None):
    items = deps.material_repo.list()
    if type:
        items = [m for m in items if m.type == type]
    if status:
        items = [m for m in items if m.audit_status == status]
    return {"count": len(items), "items": [_mat_out(m) for m in items]}


@router.get("/materials/{mid}")
def get_material(mid: str):
    try:
        return {"id": mid, "signed_url": deps.get_material_service().get_signed_url(mid)}
    except MaterialNotFound:
        raise HTTPException(404, "material not found")


@router.post("/materials/{mid}/set-audit")
def set_audit(mid: str, body: schemas.AuditSet):
    """人工审核复核:改物料审核态(F6 审核队列)。"""
    m = deps.material_repo.get(mid)
    if m is None:
        raise HTTPException(404, "material not found")
    m.audit_status = AuditStatus(body.status)
    deps.material_repo.save(m)
    return _mat_out(m)


@router.delete("/materials/{mid}")
def delete_material(mid: str):
    deps.get_material_service().delete(mid)
    return {"deleted": mid}


# ── 视频反解(F2/F5)──
@router.post("/videos")
def upload_video(body: schemas.VideoUpload):
    vsvc = deps.get_video_service()
    job = vsvc.accept_upload(body.oss_key, body.size_bytes)
    deps.jobs[job.id] = {"status": "running", "materials": []}
    materials = vsvc.run_job(job)
    for m in materials:
        deps.get_index_service().index_material(m)
    deps.jobs[job.id] = {"status": job.status, "materials": [m.id for m in materials]}
    return {"job_id": job.id, "status": job.status, "material_count": len(materials)}


@router.post("/videos/upload")
async def upload_video_file(file: UploadFile = File(...)):
    """真视频上传:存 OSS → 受理(≤10s)→ 反解(F2 核心)。"""
    data = await file.read()
    key = f"videos/{uuid.uuid4().hex}-{file.filename}"
    deps.storage.put(key, data)
    vsvc = deps.get_video_service()
    job = vsvc.accept_upload(key, len(data))
    materials = vsvc.run_job(job)
    for m in materials:
        deps.get_index_service().index_material(m)
    return {"job_id": job.id, "status": job.status,
            "materials": [_mat_out(m) for m in materials]}


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
    return {"count": len(results), "results": [_mat_out(m) for m in results]}


# ── 用户(F7)──
@router.post("/users/register")
def register(body: schemas.RegisterIn):
    u = deps.get_user_service().register(body.name, body.password)
    return {"id": u.id, "name": u.name, "role": u.role}


@router.post("/users/login")
def login(body: schemas.LoginIn):
    try:
        return {"token": deps.get_user_service().login(body.name, body.password)}
    except InvalidCredentials:
        raise HTTPException(401, "invalid credentials")


# ── 功能权限后台(F8)──
@router.post("/admin/grant")
def grant(body: schemas.GrantIn):
    deps.get_authz_service().grant(body.role, body.permission)
    return {"role": body.role, "permissions": sorted(deps.rbac.permissions_of(body.role))}


@router.get("/admin/permissions")
def role_permissions(role: str):
    return {"role": role, "permissions": sorted(deps.rbac.permissions_of(role))}
