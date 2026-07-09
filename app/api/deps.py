"""组合根(Composition Root):在 api 层装配 service + infra。
现用假 infra;Phase 2 换成真阿里云客户端时,只改这里,service/domain 不动。"""
from __future__ import annotations
from app.config import settings
from app.service.material import MaterialService
from app.service.search import SearchService
from app.service.audit import AuditService
from app.service.video_parsing import VideoParsingService
from app.service.indexing import IndexService
from app.service.user import UserService
from app.service.authorization import AuthorizationService
from app.infrastructure.fakes import (
    InMemoryMaterialRepo, FakeStorage, FakeQueryEmbedder, FakePassAuditor,
    FakeVideoParser, FakeEmbedder, InMemoryVectorIndex, InMemoryUserRepo,
    FakeHasher, FakeTokenIssuer, InMemoryRbac, ListAuditLog,
)

# ── 进程内单例;OSS 有密钥就用真实现,其余仍用假实现(逐个可接真)──
material_repo = InMemoryMaterialRepo()

if settings.oss_access_key_id and settings.oss_bucket:
    from app.infrastructure.aliyun_oss import OssStorage
    storage = OssStorage()   # 真 OSS
else:
    storage = FakeStorage()
index = InMemoryVectorIndex()
user_repo = InMemoryUserRepo()
rbac = InMemoryRbac()
audit_log = ListAuditLog()
jobs: dict[str, dict] = {}


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
