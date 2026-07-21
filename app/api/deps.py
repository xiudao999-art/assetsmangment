"""组合根(Composition Root):在 api 层装配 service + infra。
现用假 infra;Phase 2 换成真阿里云客户端时,只改这里,service/domain 不动。"""
from __future__ import annotations
import uuid
import time
from app.config import settings
from app.infrastructure.snowflake import next_id, next_id_str
from app.domain.models import User, Project
from app.service.material import MaterialService
from app.service.search import SearchService
from app.service.audit import AuditService
from app.service.video_parsing import VideoParsingService
from app.service.indexing import IndexService
from app.service.user import UserService
from app.service.authorization import AuthorizationService
from app.service.library import LibraryService
from app.service.audit_pipeline import AuditPipelineService
from app.infrastructure.fakes import (
    InMemoryMaterialRepo, FakeStorage, FakeQueryEmbedder, FakePassAuditor,
    FakeVideoParser, FakeEmbedder, InMemoryVectorIndex, InMemoryUserRepo,
    FakeHasher, FakeTokenIssuer, InMemoryRbac, ListAuditLog, InMemoryFavoriteRepo,
    FakeTranscriber, FakeVisionDescriber, FakeLlm, InMemoryAuditRuleRepo, InMemoryAuditReportRepo,
    InMemoryAuditTaskRepo, InMemoryWhitelistRepo, InMemoryProjectRepo, InMemoryBlockwordRepo,
    InMemoryTrainingSetRepo, InMemoryTrainingExampleRepo,
)

# ── 进程内单例 ──
# 存储:OSS 有密钥用真实现,否则假实现。
if settings.oss_access_key_id and settings.oss_bucket:
    from app.infrastructure.aliyun_oss import OssStorage
    storage = OssStorage()   # 真 OSS
else:
    storage = FakeStorage()

# 占位 DSN(默认值)= 视为未配库;真实 DSN 才接 PG(规则真源 + pgvector)。
_placeholder_db = settings.database_url.startswith("postgresql://user:pass@localhost")
_real_db = bool(settings.database_url) and not _placeholder_db

# 物料/用户/收藏/权限:设了 AM_DATA_DIR 就落 JSON 文件(容器重启不丢),否则纯内存。
if settings.data_dir:
    from app.infrastructure.jsonstore import (
        Store, JsonMaterialRepo, JsonUserRepo, JsonFavoriteRepo, JsonRbac,
        JsonAuditRuleRepo, JsonAuditReportRepo, JsonAuditTaskRepo, JsonWhitelistRepo, JsonProjectRepo,
        JsonBlockwordRepo,
    )
    _store = Store(f"{settings.data_dir.rstrip('/')}/state.json")
    material_repo = JsonMaterialRepo(_store)
    user_repo = JsonUserRepo(_store)
    favorites = JsonFavoriteRepo(_store)
    rbac = JsonRbac(_store)
    rule_repo = JsonAuditRuleRepo(_store)
    report_repo = JsonAuditReportRepo(_store)
    task_repo = JsonAuditTaskRepo(_store)
    whitelist_repo = JsonWhitelistRepo(_store)
    blockword_repo = JsonBlockwordRepo(_store)
    project_repo = JsonProjectRepo(_store)
    training_set_repo = InMemoryTrainingSetRepo()
    training_example_repo = InMemoryTrainingExampleRepo()
else:
    material_repo = InMemoryMaterialRepo()
    user_repo = InMemoryUserRepo()
    favorites = InMemoryFavoriteRepo()
    rbac = InMemoryRbac()
    rule_repo = InMemoryAuditRuleRepo()
    report_repo = InMemoryAuditReportRepo()
    task_repo = InMemoryAuditTaskRepo()
    whitelist_repo = InMemoryWhitelistRepo()
    blockword_repo = InMemoryBlockwordRepo()
    project_repo = InMemoryProjectRepo()
    training_set_repo = InMemoryTrainingSetRepo()
    training_example_repo = InMemoryTrainingExampleRepo()

