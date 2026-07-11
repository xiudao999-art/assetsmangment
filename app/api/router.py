"""HTTP 路由 —— 8 大功能 + 用户物料库/公共库/收藏/发布 + 多模态内容审核。只依赖 service(+组合根 deps)。"""
from __future__ import annotations
import uuid
import time
import hashlib
import threading
from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Header, Depends, Query
from fastapi.concurrency import run_in_threadpool
from app.api import deps, schemas
from app.domain.models import MaterialType, AuditStatus, Material, AuditRule, User, AuditTask, JobStatus
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


def _preview_url(m) -> str:
    """卡片预览:图片→签名图;视频→OSS 截帧封面;声音/文字→无(前端显字形)。"""
    if not m.oss_key:
        return ""
    try:
        if m.type in (MaterialType.IMAGE, MaterialType.MEME, MaterialType.STYLE):
            return deps.storage.signed_url(m.oss_key)
        if m.type == MaterialType.VIDEO:
            return deps.storage.snapshot_url(m.oss_key)
    except Exception:
        return ""
    return ""


def _media_url(m) -> str:
    """审核卡片内联播放:图片/视频/声音都给真实文件签名 URL(前端直接 <img>/<video>/<audio>)。"""
    if not m.oss_key:
        return ""
    try:
        return deps.storage.signed_url(m.oss_key)
    except Exception:
        return ""


def _page_args(page: int, size: int) -> tuple[int, int]:
    """1 基 page/size → repo 的 offset/limit。"""
    return (page - 1) * size, size


def _page_out(items: list, total: int, page: int, size: int, key: str = "items") -> dict:
    """统一分页响应。count = 当页长度(向后兼容);翻页控件只认 total。"""
    return {"total": total, "page": page, "size": size, "count": len(items), key: items}


def _check_type(type: str | None) -> str | None:
    """校验物料类型(非法值 400,别静默返回空页)。"""
    if type:
        try:
            MaterialType(type)
        except ValueError:
            raise HTTPException(400, f"不支持的物料类型: {type}")
    return type or None


def _check_status(status: str | None) -> str | None:
    if status:
        try:
            AuditStatus(status)
        except ValueError:
            raise HTTPException(400, f"非法审核状态: {status}(应为 pass/review/block)")
    return status or None


def _owner_name(owner_id: str) -> str:
    """把 owner_id 解析成用户名(管理视图展示用);用户已删除 → 空(前端显示"已删除用户")。"""
    if not owner_id:
        return ""
    u = deps.user_repo.get(owner_id)
    return u.name if u else ""


