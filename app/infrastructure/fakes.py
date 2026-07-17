"""infra 层假实现(本地/测试用,先不接真阿里云)。实现 domain 端口 → infra→domain。
真实现(OSS/DashScope Qwen-VL/内容安全/pgvector)后续替换,service 无需改动。"""
from __future__ import annotations
import hashlib
import hmac
import time
from typing import Optional
from app.config import settings
from app.domain.models import (
    Material, MaterialCandidate, MaterialType, AuditStatus, User,
    TextSegment, TextSourceType, AuditRule, AuditTask,
)
from app.domain.query import MaterialQuery, paginate


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

    def put_fileobj(self, oss_key: str, fileobj) -> None:
        """流式上传:从 file-like 对象读取并存储。"""
        self._keys.add(oss_key)

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

    def list(self) -> list[User]:
        return list(self._by_id.values())

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
    """HMAC 签名 token:`<uid>.<exp>.<sig>`。无密钥无法伪造(修复"任意伪造 admin")。
    真实现可换成 JWT(python-jose);接口不变。"""

    def __init__(self, secret: Optional[str] = None, ttl: Optional[int] = None) -> None:
        self._secret = (secret or settings.token_secret).encode()
        self._ttl = ttl if ttl is not None else settings.token_ttl_seconds

    def _sign(self, msg: str) -> str:
        return hmac.new(self._secret, msg.encode(), hashlib.sha256).hexdigest()

    def issue(self, user_id: str) -> str:
        exp = int(time.time()) + self._ttl
        msg = f"{user_id}.{exp}"
        return f"{msg}.{self._sign(msg)}"

    def verify(self, token: str) -> Optional[str]:
        try:
            uid, exp, sig = token.rsplit(".", 2)
        except ValueError:
            return None
        if not hmac.compare_digest(sig, self._sign(f"{uid}.{exp}")):
            return None  # 签名不符 → 伪造
        try:
            if int(exp) < int(time.time()):
                return None  # 已过期
        except ValueError:
            return None
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
    def describe_image(self, url: str) -> str:
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

    def list_for(self, owner_id: str) -> list[AuditTask]:
        return sorted((t for t in self._tasks.values() if t.owner_id == owner_id),
                      key=lambda t: t.created_ms, reverse=True)

    def list_all(self) -> list[AuditTask]:
        return sorted(self._tasks.values(), key=lambda t: t.created_ms, reverse=True)
