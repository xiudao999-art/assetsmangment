"""审核引擎单测:各素材链路 + 关键词快筛 + 大模型兜底 + 报告持久化。"""
from app.service.audit_pipeline import AuditPipelineService
from app.domain.models import (
    MaterialType, AuditStatus, AuditRule, TextSourceType, Material,
)
from app.infrastructure.fakes import (
    FakeTranscriber, FakeVisionDescriber, FakeLlm, InMemoryAuditRuleRepo,
    InMemoryAuditReportRepo, FakeStorage, InMemoryMaterialRepo, FakeEmbedder,
    InMemoryVectorIndex,
)


def _svc(llm=None, rules=(), repo=None, auditor=None):
    rr = InMemoryAuditRuleRepo()
    for r in rules:
        rr.add(r)
    repo = repo or InMemoryMaterialRepo()
    reports = InMemoryAuditReportRepo()
    svc = AuditPipelineService(FakeTranscriber(), FakeVisionDescriber(), llm or FakeLlm(),
                               rr, reports, FakeStorage(), repo, FakeEmbedder(), InMemoryVectorIndex(), auditor)
    return svc, repo, reports


def test_text_audit_pass_without_rules():
    svc, _, _ = _svc()
    job = svc.submit(MaterialType.CORPUS)
    report = svc.run(job, text="今天天气很好")
    assert report.verdict == AuditStatus.PASS
    assert report.segments[0].source_type == TextSourceType.ORIGINAL_TEXT


def test_keyword_prefilter_blocks():
    rule = AuditRule(id="r1", source_type="any", keywords=["赌博"], action="block")
    svc, _, _ = _svc(rules=[rule])
    report = svc.run(svc.submit(MaterialType.CORPUS), text="这是一个赌博网站广告")
    assert report.verdict == AuditStatus.BLOCK
    assert any(t["rule_id"] == "r1" for t in report.triggered)


def test_keyword_source_type_scoped():
    # 规则只对 transcript 生效;原文里出现关键词不应命中
    rule = AuditRule(id="r2", source_type=TextSourceType.TRANSCRIPT.value, keywords=["违禁"], action="block")
    svc, _, _ = _svc(rules=[rule])
    report = svc.run(svc.submit(MaterialType.CORPUS), text="这里有违禁内容")
    assert report.verdict == AuditStatus.PASS  # 原文来源不适用该转写规则


def test_llm_condition_review():
    llm = FakeLlm(response={"decision": "review", "triggered_rule_ids": [1], "reason": "疑似引流"})
    rule = AuditRule(id="r3", source_type="any", condition="出现导流到站外的联系方式", action="review")
    svc, _, _ = _svc(llm=llm, rules=[rule])
    report = svc.run(svc.submit(MaterialType.CORPUS), text="加我微信领福利")
    assert report.verdict == AuditStatus.REVIEW
    assert any(t["rule_id"] == "r3" for t in report.triggered)


def test_image_chain_uses_vision():
    svc, _, _ = _svc()
    job = svc.submit(MaterialType.IMAGE, oss_key="img/x.png")
    report = svc.run(job)
    assert report.segments[0].source_type == TextSourceType.IMAGE_CONTENT
    assert report.verdict == AuditStatus.PASS


def test_audio_chain_transcribes():
    svc, _, _ = _svc()
    report = svc.run(svc.submit(MaterialType.AUDIO, oss_key="a/x.mp3"))
    assert all(s.source_type == TextSourceType.TRANSCRIPT for s in report.segments)
    assert len(report.segments) >= 1


def test_video_chain_saves_frames_as_materials():
    llm = FakeLlm(response={"moments_ms": [1000, 3000]})
    svc, repo, _ = _svc(llm=llm)
    report = svc.run(svc.submit(MaterialType.VIDEO, oss_key="v/x.mp4", owner_id="u1"))
    # 转写段 + 帧段都在报告里
    assert any(s.source_type == TextSourceType.TRANSCRIPT for s in report.segments)
    assert any(s.source_type == TextSourceType.VIDEO_FRAME for s in report.segments)
    # 帧顺带自动存成物料(owner u1)
    saved = [m for m in repo.list() if m.owner_id == "u1" and m.type == MaterialType.IMAGE]
    assert len(saved) >= 1


