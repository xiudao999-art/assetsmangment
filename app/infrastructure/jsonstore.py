"""JSON 文件持久化仓储(infra→domain)。
把物料/用户/收藏/权限落到 {data_dir}/state.json,容器重启不丢。
写操作后原子落盘(tmp + os.replace);启动时加载。接口与 fakes 里的内存仓储完全一致,
所以 deps 里换成它 = 只改组合根,service/domain 不动(端口未变)。
向量索引/视频 job 仍留内存(可重建、且属瞬态),不影响收藏/物料持久化。"""
from __future__ import annotations
import json
import os
import threading
from dataclasses import asdict
from typing import Optional

from app.domain.models import (
    Material, MaterialType, AuditStatus, User,
    AuditRule, AuditReport, TextSegment, TextSourceType,
    AuditTask, JobStatus,
)
from app.domain.query import MaterialQuery, paginate


class Store:
    """单一状态容器 + 原子落盘。所有 Json* 仓储共享一个 Store 实例。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        self.materials: dict[str, Material] = {}
        self.users: dict[str, User] = {}
        self.favorites: set[tuple[str, str]] = set()
        self.roles: dict[str, set[str]] = {}
        self.user_perms: dict[str, set[str]] = {}   # 用户级授权(叠加在角色默认权限之上)
        self.content_whitelist: set[str] = set()     # 内容安全白名单(命中词即便阿里云判违规也放行)
        self.rules: dict[str, AuditRule] = {}
        self.audit_reports: dict[str, AuditReport] = {}
        self.audit_tasks: dict[str, AuditTask] = {}
        self._load()

    # ── 序列化 ──
    @staticmethod
    def _mat_to_dict(m: Material) -> dict:
        d = asdict(m)
        d["type"] = m.type.value
        d["audit_status"] = m.audit_status.value
        return d

    @staticmethod
    def _mat_from_dict(d: dict) -> Material:
        d = dict(d)
        d["type"] = MaterialType(d["type"])
        d["audit_status"] = AuditStatus(d["audit_status"])
        return Material(**d)

    @staticmethod
    def _report_to_dict(r: AuditReport) -> dict:
        return {
            "verdict": r.verdict.value,
            "summary": r.summary,
            "triggered": r.triggered,
            "segments": [{"source_type": s.source_type.value, "text": s.text,
                          "begin_ms": s.begin_ms, "end_ms": s.end_ms,
                          "frame_oss_key": s.frame_oss_key} for s in r.segments],
        }

    @staticmethod
    def _report_from_dict(d: dict) -> AuditReport:
        segs = [TextSegment(source_type=TextSourceType(s["source_type"]), text=s["text"],
                            begin_ms=s.get("begin_ms"), end_ms=s.get("end_ms"),
                            frame_oss_key=s.get("frame_oss_key", "")) for s in d.get("segments", [])]
        return AuditReport(verdict=AuditStatus(d["verdict"]), segments=segs,
                           triggered=d.get("triggered", []), summary=d.get("summary", ""))

    @staticmethod
    def _task_to_dict(t: AuditTask) -> dict:
        d = asdict(t)
        d["material_type"] = t.material_type.value
        d["status"] = t.status.value
        return d

    @staticmethod
    def _task_from_dict(d: dict) -> AuditTask:
        d = dict(d)
        d["material_type"] = MaterialType(d["material_type"])
        # 重启会杀掉后台线程:未结束的任务(pending/running)标为 failed,页面不再假装"审核中"
        st = JobStatus(d["status"])
        if st in (JobStatus.PENDING, JobStatus.RUNNING):
            st = JobStatus.FAILED
            d["error"] = d.get("error") or "服务重启中断,请重新提交"
        d["status"] = st
        return AuditTask(**d)

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as f:
            d = json.load(f)
        for m in d.get("materials", []):
            mat = self._mat_from_dict(m)
            self.materials[mat.id] = mat
        for u in d.get("users", []):
            user = User(**u)
            self.users[user.id] = user
        self.favorites = {tuple(x) for x in d.get("favorites", [])}
        self.roles = {k: set(v) for k, v in d.get("roles", {}).items()}
        self.user_perms = {k: set(v) for k, v in d.get("user_perms", {}).items()}
        self.content_whitelist = set(d.get("content_whitelist", []))
        for r in d.get("rules", []):
            rule = AuditRule(**r)
            self.rules[rule.id] = rule
        for rid, rep in d.get("audit_reports", {}).items():
            self.audit_reports[rid] = self._report_from_dict(rep)
        for t in d.get("audit_tasks", []):
            task = self._task_from_dict(t)
            self.audit_tasks[task.id] = task

    def save(self) -> None:
        with self._lock:
            payload = {
                "materials": [self._mat_to_dict(m) for m in self.materials.values()],
                "users": [asdict(u) for u in self.users.values()],
                "favorites": [list(x) for x in self.favorites],
                "roles": {k: sorted(v) for k, v in self.roles.items()},
                "user_perms": {k: sorted(v) for k, v in self.user_perms.items() if v},
                "content_whitelist": sorted(self.content_whitelist),
                "rules": [asdict(r) for r in self.rules.values()],
                "audit_reports": {rid: self._report_to_dict(rep)
                                  for rid, rep in self.audit_reports.items()},
                "audit_tasks": [self._task_to_dict(t) for t in self.audit_tasks.values()],
            }
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, self.path)  # 原子替换,防写一半损坏


# ── 物料仓储 ──
class JsonMaterialRepo:
    def __init__(self, store: Store) -> None:
        self._s = store

    def save(self, material: Material) -> None:
        self._s.materials[material.id] = material  # 同 id 覆盖(支持审核写回)
        self._s.save()

    def get(self, material_id: str) -> Optional[Material]:
        return self._s.materials.get(material_id)

    def delete(self, material_id: str) -> None:
        self._s.materials.pop(material_id, None)
        self._s.save()

    def list(self) -> list[Material]:
        return list(self._s.materials.values())

    def query(self, spec: MaterialQuery) -> tuple[list[Material], int]:
        # 服务端翻页/筛选:谓词过滤 → 切片前算 total → 切片(未来换真表只改此处为 SQL)
        return paginate(self._s.materials.values(), spec)

    def by_content_hash(self, owner_id: str, content_hash: str) -> Optional[Material]:
        if not content_hash:
            return None
        return next((m for m in self._s.materials.values()
                     if m.owner_id == owner_id and m.content_hash == content_hash), None)

    def search(self, query_text: str, only_pass: bool = True) -> list[Material]:
        pool = [m for m in self._s.materials.values()
                if (not only_pass or m.audit_status == AuditStatus.PASS)]

        def score(m: Material) -> float:
            hay = " ".join([m.thumb, m.description, m.ai_summary, m.ai_emotion,
                            m.ai_atmosphere, m.ai_scene, " ".join(m.tags or [])])
            return 1.0 if (query_text and query_text in hay) else 0.0

        return sorted(pool, key=score, reverse=True)


# ── 用户仓储 ──
class JsonUserRepo:
    def __init__(self, store: Store) -> None:
        self._s = store

    def save(self, user: User) -> None:
        self._s.users[user.id] = user
        self._s.save()

    def get_by_name(self, name: str) -> Optional[User]:
        return next((u for u in self._s.users.values() if u.name == name), None)

    def get(self, user_id: str) -> Optional[User]:
        return self._s.users.get(user_id)

    def list(self) -> list[User]:
        return list(self._s.users.values())

    def delete(self, user_id: str) -> None:
        self._s.users.pop(user_id, None)
        self._s.user_perms.pop(user_id, None)   # 连带清用户级权限
        self._s.save()


# ── 收藏关系 ──
class JsonFavoriteRepo:
    def __init__(self, store: Store) -> None:
        self._s = store

    def add(self, user_id: str, material_id: str) -> None:
        self._s.favorites.add((user_id, material_id))
        self._s.save()

    def remove(self, user_id: str, material_id: str) -> None:
        self._s.favorites.discard((user_id, material_id))
        self._s.save()

    def material_ids(self, user_id: str) -> set[str]:
        return {mid for (uid, mid) in self._s.favorites if uid == user_id}

    def has(self, user_id: str, material_id: str) -> bool:
        return (user_id, material_id) in self._s.favorites


# ── RBAC ──
class JsonRbac:
    def __init__(self, store: Store) -> None:
        self._s = store

    def permissions_of(self, role: str) -> set[str]:
        return set(self._s.roles.get(role, set()))

    def grant(self, role: str, permission: str) -> None:
        self._s.roles.setdefault(role, set()).add(permission)
        self._s.save()

    def revoke(self, role: str, permission: str) -> None:
        self._s.roles.get(role, set()).discard(permission)
        self._s.save()

    def user_permissions(self, user_id: str) -> set[str]:
        return set(self._s.user_perms.get(user_id, set()))

    def set_user_permissions(self, user_id: str, permissions: set[str]) -> None:
        self._s.user_perms[user_id] = set(permissions)
        self._s.save()


# ── 审核规则 ──
class JsonAuditRuleRepo:
    def __init__(self, store: Store) -> None:
        self._s = store

    def add(self, rule: AuditRule) -> None:
        self._s.rules[rule.id] = rule
        self._s.save()

    def delete(self, rule_id: str) -> None:
        self._s.rules.pop(rule_id, None)
        self._s.save()

    def list(self) -> list[AuditRule]:
        return list(self._s.rules.values())

    def list_for(self, source_type: str) -> list[AuditRule]:
        return [r for r in self._s.rules.values() if r.applies_to(source_type)]


# ── 审核报告 ──
class JsonAuditReportRepo:
    def __init__(self, store: Store) -> None:
        self._s = store

    def save(self, report_id: str, report: AuditReport) -> None:
        self._s.audit_reports[report_id] = report
        self._s.save()

    def get(self, report_id: str) -> Optional[AuditReport]:
        return self._s.audit_reports.get(report_id)


# ── 待审核任务 ──
class JsonAuditTaskRepo:
    def __init__(self, store: Store) -> None:
        self._s = store

    def save(self, task: AuditTask) -> None:
        self._s.audit_tasks[task.id] = task  # 同 id 覆盖(状态回写)
        self._s.save()

    def get(self, task_id: str) -> Optional[AuditTask]:
        return self._s.audit_tasks.get(task_id)

    def delete(self, task_id: str) -> None:
        self._s.audit_tasks.pop(task_id, None)
        self._s.save()

    def list_for(self, owner_id: str) -> list[AuditTask]:
        return sorted((t for t in self._s.audit_tasks.values() if t.owner_id == owner_id),
                      key=lambda t: t.created_ms, reverse=True)

    def list_all(self) -> list[AuditTask]:
        return sorted(self._s.audit_tasks.values(), key=lambda t: t.created_ms, reverse=True)


# ── 内容安全白名单 ──
class JsonWhitelistRepo:
    def __init__(self, store: Store) -> None:
        self._s = store

    def words(self) -> set[str]:
        return set(self._s.content_whitelist)

    def list(self) -> list[str]:
        return sorted(self._s.content_whitelist)

    def add(self, word: str) -> None:
        w = (word or "").strip()
        if w:
            self._s.content_whitelist.add(w)
            self._s.save()

    def remove(self, word: str) -> None:
        self._s.content_whitelist.discard((word or "").strip())
        self._s.save()
