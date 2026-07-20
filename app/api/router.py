"""HTTP 路由 —— 8 大功能 + 用户物料库/公共库/收藏/发布 + 多模态内容审核。只依赖 service(+组合根 deps)。"""
from __future__ import annotations
import uuid
import time
import hashlib
import threading
import zipfile
from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Header, Depends, Query
from fastapi.concurrency import run_in_threadpool
from app.api import deps, schemas
from app.infrastructure.snowflake import next_id_str   # 规则主键:雪花 BIGINT 的字符串形态(PG 规范)
from app.domain.models import MaterialType, AuditStatus, Material, AuditRule, User, AuditTask, JobStatus, Project, TextSourceType
from app.domain.mp4 import parse_mp4_duration_ms
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
        "project_id": getattr(m, "project_id", ""),
        "tags": list(getattr(m, "tags", []) or []),
        "ai_summary": getattr(m, "ai_summary", ""),
        "ai_scenarios": list(getattr(m, "ai_scenarios", []) or []),
        "ai_emotions": list(getattr(m, "ai_emotions", []) or []),
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
    if new_status == AuditStatus.PROCESSING:               # 「审核中」是机器内部态,人工不可设
        raise HTTPException(400, "人工只能设 pass/review/block")
    m = deps.material_repo.get(mid)
    if m is None:
        raise HTTPException(404, "material not found")
    m.audit_status = new_status
    if new_status == AuditStatus.BLOCK:                     # 人工退回 → 记入退回历史(作品审核记录用)
        _record_reject(m, body.reason or "人工退回", "人工")
    deps.material_repo.save(m)
    return _mat_out(m, uid=user["id"])


def _record_reject(m, reason: str, by: str) -> None:
    """作品/物料被判 block 时追加一条退回记录。就地改 m.reject_events(调用方负责 save)。"""
    if not hasattr(m, "reject_events") or m.reject_events is None:
        m.reject_events = []
    m.reject_events.append({"ms": int(time.time() * 1000), "reason": (reason or "")[:200], "by": by})


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
    # 把触发规则信息挂到对应 segment 上，前端可直接渲染"命中: 规则#5"
    trig_by_ms: dict[int, list[dict]] = {}
    for t in r.triggered:
        ms = t.get("begin_ms")
        if ms is not None:
            trig_by_ms.setdefault(ms, []).append(t)
    segs_out = []
    for s in r.segments:
        seg = {"source_type": s.source_type, "text": s.text, "begin_ms": s.begin_ms,
               "end_ms": s.end_ms, "frame_oss_key": s.frame_oss_key}
        # 匹配：triggered 的 begin_ms 落在 segment 时间范围内
        matched = []
        s_begin = s.begin_ms
        s_end = s.end_ms
        if s_begin is not None:
            for ms, items in trig_by_ms.items():
                if s_end is not None:
                    in_range = s_begin <= ms <= s_end
                else:
                    in_range = ms == s_begin
                if in_range:
                    for it in items:
                        matched.append({"rule_no": it.get("rule_no", 0),
                                        "rule_desc": (it.get("rule_desc") or "")[:80],
                                        "action": it.get("action", ""),
                                        "reason": (it.get("reason") or "")[:200]})
        seg["triggered_rules"] = matched
        segs_out.append(seg)
    return {
        "verdict": r.verdict, "summary": r.summary, "triggered": trig,
        "segments": segs_out,
    }


def _norm_level(v: str | None) -> str:
    """严格程度归一:literal=字面、regex=正则(不走大模型)保留原值;其余(含缺省/非法)→ metaphor(隐喻,安全默认)。"""
    return v if v in ("literal", "regex") else "metaphor"


def _norm_source_type(raw: str) -> str:
    """来源类型归一:逗号分隔多值,每部分必须是合法值;非法/空→ any"""
    parts = [p.strip() for p in (raw or "").split(",") if p.strip()]
    valid = [p for p in parts if p in _SOURCE_TYPES]
    return ",".join(valid) if valid else "any"


def _next_rule_no() -> int:
    """下一个规则编号:全局现有最大 no + 1(稳定、不复用、递增)。"""
    return max((getattr(r, "no", 0) for r in deps.rule_repo.list()), default=0) + 1


def _rule_out(r: AuditRule) -> dict:
    return {"id": r.id, "no": getattr(r, "no", 0), "source_type": r.source_type, "keywords": r.keywords,
            "condition": r.condition, "action": r.action, "enabled": r.enabled,
            "project_id": getattr(r, "project_id", ""),
            "guidance": getattr(r, "guidance", ""),
            "match_level": _norm_level(getattr(r, "match_level", "metaphor")),
            "regex": getattr(r, "regex", ""),
            "exceptions": getattr(r, "exceptions", [])}


def _project_out(p: Project) -> dict:
    return {"id": p.id, "name": p.name, "created_ms": p.created_ms}


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
            "video_kind": getattr(t, "video_kind", "material"),
            "project_id": getattr(t, "project_id", "")}


def _new_task(owner_id: str, name: str, mtype: MaterialType, material_id: str, chash: str,
              video_kind: str = "material", project_id: str = "") -> AuditTask:
    task = AuditTask(id=uuid.uuid4().hex, owner_id=owner_id, name=name, material_type=mtype,
                     material_id=material_id, content_hash=chash, status=JobStatus.PENDING,
                     created_ms=int(time.time() * 1000), video_kind=video_kind, project_id=project_id)
    deps.task_repo.save(task)
    return task


