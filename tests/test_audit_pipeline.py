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


# ── 视频分「物料 / 作品」两条链路 ──
from app.service.audit_pipeline import _MOMENT_RULE_SYS, _MOMENT_SYS, _WORK_MAX_FRAMES


def test_material_moments_tiers():
    M = AuditPipelineService._material_moments
    assert M(4000) == [2000]                       # ≤5s → 1 帧(中)
    assert len(M(8000)) == 2                        # ≤10s → 2 帧
    assert M(16000) == [4000, 8000, 12000]          # ≤20s → 3 帧(25/50/75%)
    assert M(0) == [500] and M(None) == [500]       # 拿不到时长 → 1 帧


def test_safety_net_sparse_and_capped():
    N = AuditPipelineService._safety_net
    pts = N(120000)                                 # 2 分钟 → ~6 帧均匀
    assert 3 <= len(pts) <= _WORK_MAX_FRAMES and all(0 < p < 120000 for p in pts)
    assert N(0) == [1000]
    assert len(N(600000)) <= _WORK_MAX_FRAMES        # 10 分钟也封顶


def test_material_path_saves_frames():
    svc, repo, _ = _svc()
    report = svc.run(svc.submit(MaterialType.VIDEO, oss_key="v/x.mp4", owner_id="u1"))  # 默认 material
    assert any(s.source_type == TextSourceType.VIDEO_FRAME for s in report.segments)
    saved = [m for m in repo.list() if m.owner_id == "u1" and m.type == MaterialType.IMAGE]
    assert len(saved) >= 1                          # 物料:帧存成素材


def test_work_path_does_not_save_frames():
    llm = FakeLlm(response={"moments_ms": [1000, 3000]})
    svc, repo, _ = _svc(llm=llm)
    report = svc.run(svc.submit(MaterialType.VIDEO, oss_key="v/x.mp4", owner_id="u1", video_kind="work"))
    assert any(s.source_type == TextSourceType.VIDEO_FRAME for s in report.segments)  # 帧仍被核验
    saved = [m for m in repo.list() if m.owner_id == "u1" and m.type == MaterialType.IMAGE]
    assert saved == []                              # 作品:帧不入库


def test_work_moment_prompt_includes_rules():
    # 作品抽帧点按「画面拦截规则」反推:规则清单要出现在给 LLM 的提示里
    rule = AuditRule(id="rv", source_type=TextSourceType.VIDEO_FRAME.value,
                     keywords=["刀", "血"], condition="出现血腥暴力画面", action="review")
    llm = FakeLlm()   # 默认对含「时间段」的 system 返回 {"moments_ms":[]},但会记录调用
    svc, _, _ = _svc(llm=llm, rules=[rule])
    svc.run(svc.submit(MaterialType.VIDEO, oss_key="v/x.mp4", owner_id="u1", video_kind="work"))
    assert any(sys == _MOMENT_RULE_SYS and ("血腥暴力" in usr or "刀" in usr) for sys, usr in llm.calls)


def test_work_no_transcript_uses_net_only():
    class _EmptyTx:
        def transcribe(self, url): return []
    llm = FakeLlm()
    rr = InMemoryAuditRuleRepo(); repo = InMemoryMaterialRepo()
    svc = AuditPipelineService(_EmptyTx(), FakeVisionDescriber(), llm, rr,
                               InMemoryAuditReportRepo(), FakeStorage(), repo,
                               FakeEmbedder(), InMemoryVectorIndex(), None)
    report = svc.run(svc.submit(MaterialType.VIDEO, oss_key="v/x.mp4", owner_id="u1", video_kind="work"))
    assert any(s.source_type == TextSourceType.VIDEO_FRAME for s in report.segments)   # 安全网仍抽到帧
    assert not any(s.source_type == TextSourceType.TRANSCRIPT for s in report.segments)
    assert not any(sys in (_MOMENT_RULE_SYS, _MOMENT_SYS) for sys, _ in llm.calls)     # 无转写→不问 LLM 时刻


# ── 命中详情:定位到具体文本片段 / 帧图片(报告标红用)──
def test_content_safety_pinpoints_text_segment():
    from app.infrastructure.fakes import FakeBlockAuditor
    svc, _, _ = _svc(auditor=FakeBlockAuditor())
    report = svc.run(svc.submit(MaterialType.CORPUS), text="某段可疑文字")
    cs = [t for t in report.triggered if t["rule_id"] == "content-safety"]
    assert cs and cs[0]["source_type"] == "original_text" and cs[0]["text"] == "某段可疑文字"


