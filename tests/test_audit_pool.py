"""审核有界工作池 + 批量 fan-out 并发(优化 B)。"""
import time
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def _tok(n, p):
    return client.post("/users/login", json={"name": n, "password": p}).json()["token"]


def _admin():
    return {"Authorization": f"Bearer {_tok('admin', 'admin123')}"}


def _user():
    return {"Authorization": f"Bearer {_tok('demo', 'pw123456')}"}


def _wait(tid, hdr, n=250):
    for _ in range(n):
        t = client.get(f"/audit/tasks/{tid}", headers=hdr).json()
        if t.get("status") in ("done", "failed"):
            return t
        time.sleep(0.02)
    return client.get(f"/audit/tasks/{tid}", headers=hdr).json()


def test_audit_pool_is_bounded():
    from app.api import deps
    from app.config import settings
    assert deps.audit_pool._max_workers == max(1, settings.audit_concurrency)   # 有界


def test_batch_audits_run_concurrently(monkeypatch):
    from app.api import deps

    class _SlowVision:
        def describe_image(self, url):
            time.sleep(0.3)
            return "画面"

    monkeypatch.setattr(deps, "_vision", _SlowVision())   # 每帧描述慢 0.3s
    ah, uh = _admin(), _user()
    pid = client.post("/admin/projects", json={"name": "批量并发QC"}, headers=ah).json()["id"]
    files = [("files", (f"w{i}.mp4", f"batch-conc-{i}".encode(), "video/mp4")) for i in range(4)]
    t0 = time.time()
    r = client.post("/audit/batch", data={"video_kind": "work", "project_id": pid}, files=files, headers=uh)
    task_ids = r.json()["task_ids"]
    for tid in task_ids:
        _wait(tid, uh)
    elapsed = time.time() - t0
    assert len(task_ids) == 4
    # 每条作品 ≈ 1 帧 × 0.3s;串行需 ~1.2s,批量并发(池≥4)应明显更快
    assert elapsed < 1.0, f"批量未并发?elapsed={elapsed:.2f}s(串行约 1.2s)"