def _fail_task(task: AuditTask, error: str) -> None:
    """审核失败:标失败 + 暴露原因 + 物料降级为 REVIEW(不删,方便重试)。
    不删 OSS/元数据 → 用户可从「待审核」页点「重试」重新跑审核;想彻底清除可手动删任务+物料。"""
    task.status = JobStatus.FAILED
    task.error = (error or "审核失败,请重试。")[:200]
    if task.material_id:
        try:
            m = deps.material_repo.get(task.material_id)
            if m is not None and m.audit_status == AuditStatus.PROCESSING:
                m.audit_status = AuditStatus.REVIEW
                deps.material_repo.save(m)
        except Exception:
            pass
    deps.task_repo.save(task)


def _finish_task(task: AuditTask, job, report, delete_on_fail: bool = True) -> None:
    # 机器只出 pass/review;退回历史只在人工拒绝(set-audit block)时记,机审不记。
    if job.status != JobStatus.DONE:                       # 审核没跑成(内部兜底转人工时把 job 标了 FAILED)
        if delete_on_fail:                                 # 首审失败 → 删没成功的物料 + 暴露原因(可重传)
            _fail_task(task, report.summary or "审核未完成,请重试。")
        else:                                              # 重判失败 → 只标失败,别删已入库的物料
            task.status = JobStatus.FAILED
            task.error = (report.summary or "重新审核失败,请重试。")[:200]
            deps.task_repo.save(task)
        return
    task.verdict = report.verdict.value
    task.status = JobStatus.DONE
    m = deps.material_repo.get(task.material_id)
    task.report_id = m.audit_report_id if m else ""
    deps.task_repo.save(task)


def _sync_task_after_recheck(mid: str, report) -> None:
    """按物料重判后,把关联的 AuditTask(若有)同步到新裁定/报告,避免「待审核任务」页与队列不一致。
    best-effort:没有任务就跳过;task_repo 无 material_id 索引,扫全量匹配(管理员单次动作,量小)。"""
    m = deps.material_repo.get(mid)
    new_rid = m.audit_report_id if m else ""
    for t in deps.task_repo.list_all():
        if t.material_id == mid:
            t.verdict = report.verdict.value
            t.status = JobStatus.DONE
            t.report_id = new_rid
            t.error = ""
            deps.task_repo.save(t)


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
                     video_kind=getattr(task, "video_kind", "material"),
                     project_id=getattr(task, "project_id", ""))
    try:
        report = svc.run(job, text)
        _finish_task(task, job, report)
    except Exception as e:
        _fail_task(task, str(e))   # 首审异常 → 删没成功的物料 + 暴露原因(可重传)


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
                     video_kind=getattr(task, "video_kind", "material"),
                     project_id=getattr(task, "project_id", ""))
    try:
        report = svc.recheck(job, old)
        _finish_task(task, job, report, delete_on_fail=False)   # 重判失败别删已入库的物料
    except Exception as e:
        task.status = JobStatus.FAILED
        task.error = str(e)[:200]
        deps.task_repo.save(task)


async def _batch_prepare_item(owner_id: str, name: str, data: bytes, mtype: MaterialType,
                               chash: str, video_kind: str, project_id: str,
                               fileobj=None) -> str | None:
    """批量内单条准备:上传 OSS + 建物料 + 时长检查 + 建任务,只提交「审核」到线程池(数据已释放)。
    在调用方循环内 await,完成后 data 引用即可释放,内存峰值仅当前单条。
    有 fileobj 时优先流式直传 OSS(免 data 二次拷贝);无则用 data 上传(zip 条目/语料)。
    返回 task_id;失败时 _fail_task 标失败并仍返回 task_id(前端可追踪失败原因)。"""
    svc = deps.get_audit_service()
    msvc = deps.get_material_service()
    task = _new_task(owner_id, name, mtype, "", chash,
                     video_kind=video_kind, project_id=project_id)
    try:
        if mtype == MaterialType.CORPUS:
            text = data.decode("utf-8", "ignore")
            m = Material(id=uuid.uuid4().hex, type=mtype, thumb="", source_timecode=0.0, embedding=[],
                         audit_status=AuditStatus.PROCESSING, source_job="", oss_key="",
                         description=text, owner_id=owner_id, content_hash=chash)
            deps.material_repo.save(m)
        else:
            text = ""
            key = f"materials/{uuid.uuid4().hex}-{name.rsplit('/', 1)[-1]}"
            if fileobj is not None:
                # 流式:从 file-like 对象分块直传 OSS,避免 data bytes 二次拷贝
                m = await run_in_threadpool(msvc.create_file, mtype, key, fileobj, owner_id, chash)
            else:
                m = await run_in_threadpool(msvc.create, mtype, key, data, owner_id, chash)
            # 物料视频 ≤20s 护栏(与单个上传一致):超时长→删物料+任务失败,不入库不审核;请改选「作品」
            if mtype == MaterialType.VIDEO and video_kind == "material":
                dur = parse_mp4_duration_ms(data)   # 优先内存解析,避免 OSS 回读
                if dur is None:
                    dur = await run_in_threadpool(deps.storage.video_duration_ms, m.oss_key)
                if dur is not None and dur > 20000:
                    await run_in_threadpool(msvc.delete, m.id)
                    task.status = JobStatus.FAILED
                    task.error = "物料视频需 ≤20 秒;请改选「作品」或裁剪后重传。"
                    deps.task_repo.save(task)
                    return task.id
            deps.get_index_service().index_material(m)
        if project_id and video_kind == "work" and mtype == MaterialType.VIDEO:   # 作品(视频)落项目
            m.project_id = project_id
            deps.material_repo.save(m)
        task.material_id = m.id
        deps.task_repo.save(task)
        # 只提交审核到有界池,不传 data —— 内存已释放
        deps.audit_pool.submit(_batch_run_audit, task.id, m.id, mtype, m.oss_key, text, owner_id)
        return task.id
    except Exception as e:
        _fail_task(task, str(e))
        return task.id