# 审核规则:配置了真实 AM_DATABASE_URL → PG 是唯一真源(audit_rule 表,雪花ID+软删基础字段),
# 覆盖上面的 JSON/内存实现。连接/建表失败 = 启动即报错,**不静默回退 JSON**(回退会分叉真源、
# 丢数据)—— 与 PgVectorIndex import 时建表的既有行为一致。
if _real_db:
    from app.infrastructure.pg_rule_repo import PgAuditRuleRepo
    try:
        rule_repo = PgAuditRuleRepo(settings.database_url)
    except Exception as _e:
        raise RuntimeError(
            f"AM_DATABASE_URL 已配置但 PG 连接/建表失败:{_e}。"
            "规则真源在 PG,不做静默回退 —— 请检查数据库可达性/凭据后再启动。"
        ) from _e

# 作品项目:配了真 PG 同样走雪花ID+软删,material.project_id 引用项目 id str,
# 与规则一致(同一 PG 实例,不额外连)。连接/建表失败同理不静默回退。
if _real_db:
    from app.infrastructure.pg_project_repo import PgProjectRepo
    try:
        project_repo = PgProjectRepo(settings.database_url)
    except Exception as _e:
        raise RuntimeError(
            f"AM_DATABASE_URL 已配置但 PG 项目表连接/建表失败:{_e}。"
            "项目真源在 PG,不做静默回退 —— 请检查数据库可达性/凭据后再启动。"
        ) from _e

# ── 全仓储 PG 真源切换(fail-fast,不静默回退 JSON) ──
if _real_db:
    # 物料
    from app.infrastructure.pg_material_repo import PgMaterialRepo
    try:
        material_repo = PgMaterialRepo(settings.database_url)
    except Exception as _e:
        raise RuntimeError(
            f"AM_DATABASE_URL 已配置但 PG 物料表连接/建表失败:{_e}。"
            "物料真源在 PG,不做静默回退 —— 请检查数据库可达性/凭据后再启动。"
        ) from _e

    # 用户
    from app.infrastructure.pg_user_repo import PgUserRepo
    try:
        user_repo = PgUserRepo(settings.database_url)
    except Exception as _e:
        raise RuntimeError(
            f"AM_DATABASE_URL 已配置但 PG 用户表连接/建表失败:{_e}。"
        ) from _e

    # 收藏
    from app.infrastructure.pg_favorite_repo import PgFavoriteRepo
    try:
        favorites = PgFavoriteRepo(settings.database_url)
    except Exception as _e:
        raise RuntimeError(
            f"AM_DATABASE_URL 已配置但 PG 收藏表连接/建表失败:{_e}。"
        ) from _e

    # RBAC
    from app.infrastructure.pg_rbac_repo import PgRbacRepo
    try:
        rbac = PgRbacRepo(settings.database_url)
    except Exception as _e:
        raise RuntimeError(
            f"AM_DATABASE_URL 已配置但 PG RBAC 表连接/建表失败:{_e}。"
        ) from _e

    # 审核任务
    from app.infrastructure.pg_task_repo import PgAuditTaskRepo
    try:
        task_repo = PgAuditTaskRepo(settings.database_url)
    except Exception as _e:
        raise RuntimeError(
            f"AM_DATABASE_URL 已配置但 PG 审核任务表连接/建表失败:{_e}。"
        ) from _e

    # 审核报告
    from app.infrastructure.pg_report_repo import PgAuditReportRepo
    try:
        report_repo = PgAuditReportRepo(settings.database_url)
    except Exception as _e:
        raise RuntimeError(
            f"AM_DATABASE_URL 已配置但 PG 审核报告表连接/建表失败:{_e}。"
        ) from _e

    # 白名单
    from app.infrastructure.pg_whitelist_repo import PgWhitelistRepo
    try:
        whitelist_repo = PgWhitelistRepo(settings.database_url)
    except Exception as _e:
        raise RuntimeError(
            f"AM_DATABASE_URL 已配置但 PG 白名单表连接/建表失败:{_e}。"
        ) from _e

    # 禁词
    from app.infrastructure.pg_blockword_repo import PgBlockwordRepo
    try:
        blockword_repo = PgBlockwordRepo(settings.database_url)
    except Exception as _e:
        raise RuntimeError(
            f"AM_DATABASE_URL 已配置但 PG 禁词表连接/建表失败:{_e}。"
        ) from _e

    # 审计日志
    from app.infrastructure.pg_audit_log import PgAuditLog
    try:
        audit_log = PgAuditLog(settings.database_url)
    except Exception as _e:
        raise RuntimeError(
            f"AM_DATABASE_URL 已配置但 PG 审计日志表连接/建表失败:{_e}。"
        ) from _e

    # 规则训练集
    from app.infrastructure.pg_training_set_repo import PgTrainingSetRepo
    try:
        training_set_repo = PgTrainingSetRepo(settings.database_url)
    except Exception as _e:
        raise RuntimeError(
            f"AM_DATABASE_URL 已配置但 PG 训练集表连接/建表失败:{_e}。"
        ) from _e

    # 规则训练样本
    from app.infrastructure.pg_training_example_repo import PgTrainingExampleRepo
    try:
        training_example_repo = PgTrainingExampleRepo(settings.database_url)
    except Exception as _e:
        raise RuntimeError(
            f"AM_DATABASE_URL 已配置但 PG 训练样本表连接/建表失败:{_e}。"
        ) from _e

