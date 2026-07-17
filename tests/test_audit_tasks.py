"""待审核任务 + 内容去重 仓储测试(hermetic:内存 + JSON 双实现)。"""
import pytest
from app.domain.models import Material, MaterialType, AuditStatus, AuditTask, JobStatus
from app.infrastructure.fakes import InMemoryMaterialRepo, InMemoryAuditTaskRepo
from app.infrastructure.jsonstore import Store, JsonMaterialRepo, JsonAuditTaskRepo


def _m(mid, owner, chash):
    return Material(id=mid, type=MaterialType.IMAGE, thumb="", source_timecode=0.0, embedding=[],
                    audit_status=AuditStatus.REVIEW, source_job="", oss_key=f"{mid}.png",
                    owner_id=owner, content_hash=chash)


@pytest.fixture(params=["mem", "json"])
def mrepo(request, tmp_path):
    if request.param == "mem":
        return InMemoryMaterialRepo()
    return JsonMaterialRepo(Store(str(tmp_path / "s.json")))


def test_by_content_hash_owner_scoped(mrepo):
    mrepo.save(_m("a", "u1", "H1"))
    mrepo.save(_m("b", "u2", "H1"))               # 同 hash 不同 owner
    assert mrepo.by_content_hash("u1", "H1").id == "a"   # 各自库内命中,互不影响
    assert mrepo.by_content_hash("u2", "H1").id == "b"
    assert mrepo.by_content_hash("u1", "H2") is None     # 不同 hash 不命中
    assert mrepo.by_content_hash("u1", "") is None        # 空 hash 从不命中


def _t(tid, owner, created, status=JobStatus.PENDING):
    return AuditTask(id=tid, owner_id=owner, name=tid, material_type=MaterialType.IMAGE,
                     status=status, created_ms=created)


@pytest.fixture(params=["mem", "json"])
def trepo(request, tmp_path):
    if request.param == "mem":
        return InMemoryAuditTaskRepo()
    return JsonAuditTaskRepo(Store(str(tmp_path / "t.json")))


def test_task_repo_owner_scope_and_order(trepo):
    trepo.save(_t("t1", "u1", 100))
    trepo.save(_t("t2", "u1", 300))
    trepo.save(_t("t3", "u2", 200))
    assert [t.id for t in trepo.list_for("u1")] == ["t2", "t1"]   # 按 created 倒序,仅本人
    assert {t.id for t in trepo.list_all()} == {"t1", "t2", "t3"}
    trepo.delete("t1")
    assert trepo.get("t1") is None


def test_task_repo_status_writeback(trepo):
    trepo.save(_t("x", "u1", 1, JobStatus.PENDING))
    t = trepo.get("x"); t.status = JobStatus.DONE; t.verdict = "pass"; trepo.save(t)
    assert trepo.get("x").status == JobStatus.DONE and trepo.get("x").verdict == "pass"


def test_json_task_reset_stuck_on_reload(tmp_path):
    """容器重启会杀后台线程:重载后未结束任务标 failed,已完成不变(页面不再假装审核中)。"""
    path = str(tmp_path / "r.json")
    r = JsonAuditTaskRepo(Store(path))
    r.save(_t("run", "u1", 1, JobStatus.RUNNING))
    r.save(_t("done", "u1", 2, JobStatus.DONE))
    r2 = JsonAuditTaskRepo(Store(path))   # 模拟重启:新建 Store 触发 _load
    assert r2.get("run").status == JobStatus.FAILED and "中断" in r2.get("run").error
    assert r2.get("done").status == JobStatus.DONE


def test_task_video_kind_persists_and_defaults(tmp_path):
    path = str(tmp_path / "vk.json")
    r = JsonAuditTaskRepo(Store(path))
    r.save(AuditTask(id="w", owner_id="u1", name="film.mp4", material_type=MaterialType.VIDEO,
                     status=JobStatus.DONE, created_ms=1, video_kind="work"))
    assert JsonAuditTaskRepo(Store(path)).get("w").video_kind == "work"   # 存/取
    # 旧任务(state.json.bak 里缺 video_kind)重载 → 默认 material
    from app.infrastructure.jsonstore import Store as _S
    s = _S(path)
    d = s._task_to_dict(s.audit_tasks["w"]); d.pop("video_kind")
    assert s._task_from_dict(d).video_kind == "material"


def test_whitelist_repo_persist(tmp_path):
    from app.infrastructure.jsonstore import JsonWhitelistRepo
    path = str(tmp_path / "wl.json")
    r = JsonWhitelistRepo(Store(path))
    r.add("词一"); r.add("词二"); r.add("  "); r.add("词一")   # 空/重复忽略
    assert r.words() == {"词一", "词二"} and r.list() == ["词一", "词二"]
    r.remove("词一")
    assert JsonWhitelistRepo(Store(path)).words() == {"词二"}   # 持久化(重载)
