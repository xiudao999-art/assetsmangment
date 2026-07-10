"""HTTP 路由 —— 8 大功能 + 用户物料库/公共库/收藏/发布 + 多模态内容审核。只依赖 service(+组合根 deps)。"""
from __future__ import annotations
import uuid
import threading
from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Header, Depends
from fastapi.concurrency import run_in_threadpool
from app.api import deps, schemas
from app.domain.models import MaterialType, AuditStatus, Material, AuditRule, User
from app.service.material import MaterialNotFound
from app.service.user import InvalidCredentials, DuplicateName
from app.service.authorization import PermissionDenied

router = APIRouter()


def _user(authorization: str | None = Header(default=None)):
    return deps.current_user(authorization)


def _require_auth(user: dict) -> None:
    """必须是已登录用户(非游客)。"""
    if user["role"] == "guest":
        raise HTTPException(401, "请先登录")


def _require_perm(user: dict, permission: str) -> None:
    """RBAC 鉴权:按角色权限判定(后台 grant 即时生效)。无权限→403+审计。"""
    _require_auth(user)
    u = User(id=user["id"], name=user.get("name", ""), pwd_hash="", role=user["role"])
    try:
        deps.get_authz_service().authorize(u, permission)
    except PermissionDenied:
        raise HTTPException(403, "无权限执行该操作")


def _can_view(user: dict, m) -> bool:
    """可查看/取签名URL:管理员 / 物主 / 已发布且审核通过(公共)。"""
    return (
        user["role"] == "admin"
        or m.owner_id == user["id"]
        or (m.is_public and m.audit_status == AuditStatus.PASS)
    )


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
    _require_auth(user)
    m = deps.get_material_service().create(body.type, body.oss_key, b"", user["id"])
    deps.get_index_service().index_material(m)
    return _mat_out(m, uid=user["id"])


@router.post("/materials/upload")
async def upload_material(file: UploadFile = File(...), type: str = Form("image"), user: dict = Depends(_user)):
    """真文件上传:存 OSS + 落库(归属当前用户,状态 待审核)。"""
    _require_auth(user)
    try:
        mtype = MaterialType(type)
    except ValueError:
        raise HTTPException(400, f"不支持的物料类型: {type}")
    data = await file.read()
    key = f"materials/{uuid.uuid4().hex}-{file.filename}"
    m = deps.get_material_service().create(mtype, key, data, user["id"])
    deps.get_index_service().index_material(m)
    return _mat_out(m, uid=user["id"])


@router.get("/materials")
def list_materials(type: str | None = None, status: str | None = None, user: dict = Depends(_user)):
    """列出全部物料(含 review/block/他人)—— 仅管理员(审核队列用)。"""
    _require_perm(user, "materials.audit")
    items = deps.material_repo.list()
    if type:
        items = [m for m in items if m.type == type]
    if status:
        items = [m for m in items if m.audit_status == status]
    return {"count": len(items), "items": [_mat_out(m, uid=user["id"]) for m in items]}


@router.get("/materials/{mid}")
def get_material(mid: str, user: dict = Depends(_user)):
    """取物料签名 URL。仅 管理员/物主/已发布过审(公共)可取,block/review/他人私有拒绝。"""
    m = deps.material_repo.get(mid)
    if m is None:
        raise HTTPException(404, "material not found")
    if not _can_view(user, m):
        raise HTTPException(403, "无权访问该物料")
    return {"id": mid, "signed_url": deps.storage.signed_url(m.oss_key)}


@router.get("/materials/{mid}/download")
def download_material(mid: str, user: dict = Depends(_user)):
    """下载物料文件 —— 仅"我的物料库"(我上传的 或 我收藏的)可下载;
    公共库里未收藏的物料不提供下载(先收藏进自己的库再下)。"""
    _require_auth(user)
    m = deps.material_repo.get(mid)
    if m is None:
        raise HTTPException(404, "material not found")
    in_my_library = m.owner_id == user["id"] or deps.favorites.has(user["id"], mid)
    if not (user["role"] == "admin" or in_my_library):
        raise HTTPException(403, "只能下载你物料库中的物料(公共物料请先收藏)")
    return {"download_url": deps.storage.download_url(m.oss_key)}


@router.post("/materials/{mid}/set-audit")
def set_audit(mid: str, body: schemas.AuditSet, user: dict = Depends(_user)):
    """人工审核复核 —— 仅管理员(普通用户上传后等审核)。"""
    _require_perm(user, "materials.audit")
    try:
        new_status = AuditStatus(body.status)
    except ValueError:
        raise HTTPException(400, f"非法审核状态: {body.status}(应为 pass/review/block)")
    m = deps.material_repo.get(mid)
    if m is None:
        raise HTTPException(404, "material not found")
    m.audit_status = new_status
    deps.material_repo.save(m)
    return _mat_out(m, uid=user["id"])


