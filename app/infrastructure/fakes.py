"""infra 层假实现(本地/测试用,先不接真阿里云)。实现 domain 端口 → infra→domain。
真实现(OSS/DashScope Qwen-VL/内容安全/pgvector)后续替换,service 无需改动。"""
from __future__ import annotations
import hashlib
import hmac
import secrets
import threading
import time
from typing import Optional
from app.config import settings
from app.domain.models import (
    Material, MaterialCandidate, MaterialType, AuditStatus, User,
    TextSegment, TextSourceType, AuditRule, AuditTask,
    MaterialSubmission,
)
from app.domain.query import MaterialQuery, paginate
from app.infrastructure.snowflake import next_id, timestamp_ms


# ── 反解 / embedding ──
class FakeVideoParser:
    def parse_video(self, oss_key: str) -> list[MaterialCandidate]:
        return [MaterialCandidate(
            type=MaterialType.IMAGE, thumb=f"{oss_key}#frame1",
            source_timecode=1.0, description="frame at 1s",
        )]


class FakeEmbedder:
    def embed(self, candidate: MaterialCandidate) -> list[float]:
        return [0.1] * 8


class FakeQueryEmbedder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed_text(self, text: str) -> list[float]:
        self.calls.append(text)
        return [0.1] * 8


# ── 审核器 ──
class FakePassAuditor:
    def audit(self, content) -> str:
        return "pass"


class FakeBlockAuditor:
    def audit(self, content) -> str:
        return "block"


class TimeoutAuditor:
    def audit(self, content) -> str:
        raise TimeoutError("审核超时")


# ── 仓储 ──
class InMemoryMaterialRepo:
    def __init__(self) -> None:
        self.items: list[Material] = []

    def save(self, material: Material) -> None:
        # 幂等:同 id 覆盖,否则追加(支持审核写回)
        for i, m in enumerate(self.items):
            if m.id == material.id:
                self.items[i] = material
                return
        self.items.append(material)

    def get(self, material_id: str) -> Optional[Material]:
        return next((m for m in self.items if m.id == material_id), None)

    def delete(self, material_id: str) -> None:
        self.items = [m for m in self.items if m.id != material_id]

    def list(self) -> list[Material]:
        return list(self.items)

    def query(self, spec: MaterialQuery) -> tuple[list[Material], int]:
        return paginate(self.items, spec)

    def by_content_hash(self, owner_id: str, content_hash: str) -> Optional[Material]:
        if not content_hash:
            return None
        return next((m for m in self.items
                     if m.owner_id == owner_id and m.content_hash == content_hash), None)

    def search(self, query_text: str, only_pass: bool = True) -> list[Material]:
        pool = [m for m in self.items if (not only_pass or m.audit_status == AuditStatus.PASS)]

        def score(m: Material) -> float:
            hay = " ".join([m.thumb, m.description, m.ai_summary, " ".join(m.ai_emotions or []),
                            m.ai_atmosphere, " ".join(m.ai_scenarios or []), " ".join(m.tags or [])])
            return 1.0 if (query_text and query_text in hay) else 0.0

        return sorted(pool, key=score, reverse=True)


# ── OSS 存储 ──
class FakeStorage:
    def __init__(self) -> None:
        self._keys: set[str] = set()

    def put(self, oss_key: str, data: bytes = b"") -> None:
        self._keys.add(oss_key)

    def put_fileobj(self, oss_key: str, fileobj, progress_callback=None) -> None:
        """流式上传:从 file-like 对象读取并存储。"""
        self._keys.add(oss_key)
        if progress_callback:
            try:
                pos = fileobj.tell()
                fileobj.seek(0, 2)
                total = fileobj.tell()
                fileobj.seek(pos)
            except Exception:
                total = 0
            progress_callback(total, total)

    def download_to_file(self, oss_key: str, path: str) -> None:
        with open(path, "wb") as target:
            target.write(b"")

    def signed_url(self, oss_key: str) -> str:
        return f"https://oss.fake/{oss_key}?Expires=3600&Signature=xyz"

    def download_url(self, oss_key: str) -> str:
        return f"https://oss.fake/{oss_key}?Expires=3600&Signature=xyz&response-content-disposition=attachment"

    def exists(self, oss_key: str) -> bool:
        return oss_key in self._keys

    def delete(self, oss_key: str) -> None:
        self._keys.discard(oss_key)

    def snapshot_frame(self, video_key: str, ms: int, dest_key: str) -> bool:
        self._keys.add(dest_key)
        return True

    def video_duration_ms(self, oss_key: str):
        return 8000  # 假实现:固定 8 秒

    def snapshot_url(self, oss_key: str, ms: int = 1000) -> str:
        return f"https://oss.fake/{oss_key}?x-oss-process=video/snapshot,t_{ms}"