def _batch_run_audit(task_id: str, material_id: str, mtype: MaterialType, oss_key: str,
                     text: str, owner_id: str) -> None:
    """纯审核(线程池内跑):OSS 已上传,只跑 audit pipeline + 回写任务。"""
    svc = deps.get_audit_service()
    task = deps.task_repo.get(task_id)
    if task is None:
        return
    task.status = JobStatus.RUNNING
    deps.task_repo.save(task)
    kind = getattr(task, "video_kind", "material")
    pid = getattr(task, "project_id", "")
    try:
        job = svc.submit(mtype, oss_key=oss_key, owner_id=owner_id, material_id=material_id,
                         video_kind=kind, project_id=pid)
        report = svc.run(job, text)
        _finish_task(task, job, report)
    except Exception as e:
        _fail_task(task, str(e))


# 去重要原子:检查「库内已有」+「同内容正在处理中」并登记,防并发/连点重复提交(检查-建库非原子的竞态)
_dedup_lock = threading.Lock()
_inflight_hashes: set = set()


def _task_for_material(mid: str):
    """按 material_id 找它的审核任务(量小,遍历可接受)。"""
    if not mid:
        return None
    return next((t for t in deps.task_repo.list_all() if t.material_id == mid), None)


def _purge_if_dead(m) -> bool:
    """m 是否「死上传」(有对应任务且已 failed:审核没成功)。是→清掉物料+失败任务并返回 True。
    兜底覆盖历史残留 + 删物料与去重之间的竞态,别让没成功的上传永久挡住重传。"""
    t = _task_for_material(m.id)
    if t is None or t.status != JobStatus.FAILED:
        return False
    try:
        deps.get_material_service().delete(m.id)
    except Exception:
        pass
    try:
        deps.task_repo.delete(t.id)
    except Exception:
        pass
    return True


def _dedup_reserve(owner_id: str, chash: str):
    """原子登记。返回 (已存在物料 or None, 是否重复)。重复=库内已有 或 同内容正在处理中。
    库内命中若是「死上传」(审核没成功的残留)→ 清掉、当作不重复放行(不挡重传)。"""
    with _dedup_lock:
        existing = deps.material_repo.by_content_hash(owner_id, chash)
        if existing is not None and not _purge_if_dead(existing):
            return existing, True
        if (owner_id, chash) in _inflight_hashes:
            return None, True
        _inflight_hashes.add((owner_id, chash))
        return None, False


def _dedup_release(owner_id: str, chash: str) -> None:
    with _dedup_lock:
        _inflight_hashes.discard((owner_id, chash))


def _resolve_project(video_kind: str, project_id: str) -> str:
    """作品(video_kind=work)必须选一个存在的项目;非作品不带项目。归一化并校验;非法→400。"""
    project_id = (project_id or "").strip()
    if video_kind == "work":
        if not project_id:
            raise HTTPException(400, "作品必须选择所属项目。")
        if deps.project_repo.get(project_id) is None:
            raise HTTPException(400, "所选项目不存在,请刷新后重试。")
        return project_id
    return ""


@router.post("/audit/submit")
async def audit_submit(type: str = Form("image"), content: str = Form(""),
                       video_kind: str = Form("material"), project_id: str = Form(""),
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
    project_id = _resolve_project(video_kind, project_id)   # 作品必须选存在的项目

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
                         audit_status=AuditStatus.PROCESSING, source_job="", oss_key="",
                         description=text, owner_id=owner, content_hash=chash)
            deps.material_repo.save(m)
            task = _new_task(owner, "文字审核", mtype, m.id, chash)
            deps.audit_pool.submit(_run_task_audit, task.id, text)   # 提交到有界审核池(超上限排队=背压)
            return {"status": "submitted", "task_id": task.id, "material_id": m.id}
        finally:
            _dedup_release(owner, chash)

    # 文件:校验(格式/大小)→ 读字节 → 原子去重 → 存 OSS 建物料(此步=上传成功)→ 异步审核
    if file is None:
        raise HTTPException(400, "缺少文件")
    fname = file.filename or "文件"
    terr = _type_error(fname, mtype.value)                 # 不正确的物料/作品 → 明确提示
    if terr:
        raise HTTPException(400, terr)
    if getattr(file, "size", None) and file.size > _MAX_UPLOAD:   # 按 Content-Length 粗检,省得白传 1GB 再拒
        raise HTTPException(413, _size_error(file.size))
    data = await file.read()
    if not data:
        raise HTTPException(400, "文件是空的,请重新选择。")
    if len(data) > _MAX_UPLOAD:                             # 按真实字节数定检
        raise HTTPException(413, _size_error(len(data)))
    chash = hashlib.md5(data).hexdigest()
    existing, is_dup = _dedup_reserve(owner, chash)
    if is_dup:
        return {"status": "duplicate", "material_id": existing.id if existing else "",
                "message": f"「{fname}」已在你的库中,未重复上传。"}
    try:
        key = f"audit/{uuid.uuid4().hex}-{fname}"
        # 流式上传:seek 回文件头,OSS 从 file-like 对象分块读取,避免全量 bytes 再次拷贝
        await file.seek(0)
        m = await run_in_threadpool(deps.get_material_service().create_file,
                                     mtype, key, file.file, owner, chash)
        if project_id and mtype == MaterialType.VIDEO:     # 作品(视频)落项目(队列按 Material.project_id 分栏/筛)
            m.project_id = project_id
            deps.material_repo.save(m)
        # 物料视频强制 ≤20 秒(作品不限);优先从内存 data 解析 MP4 时长,失败回退 OSS
        if mtype == MaterialType.VIDEO and video_kind == "material":
            dur = parse_mp4_duration_ms(data)
            if dur is None:
                dur = await run_in_threadpool(deps.storage.video_duration_ms, m.oss_key)
            if dur is not None and dur > 20000:
                await run_in_threadpool(deps.get_material_service().delete, m.id)  # 删 OSS + 元数据
                return {"status": "too_long",
                        "message": f"物料视频需 ≤20 秒,当前约 {round(dur/1000)} 秒;请改选「作品」或裁剪后再传。"}
        deps.get_index_service().index_material(m)
        task = _new_task(owner, fname, mtype, m.id, chash, video_kind=video_kind, project_id=project_id)
        deps.audit_pool.submit(_run_task_audit, task.id, "")   # 提交到有界审核池
        return {"status": "submitted", "task_id": task.id, "material_id": m.id}
    except HTTPException:
        raise
    except Exception as _e:                                 # OSS 上传/建库/索引失败 → 友好提示,不抛 500
        import traceback
        traceback.print_exc()
        raise HTTPException(502, f"上传到存储或建库失败,请稍后重试。({_e})")
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