def test_content_safety_pinpoints_frame():
    from app.infrastructure.fakes import FakeBlockAuditor
    svc, _, _ = _svc(auditor=FakeBlockAuditor())
    report = svc.run(svc.submit(MaterialType.VIDEO, oss_key="v/x.mp4", owner_id="u1"))
    fr = [t for t in report.triggered if t["rule_id"] == "content-safety" and t["source_type"] == "video_frame"]
    assert fr and fr[0].get("frame_oss_key") and fr[0]["begin_ms"] is not None


def test_prefilter_keeps_offending_text():
    rule = AuditRule(id="rk", source_type="any", keywords=["违规词"], action="block")
    svc, _, _ = _svc(rules=[rule])
    report = svc.run(svc.submit(MaterialType.CORPUS), text="这里有违规词出现")
    hit = [t for t in report.triggered if t["rule_id"] == "rk"]
    assert hit and hit[0]["text"] == "这里有违规词出现"   # 命中项带上具体片段


# ── 内容安全命中词 riskWords 入报告(供标红 + 一键加白)──
class _WordAuditor:
    """审核器:可选实现 audit_detail 交出命中词(文本才有,图片无)。"""
    def audit(self, content) -> str:
        return "review"
    def audit_detail(self, content):
        return ("review", "杀人犯,枪") if getattr(content, "description", "") else ("review", "")


def test_content_safety_surfaces_risk_words():
    svc, _, _ = _svc(auditor=_WordAuditor())
    report = svc.run(svc.submit(MaterialType.CORPUS), text="我本可以成为杀人犯")
    cs = [t for t in report.triggered if t["rule_id"] == "content-safety" and t.get("risk_words")]
    assert cs and "杀人犯" in cs[0]["risk_words"]


def test_content_safety_no_audit_detail_still_works():
    # 审核器只有 audit()(如 fakes)→ getattr 兜底,不报错,risk_words 缺省
    from app.infrastructure.fakes import FakeBlockAuditor
    svc, _, _ = _svc(auditor=FakeBlockAuditor())
    report = svc.run(svc.submit(MaterialType.CORPUS), text="某段文字")
    cs = [t for t in report.triggered if t["rule_id"] == "content-safety"]
    assert cs and cs[0].get("risk_words", "") == ""


# ── recheck:只对已存报告重判,不重抽帧/转写/不重复生成素材 ──
class _ToggleAuditor:
    def __init__(self):
        self.verdict = "block"
    def audit(self, content) -> str:
        return self.verdict


def test_recheck_reevaluates_without_reextraction():
    from app.domain.models import TextSegment
    class _RecTx:
        def __init__(self): self.n = 0
        def transcribe(self, url):
            self.n += 1
            return [TextSegment(TextSourceType.TRANSCRIPT, "对白", begin_ms=0)]
    class _RecVision:
        def __init__(self): self.n = 0
        def describe_image(self, url):
            self.n += 1
            return "画面内容"
    tx, vi, aud = _RecTx(), _RecVision(), _ToggleAuditor()
    repo = InMemoryMaterialRepo()
    svc = AuditPipelineService(tx, vi, FakeLlm(response={"moments_ms": [1000]}),
                               InMemoryAuditRuleRepo(), InMemoryAuditReportRepo(), FakeStorage(),
                               repo, FakeEmbedder(), InMemoryVectorIndex(), aud)
    rep = svc.run(svc.submit(MaterialType.VIDEO, oss_key="v/x.mp4", owner_id="u1"))
    assert rep.verdict == AuditStatus.BLOCK
    n_mats, tx_calls, vi_calls = len(repo.list()), tx.n, vi.n
    assert n_mats >= 1                                    # 首审:帧已存成素材

    aud.verdict = "pass"                                  # 模拟"加白后放行"
    rep2 = svc.recheck(svc.submit(MaterialType.VIDEO, oss_key="v/x.mp4", owner_id="u1"), rep)
    assert rep2.verdict == AuditStatus.PASS               # 用当前策略重判 → 翻成通过
    assert tx.n == tx_calls and vi.n == vi_calls          # 未重新转写/反解
    assert len(repo.list()) == n_mats                     # 未新增帧素材(不重复入库)
    assert rep2.segments == rep.segments                  # 复用已存 segments
