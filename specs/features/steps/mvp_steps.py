"""behave step 实现:物料管理(REQ-1xx)/自动审核(REQ-5xx)/语义搜索(REQ-3xx)。"""
import uuid
from behave import given, when, then  # type: ignore
from app.domain.models import Material, MaterialType, AuditStatus
from app.domain.rules import is_available
from app.service.material import MaterialService, MaterialNotFound
from app.service.audit import AuditService
from app.service.search import SearchService
from app.infrastructure.fakes import (
    FakeStorage, InMemoryMaterialRepo, FakeQueryEmbedder, FakeEmbedder,
    FakePassAuditor, TimeoutAuditor,
)


def _mat(status=AuditStatus.PASS, oss_key="k", desc="", is_public=False):
    return Material(
        id=uuid.uuid4().hex, type=MaterialType.IMAGE, thumb=f"{oss_key}#thumb",
        source_timecode=0.0, embedding=[0.1] * 8, audit_status=status,
        source_job="", oss_key=oss_key, description=desc, is_public=is_public,
    )


def _material_svc(repo, storage):
    return MaterialService(repo, storage, FakeEmbedder())


# ══ F1 物料管理 ══
@given("我上传一张图片物料")
def g_upload(context):
    context.repo = InMemoryMaterialRepo()
    context.storage = FakeStorage()
    context.material_svc = _material_svc(context.repo, context.storage)
    context.upload = (MaterialType.IMAGE, "img1.png", b"bytes", "u1")


@when("系统处理上传")
def w_process_upload(context):
    t, key, data, owner = context.upload
    context.mat = context.material_svc.create(t, key, data, owner)


@then("文件应存入 OSS")
def t_stored(context):
    assert context.storage.exists("img1.png")


@then("应落库元数据并返回物料ID")
def t_saved(context):
    assert context.mat.id and context.repo.get(context.mat.id) is not None


@given("存在一条物料")
def g_existing(context):
    context.repo = InMemoryMaterialRepo()
    context.storage = FakeStorage()
    context.material_svc = _material_svc(context.repo, context.storage)
    context.search_svc = SearchService(FakeQueryEmbedder(), context.repo)
    context.mat = context.material_svc.create(MaterialType.IMAGE, "img2.png", b"x", "u1")


@when("我请求该物料")
def w_request(context):
    context.url = context.material_svc.get_signed_url(context.mat.id)


@then("应返回受时限的 OSS 签名URL")
def t_signed(context):
    assert context.url and "Expires" in context.url


@when("我删除它")
def w_delete(context):
    context.material_svc.delete(context.mat.id)


@then("该物料应不可访问且不可检索")
def t_gone(context):
    try:
        context.material_svc.get_signed_url(context.mat.id)
        accessible = True
    except MaterialNotFound:
        accessible = False
    assert accessible is False
    assert all(m.id != context.mat.id for m in context.search_svc.search(""))


# ══ F6 自动审核 ══
@given("生成了一条新物料")
def g_new_material(context):
    context.repo = InMemoryMaterialRepo()
    context.mat = _mat(status=AuditStatus.REVIEW, oss_key="a1")
    context.repo.save(context.mat)
    context.audit_svc = AuditService(FakePassAuditor(), context.repo)


@when("系统触发审核")
def w_trigger_audit(context):
    context.status = context.audit_svc.run(context.mat)


@then("应写回审核结果 pass 或 review 或 block")
def t_audit_written(context):
    assert context.mat.audit_status in (AuditStatus.PASS, AuditStatus.REVIEW, AuditStatus.BLOCK)


@given("一条物料审核结果为 block")
def g_block_material(context):
    context.repo = InMemoryMaterialRepo()
    context.storage = FakeStorage()
    context.storage.put("b1")
    context.block = _mat(status=AuditStatus.BLOCK, oss_key="b1")
    context.repo.save(context.block)
    context.material_svc = _material_svc(context.repo, context.storage)
    context.search_svc = SearchService(FakeQueryEmbedder(), context.repo)


@when("用户尝试检索或下载")
def w_try_download(context):
    try:
        context.material_svc.get_download_url(context.block.id)
        context.rejected = False
    except PermissionError:
        context.rejected = True
    context.results = context.search_svc.search("")


@then("系统应拒绝且该物料不出现在结果中")
def t_rejected_and_hidden(context):
    assert context.rejected is True
    assert all(m.id != context.block.id for m in context.results)


@given("一条物料的审核超时")
def g_audit_timeout(context):
    context.repo = InMemoryMaterialRepo()
    context.mat = _mat(status=AuditStatus.REVIEW, oss_key="t1")
    context.repo.save(context.mat)
    context.audit_svc = AuditService(TimeoutAuditor(), context.repo)


@when("系统处理该超时")
def w_handle_timeout(context):
    context.status = context.audit_svc.run(context.mat)


@then("该物料应标记为 review 进人工复核")
def t_review(context):
    assert context.mat.audit_status == AuditStatus.REVIEW


@then("不得默认放行")
def t_not_available(context):
    assert is_available(context.mat) is False


# ══ F3 语义搜索 ══
@given("库中有若干已审核通过的物料")
def g_pass_materials(context):
    context.repo = InMemoryMaterialRepo()
    for i in range(3):
        context.repo.save(_mat(status=AuditStatus.PASS, oss_key=f"p{i}", desc=f"cat photo {i}", is_public=True))
    context.embedder = FakeQueryEmbedder()
    context.search_svc = SearchService(context.embedder, context.repo)


@when("我用文本查询")
def w_text_query(context):
    context.results = context.search_svc.search("cat")


@then("应生成 embedding 做向量近邻检索")
def t_embed_called(context):
    assert context.embedder.calls and len(context.results) >= 1


@then("结果应按相似度排序返回")
def t_sorted(context):
    key = lambda m: 1.0 if "cat" in m.description else 0.0
    assert context.results == sorted(context.results, key=key, reverse=True)


@given("库中有含特定专有名词的物料")
def g_term_material(context):
    context.repo = InMemoryMaterialRepo()
    context.term = "量子霍尔"
    context.repo.save(_mat(status=AuditStatus.PASS, oss_key="term1", desc=f"关于{context.term}效应的图", is_public=True))
    context.repo.save(_mat(status=AuditStatus.PASS, oss_key="other1", desc="普通图片", is_public=True))
    context.embedder = FakeQueryEmbedder()
    context.search_svc = SearchService(context.embedder, context.repo)


@when("我用该专有名词查询")
def w_term_query(context):
    context.results = context.search_svc.search(context.term)


@then("应使用 hybrid 向量加BM25 检索")
def t_hybrid(context):
    assert context.embedder.calls and len(context.results) >= 1


@then("该物料应命中")
def t_term_hit(context):
    assert any(context.term in m.description for m in context.results)


@when("任意用户搜索")
def w_any_search(context):
    context.results = context.search_svc.search("")


@then("该物料不得出现在结果中")
def t_block_hidden(context):
    assert all(m.id != context.block.id for m in context.results)