_MAX_UPLOAD = 1024 * 1024 * 1024   # 单文件上限 1 GB
_TYPE_CN = {"image": "图片", "video": "视频", "audio": "声音", "corpus": "文字",
            "meme": "表情包", "style": "风格", "music": "音乐"}


def _size_error(n: int) -> str:
    return f"文件不能超过 1GB(当前约 {round(n / 1024 / 1024)} MB),请压缩或裁剪后再传。"


# 音频文件既可当「声音」也可当「音乐/歌曲」——同一媒体家族,语义标签由用户选(音乐才走联网搜档案)
_TYPE_FAMILY = {"audio": "audio", "music": "audio"}


def _type_error(filename: str, mtype_value: str) -> str | None:
    """上传的是否『正确的物料/作品』:格式支持 + 与所选类型相符。返回中文错误(None=OK)。
    声音/音乐同属音频家族:音频文件选「音乐」也放行(歌曲→联网搜情绪/场景)。"""
    inferred = _infer_type(filename)
    if inferred is None:
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        return f"不支持的文件格式{('「.' + ext + '」') if ext else ''};请上传 图片/视频/声音/音乐 文件(文本用「文字」粘贴)。"
    if _TYPE_FAMILY.get(inferred, inferred) != _TYPE_FAMILY.get(mtype_value, mtype_value):
        return (f"文件与所选类型不符:你选了「{_TYPE_CN.get(mtype_value, mtype_value)}」,"
                f"但「{filename}」是{_TYPE_CN.get(inferred, inferred)}文件;请改选类型或换文件。")
    return None


def _zip_entry_ok(info) -> bool:
    """zip 条目跳过目录、隐藏文件、__MACOSX。"""
    if info.is_dir():
        return False
    base = info.filename.rsplit("/", 1)[-1]
    return not (info.filename.startswith("__MACOSX") or base.startswith("."))


@router.post("/audit/batch")
async def audit_batch(files: list[UploadFile] = File(...),
                      video_kind: str = Form("material"), project_id: str = Form(""),
                      user: dict = Depends(_user)):
    """批量:多文件(文件夹拖拽)或单个 zip(自动解包)。逐个上传+审核,状态在「待审核」页看。
    视频统一按顶部 tab 的 video_kind(material/work)分类(不再按时长自动猜);物料视频仍需 ≤20s。
    同一用户库内按内容 MD5 去重(库内已有 + 批内重复都跳过);不支持的扩展名也跳过。
    zip 文件直接从磁盘流式解压,不把整个压缩包读入内存。"""
    _require_auth(user)
    owner = user["id"]
    video_kind = video_kind if video_kind in ("material", "work") else "material"
    project_id = _resolve_project(video_kind, project_id)   # 作品批量必须选存在的项目
    task_ids: list[str] = []
    skipped_big = skipped_type = skipped_dup = 0
    seen: set = set()
    count = 0

    for f in files:
        fname = f.filename or "file"

        if fname.lower().endswith(".zip"):
            # 流式解压:直接从 UploadFile 的 SpooledTemporaryFile 读,不把整个 zip 加载到内存
            await f.seek(0)
            try:
                with zipfile.ZipFile(f.file) as z:
                    for info in z.infolist():
                        if not _zip_entry_ok(info):
                            continue
                        if count >= 200:
                            break
                        name = info.filename
                        if info.file_size > _MAX_UPLOAD:
                            skipped_big += 1
                            continue
                        t = _infer_type(name)
                        if t is None:
                            skipped_type += 1
                            continue
                        # 逐条读取,内存只持有当前这条的 bytes
                        data = z.read(info)
                        if not data:
                            continue
                        chash = hashlib.md5(data).hexdigest()
                        if chash in seen or deps.material_repo.by_content_hash(owner, chash) is not None:
                            skipped_dup += 1
                            continue
                        seen.add(chash)
                        mtype = MaterialType(t)
                        tid = await _batch_prepare_item(
                            owner, name.rsplit("/", 1)[-1], data, mtype, chash,
                            video_kind, project_id)
                        if tid:
                            task_ids.append(tid)
                            count += 1
            except Exception:
                pass   # 损坏/非 zip 文件 → 跳过,不阻塞批量
        else:
            # 非 zip 单文件:读入哈希 + 去重,然后 seek 回文件头流式传 OSS(免 data 二次拷贝)
            data = await f.read()
            if not data or len(data) > _MAX_UPLOAD:
                skipped_big += 1 if data else 0
                continue
            t = _infer_type(fname)
            if t is None:
                skipped_type += 1
                continue
            chash = hashlib.md5(data).hexdigest()
            if chash in seen or deps.material_repo.by_content_hash(owner, chash) is not None:
                skipped_dup += 1
                continue
            seen.add(chash)
            mtype = MaterialType(t)
            await f.seek(0)   # seek 回文件头,OSS 从 file-like 对象分块直传,不传 data bytes
            tid = await _batch_prepare_item(
                owner, fname.rsplit("/", 1)[-1], data, mtype, chash,
                video_kind, project_id, fileobj=f.file)
            if tid:
                task_ids.append(tid)
                count += 1
            if count >= 200:
                break

    skipped = skipped_big + skipped_type + skipped_dup
    _sk = {"skipped": skipped, "skipped_big": skipped_big,
           "skipped_type": skipped_type, "skipped_dup": skipped_dup}
    if not task_ids:
        return {"status": "done", "created": 0, **_sk, "task_ids": []}
    return {"status": "submitted", "created": len(task_ids), **_sk,
            "task_ids": task_ids}


