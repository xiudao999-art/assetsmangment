"""审核队列「重新审核(按最新规则)」—— 按物料同步重判(闭环③/④)。
复用 svc.recheck:不重转写/抽帧,只用当前规则重跑三波级联,回写报告 + 物料状态 + 关联任务。
hermetic:直接 seed 内存 repo + monkeypatch deps._llm 控制语义判。"""
import uuid
from fastapi.testclient import TestClient
from app.main import app
from app.api import deps
from app.domain.models import (
    Material, MaterialType, AuditStatus, AuditReport, TextSegment, TextSourceType,
    AuditRule, AuditTask, JobStatus,
)
from app.infrastructure.fakes import FakeLlm

client = TestClient(app)


def _tok(n, p):
    return client.post("/users/login", json={"name": n, "password": p}).json()["token"]


def _admin():
    return {"Authorization": f"Bearer {_tok('admin', 'admin123')}"}


def _user():
    return {"Authorization": f"Bearer {_tok('demo', 'pw123456')}"}


def _seed_review_material(*, rule=True, triggered=True):
    """建一条 review 物料 + 一份已存报告(含 corpus segment、可选一条命中项)+ 可选规则。返回 mid。"""
    mid = "rc-" + uuid.uuid4().hex[:8]
    rid = uuid.uuid4().hex
    if rule:
        deps.rule_repo.add(AuditRule(id="rc-rule", source_type="any",
                                     condition="出现导流到站外的联系方式", action="review"))
    trig = ([{"rule_id": "rc-rule", "rule_desc": "出现导流到站外的联系方式",
              "source_type": "text", "action": "review", "reason": "疑似引流",
              "text": "加我微信领福利", "begin_ms": None, "frame_oss_key": ""}] if triggered else [])
    report = AuditReport(verdict=AuditStatus.REVIEW,
                         segments=[TextSegment(TextSourceType.ORIGINAL_TEXT, "加我微信领福利")],
                         triggered=trig, summary="待人工复核")
    deps.report_repo.save(rid, report)
    deps.material_repo.save(Material(
        id=mid, type=MaterialType.CORPUS, thumb="", source_timecode=0.0, embedding=[],
        audit_status=AuditStatus.REVIEW, source_job="", oss_key="", owner_id="admin",
        description="加我微信领福利", audit_report_id=rid))
    return mid


def test_recheck_requires_admin_perm():
    mid = _seed_review_material()
    assert client.post(f"/materials/{mid}/recheck").status_code == 401           # 游客
    assert client.post(f"/materials/{mid}/recheck", headers=_user()).status_code == 403  # 普通用户


def test_recheck_no_report_400():
    mid = "rc-noreport-" + uuid.uuid4().hex[:6]
    deps.material_repo.save(Material(id=mid, type=MaterialType.IMAGE, thumb="", source_timecode=0.0,
                                     embedding=[], audit_status=AuditStatus.REVIEW, source_job="",
                                     oss_key=f"{mid}.png", owner_id="admin"))   # 无 audit_report_id
    assert client.post(f"/materials/{mid}/recheck", headers=_admin()).status_code == 400


def test_recheck_after_rule_edit_clears_red(monkeypatch):
    """核心:管理员删掉触发它的规则后重判 → 转 pass、红标清空、物料状态回写、报告换新。"""
    mid = _seed_review_material()
    monkeypatch.setattr(deps, "_llm", FakeLlm(response={"findings": []}))   # 语义判无命中
    deps.rule_repo.delete("rc-rule")                                        # 规则被删=无适用规则
    old_rid = deps.material_repo.get(mid).audit_report_id
    r = client.post(f"/materials/{mid}/recheck", headers=_admin())
    assert r.status_code == 200
    d = r.json()
    assert d["audit_status"] == "pass" and d["report"]["triggered"] == []
    m = deps.material_repo.get(mid)
    assert m.audit_status == AuditStatus.PASS and m.audit_report_id != old_rid   # 状态回写 + 新报告


def test_recheck_still_flags_with_current_rule(monkeypatch):
    """规则仍在、语义判仍命中 → verdict review、triggered 保留契约字段。"""
    mid = _seed_review_material()
    monkeypatch.setattr(deps, "_llm", FakeLlm(response={"findings": [
        {"rule": 1, "segment": 1, "reason": "仍在引流"}]}))
    r = client.post(f"/materials/{mid}/recheck", headers=_admin())
    assert r.status_code == 200
    d = r.json()
    assert d["audit_status"] == "review" and d["report"]["triggered"]
    hit = next(t for t in d["report"]["triggered"] if t["reason"] == "仍在引流")   # 语义判命中被保留
    assert hit["rule_id"] and hit["action"] and hit["source_type"]                 # 命中项契约字段齐全


def test_recheck_syncs_associated_task(monkeypatch):
    """有关联 AuditTask 时,重判后任务的 verdict/report_id/status 同步,防「待审核任务」页与队列不一致。"""
    mid = _seed_review_material()
    old_rid = deps.material_repo.get(mid).audit_report_id
    tid = "rc-task-" + uuid.uuid4().hex[:6]
    deps.task_repo.save(AuditTask(id=tid, owner_id="admin", name="文字审核",
                                  material_type=MaterialType.CORPUS, material_id=mid,
                                  status=JobStatus.DONE, verdict="review", report_id=old_rid,
                                  created_ms=1))
    monkeypatch.setattr(deps, "_llm", FakeLlm(response={"findings": []}))
    deps.rule_repo.delete("rc-rule")
    assert client.post(f"/materials/{mid}/recheck", headers=_admin()).status_code == 200
    t = deps.task_repo.get(tid)
    new_rid = deps.material_repo.get(mid).audit_report_id
    assert t.verdict == "pass" and t.report_id == new_rid and t.status == JobStatus.DONE
