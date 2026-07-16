"""作品「项目」维度:项目规则作用域 + 项目仓储 + MaterialQuery 项目门。"""
from app.service.audit_pipeline import AuditPipelineService
from app.domain.models import MaterialType, AuditStatus, AuditRule, Project, Material
from app.domain.query import MaterialQuery, matches
from app.infrastructure.fakes import (
    FakeTranscriber, FakeVisionDescriber, FakeLlm, InMemoryAuditRuleRepo,
    InMemoryAuditReportRepo, FakeStorage, InMemoryMaterialRepo, FakeEmbedder,
    InMemoryVectorIndex, InMemoryProjectRepo,
)


def _svc(rules=(), llm=None):
    rr = InMemoryAuditRuleRepo()
    for r in rules:
        rr.add(r)
    return AuditPipelineService(FakeTranscriber(), FakeVisionDescriber(), llm or FakeLlm(), rr,
                                InMemoryAuditReportRepo(), FakeStorage(), InMemoryMaterialRepo(),
                                FakeEmbedder(), InMemoryVectorIndex(), None)


def test_applies_to_project_scope():
    g = AuditRule(id="g", source_type="any")                      # 标准/全局
    p = AuditRule(id="p", source_type="any", project_id="P")      # 项目 P
    assert g.applies_to("original_text", "") and g.applies_to("original_text", "P")  # 全局对任意项目都生效
    assert not p.applies_to("original_text", "")                  # 项目规则对「无项目」(物料)不生效
    assert p.applies_to("original_text", "P")                     # 对本项目生效
    assert not p.applies_to("original_text", "Q")                 # 对别的项目不生效


def test_work_audited_by_global_and_project_rules():
    # 语义判定按「全局 ∪ 该项目」组成规则清单;用规则编号命中来验证作用域:
    # 作品 P 的清单 = [g, p](第 2 条=项目规则);物料/别项目的清单只有 [g](引用第 2 条即越界丢弃)。
    grule = AuditRule(id="g", source_type="any", condition="全局禁止内容", action="block")
    prule = AuditRule(id="p", source_type="any", condition="项目禁止内容", action="block", project_id="P")
    llm = FakeLlm()
    svc = _svc([grule, prule], llm=llm)
    hit1 = {"findings": [{"rule": 1, "segment": 1, "reason": "命中"}]}
    hit2 = {"findings": [{"rule": 2, "segment": 1, "reason": "命中"}]}
    # 作品 P 命中第 2 条(项目规则 p)→ review(项目规则额外生效;机器只转人工)
    llm.set_response(hit2)
    assert svc.run(svc.submit(MaterialType.CORPUS, project_id="P"), text="x").verdict == AuditStatus.REVIEW
    # 作品 P 命中第 1 条(全局 g)→ review(标准规则对作品也生效)
    llm.set_response(hit1)
    assert svc.run(svc.submit(MaterialType.CORPUS, project_id="P"), text="x").verdict == AuditStatus.REVIEW
    # 物料(无项目)清单只有全局[g];引用第 2 条 → 越界丢弃 → pass(物料不吃项目规则)
    llm.set_response(hit2)
    assert svc.run(svc.submit(MaterialType.CORPUS), text="x").verdict == AuditStatus.PASS
    # 物料命中第 1 条(全局)→ review
    llm.set_response(hit1)
    assert svc.run(svc.submit(MaterialType.CORPUS), text="x").verdict == AuditStatus.REVIEW
    # 别的项目 Q 的作品清单也只有全局[g](p 属 P);引用第 2 条 → 越界 → pass
    llm.set_response(hit2)
    assert svc.run(svc.submit(MaterialType.CORPUS, project_id="Q"), text="x").verdict == AuditStatus.PASS


def test_project_repo_crud():
    r = InMemoryProjectRepo()
    r.add(Project(id="1", name="汽水音乐", created_ms=1))
    r.add(Project(id="2", name="夏日", created_ms=2))
    assert r.get("1").name == "汽水音乐"
    assert r.get_by_name("汽水音乐").id == "1" and r.get_by_name("不存在") is None
    assert [p.id for p in r.list()] == ["1", "2"]              # 按 created_ms 排序
    r.delete("1")
    assert r.get("1") is None


def test_material_query_project_gate():
    def mat(pid):
        return Material(id="x", type=MaterialType.VIDEO, thumb="", source_timecode=0.0, embedding=[],
                        audit_status=AuditStatus.REVIEW, source_job="", project_id=pid)
    assert matches(mat("P"), MaterialQuery(project_id="P"))
    assert not matches(mat(""), MaterialQuery(project_id="P"))
    assert matches(mat(""), MaterialQuery(project_id=""))       # 物料栏 = 无项目
    assert not matches(mat("P"), MaterialQuery(project_id=""))  # 作品不落物料栏
    assert matches(mat("P"), MaterialQuery(project_id=None))    # None = 不筛