# ── 审核规则后台(管理员)——放在 /audit/{job_id} 之前,避免 rules 被当作 job_id ──
@router.get("/audit/rules")
def list_audit_rules(project: str | None = None, user: dict = Depends(_user)):
    """列规则。project 缺省=全部;project=""=只看标准/全局;project=P=只看该项目规则。"""
    _require_perm(user, "audit.rules")
    rules = deps.rule_repo.list()
    if project is not None:
        rules = [r for r in rules if getattr(r, "project_id", "") == project]
    return {"rules": [_rule_out(r) for r in rules]}


@router.post("/audit/rules")
def add_audit_rule(body: schemas.RuleIn, user: dict = Depends(_user)):
    _require_perm(user, "audit.rules")
    action = body.action if body.action in ("block", "review") else "block"
    project_id = (body.project_id or "").strip()
    if project_id and deps.project_repo.get(project_id) is None:
        raise HTTPException(400, "所选项目不存在。")
    st = _norm_source_type(body.source_type)
    rule = AuditRule(id=next_id_str(), no=_next_rule_no(), source_type=st,
                     keywords=[k for k in body.keywords if k.strip()], condition=body.condition.strip(),
                     action=action, enabled=True, created_by=user["id"], project_id=project_id,
                     guidance=(body.guidance or "").strip(), match_level=_norm_level(body.match_level),
                     regex=(body.regex or "").strip())
    deps.rule_repo.add(rule, by=user["id"])
    return _rule_out(rule)


@router.put("/audit/rules/{rule_id}")
def update_audit_rule(rule_id: str, body: schemas.RuleIn, user: dict = Depends(_user)):
    """编辑已有规则:按 id 覆盖(保留 id/created_by/enabled,其余按请求更新)。归一化/校验同新增。"""
    _require_perm(user, "audit.rules")
    existing = next((r for r in deps.rule_repo.list() if r.id == rule_id), None)
    if existing is None:
        raise HTTPException(404, "规则不存在。")
    action = body.action if body.action in ("block", "review") else "block"
    project_id = (body.project_id or "").strip()
    if project_id and deps.project_repo.get(project_id) is None:
        raise HTTPException(400, "所选项目不存在。")
    updated = AuditRule(id=rule_id, no=getattr(existing, "no", 0) or _next_rule_no(),
                        source_type=_norm_source_type(body.source_type),
                        keywords=[k for k in body.keywords if k.strip()], condition=body.condition.strip(),
                        action=action, enabled=existing.enabled, created_by=existing.created_by,
                        project_id=project_id, guidance=(body.guidance or "").strip(),
                        match_level=_norm_level(body.match_level), regex=(body.regex or "").strip(),
                        exceptions=getattr(existing, "exceptions", []))   # 编号/例外不随编辑丢失
    deps.rule_repo.add(updated, by=user["id"])   # 按 id 覆盖(upsert)
    return _rule_out(updated)


_SOURCE_TYPES = {"any"} | {t.value for t in TextSourceType}


@router.post("/audit/rules/parse")
def parse_audit_rules(body: schemas.RuleParseIn, user: dict = Depends(_user)):
    """粘贴整篇「卡审/审核标准」文案 → 大模型拆成结构化规则草案(预览用,不落库)。
    需先选定项目作用域(作品规则都归属某个项目)。前端预览可删个别条后再走 /audit/rules/bulk 落库。"""
    _require_perm(user, "audit.rules")
    project_id = (body.project_id or "").strip()
    if not project_id:
        raise HTTPException(400, "请先选择规则所属的项目。")
    if deps.project_repo.get(project_id) is None:
        raise HTTPException(400, "所选项目不存在,请刷新后重试。")
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "请粘贴要解析的审核文案。")
    drafts = deps.get_audit_service().parse_rules(text)
    if not drafts:
        raise HTTPException(422, "没能从这段文案里解析出规则,请检查内容后重试。")
    return {"rules": drafts, "project_id": project_id}


@router.post("/audit/rules/compile-regex")
def compile_rule_regex(body: schemas.RegexCompileIn, user: dict = Depends(_user)):
    """正则规则:把管理员的自然语言描述交大模型编译成 {keywords, regex}(预览用,不落库)。
    只在建/编辑规则时用一次;审核时用编译出的正则纯匹配、不再调大模型。管理员可在前端再手改 regex。"""
    _require_perm(user, "audit.rules")
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "请先填写要拦的内容(自然语言)。")
    out = deps.get_audit_service().compile_regex(text)
    if not out.get("regex") and not out.get("keywords"):
        raise HTTPException(422, "没能从这段描述编译出正则,请换个说法重试。")
    return out