@router.delete("/materials/{mid}")
def delete_material(mid: str, user: dict = Depends(_user)):
    """删除物料 —— 仅物主或管理员。"""
    _require_auth(user)
    m = deps.material_repo.get(mid)
    if m is None:
        raise HTTPException(404, "material not found")
    if not (user["role"] == "admin" or m.owner_id == user["id"]):
        raise HTTPException(403, "只能删除自己的物料")
    deps.get_material_service().delete(mid)
    return {"deleted": mid}


# ── 视频反解(F2/F5)──
@router.post("/videos")
def upload_video(body: schemas.VideoUpload, user: dict = Depends(_user)):
    _require_auth(user)
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
    """真视频上传:存 OSS → 受理 → 反解(归属当前用户)。反解在线程池执行,不阻塞事件循环。"""
    _require_auth(user)
    data = await file.read()
    key = f"videos/{uuid.uuid4().hex}-{file.filename}"
    await run_in_threadpool(deps.storage.put, key, data)
    vsvc = deps.get_video_service()
    job = vsvc.accept_upload(key, len(data))
    deps.jobs[job.id] = {"status": "running", "materials": []}
    materials = await run_in_threadpool(vsvc.run_job, job, user["id"])
    for m in materials:
        deps.get_index_service().index_material(m)
    deps.jobs[job.id] = {"status": job.status, "materials": [m.id for m in materials]}
    return {"job_id": job.id, "status": job.status,
            "materials": [_mat_out(m, uid=user["id"]) for m in materials]}


@router.get("/videos/{jid}")
def video_status(jid: str):
    job = deps.jobs.get(jid)
    if job is None:
        raise HTTPException(404, "job not found")
    return {"job_id": jid, **job}


# ── 多模态内容审核 ──
def _report_out(r) -> dict:
    return {
        "verdict": r.verdict, "summary": r.summary, "triggered": r.triggered,
        "segments": [{"source_type": s.source_type, "text": s.text, "begin_ms": s.begin_ms,
                      "end_ms": s.end_ms, "frame_oss_key": s.frame_oss_key} for s in r.segments],
    }


def _rule_out(r: AuditRule) -> dict:
    return {"id": r.id, "source_type": r.source_type, "keywords": r.keywords,
            "condition": r.condition, "action": r.action, "enabled": r.enabled}


def _run_audit_bg(job) -> None:
    """后台线程跑审核(视频/音频耗时),完成后写回 deps.jobs 供轮询。"""
    try:
        report = deps.get_audit_service().run(job)
        deps.jobs[job.id] = {"status": job.status, "material_id": job.material_id,
                             "report": _report_out(report)}
    except Exception as e:  # 兜底,任务不悬挂
        deps.jobs[job.id] = {"status": "failed", "material_id": job.material_id, "error": str(e)}


@router.post("/audit/submit")
async def audit_submit(type: str = Form("image"), content: str = Form(""),
                       file: UploadFile = File(None), user: dict = Depends(_user)):
    """审核入口:文字/图片同步出报告;视频/音频返回 job_id 异步轮询。"""
    _require_auth(user)
    try:
        mtype = MaterialType(type)
    except ValueError:
        raise HTTPException(400, f"不支持的类型: {type}")
    svc = deps.get_audit_service()

    # 文字:直接建语料物料 + 同步审核
    if mtype == MaterialType.CORPUS:
        if not content.strip():
            raise HTTPException(400, "文字内容不能为空")
        m = Material(id=uuid.uuid4().hex, type=mtype, thumb="", source_timecode=0.0, embedding=[],
                     audit_status=AuditStatus.REVIEW, source_job="", oss_key="",
                     description=content.strip(), owner_id=user["id"])
        deps.material_repo.save(m)
        job = svc.submit(mtype, owner_id=user["id"], material_id=m.id)
        report = await run_in_threadpool(svc.run, job, content.strip())
        deps.jobs[job.id] = {"status": job.status, "material_id": m.id, "report": _report_out(report)}
        return {"job_id": job.id, "status": job.status, "material_id": m.id, "report": _report_out(report)}

    # 文件类:存 OSS + 建物料
    if file is None:
        raise HTTPException(400, "缺少文件")
    data = await file.read()
    key = f"audit/{uuid.uuid4().hex}-{file.filename}"
    m = await run_in_threadpool(deps.get_material_service().create, mtype, key, data, user["id"])
    deps.get_index_service().index_material(m)
    job = svc.submit(mtype, oss_key=key, owner_id=user["id"], material_id=m.id)

    if mtype in (MaterialType.VIDEO, MaterialType.AUDIO, MaterialType.MUSIC):
        deps.jobs[job.id] = {"status": "running", "material_id": m.id}
        threading.Thread(target=_run_audit_bg, args=(job,), daemon=True).start()  # 异步跑
        return {"job_id": job.id, "status": "running", "material_id": m.id}

    # 图片/表情/风格:同步(反解较快)
    report = await run_in_threadpool(svc.run, job)
    deps.jobs[job.id] = {"status": job.status, "material_id": m.id, "report": _report_out(report)}
    return {"job_id": job.id, "status": job.status, "material_id": m.id, "report": _report_out(report)}


