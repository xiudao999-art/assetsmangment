"""组合根(Composition Root):在 api 层装配 service + infra。
现用假 infra;Phase 2 换成真阿里云客户端时,只改这里,service/domain 不动。"""
from __future__ import annotations
from app.config import settings
from app.domain.models import User
from app.service.material import MaterialService
from app.service.search import SearchService
from app.service.audit import AuditService
from app.service.video_parsing import VideoParsingService
from app.service.indexing import IndexService
from app.service.user import UserService
from app.service.authorization import AuthorizationService
from app.service.library import LibraryService
from app.infrastructure.fakes import (
    InMemoryMaterialRepo, FakeStorage, FakeQueryEmbedder, FakePassAuditor,
    FakeVideoParser, FakeEmbedder, InMemoryVectorIndex, InMemoryUserRepo,
    FakeHasher, FakeTokenIssuer, InMemoryRbac, ListAuditLog, InMemoryFavoriteRepo,
)

# ── 进程内单例 ──
# 存储:OSS 有密钥用真实现,否则假实现。
if settings.oss_access_key_id and settings.oss_bucket:
    from app.infrastructure.aliyun_oss import OssStorage
    storage = OssStorage()   # 真 OSS
else:
    storage = FakeStorage()

# 物料/用户/收藏/权限:设了 AM_DATA_DIR 就落 JSON 文件(容器重启不丢),否则纯内存。
if settings.data_dir:
    from app.infrastructure.jsonstore import (
        Store, JsonMaterialRepo, JsonUserRepo, JsonFavoriteRepo, JsonRbac,
    )
    _store = Store(f"{settings.data_dir.rstrip('/')}/state.json")
    material_repo = JsonMaterialRepo(_store)
    user_repo = JsonUserRepo(_store)
    favorites = JsonFavoriteRepo(_store)
    rbac = JsonRbac(_store)
else:
    material_repo = InMemoryMaterialRepo()
    user_repo = InMemoryUserRepo()
    favorites = InMemoryFavoriteRepo()
    rbac = InMemoryRbac()

index = InMemoryVectorIndex()
audit_log = ListAuditLog()
jobs: dict[str, dict] = {}

# ── 播种账号:一个管理员 + 一个普通用户(演示用)。已存在则不覆盖(持久化后只种一次)──
_h = FakeHasher()
if user_repo.get("admin") is None:
    user_repo.save(User(id="admin", name="admin", pwd_hash=_h.hash("admin123"), role="admin"))
if user_repo.get("user01") is None:
    user_repo.save(User(id="user01", name="demo", pwd_hash=_h.hash("pw123456"), role="user"))


def get_material_service() -> MaterialService:
    return MaterialService(material_repo, storage)


def get_search_service() -> SearchService:
    return SearchService(FakeQueryEmbedder(), material_repo)


def get_video_service() -> VideoParsingService:
    return VideoParsingService(FakeVideoParser(), FakeEmbedder(), FakePassAuditor(), material_repo, storage)


def get_index_service() -> IndexService:
    return IndexService(index)


def get_user_service() -> UserService:
    return UserService(user_repo, FakeHasher(), FakeTokenIssuer())


def get_authz_service() -> AuthorizationService:
    return AuthorizationService(rbac, audit_log)


def get_library_service() -> LibraryService:
    return LibraryService(material_repo, favorites)


def current_user(authorization: str | None):
    """从 Authorization: Bearer token-<uid>-exp… 解析当前用户。缺省=游客。"""
    uid = "guest"
    if authorization and authorization.startswith("Bearer "):
        parts = authorization[7:].split("-")
        if len(parts) >= 2 and parts[0] == "token":
            uid = parts[1]
    u = user_repo.get(uid)
    role = u.role if u else "user"
    name = u.name if u else "游客"
    return {"id": uid, "role": role, "name": name}