@router.post("/audit/rules/bulk")
def bulk_add_audit_rules(body: schemas.RulesBulkIn, user: dict = Depends(_user)):
    """把预览确认后的规则草案批量落库到指定项目作用域;逐条归一化并跳过空规则。"""
    _require_perm(user, "audit.rules")
    project_id = (body.project_id or "").strip()
    if not project_id:
        raise HTTPException(400, "请先选择规则所属的项目。")
    if deps.project_repo.get(project_id) is None:
        raise HTTPException(400, "所选项目不存在,请刷新后重试。")
    created: list[AuditRule] = []
    for d in body.rules:
        st = _norm_source_type(d.source_type)
        kws = list(dict.fromkeys(k.strip() for k in d.keywords if k.strip()))
        cond = (d.condition or "").strip()
        if not kws and not cond:
            continue    # 空规则(无词无条件)—— 无法命中,跳过
        action = d.action if d.action in ("block", "review") else "review"
        rule = AuditRule(id=next_id_str(), no=_next_rule_no(), source_type=st, keywords=kws,
                         condition=cond, action=action, enabled=True,
                         created_by=user["id"], project_id=project_id,
                         match_level=_norm_level(getattr(d, "match_level", "metaphor")))
        deps.rule_repo.add(rule, by=user["id"])   # 立刻落库 → 下一条 _next_rule_no 见到它、编号递增
        created.append(rule)
    return {"created": len(created), "rules": [_rule_out(r) for r in created]}


@router.delete("/audit/rules/{rule_id}")
def delete_audit_rule(rule_id: str, user: dict = Depends(_user)):
    _require_perm(user, "audit.rules")
    deps.rule_repo.delete(rule_id, by=user["id"])
    return {"deleted": rule_id}


_SYNTHETIC_RULE_IDS = {"", "blockword", "content-safety"}


@router.post("/audit/rules/{rule_id}/exceptions")
def add_rule_exception(rule_id: str, body: schemas.RuleExceptionIn, user: dict = Depends(_user)):
    """审核员「忽略这条」→ 把这段命中内容记为该规则的可放行例外(喂回语义判定,后续同类放行)。
    仅对真规则生效(禁词/内容安全等合成命中不走这里:禁词去删词、内容安全用白名单)。"""
    _require_perm(user, "audit.rules")
    if rule_id in _SYNTHETIC_RULE_IDS:
        raise HTTPException(400, "该命中不是规则命中,无法记为规则例外(禁词请去禁词库删词,内容安全请用白名单)。")
    rule = next((r for r in deps.rule_repo.list() if r.id == rule_id), None)
    if rule is None:
        raise HTTPException(404, "规则不存在。")
    text = (body.text or "").strip() or (body.note or "").strip()   # 无定位文本时退回用 AI 原因
    if not text:
        raise HTTPException(400, "例外内容为空。")
    if not hasattr(rule, "exceptions") or rule.exceptions is None:
        rule.exceptions = []
    rule.exceptions.append({"text": text[:500], "note": (body.note or "").strip()[:500],
                            "by": user["id"], "ms": int(time.time() * 1000)})
    deps.rule_repo.add(rule, by=user["id"])
    return _rule_out(rule)


@router.delete("/audit/rules/{rule_id}/exceptions")
def delete_rule_exception(rule_id: str, index: int, user: dict = Depends(_user)):
    """删掉规则的第 index 条例外(撤销误标)。"""
    _require_perm(user, "audit.rules")
    rule = next((r for r in deps.rule_repo.list() if r.id == rule_id), None)
    if rule is None:
        raise HTTPException(404, "规则不存在。")
    exc = getattr(rule, "exceptions", None) or []
    if 0 <= index < len(exc):
        exc.pop(index)
        rule.exceptions = exc
        deps.rule_repo.add(rule, by=user["id"])
    return _rule_out(rule)


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
    """加白/改规则后,用当前白名单重新判定该任务(画面用当前 vision 提示词重新反解,不重抽帧/转写)。改判需审核权限。"""
    _require_perm(user, "materials.audit")
    t = deps.task_repo.get(task_id)
    if t is None:
        raise HTTPException(404, "task not found")
    if t.status != JobStatus.DONE or not t.report_id:
        raise HTTPException(400, "仅可对已完成且有报告的任务重新审核")
    t.status = JobStatus.RUNNING   # 同步置「审核中」→ 前端立刻看到并开始轮询(消除竞态)
    t.error = ""
    deps.task_repo.save(t)
    deps.audit_pool.submit(_run_task_recheck, t.id)   # 提交到有界审核池
    return {"status": "rechecking", "id": t.id}


@router.post("/audit/tasks/{task_id}/retry")
def retry_audit_task(task_id: str, user: dict = Depends(_user)):
    """对失败的任务重新跑**完整**审核(重新抽帧/转写/反解)。需审核权限。
    与 recheck 区别:recheck 复用已存报告只重判;retry 从零跑全流程。"""
    _require_perm(user, "materials.audit")
    t = deps.task_repo.get(task_id)
    if t is None:
        raise HTTPException(404, "task not found")
    if t.status != JobStatus.FAILED:
        raise HTTPException(400, "仅可重试失败的任务")
    m = deps.material_repo.get(t.material_id) if t.material_id else None
    if m is None:
        raise HTTPException(400, "物料已被删除,请重新上传")
    t.status = JobStatus.RUNNING   # 同步置「审核中」→ 前端立刻看到并开始轮询
    t.error = ""
    deps.task_repo.save(t)
    deps.audit_pool.submit(_run_task_audit, t.id, "")
    return {"status": "retrying", "id": t.id}


@router.post("/materials/{mid}/recheck")
def recheck_material(mid: str, user: dict = Depends(_user)):
    """审核队列里对单条物料「按最新规则重新审核」:画面用当前 vision 提示词重新反解,
    口播/原文复用已存 segments;只用**当前**规则重跑三波级联 → 回写报告 + 物料状态 + 关联任务,同步返回新报告(标红即刷新)。"""
    _require_perm(user, "materials.audit")
    m = deps.material_repo.get(mid)
    if m is None:
        raise HTTPException(404, "material not found")
    rid = getattr(m, "audit_report_id", "")
    old = deps.report_repo.get(rid) if rid else None
    if old is None:
        raise HTTPException(400, "该物料还没有可复用的审核报告,请先完成一次审核")
    svc = deps.get_audit_service()
    pid = getattr(m, "project_id", "") or ""
    job = svc.submit(m.type, oss_key=m.oss_key, owner_id=m.owner_id, material_id=mid,
                     video_kind=("work" if pid else "material"), project_id=pid)
    report = svc.recheck(job, old)                 # 同步重判 + 持久化(_persist 回写 m.audit_status/report_id)
    _sync_task_after_recheck(mid, report)          # 关联任务(若有)同步
    return {"id": mid, "audit_status": report.verdict, "report": _report_out(report)}


