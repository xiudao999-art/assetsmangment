"""infra 层假实现(本地/测试用,先不接真阿里云)。实现 domain 端口 → infra→domain。
真实现(OSS/DashScope Qwen-VL/内容安全/pgvector)后续替换,service 无需改动。"""
from __future__ import annotations
import hashlib
import hmac
import time
from typing import Optional
from app.config import settings
from app.domain.models import Material, MaterialCandidate, MaterialType, AuditStatus, User


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

    def search(self, query_text: str, only_pass: bool = True) -> list[Material]:
        pool = [m for m in self.items if (not only_pass or m.audit_status == AuditStatus.PASS)]

        def score(m: Material) -> float:
            hit = query_text and (query_text in m.thumb or query_text in m.description)
            return 1.0 if hit else 0.0  # hybrid:关键词命中加权(真实现再叠向量相似度)

        return sorted(pool, key=score, reverse=True)


# ── OSS 存储 ──
class FakeStorage:
    def __init__(self) -> None:
        self._keys: set[str] = set()

    def put(self, oss_key: str, data: bytes = b"") -> None:
        self._keys.add(oss_key)

    def signed_url(self, oss_key: str) -> str:
        return f"https://oss.fake/{oss_key}?Expires=3600&Signature=xyz"

    def exists(self, oss_key: str) -> bool:
        return oss_key in self._keys

    def delete(self, oss_key: str) -> None:
        self._keys.discard(oss_key)


# ── 向量索引(F4)──
class InMemoryVectorIndex:
    def __init__(self) -> None:
        self._items: dict[str, list[float]] = {}

    def add(self, material_id: str, vector: list[float]) -> None:
        self._items[material_id] = vector

    def query(self, vector: list[float], k: int = 10) -> list[str]:
        return list(self._items.keys())[:k]

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

    def permissions_of(self, role: str) -> set[str]:
        return set(self._map.get(role, set()))

    def grant(self, role: str, permission: str) -> None:
        self._map.setdefault(role, set()).add(permission)

    def revoke(self, role: str, permission: str) -> None:
        self._map.get(role, set()).discard(permission)


class ListAuditLog:
    def __init__(self) -> None:
        self.events: list[str] = []

    def record(self, event: str) -> None:
        self.events.append(event)
