"""MVP 三服务单测(闭环③):物料管理/审核/搜索。"""
import pytest
from app.domain.models import MaterialType, AuditStatus, Material
from app.domain.rules import is_available
from app.service.material import MaterialService, MaterialNotFound
from app.service.audit import AuditService
from app.service.search import SearchService
from app.infrastructure.fakes import (
    FakeStorage, InMemoryMaterialRepo, FakeQueryEmbedder, FakeEmbedder,
    FakePassAuditor, FakeBlockAuditor, TimeoutAuditor,
)


def _mat(status=AuditStatus.PASS, oss_key="k", desc="", is_public=False):
    import uuid
    return Material(uuid.uuid4().hex, MaterialType.IMAGE, f"{oss_key}#t", 0.0,
                    [0.1] * 8, status, "", oss_key, desc, is_public=is_public)


def _material_service(repo, storage):
    return MaterialService(repo, storage, FakeEmbedder())


# ── F1 物料管理 ──
def test_create_stores_and_persists():  # REQ-101
    repo, storage = InMemoryMaterialRepo(), FakeStorage()
    svc = _material_service(repo, storage)
    m = svc.create(MaterialType.IMAGE, "x.png", b"d", "u1")
    assert storage.exists("x.png") and repo.get(m.id) is not None
    assert m.embedding  # F4:入库即生成向量(索引真源,非空)


def test_get_signed_url_time_limited():  # REQ-102
    repo, storage = InMemoryMaterialRepo(), FakeStorage()
    svc = _material_service(repo, storage)
    m = svc.create(MaterialType.IMAGE, "x.png", b"d", "u1")
    assert "Expires" in svc.get_signed_url(m.id)


def test_delete_makes_inaccessible():  # REQ-103
    repo, storage = InMemoryMaterialRepo(), FakeStorage()
    svc = _material_service(repo, storage)
    m = svc.create(MaterialType.IMAGE, "x.png", b"d", "u1")
    svc.delete(m.id)
    with pytest.raises(MaterialNotFound):
        svc.get_signed_url(m.id)


def test_delete_corpus_no_osskey_survives_storage_error():
    # 文字/语料无 oss_key:不调 OSS 删除;即便 OSS 抛错也要删掉元数据(修复真 OSS 删空key 报错)
    from app.domain.models import Material
    repo = InMemoryMaterialRepo()
    m = Material(id="c1", type=MaterialType.CORPUS, thumb="", source_timecode=0.0, embedding=[],
                 audit_status=AuditStatus.REVIEW, source_job="", oss_key="", description="一段文字", owner_id="u1")
    repo.save(m)

    class _Boom:
        def put(self, *a, **k): pass
        def delete(self, key): raise RuntimeError("真 OSS 删空 key 报错")

    _material_service(repo, _Boom()).delete("c1")
    assert repo.get("c1") is None


# ── F6 审核 ──
def test_audit_writes_status():  # REQ-501
    repo = InMemoryMaterialRepo(); m = _mat(AuditStatus.REVIEW); repo.save(m)
    AuditService(FakePassAuditor(), repo).run(m)
    assert m.audit_status == AuditStatus.PASS


def test_block_not_downloadable():  # REQ-502
    repo, storage = InMemoryMaterialRepo(), FakeStorage()
    m = _mat(AuditStatus.BLOCK, "b1"); repo.save(m); storage.put("b1")
    with pytest.raises(PermissionError):
        _material_service(repo, storage).get_download_url(m.id)


def test_audit_timeout_review_not_available():  # REQ-503
    repo = InMemoryMaterialRepo(); m = _mat(AuditStatus.REVIEW); repo.save(m)
    AuditService(TimeoutAuditor(), repo).run(m)
    assert m.audit_status == AuditStatus.REVIEW and not is_available(m)


# ── F3 搜索(公共库范围 = 已发布 is_public 且 pass)──
def test_search_only_returns_public_pass():  # REQ-303
    repo = InMemoryMaterialRepo()
    repo.save(_mat(AuditStatus.PASS, "p", "cat", is_public=True))   # 公共 + 过审 → 可搜
    repo.save(_mat(AuditStatus.BLOCK, "b", "cat", is_public=True))  # 被拦截 → 不可搜
    repo.save(_mat(AuditStatus.PASS, "priv", "cat"))               # 过审但未发布 → 不泄露
    results, total = SearchService(FakeQueryEmbedder(), repo).search("cat")
    assert all(m.audit_status == AuditStatus.PASS and m.is_public for m in results)
    assert len(results) == 1 and total == 1