def _mat_out(m, fav_ids: set | None = None, uid: str | None = None):
    return {
        "id": m.id, "type": m.type, "audit_status": m.audit_status,
        "oss_key": m.oss_key, "thumb": m.thumb, "description": m.description,
        "source_timecode": m.source_timecode, "owner_id": m.owner_id,
        "owner_name": _owner_name(m.owner_id),
        "is_public": m.is_public, "preview_url": _preview_url(m),
        "is_favorited": bool(fav_ids and m.id in fav_ids),
        "is_mine": bool(uid and m.owner_id == uid),
        "tags": list(getattr(m, "tags", []) or []),
        "ai_summary": getattr(m, "ai_summary", ""),
        "ai_scene": getattr(m, "ai_scene", ""),
        "ai_emotion": getattr(m, "ai_emotion", ""),
        "ai_atmosphere": getattr(m, "ai_atmosphere", ""),
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
def list_materials(page: int = Query(1, ge=1), size: int = Query(24, ge=1, le=100),
                   type: str | None = None, status: str | None = None, q: str | None = None,
                   user: dict = Depends(_user)):
    """列出全部物料(含 review/block/他人)—— 仅管理员(审核队列用)。服务端分页/筛选。"""
    _require_perm(user, "materials.audit")
    off, lim = _page_args(page, size)
    items, total = deps.get_library_service().all(
        status=_check_status(status), type=_check_type(type), keyword=q or None,
        offset=off, limit=lim)
    return _page_out([_mat_out(m, uid=user["id"]) for m in items], total, page, size)


@router.get("/materials/{mid}")
def get_material(mid: str, user: dict = Depends(_user)):
    """取物料签名 URL。仅 管理员/物主/已发布过审(公共)可取,block/review/他人私有拒绝。"""
    m = deps.material_repo.get(mid)
    if m is None:
        raise HTTPException(404, "material not found")
    if not _can_view(user, m):
        raise HTTPException(403, "无权访问该物料")
    return {"id": mid, "signed_url": _media_url(m), "type": m.type, "preview_url": _preview_url(m)}


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


@router.post("/materials/{mid}/summarize")
def summarize_material(mid: str, user: dict = Depends(_user)):
    """按需生成 AI 摘要(重新解析物料 → 情绪/氛围/场景/标签)。仅物主或管理员。"""
    _require_auth(user)
    m = deps.material_repo.get(mid)
    if m is None:
        raise HTTPException(404, "material not found")
    if not (user["role"] == "admin" or m.owner_id == user["id"]):
        raise HTTPException(403, "只能给自己的物料生成摘要")
    deps.get_audit_service().summarize_material(m)
    return _mat_out(m, uid=user["id"])


@router.put("/materials/{mid}/tags")
def set_material_tags(mid: str, body: schemas.TagsIn, user: dict = Depends(_user)):
    """设置物料标签(项目分类)。仅物主或管理员。"""
    _require_auth(user)
    m = deps.material_repo.get(mid)
    if m is None:
        raise HTTPException(404, "material not found")
    if not (user["role"] == "admin" or m.owner_id == user["id"]):
        raise HTTPException(403, "只能修改自己物料的标签")
    m.tags = list(dict.fromkeys([t.strip() for t in body.tags if t.strip()]))[:12]
    deps.material_repo.save(m)
    return _mat_out(m, uid=user["id"])


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
    trig = []
    for t in r.triggered:
        d = dict(t)
        fk = d.get("frame_oss_key")
        if fk:                                   # 命中的帧/图 → 给个签名 URL,报告里标红显示这张图
            try:
                d["frame_url"] = deps.storage.signed_url(fk)
            except Exception:
                d["frame_url"] = ""
        trig.append(d)
    return {
        "verdict": r.verdict, "summary": r.summary, "triggered": trig,
        "segments": [{"source_type": s.source_type, "text": s.text, "begin_ms": s.begin_ms,
                      "end_ms": s.end_ms, "frame_oss_key": s.frame_oss_key} for s in r.segments],
    }


def _rule_out(r: AuditRule) -> dict:
    return {"id": r.id, "source_type": r.source_type, "keywords": r.keywords,
            "condition": r.condition, "action": r.action, "enabled": r.enabled}


def _task_out(t: AuditTask) -> dict:
    # 任务裁定跟随物料现状:管理员在审核队列改判(pass/block)后,待审核页立即反映,
    # 不再停留在机审时的「待人工复核」。物料被删则退回任务存的裁定。
    verdict = t.verdict
    if t.material_id:
        m = deps.material_repo.get(t.material_id)
        if m is not None:
            verdict = getattr(m.audit_status, "value", m.audit_status)
    return {"id": t.id, "name": t.name, "material_type": t.material_type,
            "material_id": t.material_id, "status": t.status, "verdict": verdict,
            "report_id": t.report_id, "created_ms": t.created_ms, "error": t.error,
            "video_kind": getattr(t, "video_kind", "material")}


def _new_task(owner_id: str, name: str, mtype: MaterialType, material_id: str, chash: str,
              video_kind: str = "material") -> AuditTask:
    task = AuditTask(id=uuid.uuid4().hex, owner_id=owner_id, name=name, material_type=mtype,
                     material_id=material_id, content_hash=chash, status=JobStatus.PENDING,
                     created_ms=int(time.time() * 1000), video_kind=video_kind)
    deps.task_repo.save(task)
    return task


def _finish_task(task: AuditTask, job, report) -> None:
    task.verdict = report.verdict.value
    task.status = JobStatus.DONE if job.status == JobStatus.DONE else JobStatus.FAILED
    m = deps.material_repo.get(task.material_id)
    task.report_id = m.audit_report_id if m else ""
    deps.task_repo.save(task)


def _run_task_audit(task_id: str, text: str = "") -> None:
    """后台:对已建物料的任务跑审核,回写任务状态/裁定/报告(单条提交用)。"""
    task = deps.task_repo.get(task_id)
    if task is None:
        return
    task.status = JobStatus.RUNNING
    deps.task_repo.save(task)
    svc = deps.get_audit_service()
    m = deps.material_repo.get(task.material_id)
    job = svc.submit(task.material_type, oss_key=(m.oss_key if m else ""),
                     owner_id=task.owner_id, material_id=task.material_id,
                     video_kind=getattr(task, "video_kind", "material"))
    try:
        report = svc.run(job, text)
        _finish_task(task, job, report)
    except Exception as e:
        task.status = JobStatus.FAILED
        task.error = str(e)[:200]
        deps.task_repo.save(task)


def _run_task_recheck(task_id: str) -> None:
    """后台:对已存报告用当前白名单/规则**只重判**(不重抽帧/转写),回写任务状态/裁定/报告。"""
    task = deps.task_repo.get(task_id)
    if task is None:
        return
    old = deps.report_repo.get(task.report_id) if task.report_id else None
    if old is None:
        return
    task.status = JobStatus.RUNNING
    task.error = ""
    deps.task_repo.save(task)
    svc = deps.get_audit_service()
    m = deps.material_repo.get(task.material_id)
    job = svc.submit(task.material_type, oss_key=(m.oss_key if m else ""),
                     owner_id=task.owner_id, material_id=task.material_id,
                     video_kind=getattr(task, "video_kind", "material"))
    try:
        report = svc.recheck(job, old)
        _finish_task(task, job, report)
    except Exception as e:
        task.status = JobStatus.FAILED
        task.error = str(e)[:200]
        deps.task_repo.save(task)


def _run_batch_tasks(created: list, owner_id: str) -> None:
    """后台:批量逐个上传+建物料+审核,每个文件回写各自的任务(异步呈现在「待审核」页)。"""
    svc = deps.get_audit_service()
    msvc = deps.get_material_service()
    for task_id, name, data, mtype, chash in created:
        task = deps.task_repo.get(task_id)
        if task is None:
            continue
        task.status = JobStatus.RUNNING
        deps.task_repo.save(task)
        kind = getattr(task, "video_kind", "material")   # 物料/作品由顶部 tab 决定(建任务时已写入),不再按时长猜
        try:
            if mtype == MaterialType.CORPUS:
                text = data.decode("utf-8", "ignore")
                m = Material(id=uuid.uuid4().hex, type=mtype, thumb="", source_timecode=0.0, embedding=[],
                             audit_status=AuditStatus.REVIEW, source_job="", oss_key="",
                             description=text, owner_id=owner_id, content_hash=chash)
                deps.material_repo.save(m)
            else:
                text = ""
                key = f"materials/{uuid.uuid4().hex}-{name.rsplit('/', 1)[-1]}"
                m = msvc.create(mtype, key, data, owner_id, chash)
                # 物料视频 ≤20s 护栏(与单个上传一致):超时长→删物料+任务失败,不入库不审核;请改选「作品」
                if mtype == MaterialType.VIDEO and kind == "material":
                    dur = deps.storage.video_duration_ms(m.oss_key)
                    if dur is not None and dur > 20000:
                        msvc.delete(m.id)
                        task.status = JobStatus.FAILED
                        task.error = "物料视频需 ≤20 秒;请改选「作品」或裁剪后重传。"
                        deps.task_repo.save(task)
                        continue
                deps.get_index_service().index_material(m)
            task.material_id = m.id
            deps.task_repo.save(task)
            job = svc.submit(mtype, oss_key=m.oss_key, owner_id=owner_id, material_id=m.id,
                             video_kind=kind)
            report = svc.run(job, text)
            _finish_task(task, job, report)
        except Exception as e:
            task.status = JobStatus.FAILED
            task.error = str(e)[:200]
            deps.task_repo.save(task)


# 去重要原子:检查「库内已有」+「同内容正在处理中」并登记,防并发/连点重复提交(检查-建库非原子的竞态)
_dedup_lock = threading.Lock()
_inflight_hashes: set = set()


def _dedup_reserve(owner_id: str, chash: str):
    """原子登记。返回 (已存在物料 or None, 是否重复)。重复=库内已有 或 同内容正在处理中。"""
    with _dedup_lock:
        existing = deps.material_repo.by_content_hash(owner_id, chash)
        if existing is not None:
            return existing, True
        if (owner_id, chash) in _inflight_hashes:
            return None, True
        _inflight_hashes.add((owner_id, chash))
        return None, False


def _dedup_release(owner_id: str, chash: str) -> None:
    with _dedup_lock:
        _inflight_hashes.discard((owner_id, chash))


@router.post("/audit/submit")
async def audit_submit(type: str = Form("image"), content: str = Form(""),
                       video_kind: str = Form("material"),
                       file: UploadFile = File(None), user: dict = Depends(_user)):
    """审核入口:上传即成功、可立刻再提交;审核异步跑,统一到「待审核」页看状态。
    同一用户库内按内容 MD5 去重,重复不再上传。视频分 物料(material,≤20s,抽帧入库)/ 作品(work,仅扫描)。"""
    _require_auth(user)
    try:
        mtype = MaterialType(type)
    except ValueError:
        raise HTTPException(400, f"不支持的类型: {type}")
    owner = user["id"]
    video_kind = video_kind if video_kind in ("material", "work") else "material"

    # 文字:内容 hash 原子去重(防连点/并发)→ 建语料物料 → 异步审核
    if mtype == MaterialType.CORPUS:
        text = content.strip()
        if not text:
            raise HTTPException(400, "文字内容不能为空")
        chash = hashlib.md5(text.encode("utf-8")).hexdigest()
        existing, is_dup = _dedup_reserve(owner, chash)
        if is_dup:
            return {"status": "duplicate", "material_id": existing.id if existing else "",
                    "message": "这段文字你已提交过,未重复。"}
        try:
            m = Material(id=uuid.uuid4().hex, type=mtype, thumb="", source_timecode=0.0, embedding=[],
                         audit_status=AuditStatus.REVIEW, source_job="", oss_key="",
                         description=text, owner_id=owner, content_hash=chash)
            deps.material_repo.save(m)
            task = _new_task(owner, "文字审核", mtype, m.id, chash)
            threading.Thread(target=_run_task_audit, args=(task.id, text), name="audit-worker", daemon=True).start()
            return {"status": "submitted", "task_id": task.id, "material_id": m.id}
        finally:
            _dedup_release(owner, chash)

    # 文件:读字节 → 原子去重 → 存 OSS 建物料(此步=上传成功)→ 异步审核
    if file is None:
        raise HTTPException(400, "缺少文件")
    data = await file.read()
    chash = hashlib.md5(data).hexdigest()
    existing, is_dup = _dedup_reserve(owner, chash)
    if is_dup:
        return {"status": "duplicate", "material_id": existing.id if existing else "",
                "message": f"「{file.filename}」已在你的库中,未重复上传。"}
    try:
        key = f"audit/{uuid.uuid4().hex}-{file.filename}"
        m = await run_in_threadpool(deps.get_material_service().create, mtype, key, data, owner, chash)
        # 物料视频强制 ≤20 秒(作品不限);解析不到时长则放行,前端已预检
        if mtype == MaterialType.VIDEO and video_kind == "material":
            dur = await run_in_threadpool(deps.storage.video_duration_ms, m.oss_key)
            if dur is not None and dur > 20000:
                await run_in_threadpool(deps.get_material_service().delete, m.id)  # 删 OSS + 元数据
                return {"status": "too_long",
                        "message": f"物料视频需 ≤20 秒,当前约 {round(dur/1000)} 秒;请改选「作品」或裁剪后再传。"}
        deps.get_index_service().index_material(m)
        task = _new_task(owner, file.filename or "文件", mtype, m.id, chash, video_kind=video_kind)
        threading.Thread(target=_run_task_audit, args=(task.id, ""), name="audit-worker", daemon=True).start()
        return {"status": "submitted", "task_id": task.id, "material_id": m.id}
    finally:
        _dedup_release(owner, chash)


# ── 批量上传(ZIP 解包 / 文件夹多文件)──
_EXT_TYPE = {
    "jpg": "image", "jpeg": "image", "png": "image", "gif": "image", "webp": "image", "bmp": "image",
    "mp4": "video", "mov": "video", "mkv": "video", "avi": "video", "webm": "video", "flv": "video",
    "mp3": "audio", "wav": "audio", "m4a": "audio", "aac": "audio", "flac": "audio", "ogg": "audio",
    "txt": "corpus", "md": "corpus",
}


def _infer_type(name: str):
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return _EXT_TYPE.get(ext)


def _expand_zip(data: bytes) -> list[tuple]:
    """解包 zip → [(entry名, bytes)];跳过目录、隐藏文件、__MACOSX。"""
    import zipfile, io
    out = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                base = info.filename.rsplit("/", 1)[-1]
                if info.filename.startswith("__MACOSX") or base.startswith("."):
                    continue
                out.append((info.filename, z.read(info)))
    except Exception:
        pass
    return out


@router.post("/audit/batch")
async def audit_batch(files: list[UploadFile] = File(...),
                      video_kind: str = Form("material"), user: dict = Depends(_user)):
    """批量:多文件(文件夹拖拽)或单个 zip(自动解包)。逐个上传+审核,状态在「待审核」页看。
    视频统一按顶部 tab 的 video_kind(material/work)分类(不再按时长自动猜);物料视频仍需 ≤20s。
    同一用户库内按内容 MD5 去重(库内已有 + 批内重复都跳过);不支持的扩展名也跳过。"""
    _require_auth(user)
    owner = user["id"]
    video_kind = video_kind if video_kind in ("material", "work") else "material"
    raw = [(f.filename or "file", await f.read()) for f in files]
    items: list[tuple] = []
    for name, data in raw:
        if name.lower().endswith(".zip"):
            items += _expand_zip(data)
        else:
            items.append((name, data))
    items = [(n, d) for n, d in items if n and d]
    if not items:
        raise HTTPException(400, "没有可上传的文件")
    if len(items) > 200:
        raise HTTPException(400, "单次批量最多 200 个文件")
    created: list = []
    skipped = 0
    seen: set = set()
    for name, data in items:
        t = _infer_type(name)
        if t is None:
            skipped += 1
            continue                                       # 不支持的扩展名
        chash = hashlib.md5(data).hexdigest()
        if chash in seen or deps.material_repo.by_content_hash(owner, chash) is not None:
            skipped += 1
            continue                                       # 批内重复 + 库内已有
        seen.add(chash)
        mtype = MaterialType(t)
        task = _new_task(owner, name.rsplit("/", 1)[-1], mtype, "", chash, video_kind=video_kind)
        created.append((task.id, name, data, mtype, chash))
    if not created:
        return {"status": "done", "created": 0, "skipped": skipped, "task_ids": []}
    threading.Thread(target=_run_batch_tasks, args=(created, owner), name="audit-worker", daemon=True).start()
    return {"status": "submitted", "created": len(created), "skipped": skipped,
            "task_ids": [c[0] for c in created]}


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


# ── 待审核任务(异步审核状态,统一呈现在「待审核」页;用户看自己的,管理员看全部)──
@router.get("/audit/tasks")
def list_audit_tasks(user: dict = Depends(_user)):
    _require_auth(user)
    tasks = deps.task_repo.list_all() if user["role"] == "admin" else deps.task_repo.list_for(user["id"])
    return {"tasks": [_task_out(t) for t in tasks]}


@router.get("/audit/tasks/{task_id}")
def get_audit_task(task_id: str, user: dict = Depends(_user)):
    _require_auth(user)
    t = deps.task_repo.get(task_id)
    if t is None:
        raise HTTPException(404, "task not found")
    if not (user["role"] == "admin" or t.owner_id == user["id"]):
        raise HTTPException(403, "无权查看该任务")
    report = deps.report_repo.get(t.report_id) if t.report_id else None
    return {**_task_out(t), "report": _report_out(report) if report else None}


@router.delete("/audit/tasks/{task_id}")
def delete_audit_task(task_id: str, user: dict = Depends(_user)):
    _require_auth(user)
    t = deps.task_repo.get(task_id)
    if t is not None and not (user["role"] == "admin" or t.owner_id == user["id"]):
        raise HTTPException(403, "无权删除该任务")
    deps.task_repo.delete(task_id)
    return {"deleted": task_id}


@router.post("/audit/tasks/{task_id}/recheck")
def recheck_audit_task(task_id: str, user: dict = Depends(_user)):
    """加白/改规则后,用当前白名单重新判定该任务(只对已存报告重判,不重抽帧/转写)。改判需审核权限。"""
    _require_perm(user, "materials.audit")
    t = deps.task_repo.get(task_id)
    if t is None:
        raise HTTPException(404, "task not found")
    if t.status != JobStatus.DONE or not t.report_id:
        raise HTTPException(400, "仅可对已完成且有报告的任务重新审核")
    t.status = JobStatus.RUNNING   # 同步置「审核中」→ 前端立刻看到并开始轮询(消除竞态)
    t.error = ""
    deps.task_repo.save(t)
    threading.Thread(target=_run_task_recheck, args=(t.id,), name="audit-worker", daemon=True).start()
    return {"status": "rechecking", "id": t.id}


@router.get("/audit/queue")
def audit_queue(page: int = Query(1, ge=1), size: int = Query(50, ge=1, le=100),
                type: str | None = None, user: dict = Depends(_user)):
    """人工审核队列(管理员):待复核物料 + 可内联播放的签名 URL + 命中原因报告,一次拉齐 → 卡片内直接看直接判。"""
    _require_perm(user, "materials.audit")
    off, lim = _page_args(page, size)
    items, total = deps.get_library_service().all(
        status=_check_status("review"), type=_check_type(type), offset=off, limit=lim)
    out = []
    for m in items:
        rid = getattr(m, "audit_report_id", "")
        rep = deps.report_repo.get(rid) if rid else None
        rep_out = _report_out(rep) if rep else None
        if rep_out and m.type != MaterialType.CORPUS:
            rep_out = {**rep_out, "segments": []}  # 卡片只用 triggered 命中项;非文本无需回传整条转写(省带宽)
        out.append({**_mat_out(m, uid=user["id"]), "media_url": _media_url(m), "report": rep_out})
    return _page_out(out, total, page, size)


# ── 语义搜索(F3)——在公共库范围内搜索 ──
@router.get("/search")
def search(q: str = "", page: int = Query(1, ge=1), size: int = Query(24, ge=1, le=100),
           type: str | None = None, tag: str | None = None, user: dict = Depends(_user)):
    off, lim = _page_args(page, size)
    results, total = deps.get_search_service().search(
        q, type=_check_type(type), tag=tag or None, offset=off, limit=lim)
    fav = deps.favorites.material_ids(user["id"])
    return _page_out([_mat_out(m, fav, user["id"]) for m in results], total, page, size, key="results")


# ── 物料库:我的 / 公共 / 全部(管理员)──
@router.get("/library/mine")
def my_library(page: int = Query(1, ge=1), size: int = Query(24, ge=1, le=100),
               type: str | None = None, tag: str | None = None, q: str | None = None,
               user: dict = Depends(_user)):
    _require_auth(user)
    off, lim = _page_args(page, size)
    fav = deps.favorites.material_ids(user["id"])
    items, total = deps.get_library_service().mine(
        user["id"], type=_check_type(type), tag=tag or None, keyword=q or None,
        offset=off, limit=lim)
    return _page_out([_mat_out(m, fav, user["id"]) for m in items], total, page, size)


@router.get("/library/public")
def public_library(page: int = Query(1, ge=1), size: int = Query(24, ge=1, le=100),
                   type: str | None = None, tag: str | None = None, q: str | None = None,
                   user: dict = Depends(_user)):
    off, lim = _page_args(page, size)
    fav = deps.favorites.material_ids(user["id"])
    items, total = deps.get_library_service().public(
        type=_check_type(type), tag=tag or None, keyword=q or None, offset=off, limit=lim)
    return _page_out([_mat_out(m, fav, user["id"]) for m in items], total, page, size)


@router.get("/library/all")
def all_library(page: int = Query(1, ge=1), size: int = Query(24, ge=1, le=100),
                status: str | None = None, type: str | None = None, tag: str | None = None,
                q: str | None = None, user: dict = Depends(_user)):
    """管理员:看所有用户的物料。服务端分页/筛选。"""
    _require_perm(user, "library.all")
    off, lim = _page_args(page, size)
    items, total = deps.get_library_service().all(
        status=_check_status(status), type=_check_type(type), tag=tag or None,
        keyword=q or None, offset=off, limit=lim)
    return _page_out([_mat_out(m, uid=user["id"]) for m in items], total, page, size)


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
# 权限目录:每条权限写清楚是什么、什么意思(功能权限页授权弹窗用)
PERM_CATALOG = [
    {"key": "materials.audit", "label": "内容审核复核", "desc": "复核待定物料、操作审核队列(通过/拦截)"},
    {"key": "materials.publish", "label": "发布物料", "desc": "把物料发布到公共库,或从公共库下架"},
    {"key": "library.all", "label": "查看全部物料", "desc": "查看所有用户的物料(管理视图)"},
    {"key": "audit.rules", "label": "审核规则", "desc": "新增 / 删除违规判定规则"},
    {"key": "admin.grant", "label": "权限与账号管理", "desc": "给用户授权 / 收回权限、增删账号"},
    {"key": "materials.delete_any", "label": "删除任意物料", "desc": "删除其他用户上传的物料"},
]
_PERM_KEYS = {p["key"] for p in PERM_CATALOG}


@router.get("/admin/perm-catalog")
def perm_catalog(user: dict = Depends(_user)):
    """可授予的功能权限清单(带中文名+说明),给授权弹窗用。"""
    _require_perm(user, "admin.grant")
    return {"catalog": PERM_CATALOG}


@router.get("/admin/users")
def list_users(user: dict = Depends(_user)):
    """账号列表(名字 / 角色 / 已被单独授予的权限)。"""
    _require_perm(user, "admin.grant")
    users = [{"id": u.id, "name": u.name, "role": u.role,
              "permissions": sorted(deps.rbac.user_permissions(u.id))}
             for u in deps.user_repo.list()]
    users.sort(key=lambda x: (x["role"] != "admin", x["name"]))   # 管理员置顶
    return {"users": users}


@router.post("/admin/users")
def create_user(body: schemas.UserCreate, user: dict = Depends(_user)):
    """新增账号(默认普通用户;admin 是唯一管理员)。"""
    _require_perm(user, "admin.grant")
    try:
        u = deps.get_user_service().register(body.name, body.password)
    except DuplicateName:
        raise HTTPException(409, "用户名已被占用")
    except InvalidCredentials:
        raise HTTPException(400, "用户名和密码不能为空")
    return {"id": u.id, "name": u.name, "role": u.role}


@router.delete("/admin/users/{uid}")
def delete_user(uid: str, user: dict = Depends(_user)):
    """删除账号。不能删管理员、不能删自己。"""
    _require_perm(user, "admin.grant")
    target = deps.user_repo.get(uid)
    if target is None:
        return {"deleted": uid}
    if target.role == "admin":
        raise HTTPException(400, "不能删除管理员账号")
    if uid == user["id"]:
        raise HTTPException(400, "不能删除自己")
    deps.user_repo.delete(uid)
    return {"deleted": uid}


@router.post("/admin/users/{uid}/perms")
def set_user_perms(uid: str, body: schemas.UserPermsIn, user: dict = Depends(_user)):
    """给某用户设置功能权限(整套替换;只接受权限目录内的权限)。授权即时生效。"""
    _require_perm(user, "admin.grant")
    target = deps.user_repo.get(uid)
    if target is None:
        raise HTTPException(404, "用户不存在")
    perms = {p for p in body.permissions if p in _PERM_KEYS}
    deps.rbac.set_user_permissions(uid, perms)
    return {"id": uid, "name": target.name, "permissions": sorted(perms)}


# 旧的按角色授权端点(保留兼容,前端已改用按用户授权)
@router.post("/admin/grant")
def grant(body: schemas.GrantIn, user: dict = Depends(_user)):
    _require_perm(user, "admin.grant")
    deps.get_authz_service().grant(body.role, body.permission)
    return {"role": body.role, "permissions": sorted(deps.rbac.permissions_of(body.role))}


@router.get("/admin/permissions")
def role_permissions(role: str, user: dict = Depends(_user)):
    _require_perm(user, "admin.grant")
    return {"role": role, "permissions": sorted(deps.rbac.permissions_of(role))}


# ── 内容安全白名单(治误伤:命中这些词即便阿里云判违规也放行)──
@router.get("/admin/whitelist")
def list_whitelist(user: dict = Depends(_user)):
    _require_perm(user, "admin.grant")
    return {"words": deps.whitelist_repo.list()}


@router.post("/admin/whitelist")
def add_whitelist(body: schemas.WhitelistIn, user: dict = Depends(_user)):
    _require_perm(user, "admin.grant")
    for w in body.words:
        deps.whitelist_repo.add(w)
    return {"words": deps.whitelist_repo.list()}


@router.delete("/admin/whitelist")
def remove_whitelist(word: str, user: dict = Depends(_user)):
    _require_perm(user, "admin.grant")
    deps.whitelist_repo.remove(word)
    return {"words": deps.whitelist_repo.list()}