def test_report_persisted_and_material_updated():
    repo = InMemoryMaterialRepo()
    m = Material(id="m1", type=MaterialType.CORPUS, thumb="", source_timecode=0.0, embedding=[],
                 audit_status=AuditStatus.REVIEW, source_job="", description="含赌博字样")
    repo.save(m)
    rule = AuditRule(id="r1", source_type="any", keywords=["赌博"], action="block")
    svc, repo2, reports = _svc(rules=[rule], repo=repo)
    job = svc.submit(MaterialType.CORPUS, material_id="m1")
    report = svc.run(job, text="含赌博字样")
    assert repo.get("m1").audit_status == AuditStatus.BLOCK
    assert repo.get("m1").audit_report_id  # 报告 id 写回
    assert reports.get(repo.get("m1").audit_report_id).verdict == AuditStatus.BLOCK


def test_content_safety_hard_block():
    from app.infrastructure.fakes import FakeBlockAuditor
    # 无任何规则,但内容安全判 block → 最终 block(硬拦兜底,取最严)
    svc, _, _ = _svc(auditor=FakeBlockAuditor())
    report = svc.run(svc.submit(MaterialType.IMAGE, oss_key="img/x.png"))
    assert report.verdict == AuditStatus.BLOCK
    assert any(t["rule_id"] == "content-safety" for t in report.triggered)


def test_content_safety_pass_is_noop():
    from app.infrastructure.fakes import FakePassAuditor
    # 内容安全放行(如未开通的假实现)→ 不影响,仍按规则(此处无规则)pass
    svc, _, _ = _svc(auditor=FakePassAuditor())
    report = svc.run(svc.submit(MaterialType.IMAGE, oss_key="img/x.png"))
    assert report.verdict == AuditStatus.PASS


def test_audit_generates_summary_and_tags():
    repo = InMemoryMaterialRepo()
    m = Material(id="m1", type=MaterialType.IMAGE, thumb="t", source_timecode=0.0, embedding=[],
                 audit_status=AuditStatus.REVIEW, source_job="", oss_key="img/x.png", owner_id="u1")
    repo.save(m)
    svc, _, _ = _svc(repo=repo)
    svc.run(svc.submit(MaterialType.IMAGE, oss_key="img/x.png", material_id="m1"))
    got = repo.get("m1")
    assert got.ai_summary and got.ai_emotion and got.ai_atmosphere and got.ai_scene
    assert "测试" in got.tags   # FakeLlm 摘要返回的标签


def test_summarize_material_on_demand():
    repo = InMemoryMaterialRepo()
    m = Material(id="m2", type=MaterialType.IMAGE, thumb="t", source_timecode=0.0, embedding=[],
                 audit_status=AuditStatus.REVIEW, source_job="", oss_key="img/y.png", owner_id="u1")
    repo.save(m)
    svc, _, _ = _svc(repo=repo)
    svc.summarize_material(m)
    assert repo.get("m2").ai_summary and repo.get("m2").ai_emotion


def test_rule_repo_list_for():
    rr = InMemoryAuditRuleRepo()
    rr.add(AuditRule(id="a", source_type="any", keywords=["x"]))
    rr.add(AuditRule(id="t", source_type=TextSourceType.TRANSCRIPT.value, keywords=["y"]))
    ids = {r.id for r in rr.list_for(TextSourceType.TRANSCRIPT.value)}
    assert ids == {"a", "t"}
    ids2 = {r.id for r in rr.list_for(TextSourceType.IMAGE_CONTENT.value)}
    assert ids2 == {"a"}  # transcript 规则不适用图像