# 向量索引:有真 embedding(DashScope)+ 真 pg 连接串 → pgvector 语义近邻;否则内存
if settings.dashscope_api_key and _real_db:
    from app.infrastructure.pgvector_index import PgVectorIndex
    index = PgVectorIndex(settings.database_url, dim=settings.embedding_dim)
    _vector_search = True
else:
    index = InMemoryVectorIndex()
    _vector_search = False

audit_log = ListAuditLog()
jobs: dict[str, dict] = {}
batches: dict[str, dict] = {}   # 批量上传进度(内存,轮询窗口足够)

# 有界审核工作池:单条/批量的后台审核都提交到它,超出上限的排队(背压),不再无限起线程。
from concurrent.futures import ThreadPoolExecutor
audit_pool = ThreadPoolExecutor(max_workers=max(1, settings.audit_concurrency), thread_name_prefix="audit")

# 内容安全审核器:开通「内容安全增强版」并置 AM_ENABLE_CONTENT_SAFETY=true 才接真;否则走人工审核
if settings.enable_content_safety:
    from app.infrastructure.content_safety import AliyunAuditor
    _cs_ak = settings.content_safety_access_key_id or settings.oss_access_key_id
    _cs_sk = settings.content_safety_access_key_secret or settings.oss_access_key_secret
    _auditor = AliyunAuditor(_cs_ak, _cs_sk, storage, region=settings.content_safety_region,
                             mode=settings.content_safety_mode,
                             whitelist=lambda: whitelist_repo.words())   # 白名单实时读
else:
    _auditor = FakePassAuditor()

# ── 共享单例:同一密钥签发/校验 token;同一 hasher ──
_h = FakeHasher()
token_issuer = FakeTokenIssuer()

# ── AI 能力:有 DashScope key 就接真云(Qwen-VL 反解 + multimodal-embedding),否则假实现 ──
if settings.dashscope_api_key:
    from app.infrastructure.dashscope_embed import DashScopeEmbedder, DashScopeQueryEmbedder
    _embedder = DashScopeEmbedder(settings.dashscope_api_key, settings.embedding_model)
    _query_embedder = DashScopeQueryEmbedder(settings.dashscope_api_key, settings.embedding_model)
    from app.infrastructure.dashscope_llm import DashScopeLlm
    from app.infrastructure.dashscope_asr import DashScopeTranscriber
    from app.infrastructure.qwen_vl import QwenVLVisionDescriber
    _llm = DashScopeLlm(settings.dashscope_api_key, settings.qwen_llm_model)
    _vision = QwenVLVisionDescriber(settings.dashscope_api_key, settings.qwen_vl_model)
    _transcriber = DashScopeTranscriber(settings.dashscope_api_key, settings.asr_model)
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
    _llm = FakeLlm()
    _vision = FakeVisionDescriber()
    _transcriber = FakeTranscriber()

# ── 物料档案器:有 ARK key + 豆包模型 id → 豆包 pro 2.1 直接看图/视频提「情绪/场景」多值标签 ──
# 未配 → None(pipeline 走 qwen 文本兜底,即现状);FakeArchiver 只在测试/本地 QC 注入,不做生产默认。
if settings.ark_api_key and settings.doubao_vl_model:
    from app.infrastructure.doubao_ark import DoubaoArchiver
    _archiver = DoubaoArchiver(settings.ark_api_key, settings.doubao_vl_model, settings.ark_base_url)
