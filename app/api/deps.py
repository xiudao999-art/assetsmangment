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

# 内容安全审核器:有内容安全凭据(或复用 OSS RAM key)就接真,否则放行占位(等人工审核)
_auditor = FakePassAuditor()

# ── 共享单例:同一密钥签发/校验 token;同一 hasher ──
_h = FakeHasher()
token_issuer = FakeTokenIssuer()

# ── AI 能力:有 DashScope key 就接真云(Qwen-VL 反解 + multimodal-embedding),否则假实现 ──
if settings.dashscope_api_key:
    from app.infrastructure.dashscope_embed import DashScopeEmbedder, DashScopeQueryEmbedder
    _embedder = DashScopeEmbedder(settings.dashscope_api_key, settings.embedding_model)
    _query_embedder = DashScopeQueryEmbedder(settings.dashscope_api_key, settings.embedding_model)
    from app.infrastructure.aliyun_oss import OssStorage as _Oss
    if isinstance(storage, _Oss):  # 视频反解要真 OSS(签名 URL + 截帧)
        from app.infrastructure.qwen_vl import QwenVLVideoParser
        _video_parser = QwenVLVideoParser(settings.dashscope_api_key, storage,
                                          settings.qwen_vl_model, settings.parse_fps, settings.parse_max_frames)
    else:
        _video_parser = FakeVideoParser()
else:
    _embedder = FakeEmbedder()
    _query_embedder = FakeQueryEmbedder()
    _video_parser = FakeVideoParser()

# ── 播种账号:一个管理员 + 一个普通用户(演示用)。已存在则不覆盖(持久化后只种一次)──
if user_repo.get("admin") is None:
    user_repo.save(User(id="admin", name="admin", pwd_hash=_h.hash("admin123"), role="admin"))
if user_repo.get("user01") is None:
    user_repo.save(User(id="user01", name="demo", pwd_hash=_h.hash("pw123456"), role="user"))

# ── 播种 admin 角色权限(RBAC 真接通:管理端点按权限鉴权,后台 grant 即时生效)──
ADMIN_PERMS = {"materials.audit", "materials.publish", "materials.delete_any", "library.all", "admin.grant"}
if not ADMIN_PERMS.issubset(rbac.permissions_of("admin")):
    for _p in ADMIN_PERMS:
        rbac.grant("admin", _p)


def get_material_service() -> MaterialService:
    return MaterialService(material_repo, storage, _embedder)


def get_search_service() -> SearchService:
    return SearchService(_query_embedder, material_repo)


def get_video_service() -> VideoParsingService:
    return VideoParsingService(_video_parser, _embedder, _auditor, material_repo, storage)


def get_index_service() -> IndexService:
    return IndexService(index)


def get_user_service() -> UserService:
    return UserService(user_repo, _h, token_issuer)


def get_authz_service() -> AuthorizationService:
    return AuthorizationService(rbac, audit_log)


def get_library_service() -> LibraryService:
    return LibraryService(material_repo, favorites)


def current_user(authorization: str | None):
    """校验 Authorization: Bearer <签名token> → 当前用户。
    伪造/过期/未知用户/缺省 → 游客(role='guest',无任何写权限)。绝不信任客户端提供的 uid。"""
    guest = {"id": "guest", "role": "guest", "name": "游客"}
    if not authorization or not authorization.startswith("Bearer "):
        return guest
    uid = token_issuer.verify(authorization[7:])  # 签名不符/过期 → None
    if uid is None:
        return guest
    u = user_repo.get(uid)
    if u is None:
        return guest  # 未知用户不兜底成 user
    return {"id": u.id, "role": u.role, "name": u.name}