class _InMemoryJobLock:
    def __init__(self, coordinator, job_id: str) -> None:
        self._coordinator = coordinator
        self._job_id = job_id

    def release(self) -> None:
        with self._coordinator._lock:
            self._coordinator._held.discard(self._job_id)


class InMemoryJobCoordinator:
    """测试用的 Redis 协调器替身。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._held: set[str] = set()
        self._statuses: dict[str, dict] = {}

    def acquire(self, job_id: str, *, timeout_seconds: int):
        del timeout_seconds
        with self._lock:
            if job_id in self._held:
                return None
            self._held.add(job_id)
            return _InMemoryJobLock(self, job_id)

    def set_status(self, job_id: str, status: dict, *, ttl_seconds: int) -> None:
        del ttl_seconds
        with self._lock:
            self._statuses[job_id] = dict(status)

    def get_status(self, job_id: str) -> dict | None:
        with self._lock:
            value = self._statuses.get(job_id)
            return dict(value) if value else None


# ── 向量索引(F4)──
class InMemoryVectorIndex:
    def __init__(self) -> None:
        self._items: dict[str, list[float]] = {}

    def add(self, material_id: str, vector: list[float]) -> None:
        if not vector or not any(vector):
            return  # 与真 pgvector 契约一致:空/全零向量不入库(避免污染语义近邻)
        self._items[material_id] = vector

    def query(self, vector: list[float], k: int = 10) -> list[str]:
        return list(self._items.keys())[:k]

    def query_scored(self, vector: list[float], k: int = 10) -> list[tuple[str, float]]:
        return [(mid, 0.0) for mid in list(self._items.keys())[:k]]  # 假实现:距离恒 0(全在阈值内)

    def size(self) -> int:
        return len(self._items)


# ── 用户 / 密码 / token(F7)──
class InMemoryUserRepo:
    def __init__(self) -> None:
        self._by_name: dict[str, User] = {}
        self._by_id: dict[str, User] = {}

    def save(self, user: User) -> None:
        self._by_name[user.name] = user
        self._by_id[user.id] = user

    def get_by_name(self, name: str) -> Optional[User]:
        return self._by_name.get(name)

    def get(self, user_id: str) -> Optional[User]:
        return self._by_id.get(user_id)

    def list(self, q: str = "", role: str = "", offset: int = 0,
             limit: int | None = None) -> list[User]:
        key = (q or "").lower()
        rows = [u for u in self._by_id.values()
                if (not key or key in u.name.lower()) and (not role or u.role == role)]
        rows.sort(key=lambda u: (u.role != "admin", u.name.lower(), u.id))
        return rows[offset:] if limit is None else rows[offset:offset + limit]

    def count(self, q: str = "", role: str = "") -> int:
        return len(self.list(q=q, role=role))

    def delete(self, user_id: str) -> None:
        u = self._by_id.pop(user_id, None)
        if u is not None:
            self._by_name.pop(u.name, None)


class InMemoryFavoriteRepo:
    def __init__(self) -> None:
        self._pairs: set[tuple[str, str]] = set()

    def add(self, user_id: str, material_id: str) -> None:
        self._pairs.add((user_id, material_id))

    def remove(self, user_id: str, material_id: str) -> None:
        self._pairs.discard((user_id, material_id))

    def material_ids(self, user_id: str) -> set[str]:
        return {mid for (uid, mid) in self._pairs if uid == user_id}

    def has(self, user_id: str, material_id: str) -> bool:
        return (user_id, material_id) in self._pairs


class FakeHasher:
    _SALT = "s3cr3t"

    def hash(self, password: str) -> str:
        return hashlib.sha256((self._SALT + password).encode()).hexdigest()

    def verify(self, password: str, pwd_hash: str) -> bool:
        return self.hash(password) == pwd_hash


class FakeTokenIssuer:
    """HMAC 签名 token:`<uid>.<exp>.<nonce>.<sig>`。无密钥无法伪造。

    校验仍兼容历史的 ``<uid>.<exp>.<sig>`` token，保证平滑升级。
    真实现可换成 JWT(python-jose);接口不变。"""

    def __init__(self, secret: Optional[str] = None, ttl: Optional[int] = None) -> None:
        self._secret = (secret or settings.token_secret).encode()
        self._ttl = ttl if ttl is not None else settings.token_ttl_seconds

    def _sign(self, msg: str) -> str:
        return hmac.new(self._secret, msg.encode(), hashlib.sha256).hexdigest()

    def issue(self, user_id: str) -> str:
        exp = int(time.time()) + self._ttl
        msg = f"{user_id}.{exp}.{secrets.token_urlsafe(16)}"
        return f"{msg}.{self._sign(msg)}"

    def verify(self, token: str) -> Optional[str]:
        try:
            parts = token.rsplit(".", 3)
            if len(parts) == 4:
                uid, exp, nonce, sig = parts
                msg = f"{uid}.{exp}.{nonce}"
            elif len(parts) == 3:
                uid, exp, sig = parts
                msg = f"{uid}.{exp}"
            else:
                return None
        except ValueError:
            return None
        if not hmac.compare_digest(sig, self._sign(msg)):
            return None  # 签名不符 → 伪造
        try:
            if int(exp) < int(time.time()):
                return None  # 已过期
        except ValueError:
            return None
        return uid


class RotatingRefreshTokenIssuer:
    """签发并轮换 refresh token。

    refresh token 使用独立密钥和带随机数的格式，不能冒充 access token；
    已消费 token 在有效期内会被拒绝，未消费 token 在服务重启后仍可正常刷新。
    """

    def __init__(self, secret: Optional[str] = None, ttl: Optional[int] = None) -> None:
        configured_secret = settings.refresh_token_secret or f"{settings.token_secret}:refresh"
        self._secret = (secret or configured_secret).encode()
        self._ttl = ttl if ttl is not None else settings.refresh_token_ttl_seconds
        self._consumed: dict[str, int] = {}
        self._lock = threading.Lock()

    def _sign(self, msg: str) -> str:
        return hmac.new(self._secret, msg.encode(), hashlib.sha256).hexdigest()

    def _parse(self, token: str) -> tuple[str, int] | None:
        try:
            uid, exp_raw, nonce, signature = token.rsplit(".", 3)
            exp = int(exp_raw)
        except (TypeError, ValueError):
            return None
        msg = f"{uid}.{exp_raw}.{nonce}"
        if not hmac.compare_digest(signature, self._sign(msg)):
            return None
        if exp < int(time.time()):
            return None
        return uid, exp

    def issue(self, user_id: str) -> str:
        exp = int(time.time()) + self._ttl
        nonce = secrets.token_urlsafe(24)
        msg = f"{user_id}.{exp}.{nonce}"
        return f"{msg}.{self._sign(msg)}"

    def verify(self, token: str) -> Optional[str]:
        parsed = self._parse(token)
        return parsed[0] if parsed is not None else None

    def consume(self, token: str) -> Optional[str]:
        parsed = self._parse(token)
        if parsed is None:
            return None
        uid, exp = parsed
        now = int(time.time())
        with self._lock:
            for consumed_token, consumed_exp in list(self._consumed.items()):
                if consumed_exp < now:
                    self._consumed.pop(consumed_token, None)
            if token in self._consumed:
                return None
            self._consumed[token] = exp
        return uid


# ── RBAC / 审计(F8)──
class InMemoryRbac:
    def __init__(self) -> None:
        self._map: dict[str, set[str]] = {}
        self._user_map: dict[str, set[str]] = {}

    def permissions_of(self, role: str) -> set[str]:
        return set(self._map.get(role, set()))

    def grant(self, role: str, permission: str) -> None:
        self._map.setdefault(role, set()).add(permission)

    def revoke(self, role: str, permission: str) -> None:
        self._map.get(role, set()).discard(permission)

    def user_permissions(self, user_id: str) -> set[str]:
        return set(self._user_map.get(user_id, set()))

    def user_permissions_for(self, user_ids: set[str]) -> dict[str, set[str]]:
        return {user_id: set(self._user_map.get(user_id, set())) for user_id in user_ids}

    def all_user_permissions(self) -> dict[str, set[str]]:
        return {user_id: set(permissions) for user_id, permissions in self._user_map.items()}

    def set_user_permissions(self, user_id: str, permissions: set[str]) -> None:
        self._user_map[user_id] = set(permissions)


class ListAuditLog:
    def __init__(self) -> None:
        self.events: list[str] = []

    def record(self, event: str) -> None:
        self.events.append(event)


class InMemoryWhitelistRepo:
    def __init__(self) -> None:
        self._w: set[str] = set()

    def words(self) -> set[str]:
        return set(self._w)

    def list(self) -> list[str]:
        return sorted(self._w)

    def add(self, word: str) -> None:
        w = (word or "").strip()
        if w:
            self._w.add(w)

    def remove(self, word: str) -> None:
        self._w.discard((word or "").strip())


class InMemoryBlockwordRepo:
    """绝对禁词(审核第一波,命中即拦)。"""
    def __init__(self) -> None:
        self._w: set[str] = set()

    def words(self) -> set[str]:
        return set(self._w)

    def list(self) -> list[str]:
        return sorted(self._w)

    def add(self, word: str) -> None:
        w = (word or "").strip()
        if w:
            self._w.add(w)

    def remove(self, word: str) -> None:
        self._w.discard((word or "").strip())


# ── 审核引擎假实现 ──
class FakeTranscriber:
    """假 ASR:返回两段带时间轴的转写(测试/本地用)。"""
    def transcribe(self, url: str) -> list[TextSegment]:
        return [
            TextSegment(source_type=TextSourceType.TRANSCRIPT, text="大家好这是开场白", begin_ms=0, end_ms=2000),
            TextSegment(source_type=TextSourceType.TRANSCRIPT, text="接下来进入正题", begin_ms=2000, end_ms=5000),
        ]


class FakeVisionDescriber:
    def describe_image(self, url: str, hints: str = "") -> str:
        return f"画面内容(假):{url[:40]}"


class FakeArchiver:
    """假物料档案器(豆包 pro 2.1 的占位):默认返回多值情绪/场景样本;可 set_response 编排。"""
    def __init__(self, response=None) -> None:
        self._response = response
        self.calls: list[tuple] = []

    def set_response(self, response) -> None:
        self._response = response

    def tag(self, material_type: str, media_url: str = "", is_video: bool = False,
            text: str = "") -> dict:
        self.calls.append((material_type, media_url, is_video, text))
        if self._response is not None:
            return self._response
        return {"summary": "一条测试物料", "emotions": ["欢快", "搞笑"],
                "scenarios": ["群里活跃气氛时", "需要一个搞笑停顿时"],
                "atmosphere": "轻松", "tags": ["测试", "素材"]}


class FakeTavily:
    """假联网搜索(Tavily 的占位):返回固定简报、记录查询词。测试/本地用,不打真网。"""
    def __init__(self, brief: str = "概述:这是一首广为流传的歌曲,情绪温暖治愈,常配旅行、回忆类短视频。") -> None:
        self._brief = brief
        self.calls: list[str] = []

    def search(self, query: str) -> str:
        self.calls.append(query)
        return self._brief


class FakeLlm:
    """假大模型:可编排返回。默认判 pass。可通过 set_response 指定 chat_json 的返回。"""
    def __init__(self, response: Optional[dict] = None) -> None:
        self._response = response
        self.calls: list[tuple[str, str]] = []

    def set_response(self, response: dict) -> None:
        self._response = response

    def chat_json(self, system: str, user: str) -> dict:
        self.calls.append((system, user))
        if self._response is not None:
            return self._response
        if "时间段" in system or "moment" in system.lower():
            return {"moments_ms": []}
        if "档案" in system or "摘要" in system:  # 物料档案(多值情绪/场景)
            return {"summary": "一条测试物料", "emotions": ["平静", "中性"],
                    "scenarios": ["通用场景一", "需要铺垫时"], "atmosphere": "中性",
                    "tags": ["测试", "素材"]}
        return {"decision": "pass", "triggered_rule_ids": [], "reason": "无问题"}


class InMemoryAuditRuleRepo:
    def __init__(self) -> None:
        self._rules: dict[str, AuditRule] = {}

    def add(self, rule: AuditRule, by: str = "") -> None:
        self._rules[rule.id] = rule

    def delete(self, rule_id: str, by: str = "") -> None:
        self._rules.pop(rule_id, None)

    def list(self) -> list[AuditRule]:
        return list(self._rules.values())

    def list_for(self, source_type: str, project_id: str = "") -> list[AuditRule]:
        return [r for r in self._rules.values() if r.applies_to(source_type, project_id)]


class InMemoryProjectRepo:
    def __init__(self) -> None:
        self._p: dict = {}

    def add(self, project) -> None:
        self._p[project.id] = project

    def get(self, project_id: str):
        return self._p.get(project_id)

    def get_by_name(self, name: str):
        n = (name or "").strip()
        return next((p for p in self._p.values() if p.name == n), None)

    def delete(self, project_id: str) -> None:
        self._p.pop(project_id, None)

    def list(self) -> list:
        return sorted(self._p.values(), key=lambda p: p.created_ms)


class InMemoryMaterialSubmissionRepo:
    def __init__(self) -> None:
        self._items: dict[str, MaterialSubmission] = {}
        self._permissions: dict[tuple[str, str], str] = {}
        self._operations: dict[str, list[dict]] = {}

    def add(self, submission: MaterialSubmission, by: str = "") -> None:
        now = str(int(time.time() * 1000))
        previous = self._items.get(submission.id)
        submission.created_time = previous.created_time if previous else (submission.created_time or now)
        submission.updated_by = by or submission.updated_by or submission.created_by
        submission.updated_time = now
        self._items[submission.id] = submission

    def get(self, submission_id: str):
        item = self._items.get(submission_id)
        return item if item and item.del_flag == 0 else None

    def set_decoded_oss_key_if_current(self, submission_id: str, source_oss_key: str,
                                       decoded_oss_key: str, by: str = "") -> bool:
        item = self._items.get(submission_id)
        if (item is None or item.del_flag != 0 or item.oss_del_flag != 0
                or item.oss_key != source_oss_key or item.decoded_oss_key):
            return False
        item.decoded_oss_key = decoded_oss_key
        item.requires_decode = 1
        item.updated_by = by
        item.updated_time = str(int(time.time() * 1000))
        return True

    def get_deleted(self, submission_id: str):
        item = self._items.get(submission_id)
        return item if item and item.del_flag != 0 and item.oss_del_flag == 0 else None

    def delete(self, submission_id: str, by: str = "") -> bool:
        item = self._items.get(submission_id)
        if item is None or item.del_flag != 0 or item.oss_del_flag != 0:
            return False
        item.del_flag = next_id()
        item.updated_by = by
        item.updated_time = str(int(time.time() * 1000))
        return True

    def restore(self, submission_id: str, by: str = "") -> bool:
        item = self.get_deleted(submission_id)
        if item is None:
            return False
        item.del_flag = 0
        item.updated_by = by
        item.updated_time = str(int(time.time() * 1000))
        return True

    def list_expired_deleted(self, cutoff_ms: int) -> list[MaterialSubmission]:
        return sorted(
            (item for item in self._items.values()
             if item.del_flag != 0 and item.oss_del_flag == 0
             and timestamp_ms(item.del_flag) < cutoff_ms),
            key=lambda item: item.del_flag,
        )

    def mark_oss_deleted(self, submission_id: str, by: str = "") -> bool:
        item = self.get_deleted(submission_id)
        if item is None:
            return False
        item.oss_del_flag = next_id()
        item.updated_by = by
        item.updated_time = str(int(time.time() * 1000))
        return True

    def record_operation(self, submission_id: str, action: str, by: str,
                         changes: list[dict]) -> None:
        self._operations.setdefault(submission_id, []).append({
            "id": str(int(time.time() * 1000000)),
            "action": action,
            "operator_id": by,
            "operation_time": str(int(time.time() * 1000)),
            "changes": [dict(item) for item in changes],
        })

    def list_operations(self, submission_id: str) -> list[dict]:
        return [dict(item) for item in reversed(self._operations.get(submission_id, []))]

    def permission_of(self, submission_id: str, user_id: str) -> str:
        return self._permissions.get((submission_id, user_id), "")

    def permissions_for(self, submission_id: str) -> dict[str, str]:
        return {uid: value for (sid, uid), value in self._permissions.items() if sid == submission_id}

    def replace_permissions(self, submission_id: str, grants: dict[str, str], by: str = "") -> None:
        self._permissions = {k: v for k, v in self._permissions.items() if k[0] != submission_id}
        for user_id, permission_type in grants.items():
            if user_id != "admin" and permission_type in ("read", "read_edit"):
                self._permissions[(submission_id, user_id)] = permission_type

    def permissions_for_user(self, user_id: str) -> dict[str, str]:
        return {sid: value for (sid, uid), value in self._permissions.items() if uid == user_id}

    def replace_user_permissions(self, user_id: str, grants: dict[str, str], by: str = "") -> int:
        before = self.permissions_for_user(user_id)
        desired = {str(sid): value for sid, value in grants.items()
                   if str(sid) in self._items and value in ("read", "read_edit")}
        for submission in self._items.values():
            if submission.created_by == user_id:
                desired[submission.id] = "read_edit"
        self._permissions = {key: value for key, value in self._permissions.items() if key[1] != user_id}
        for submission_id, permission_type in desired.items():
            self._permissions[(submission_id, user_id)] = permission_type
        return sum(1 for sid in set(before) | set(desired) if before.get(sid, "") != desired.get(sid, ""))
    def submission_ids_for_user(self, user_id: str, require_edit: bool = False) -> set[str]:
        allowed = {"read_edit"} if require_edit else {"read", "read_edit"}
        return {sid for (sid, uid), value in self._permissions.items() if uid == user_id and value in allowed}

    def list_upload_account_names(self, keyword: str = "", limit: int | None = None) -> list[str]:
        items = []
        seen: set[str] = set()
        key = (keyword or "").lower()
        for s in sorted(self._items.values(), key=lambda x: int(x.id), reverse=True):
            name = (s.upload_account_name or "").strip()
            if not name:
                continue
            if key and key not in name.lower():
                continue
            if name in seen:
                continue
            seen.add(name)
            items.append(name)
            if limit is not None and len(items) >= limit:
                break
        return items

    def list_drama_names(self, keyword: str = "", limit: int | None = None) -> list[str]:
        items = []
        seen: set[str] = set()
        key = (keyword or "").lower()
        for s in sorted(self._items.values(), key=lambda x: int(x.id), reverse=True):
            name = (s.drama_name or "").strip()
            if not name:
                continue
            if key and key not in name.lower():
                continue
            if name in seen:
                continue
            seen.add(name)
            items.append(name)
            if limit is not None and len(items) >= limit:
                break
        return items

    def aggregate_uploads_by_drama_names(self, tasks: list[tuple[str, str]]) -> dict[str, dict]:
        result: dict[str, dict] = {}
        live_items = [item for item in self._items.values() if item.del_flag == 0]
        for task_id, drama_name in tasks:
            key = (drama_name or "").strip().casefold()
            matched = [item for item in live_items
                       if key and (key in (item.drama_name or "").casefold()
                                   or ((item.drama_name or "").strip()
                                       and (item.drama_name or "").casefold() in key))]
            result[str(task_id)] = {
                "team_names": sorted({item.team_name.strip() for item in matched
                                      if item.team_name.strip()}),
                "upload_count": len(matched),
                "can_upload_count": sum(item.can_upload_status == 1 for item in matched),
                "publish_success_count": sum(item.publish_status == 1 for item in matched),
            }
        return result

    def list_drama_names_by_team(self, team_name: str) -> list[str]:
        key = (team_name or "").strip().casefold()
        if not key:
            return []
        return sorted({
            item.drama_name.strip() for item in self._items.values()
            if item.del_flag == 0 and key in item.team_name.casefold()
            and item.drama_name.strip()
        })

    def list(self, team_name: str = "", drama_name: str = "", video_file_name: str = "",
             title_name: str = "", can_upload_status: int | None = None,
             can_upload_status_empty: bool = False,
             designated_upload_account_name: str = "", upload_account_name: str = "",
             created_by: str = "", publish_status: int | None = None,
             publish_status_empty: bool = False,
             offset: int = 0, limit: int | None = None,
             recycle_bin: bool = False, visible_to_user_id: str = "",
             exclude_ids: set[str] | None = None,
             sort_by: str = "", sort_order: str = "") -> list[MaterialSubmission]:
        items = sorted((s for s in self._items.values()
                        if (s.del_flag != 0 and s.oss_del_flag == 0) == recycle_bin
                        and (recycle_bin or s.del_flag == 0)), key=lambda s: int(s.id))
        if team_name:
            items = [s for s in items if team_name.lower() in s.team_name.lower()]
        if drama_name:
            items = [s for s in items if drama_name.lower() in s.drama_name.lower()]
        if video_file_name:
            items = [s for s in items if video_file_name.lower() in s.video_file_name.lower()]
        if title_name:
            items = [s for s in items if title_name.lower() in s.title_name.lower()]
        if can_upload_status is not None:
            items = [s for s in items if s.can_upload_status == can_upload_status]
        elif can_upload_status_empty:
            items = [s for s in items if s.can_upload_status is None]
        if designated_upload_account_name:
            items = [s for s in items if s.designated_upload_account_name == designated_upload_account_name]
        if upload_account_name:
            items = [s for s in items if s.upload_account_name == upload_account_name]
        if created_by:
            items = [s for s in items if s.created_by == created_by]
        if publish_status is not None:
            items = [s for s in items if s.publish_status == publish_status]
        elif publish_status_empty:
            items = [s for s in items if s.publish_status is None]
        if visible_to_user_id:
            visible_ids = self.submission_ids_for_user(visible_to_user_id)
            items = [s for s in items if s.id in visible_ids]
        if exclude_ids:
            items = [s for s in items if s.id not in exclude_ids]
        if sort_by:
            if sort_by not in {"team_name", "upload_date", "created_time"}:
                raise ValueError("非法素材提报排序字段")
            if sort_order not in {"asc", "desc"}:
                raise ValueError("非法素材提报排序方向")
            populated = [s for s in items if str(getattr(s, sort_by, "") or "").strip()]
            empty = [s for s in items if not str(getattr(s, sort_by, "") or "").strip()]
            populated.sort(
                key=lambda s: str(getattr(s, sort_by, "") or "").strip(),
                reverse=sort_order == "desc",
            )
            items = populated + empty
        return items if limit is None else items[offset:offset + limit]

    def count(self, team_name: str = "", drama_name: str = "", video_file_name: str = "",
              title_name: str = "", can_upload_status: int | None = None,
              can_upload_status_empty: bool = False,
              designated_upload_account_name: str = "", upload_account_name: str = "",
              created_by: str = "", publish_status: int | None = None,
              publish_status_empty: bool = False, recycle_bin: bool = False,
              visible_to_user_id: str = "", exclude_ids: set[str] | None = None) -> int:
        return len(self.list(team_name=team_name, drama_name=drama_name,
                             video_file_name=video_file_name, title_name=title_name,
                             can_upload_status=can_upload_status,
                             can_upload_status_empty=can_upload_status_empty,
                             designated_upload_account_name=designated_upload_account_name,
                             upload_account_name=upload_account_name,
                             created_by=created_by,
                             publish_status=publish_status,
                             publish_status_empty=publish_status_empty,
                             recycle_bin=recycle_bin,
                             visible_to_user_id=visible_to_user_id,
                             exclude_ids=exclude_ids))


class InMemoryAuditReportRepo:
    def __init__(self) -> None:
        self._reports: dict = {}

    def save(self, report_id: str, report) -> None:
        self._reports[report_id] = report

    def get(self, report_id: str):
        return self._reports.get(report_id)


class InMemoryAuditTaskRepo:
    def __init__(self) -> None:
        self._tasks: dict[str, AuditTask] = {}

    def save(self, task: AuditTask) -> None:
        self._tasks[task.id] = task

    def get(self, task_id: str) -> Optional[AuditTask]:
        return self._tasks.get(task_id)

    def delete(self, task_id: str) -> None:
        self._tasks.pop(task_id, None)

    def list_for(self, owner_id: str, project_id: str = "", name: str = "", offset: int = 0, limit: int | None = None) -> list[AuditTask]:
        tasks = sorted((t for t in self._tasks.values() if t.owner_id == owner_id),
                       key=lambda t: t.created_ms, reverse=True)
        if project_id:
            tasks = [t for t in tasks if getattr(t, "project_id", "") == project_id]
        if name:
            tasks = [t for t in tasks if name.lower() in (getattr(t, "name", "") or "").lower()]
        return tasks if limit is None else tasks[offset:offset + limit]

    def list_all(self, project_id: str = "", name: str = "", offset: int = 0, limit: int | None = None) -> list[AuditTask]:
        tasks = sorted(self._tasks.values(), key=lambda t: t.created_ms, reverse=True)
        if project_id:
            tasks = [t for t in tasks if getattr(t, "project_id", "") == project_id]
        if name:
            tasks = [t for t in tasks if name.lower() in (getattr(t, "name", "") or "").lower()]
        return tasks if limit is None else tasks[offset:offset + limit]

    def count_for(self, owner_id: str, project_id: str = "", name: str = "") -> int:
        tasks = [t for t in self._tasks.values() if t.owner_id == owner_id]
        if project_id:
            tasks = [t for t in tasks if getattr(t, "project_id", "") == project_id]
        if name:
            tasks = [t for t in tasks if name.lower() in (getattr(t, "name", "") or "").lower()]
        return len(tasks)

    def count_all(self, project_id: str = "", name: str = "") -> int:
        tasks = list(self._tasks.values())
        if project_id:
            tasks = [t for t in tasks if getattr(t, "project_id", "") == project_id]
        if name:
            tasks = [t for t in tasks if name.lower() in (getattr(t, "name", "") or "").lower()]
        return len(tasks)


class InMemoryTrainingSetRepo:
    def __init__(self) -> None:
        self._ts: dict = {}

    def add(self, ts, by: str = "") -> None:
        self._ts[ts.id] = ts

    def get(self, ts_id: str):
        return self._ts.get(ts_id)

    def get_by_project(self, project_id: str):
        return next((t for t in self._ts.values()
                     if t.project_id == project_id), None)

    def delete(self, ts_id: str, by: str = "") -> None:
        self._ts.pop(ts_id, None)

    def list(self) -> list:
        return list(self._ts.values())


class InMemoryTrainingExampleRepo:
    def __init__(self) -> None:
        self._te: dict = {}

    def add(self, te, by: str = "") -> None:
        self._te[te.id] = te

    def get(self, te_id: str):
        return self._te.get(te_id)

    def delete(self, te_id: str, by: str = "") -> None:
        self._te.pop(te_id, None)

    def list_for_set(self, training_set_id: str) -> list:
        return sorted(
            (e for e in self._te.values()
             if e.training_set_id == training_set_id),
            key=lambda e: e.id)