def test_search_hybrid_hits_term():  # REQ-302
    repo = InMemoryMaterialRepo()
    repo.save(_mat(AuditStatus.PASS, "t", "量子霍尔效应", is_public=True))
    repo.save(_mat(AuditStatus.PASS, "o", "普通", is_public=True))
    results, _ = SearchService(FakeQueryEmbedder(), repo).search("量子霍尔")
    assert any("量子霍尔" in m.description for m in results)


# ── 内容安全严格度档位:标签/风险等级 → 裁定映射 ──
def test_content_safety_strictness_modes():
    from app.infrastructure.content_safety import _text_verdict, _image_verdict
    # 文本
    assert _text_verdict("", "balanced") == "pass"                       # 无标签 → 放行
    assert _text_verdict("sexual_content", "balanced") == "block"        # 色情 → 硬拦(各档都拦)
    assert _text_verdict("political_content", "balanced") == "block"     # 政治 → 硬拦
    assert _text_verdict("violence", "balanced") == "review"             # 暴力("杀人犯"这类)→ 适中只转人工,不硬拦
    assert _text_verdict("violence", "strict") == "block"               # 严格:暴力仍硬拦
    assert _text_verdict("violence", "loose") == "review"               # 宽松:一律转人工
    assert _text_verdict("ad", "balanced") == "review"                  # 广告 → 转人工
    # 图片
    assert _image_verdict("high", "balanced") == "block"                # 高危图片仍硬拦
    assert _image_verdict("high", "loose") == "review"                  # 宽松:图片也只转人工
    assert _image_verdict("none", "balanced") == "pass"


def test_content_safety_whitelist_downgrade():
    from app.infrastructure.content_safety import _apply_whitelist, _parse_risk_words
    wl = {"杀人犯"}
    assert _apply_whitelist("block", "杀人犯", wl) == "pass"          # 命中词全在白名单 → 放行
    assert _apply_whitelist("block", "杀人犯,血腥", wl) == "block"    # 有词不在白名单 → 仍拦
    assert _apply_whitelist("review", "杀人犯&杀人", wl) == "pass"    # 子串匹配也算覆盖
    assert _apply_whitelist("block", "", wl) == "block"              # 无风险词 → 不动
    assert _apply_whitelist("block", "杀人犯", set()) == "block"      # 无白名单 → 不动
    assert _apply_whitelist("pass", "任意", wl) == "pass"            # pass 不动
    assert _parse_risk_words('{"riskWords":"色情服务"}') == "色情服务"
    assert _parse_risk_words({"riskWords": "x"}) == "x" and _parse_risk_words("") == ""


def test_aliyun_auditor_audit_detail_surfaces_words():
    """audit_detail 额外交出阿里云命中词;audit() 仍返回裸 verdict(back-compat)。
    用 __new__ 绕过 SDK 初始化,注入假 client(不触真实云端)。"""
    import types, json
    from app.infrastructure.content_safety import AliyunAuditor
    a = AliyunAuditor.__new__(AliyunAuditor)
    a._mode = "balanced"
    a._whitelist = lambda: set()
    a._image_service = "baselineCheck"
    a._storage = types.SimpleNamespace(signed_url=lambda k: "http://x")
    a._models = types.SimpleNamespace(TextModerationRequest=lambda **kw: None,
                                      ImageModerationRequest=lambda **kw: None)
    a._util = types.SimpleNamespace(RuntimeOptions=lambda: types.SimpleNamespace())  # _ropts 要设超时属性

    def _text_resp(req, opts):
        data = types.SimpleNamespace(labels="violence", reason=json.dumps({"riskWords": "杀人犯"}))
        body = types.SimpleNamespace(code=200, msg="", data=data)
        return types.SimpleNamespace(status_code=200, body=body)
    a._client = types.SimpleNamespace(text_moderation_with_options=_text_resp)

    content = types.SimpleNamespace(oss_key="", description="我本可以成为杀人犯")
    verdict, words = a.audit_detail(content)
    assert verdict == "review" and words == "杀人犯"   # balanced:暴力→转人工,并交出命中词
    assert a.audit(content) == "review"                # audit() 仍返回裸字符串,不破坏调用方

    a._whitelist = lambda: {"杀人犯"}                   # 加白后:降级 pass,但仍交出命中词
    v2, w2 = a.audit_detail(content)
    assert v2 == "pass" and w2 == "杀人犯"
