"""HTTP 路由 —— 8 大功能 + 用户物料库/公共库/收藏/发布。只依赖 service(+组合根 deps)。"""
from __future__ import annotations
import uuid
from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Header, Depends
from app.api import deps, schemas
from app.domain.models import MaterialType, AuditStatus
from app.service.material import MaterialNotFound
from app.service.user import InvalidCredentials

router = APIRouter()


def _user(authorization: str | None = Header(default=None)):
    return deps.current_user(authorization)


def _require_admin(user: dict):
    if user["role"] != "admin":
        raise HTTPException(403, "需要管理员权限")


def _mat_out(m, fav_ids: set | None = None, uid: str | None = None):
    return {
        "id": m.id, "type": m.type, "audit_status": m.audit_status,
        "oss_key": m.oss_key, "thumb": m.thumb, "description": m.description,
        "source_timecode": m.source_timecode, "owner_id": m.owner_id,
        "is_public": m.is_public,
        "is_favorited": bool(fav_ids and m.id in fav_ids),
        "is_mine": bool(uid and m.owner_id == uid),
    }


# ── 物料管理(F1)──
@router.post("/materials")
def create_material(body: schemas.MaterialCreate, user: dict = Depends(_user)):
    m = deps.get_material_service().create(body.type, body.oss_key, b"", user["id"])
    deps.get_index_service().index_material(m)
    return _mat_out(m)


@router.post("/materials/upload")
async def upload_material(file: UploadFile = File(...), type: str = Form("image"), user: dict = Depends(_user)):
    """真文件上传:存 OSS + 落库(归属当前用户,状态 待审核)。"""
    data = await file.read()
    key = f"materials/{uuid.uuid4().hex}-{file.filename}"
    m = deps.get_material_service().create(MaterialType(type), key, data, user["id"])
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
def set_audit(mid: str, body: schemas.AuditSet, user: dict = Depends(_user)):
    """人工审核复核 —— 仅管理员(普通用户上传后等审核)。"""
    _require_admin(user)
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
def upload_video(body: schemas.VideoUpload, user: dict = Depends(_user)):
    vsvc = deps.get_video_service()
    job = vsvc.accept_upload(body.oss_key, body.size_bytes)
    deps.jobs[job.id] = {"status": "running", "materials": []}
    materials = vsvc.run_job(job, owner_id=user["id"])
    for m in materials:
        deps.get_index_service().index_material(m)
    deps.jobs[job.id] = {"status": job.status, "materials": [m.id for m in materials]}
    return {"job_id": job.id, "status": job.status, "material_count": len(materials)}


@router.post("/videos/upload")
async def upload_video_file(file: UploadFile = File(...), user: dict = Depends(_user)):
    """真视频上传:存 OSS → 受理(≤10s)→ 反解(归属当前用户)。"""
    data = await file.read()
    key = f"videos/{uuid.uuid4().hex}-{file.filename}"
    deps.storage.put(key, data)
    vsvc = deps.get_video_service()
    job = vsvc.accept_upload(key, len(data))
    materials = vsvc.run_job(job, owner_id=user["id"])
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


# ── 语义搜索(F3)——在公共库范围内搜索 ──
@router.get("/search")
def search(q: str = ""):
    results = deps.get_search_service().search(q)
    return {"count": len(results), "results": [_mat_out(m) for m in results]}


# ── 物料库:我的 / 公共 / 全部(管理员)──
@router.get("/library/mine")
def my_library(user: dict = Depends(_user)):
    lib = deps.get_library_service()
    fav = deps.favorites.material_ids(user["id"])
    items = lib.mine(user["id"])
    return {"count": len(items), "items": [_mat_out(m, fav, user["id"]) for m in items]}


@router.get("/library/public")
def public_library(user: dict = Depends(_user)):
    lib = deps.get_library_service()
    fav = deps.favorites.material_ids(user["id"])
    items = lib.public()
    return {"count": len(items), "items": [_mat_out(m, fav, user["id"]) for m in items]}


@router.get("/library/all")
def all_library(user: dict = Depends(_user)):
    """管理员:看所有用户的物料。"""
    _require_admin(user)
    items = deps.get_library_service().all()
    return {"count": len(items), "items": [_mat_out(m, uid=user["id"]) for m in items]}


@router.post("/materials/{mid}/publish")
def publish(mid: str, user: dict = Depends(_user)):
    """管理员:把物料发布到公共物料库。"""
    _require_admin(user)
    m = deps.get_library_service().publish(mid, True)
    if m is None:
        raise HTTPException(404, "material not found")
    return _mat_out(m)


@router.post("/materials/{mid}/favorite")
def favorite(mid: str, user: dict = Depends(_user)):
    """收藏公共物料到我的物料库。"""
    deps.get_library_service().favorite(user["id"], mid)
    return {"favorited": mid}


@router.delete("/materials/{mid}/favorite")
def unfavorite(mid: str, user: dict = Depends(_user)):
    deps.get_library_service().unfavorite(user["id"], mid)
    return {"unfavorited": mid}


# ── 用户(F7)──
@router.post("/users/register")
def register(body: schemas.RegisterIn):
    u = deps.get_user_service().register(body.name, body.password)
    return {"id": u.id, "name": u.name, "role": u.role}


@router.post("/users/login")
def login(body: schemas.LoginIn):
    try:
        token = deps.get_user_service().login(body.name, body.password)
    except InvalidCredentials:
        raise HTTPException(401, "invalid credentials")
    u = deps.user_repo.get_by_name(body.name)
    return {"token": token, "user": {"id": u.id, "name": u.name, "role": u.role}}


# ── 功能权限后台(F8)──
@router.post("/admin/grant")
def grant(body: schemas.GrantIn, user: dict = Depends(_user)):
    _require_admin(user)
    deps.get_authz_service().grant(body.role, body.permission)
    return {"role": body.role, "permissions": sorted(deps.rbac.permissions_of(body.role))}


@router.get("/admin/permissions")
def role_permissions(role: str):
    return {"role": role, "permissions": sorted(deps.rbac.permissions_of(role))}