@router.get("/audit/queue")
def audit_queue(page: int = Query(1, ge=1), size: int = Query(50, ge=1, le=100),
                type: str | None = None, project: str | None = None, user: dict = Depends(_user)):
    """人工审核队列(管理员):待复核物料 + 可内联播放的签名 URL + 命中原因报告,一次拉齐 → 卡片内直接看直接判。
    project 缺省/"" → 物料栏(无项目);project=P → 项目 P 的待审作品。"""
    _require_perm(user, "materials.audit")
    off, lim = _page_args(page, size)
    items, total = deps.get_library_service().all(
        status=_check_status("review"), type=_check_type(type), project_id=(project or ""),
        offset=off, limit=lim)
    out = []
    for m in items:
        rid = getattr(m, "audit_report_id", "")
        rep = deps.report_repo.get(rid) if rid else None
        rep_out = _report_out(rep) if rep else None
        if rep_out and m.type != MaterialType.CORPUS:
            rep_out = {**rep_out, "segments": []}  # 卡片只用 triggered 命中项;非文本无需回传整条转写(省带宽)
        out.append({**_mat_out(m, uid=user["id"]), "media_url": _media_url(m), "report": rep_out})
    return _page_out(out, total, page, size)


@router.get("/audit/queue/tabs")
def audit_queue_tabs(user: dict = Depends(_user)):
    """审核栏 tab:物料(无项目)+ 每个项目一个 tab,各带待审数量角标。"""
    _require_perm(user, "materials.audit")
    items, _ = deps.get_library_service().all(status=_check_status("review"), limit=None)
    counts: dict[str, int] = {}
    for m in items:
        counts[getattr(m, "project_id", "") or ""] = counts.get(getattr(m, "project_id", "") or "", 0) + 1
    # 项目(作品)在前、物料栏放最后 —— 与上传页「作品/物料」一致的项目优先顺序
    tabs = [{"key": p.id, "label": p.name, "count": counts.get(p.id, 0)} for p in deps.project_repo.list()]
    tabs.append({"key": "", "label": "物料", "count": counts.get("", 0)})
    return {"tabs": tabs}


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
               project: str | None = None, user: dict = Depends(_user)):
    _require_auth(user)
    off, lim = _page_args(page, size)
    fav = deps.favorites.material_ids(user["id"])
    items, total = deps.get_library_service().mine(
        user["id"], type=_check_type(type), tag=tag or None, keyword=q or None,
        project_id=project, offset=off, limit=lim)
    return _page_out([_mat_out(m, fav, user["id"]) for m in items], total, page, size)


@router.get("/library/public")
def public_library(page: int = Query(1, ge=1), size: int = Query(24, ge=1, le=100),
                   type: str | None = None, tag: str | None = None, q: str | None = None,
                   project: str | None = None, user: dict = Depends(_user)):
    off, lim = _page_args(page, size)
    fav = deps.favorites.material_ids(user["id"])
    items, total = deps.get_library_service().public(
        type=_check_type(type), tag=tag or None, keyword=q or None, project_id=project,
        offset=off, limit=lim)
    return _page_out([_mat_out(m, fav, user["id"]) for m in items], total, page, size)


@router.get("/library/all")
def all_library(page: int = Query(1, ge=1), size: int = Query(24, ge=1, le=100),
                status: str | None = None, type: str | None = None, tag: str | None = None,
                q: str | None = None, project: str | None = None, user: dict = Depends(_user)):
    """管理员:看所有用户的物料。服务端分页/筛选。"""
    _require_perm(user, "library.all")
    off, lim = _page_args(page, size)
    items, total = deps.get_library_service().all(
        status=_check_status(status), type=_check_type(type), tag=tag or None,
        keyword=q or None, project_id=project, offset=off, limit=lim)
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


# ── 绝对禁词(审核第一波:命中即拦。管理员精选、非常确定不能讲的硬词)──
@router.get("/admin/blockwords")
def list_blockwords(user: dict = Depends(_user)):
    _require_perm(user, "audit.rules")
    return {"words": deps.blockword_repo.list()}


@router.post("/admin/blockwords")
def add_blockwords(body: schemas.BlockwordIn, user: dict = Depends(_user)):
    _require_perm(user, "audit.rules")
    for w in body.words:
        deps.blockword_repo.add(w)
    return {"words": deps.blockword_repo.list()}


@router.delete("/admin/blockwords")
def remove_blockword(word: str, user: dict = Depends(_user)):
    _require_perm(user, "audit.rules")
    deps.blockword_repo.remove(word)
    return {"words": deps.blockword_repo.list()}


# ── 作品项目(管理员建/删;所有登录用户可列出——供提交选项目 + 浏览筛选)──
@router.get("/projects")
def list_projects(user: dict = Depends(_user)):
    """项目列表(供提交作品选项目 + 分项目浏览/审核栏)。登录即可读。
    自愈:为空时自动补默认项目 → 作品的项目下拉永不为空,作品不会因「没项目可选」而上传失败。"""
    _require_auth(user)
    deps.ensure_default_project()
    return {"projects": [_project_out(p) for p in deps.project_repo.list()]}