else:
    _archiver = None

# ── 联网搜索:有 Tavily key → 音乐物料按歌名联网搜「情绪/场景」合成档案;未配 → None(音乐走 qwen 文本兜底)──
if settings.tavily_api_key:
    from app.infrastructure.tavily import TavilySearch
    _tavily = TavilySearch(settings.tavily_api_key)
else:
    _tavily = None

# ── 播种账号:一个管理员 + 一个普通用户(演示用)。已存在则不覆盖(持久化后只种一次)──
if user_repo.get("admin") is None:
    user_repo.save(User(id="admin", name="admin", pwd_hash=_h.hash("admin123"), role="admin"))
if user_repo.get("user01") is None:
    user_repo.save(User(id="user01", name="demo", pwd_hash=_h.hash("pw123456"), role="user"))

# ── 播种 admin 角色权限(RBAC 真接通:管理端点按权限鉴权,后台 grant 即时生效)──
ADMIN_PERMS = {"materials.audit", "materials.publish", "materials.delete_any", "library.all", "admin.grant", "audit.rules"}
if not ADMIN_PERMS.issubset(rbac.permissions_of("admin")):
    for _p in ADMIN_PERMS:
        rbac.grant("admin", _p)

def ensure_default_project() -> str:
    """保证至少有一个作品项目(作品必须归属项目);返回一个可用项目 id。
    已有 → 返回最早那个;一个都没有 → 建「汽水音乐」。幂等 —— 供启动播种 + 运行时自愈复用,
    这样即便管理员把项目删光,下次列项目/提交作品也永远有项目可选,作品不会「莫名上传失败」。"""
    existing = project_repo.list()
    if existing:
        return min(existing, key=lambda p: p.created_ms).id
    p = Project(id=next_id_str(), name="汽水音乐", created_by="admin",
                created_ms=int(time.time() * 1000))
    project_repo.add(p)
    return p.id


# ── 播种作品项目:后台至少要有一个项目才能提交作品 ──
ensure_default_project()


def ensure_rule_numbers() -> None:
    """给历史规则回填稳定编号 no(幂等):no==0 的规则按 list() 顺序续 max+1 赋号并持久化。
    在运行进程内做(不用外部脚本,避免独立进程覆盖 state 的竞态)。全有号则跳过。"""
    rules = rule_repo.list()
    missing = [r for r in rules if not getattr(r, "no", 0)]
    if not missing:
        return
    nxt = max((getattr(r, "no", 0) for r in rules), default=0) + 1
    for r in missing:
        r.no = nxt
        nxt += 1
        rule_repo.add(r)   # 覆盖持久化


ensure_rule_numbers()


# ── Task janitor (startup recovery + runtime compensation for stuck audit tasks) ──
from app.service.task_janitor import TaskJanitor   # noqa: E402 (import after singleton wiring)

task_janitor = TaskJanitor(
    task_repo=task_repo,
    material_repo=material_repo,
    storage=storage,
    scan_interval_s=settings.janitor_scan_interval_s,
    stuck_timeout_s=settings.janitor_stuck_timeout_s,
)


def get_material_service() -> MaterialService:
    return MaterialService(material_repo, storage, _embedder)


def get_search_service() -> SearchService:
    # 仅在真 pgvector(真向量)时启用语义近邻;否则不传 index,走纯关键词
    return SearchService(_query_embedder, material_repo, index if _vector_search else None,
                         max_distance=settings.search_max_distance)


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


def get_audit_service() -> AuditPipelineService:
    return AuditPipelineService(_transcriber, _vision, _llm, rule_repo, report_repo,
                                storage, material_repo, _embedder, index, _auditor,
                                blockwords=lambda: blockword_repo.words(), archiver=_archiver,
                                tavily=_tavily)


def get_training_service():
    from app.service.training_service import TrainingService
    return TrainingService(training_set_repo, training_example_repo,
                           rule_repo, material_repo, report_repo,
                           get_audit_service(), _llm)


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