# ── 审核规则后台(管理员)——放在 /audit/{job_id} 之前,避免 rules 被当作 job_id ──
@router.get("/audit/rules")
def list_audit_rules(user: dict = Depends(_user)):
    _require_perm(user, "audit.rules")
    return {"rules": [_rule_out(r) for r in deps.rule_repo.list()]}


@router.post("/audit/rules")
def add_audit_rule(body: schemas.RuleIn, user: dict = Depends(_user)):
    _require_perm(user, "audit.rules")
    action = body.action if body.action in ("block", "review") else "block"
    rule = AuditRule(id=uuid.uuid4().hex, source_type=body.source_type or "any",
                     keywords=[k for k in body.keywords if k.strip()], condition=body.condition.strip(),
                     action=action, enabled=True, created_by=user["id"])
    deps.rule_repo.add(rule)
    return _rule_out(rule)


@router.delete("/audit/rules/{rule_id}")
def delete_audit_rule(rule_id: str, user: dict = Depends(_user)):
    _require_perm(user, "audit.rules")
    deps.rule_repo.delete(rule_id)
    return {"deleted": rule_id}


@router.get("/audit/{job_id}")
def audit_status(job_id: str, user: dict = Depends(_user)):
    j = deps.jobs.get(job_id)
    if j is None:
        raise HTTPException(404, "audit job not found")
    return {"job_id": job_id, **j}


# ── 语义搜索(F3)——在公共库范围内搜索 ──
@router.get("/search")
def search(q: str = "", user: dict = Depends(_user)):
    results = deps.get_search_service().search(q)
    fav = deps.favorites.material_ids(user["id"])
    return {"count": len(results), "results": [_mat_out(m, fav, user["id"]) for m in results]}


# ── 物料库:我的 / 公共 / 全部(管理员)──
@router.get("/library/mine")
def my_library(user: dict = Depends(_user)):
    _require_auth(user)
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
    _require_perm(user, "library.all")
    items = deps.get_library_service().all()
    return {"count": len(items), "items": [_mat_out(m, uid=user["id"]) for m in items]}


@router.post("/materials/{mid}/publish")
def publish(mid: str, user: dict = Depends(_user)):
    """管理员:把物料发布到公共物料库。"""
    _require_perm(user, "materials.publish")
    m = deps.get_library_service().publish(mid, True)
    if m is None:
        raise HTTPException(404, "material not found")
    return _mat_out(m, uid=user["id"])


@router.delete("/materials/{mid}/publish")
def unpublish(mid: str, user: dict = Depends(_user)):
    """管理员:把物料撤出公共物料库。"""
    _require_perm(user, "materials.publish")
    m = deps.get_library_service().publish(mid, False)
    if m is None:
        raise HTTPException(404, "material not found")
    return _mat_out(m, uid=user["id"])


@router.post("/materials/{mid}/favorite")
def favorite(mid: str, user: dict = Depends(_user)):
    """收藏公共物料到我的物料库。仅能收藏公共库(已发布且过审)的物料。"""
    _require_auth(user)
    m = deps.material_repo.get(mid)
    if m is None:
        raise HTTPException(404, "material not found")
    if not (m.is_public and m.audit_status == AuditStatus.PASS):
        raise HTTPException(403, "只能收藏公共物料库中的物料")
    deps.get_library_service().favorite(user["id"], mid)
    return {"favorited": mid}


@router.delete("/materials/{mid}/favorite")
def unfavorite(mid: str, user: dict = Depends(_user)):
    _require_auth(user)
    deps.get_library_service().unfavorite(user["id"], mid)
    return {"unfavorited": mid}


# ── 用户(F7)──
@router.post("/users/register")
def register(body: schemas.RegisterIn):
    try:
        u = deps.get_user_service().register(body.name, body.password)
    except DuplicateName:
        raise HTTPException(409, "用户名已被占用")
    except InvalidCredentials:
        raise HTTPException(400, "用户名和密码不能为空")
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
    _require_perm(user, "admin.grant")
    deps.get_authz_service().grant(body.role, body.permission)
    return {"role": body.role, "permissions": sorted(deps.rbac.permissions_of(body.role))}


@router.get("/admin/permissions")
def role_permissions(role: str, user: dict = Depends(_user)):
    _require_perm(user, "admin.grant")
    return {"role": role, "permissions": sorted(deps.rbac.permissions_of(role))}