@router.post("/admin/projects")
def add_project(body: schemas.ProjectIn, user: dict = Depends(_user)):
    """新建作品项目。管理项目/规则复用 audit.rules 权限。名字不能空/重复。"""
    _require_perm(user, "audit.rules")
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "项目名不能为空。")
    if deps.project_repo.get_by_name(name) is not None:
        raise HTTPException(409, "项目名已存在。")
    p = Project(id=next_id_str(), name=name, created_by=user["id"], created_ms=int(time.time() * 1000))
    deps.project_repo.add(p)
    return _project_out(p)


@router.delete("/admin/projects/{project_id}")
def delete_project(project_id: str, user: dict = Depends(_user)):
    """删除项目:项目下还有作品(Material.project_id==id)则禁止删除;否则连带删它的规则。"""
    _require_perm(user, "audit.rules")
    if deps.project_repo.get(project_id) is None:
        return {"deleted": project_id}
    if len(deps.project_repo.list()) <= 1:                 # 守最后一个:作品必须有项目可归属,不能删到零
        raise HTTPException(400, "至少保留一个项目;作品需要归属项目。")
    _, n = deps.get_library_service().all(project_id=project_id, limit=0)
    if n > 0:
        raise HTTPException(400, f"该项目下还有 {n} 个作品,请先处理这些作品再删除项目。")
    for r in deps.rule_repo.list():                        # 连带删该项目的规则
        if getattr(r, "project_id", "") == project_id:
            deps.rule_repo.delete(r.id, by=user["id"])
    deps.project_repo.delete(project_id)
    return {"deleted": project_id}


# ── 作品审核记录(管理员):只作品,按项目分组,按提交时间(AuditTask.created_ms)区间筛 ──
def _work_out(task: AuditTask) -> dict:
    m = deps.material_repo.get(task.material_id) if task.material_id else None
    status = getattr(m.audit_status, "value", m.audit_status) if m else (task.verdict or "review")
    rejects = list(getattr(m, "reject_events", []) or []) if m else []
    link = ""
    if m and m.oss_key:
        try:
            link = deps.storage.download_url(m.oss_key)
        except Exception:
            link = ""
    return {"task_id": task.id, "name": task.name, "owner_name": _owner_name(task.owner_id),
            "created_ms": task.created_ms, "status": status,
            "reject_count": len(rejects), "reject_events": rejects,
            "report_id": task.report_id, "project_id": getattr(task, "project_id", ""),
            "download_url": link}


def _collect_works(from_ms, to_ms, status):
    """所有作品(video_kind=work)的审核记录,按提交时间区间 + 最终状态筛,按时间倒序。"""
    out = []
    for t in deps.task_repo.list_all():
        if getattr(t, "video_kind", "material") != "work":
            continue
        if from_ms is not None and t.created_ms < from_ms:
            continue
        if to_ms is not None and t.created_ms >= to_ms:      # 半开区间 [from, to)
            continue
        w = _work_out(t)
        if status is not None and w["status"] != status:
            continue
        out.append(w)
    out.sort(key=lambda w: w["created_ms"], reverse=True)
    return out


def _fmt_local(ms, tz_offset_min, date_only=False):
    """epoch 毫秒 → 浏览器本地时间字符串(前端传 getTimezoneOffset(),国内=-480)。"""
    if not ms:
        return ""
    local = (int(ms) - int(tz_offset_min) * 60000) / 1000.0
    return time.strftime("%Y%m%d" if date_only else "%Y-%m-%d %H:%M", time.gmtime(local))


@router.get("/works")
def list_works(from_ms: int | None = None, to_ms: int | None = None,
               status: str | None = None, user: dict = Depends(_user)):
    """作品审核记录,按项目分组。可选提交时间区间 [from_ms, to_ms) + 最终状态。仅管理员。"""
    _require_perm(user, "materials.audit")
    works = _collect_works(from_ms, to_ms, _check_status(status))
    groups: dict[str, dict] = {}
    for w in works:
        pid = w["project_id"]
        g = groups.get(pid)
        if g is None:
            proj = deps.project_repo.get(pid)
            g = groups[pid] = {"project_id": pid, "project_name": (proj.name if proj else "(未归属)"),
                               "count": 0, "works": []}
        g["works"].append(w)
        g["count"] += 1
    return {"groups": list(groups.values()), "total": len(works)}


@router.get("/works/export.xlsx")
def export_works(from_ms: int | None = None, to_ms: int | None = None, status: str | None = None,
                 tz_offset_min: int = 0, user: dict = Depends(_user)):
    """导出作品审核记录为 .xlsx。列一行内容,退回原因合并到一格。仅管理员。"""
    _require_perm(user, "materials.audit")
    import io
    from urllib.parse import quote
    from fastapi.responses import StreamingResponse
    from openpyxl import Workbook
    works = _collect_works(from_ms, to_ms, _check_status(status))
    st_cn = {"pass": "通过", "block": "退回", "review": "待复核"}
    wb = Workbook()
    ws = wb.active
    ws.title = "作品审核记录"
    ws.append(["项目", "作品名称", "上传者", "提交时间", "最终状态", "退回次数", "退回原因", "素材链接"])
    for w in works:
        proj = deps.project_repo.get(w["project_id"])
        reasons = "; ".join(f"{i + 1}) {(e.get('reason') or '')[:60]}"
                            for i, e in enumerate(w["reject_events"]))
        ws.append([proj.name if proj else "", w["name"], w["owner_name"],
                   _fmt_local(w["created_ms"], tz_offset_min), st_cn.get(w["status"], w["status"]),
                   w["reject_count"], reasons, w["download_url"]])
    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    fname = f"作品审核记录-{_fmt_local(to_ms or int(time.time() * 1000), tz_offset_min, date_only=True)}.xlsx"
    return StreamingResponse(
        bio, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"})
