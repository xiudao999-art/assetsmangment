"""API 端到端集成测试(闭环③/④)—— 用 FastAPI TestClient 驱动真实应用。"""
from fastapi.testclient import TestClient
from uuid import uuid4
from app.main import app

client = TestClient(app)


def _token(name, password):
    return client.post("/users/login", json={"name": name, "password": password}).json()["token"]


def _hdr(token):
    return {"Authorization": f"Bearer {token}"}


def _admin_hdr():
    return _hdr(_token("admin", "admin123"))


def _user_hdr():  # 播种普通用户 demo / pw123456(id=user01)
    return _hdr(_token("demo", "pw123456"))


def test_health():
    assert client.get("/health").json()["status"] == "ok"


def test_user_register_and_login():  # REQ-601
    client.post("/users/register", json={"name": "api_u", "password": "pw123456"})
    r = client.post("/users/login", json={"name": "api_u", "password": "pw123456"})
    assert r.status_code == 200 and "token" in r.json() and r.json()["user"]["role"] == "user"


def test_login_wrong_password_401():
    client.post("/users/register", json={"name": "api_u2", "password": "right"})
    assert client.post("/users/login", json={"name": "api_u2", "password": "wrong"}).status_code == 401


def test_register_duplicate_name_409():
    client.post("/users/register", json={"name": "dupapi", "password": "pw123456"})
    r = client.post("/users/register", json={"name": "dupapi", "password": "another"})
    assert r.status_code == 409


def test_material_crud():  # REQ-101/102/103
    uh = _user_hdr()
    mid = client.post("/materials", json={"type": "image", "oss_key": "api1.png"}, headers=uh).json()["id"]
    assert "Expires" in client.get(f"/materials/{mid}", headers=uh).json()["signed_url"]  # 物主可取
    client.delete(f"/materials/{mid}", headers=uh)
    assert client.get(f"/materials/{mid}", headers=uh).status_code == 404


def test_video_parse_then_searchable():  # REQ-201 → 反解产物经发布后可搜(F2→F3)
    uh, ah = _user_hdr(), _admin_hdr()
    r = client.post("/videos", json={"oss_key": "v1.mp4", "size_bytes": 1000}, headers=uh)
    assert r.json()["material_count"] >= 1
    # 反解产物需管理员审核通过 + 发布,才进公共库范围被搜到
    for m in client.get("/library/all", headers=ah).json()["items"]:
        if m["oss_key"] == "" and m["type"]:  # 反解帧(thumb 派生)
            client.post(f"/materials/{m['id']}/set-audit", json={"status": "pass"}, headers=ah)
            client.post(f"/materials/{m['id']}/publish", headers=ah)
    assert client.get("/search", params={"q": ""}).json()["count"] >= 1


# ── 鉴权/越权回归(修复验证)──
def test_forged_admin_token_rejected():  # 修复:token 无签名可伪造
    forged = {"Authorization": "Bearer token-admin-exp9999"}
    assert client.get("/library/all", headers=forged).status_code == 401       # 伪造不再是 admin
    assert client.post("/admin/grant", json={"role": "user", "permission": "x"},
                       headers=forged).status_code == 401


def test_grant_requires_auth_and_admin():  # 审核/授权是管理员专属
    assert client.post("/admin/grant", json={"role": "editor", "permission": "x"}).status_code == 401  # 游客
    assert client.post("/admin/grant", json={"role": "editor", "permission": "x"},
                       headers=_user_hdr()).status_code == 403                                          # 非管理员


def test_admin_grant_takes_effect():  # REQ-702:RBAC 真接通,grant 即时生效
    ah = _admin_hdr()
    r = client.post("/admin/grant", json={"role": "editor", "permission": "materials.edit"}, headers=ah)
    assert r.status_code == 200 and "materials.edit" in r.json()["permissions"]


def test_delete_requires_owner_or_admin():  # 修复:DELETE 无鉴权可删他人
    uh = _user_hdr()
    mid = client.post("/materials", json={"type": "image", "oss_key": "own.png"}, headers=uh).json()["id"]
    assert client.delete(f"/materials/{mid}").status_code == 401                 # 游客不能删
    # 他人(新注册用户)不能删
    client.post("/users/register", json={"name": "other1", "password": "pw123456"})
    oh = _hdr(_token("other1", "pw123456"))
    assert client.delete(f"/materials/{mid}", headers=oh).status_code == 403
    assert client.delete(f"/materials/{mid}", headers=uh).status_code == 200     # 物主可删


def test_set_audit_requires_admin_and_validates_status():
    uh, ah = _user_hdr(), _admin_hdr()
    mid = client.post("/materials", json={"type": "music", "oss_key": "s.mp3"}, headers=uh).json()["id"]
    assert client.post(f"/materials/{mid}/set-audit", json={"status": "pass"}, headers=uh).status_code == 403
    # 非法状态 → 400 而非 500
    assert client.post(f"/materials/{mid}/set-audit", json={"status": "approved"}, headers=ah).status_code == 400


def test_upload_invalid_type_400():
    uh = _user_hdr()
    r = client.post("/materials/upload", data={"type": "bogus"},
                    files={"file": ("a.bin", b"x", "application/octet-stream")}, headers=uh)
    assert r.status_code == 400


def test_get_material_gate_blocks_review_for_others():  # 修复:GET 绕审核门 + 越权
    uh = _user_hdr()
    mid = client.post("/materials", json={"type": "image", "oss_key": "gate.png"}, headers=uh).json()["id"]
    # 该物料默认 review 且非本人 → 他人取不到签名 URL
    client.post("/users/register", json={"name": "other2", "password": "pw123456"})
    oh = _hdr(_token("other2", "pw123456"))
    assert client.get(f"/materials/{mid}", headers=oh).status_code == 403


def test_cannot_favorite_private_material():  # 修复:收藏他人私有物料
    uh = _user_hdr()
    mid = client.post("/materials", json={"type": "image", "oss_key": "sec.png"}, headers=uh).json()["id"]
    client.post("/users/register", json={"name": "other3", "password": "pw123456"})
    oh = _hdr(_token("other3", "pw123456"))
    assert client.post(f"/materials/{mid}/favorite", headers=oh).status_code == 403


def test_publish_public_favorite_flow():  # 音乐类型 + 发布 + 公共库 + 收藏
    uh, ah = _user_hdr(), _admin_hdr()
    mid = client.post("/materials", json={"type": "music", "oss_key": "song.mp3"}, headers=uh).json()["id"]
    client.post(f"/materials/{mid}/set-audit", json={"status": "pass"}, headers=ah)   # 管理员审核通过
    client.post(f"/materials/{mid}/publish", headers=ah)                               # 管理员发布到公共库
    pub = client.get("/library/public", headers=uh).json()
    assert any(m["id"] == mid for m in pub["items"])                                   # 所有人可见公共库
    client.post(f"/materials/{mid}/favorite", headers=uh)                             # 用户收藏(公共物料)
    mine = client.get("/library/mine", headers=uh).json()
    assert any(m["id"] == mid and m["is_favorited"] for m in mine["items"])            # 进我的物料库
    # 撤出公共库后不再可见
    client.delete(f"/materials/{mid}/publish", headers=ah)
    pub2 = client.get("/library/public", headers=uh).json()
    assert not any(m["id"] == mid for m in pub2["items"])


def test_audit_rules_require_admin():
    assert client.post("/audit/rules", json={"keywords": ["x"]}).status_code == 401           # guest
    assert client.post("/audit/rules", json={"keywords": ["x"]}, headers=_user_hdr()).status_code == 403


def test_rule_exception_append_remove_and_perm():
    ah, uh = _admin_hdr(), _user_hdr()
    rid = client.post("/audit/rules", json={"source_type": "any", "condition": "宣传赌博",
                                            "action": "review", "guidance": "仅明确诱导才算"}, headers=ah).json()["id"]
    # 权限:游客 401 / 普通用户 403
    assert client.post(f"/audit/rules/{rid}/exceptions", json={"text": "x"}).status_code == 401
    assert client.post(f"/audit/rules/{rid}/exceptions", json={"text": "x"}, headers=uh).status_code == 403
    # 追加例外 → 规则的 exceptions +1,guidance 仍在
    r = client.post(f"/audit/rules/{rid}/exceptions",
                    json={"text": "只是提到扑克牌桌游", "note": "AI 说疑似赌博"}, headers=ah)
    assert r.status_code == 200 and len(r.json()["exceptions"]) == 1
    assert r.json()["exceptions"][0]["text"] == "只是提到扑克牌桌游" and r.json()["guidance"] == "仅明确诱导才算"
    # 合成命中(禁词)不能记为规则例外 → 400;不存在规则 → 404
    assert client.post("/audit/rules/blockword/exceptions", json={"text": "x"}, headers=ah).status_code == 400
    assert client.post("/audit/rules/nope/exceptions", json={"text": "x"}, headers=ah).status_code == 404
    # 删除第 0 条例外
    r2 = client.request("DELETE", f"/audit/rules/{rid}/exceptions", params={"index": 0}, headers=ah)
    assert r2.status_code == 200 and r2.json()["exceptions"] == []
    client.delete(f"/audit/rules/{rid}", headers=ah)


def test_update_rule_preserves_guidance_and_exceptions():
    ah = _admin_hdr()
    rid = client.post("/audit/rules", json={"source_type": "any", "condition": "老条件",
                                            "action": "review", "guidance": "尺度说明A"}, headers=ah).json()["id"]
    client.post(f"/audit/rules/{rid}/exceptions", json={"text": "例外甲"}, headers=ah)
    # 编辑规则(改 condition,不传 exceptions)→ exceptions 不能丢;guidance 按请求更新
    up = client.put(f"/audit/rules/{rid}", json={"source_type": "any", "condition": "新条件",
                                                 "action": "review", "guidance": "尺度说明B"}, headers=ah).json()
    assert up["condition"] == "新条件" and up["guidance"] == "尺度说明B"
    assert len(up["exceptions"]) == 1 and up["exceptions"][0]["text"] == "例外甲"   # 例外不随编辑丢失
    client.delete(f"/audit/rules/{rid}", headers=ah)


def _wait_task(task_id, uh, n=150):
    import time
    for _ in range(n):
        t = client.get(f"/audit/tasks/{task_id}", headers=uh).json()
        if t.get("status") in ("done", "failed"):
            return t
        time.sleep(0.02)
    return client.get(f"/audit/tasks/{task_id}", headers=uh).json()


def test_audit_text_blockword_flags_review_and_enters_queue():  # 第一波禁词命中 → 机器转「待审核」进人工队列
    ah, uh = _admin_hdr(), _user_hdr()
    client.post("/admin/blockwords", json={"words": ["赌博"]}, headers=ah)
    r = client.post("/audit/submit", data={"type": "corpus", "content": "这是赌博广告要审一下"}, headers=uh)
    assert r.status_code == 200 and r.json()["status"] == "submitted"
    t = _wait_task(r.json()["task_id"], uh)
    assert t["verdict"] == "review"                 # 机器命中禁词 → 待人工复核(不直接拦截)
    assert any("赌博" in x["reason"] for x in t["report"]["triggered"])
    # 该物料进入人工审核队列(机审已完成、被标记为 review)
    ids = [m["id"] for m in client.get("/audit/queue?size=100", headers=ah).json()["items"]]
    assert t["material_id"] in ids


def test_audit_text_pass_when_clean():
    uh = _user_hdr()
    r = client.post("/audit/submit", data={"type": "corpus", "content": "今天阳光明媚心情很好"}, headers=uh)
    assert r.status_code == 200
    assert _wait_task(r.json()["task_id"], uh)["verdict"] == "pass"


def test_audit_image_async_describes():
    uh = _user_hdr()
    r = client.post("/audit/submit", data={"type": "image"},
                    files={"file": ("audit_a.png", b"img-bytes-unique-A", "image/png")}, headers=uh)
    assert r.status_code == 200 and r.json()["status"] == "submitted"
    t = _wait_task(r.json()["task_id"], uh)
    assert t["report"]["segments"][0]["source_type"] == "image_content"


def test_audit_requires_login():
    assert client.post("/audit/submit", data={"type": "corpus", "content": "hi"}).status_code == 401


def test_upload_dedup_rejects_same_content():  # 同一 owner 库内按内容 MD5 去重
    uh = _user_hdr()
    data = b"dedup-unique-bytes-7788"
    r1 = client.post("/audit/submit", data={"type": "image"},
                     files={"file": ("d.png", data, "image/png")}, headers=uh)
    assert r1.json()["status"] == "submitted"
    r2 = client.post("/audit/submit", data={"type": "image"},
                     files={"file": ("d2.png", data, "image/png")}, headers=uh)
    assert r2.json()["status"] == "duplicate" and r2.json()["material_id"]


def test_audit_tasks_listed_for_owner():
    uh = _user_hdr()
    tid = client.post("/audit/submit", data={"type": "corpus", "content": "待审核列表测试独一内容"},
                      headers=uh).json()["task_id"]
    tasks = client.get("/audit/tasks", headers=uh).json()["tasks"]
    assert any(x["id"] == tid for x in tasks)


def test_audit_tasks_marks_work_in_training():
    ah, uh = _admin_hdr(), _user_hdr()
    uniq = uuid4().hex[:8]
    pid = client.post("/admin/projects", json={"name": f"训练显隐项目QC-{uniq}"}, headers=ah).json()["id"]
    r = client.post("/audit/submit", data={"type": "video", "video_kind": "work", "project_id": pid},
                    files={"file": ("train-flag.mp4", f"train-flag-work-{uniq}".encode(), "video/mp4")}, headers=uh)
    task = _wait_task(r.json()["task_id"], uh)
    before = [x for x in client.get("/audit/tasks", headers=uh).json()["tasks"] if x["id"] == task["id"]][0]
    assert before["in_training"] is False
    rr = client.post(f"/training/projects/{pid}/examples",
                     json={"material_id": task["material_id"], "expected_rule_ids": [], "source_note": ""},
                     headers=ah)
    assert rr.status_code == 200
    after = [x for x in client.get("/audit/tasks", headers=uh).json()["tasks"] if x["id"] == task["id"]][0]
    assert after["in_training"] is True


def test_batch_upload_multiple_files():
    uh = _user_hdr()
    files = [("files", ("bm_a.png", b"\x89PNG-batch-A", "image/png")),
             ("files", ("bm_b.txt", "批量文本甲".encode(), "text/plain")),
             ("files", ("bm_junk.xyz", b"zzz-batch", "application/octet-stream"))]
    r = client.post("/audit/batch", files=files, headers=uh)
    assert r.status_code == 200
    body = r.json()
    assert body["created"] == 2 and body["skipped"] == 1   # 图片+文本受理,未知扩展名跳过
    for tid in body["task_ids"]:
        t = _wait_task(tid, uh)
        assert t["status"] in ("done", "failed") and t["material_id"]


def test_batch_upload_zip_unpacks():
    import io, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("pics/bz_a.png", b"\x89PNG-batch-ZIP")
        z.writestr("notes/bz_b.txt", "批量zip文本乙")
        z.writestr("__MACOSX/x", b"skip")     # 应被过滤
    uh = _user_hdr()
    r = client.post("/audit/batch", files={"files": ("pack.zip", buf.getvalue(), "application/zip")}, headers=uh)
    assert r.status_code == 200 and r.json()["created"] == 2   # zip 解包出 2 个(过滤 __MACOSX)
    for tid in r.json()["task_ids"]:
        assert _wait_task(tid, uh)["material_id"]


def test_batch_dedup_skips_duplicates():  # 批内同内容只受理一个
    uh = _user_hdr()
    files = [("files", ("dup1.png", b"batch-dup-same", "image/png")),
             ("files", ("dup2.png", b"batch-dup-same", "image/png"))]
    r = client.post("/audit/batch", files=files, headers=uh)
    assert r.json()["created"] == 1 and r.json()["skipped"] == 1


def test_batch_requires_login():
    assert client.post("/audit/batch", files={"files": ("a.png", b"x", "image/png")}).status_code == 401


def test_material_tags_owner_only():
    uh = _user_hdr()
    mid = client.post("/materials", json={"type": "image", "oss_key": "tg.png"}, headers=uh).json()["id"]
    r = client.put(f"/materials/{mid}/tags", json={"tags": ["项目A", "春季", "项目A"]}, headers=uh)
    assert r.status_code == 200 and r.json()["tags"] == ["项目A", "春季"]   # 去重
    client.post("/users/register", json={"name": "tgother", "password": "pw123456"})
    oh = _hdr(_token("tgother", "pw123456"))
    assert client.put(f"/materials/{mid}/tags", json={"tags": ["x"]}, headers=oh).status_code == 403


def test_summarize_endpoint():
    uh = _user_hdr()
    mid = client.post("/materials", json={"type": "image", "oss_key": "sm.png"}, headers=uh).json()["id"]
    r = client.post(f"/materials/{mid}/summarize", headers=uh)
    assert r.status_code == 200 and r.json()["ai_summary"] and r.json()["ai_emotions"]


def test_download_only_in_my_library():
    """我的物料库(自己上传/已收藏)可下载;公共库未收藏不可下载。"""
    uh, ah = _user_hdr(), _admin_hdr()
    # 1) 自己上传的可下载
    own = client.post("/materials", json={"type": "image", "oss_key": "dl_own.png"}, headers=uh).json()["id"]
    r = client.get(f"/materials/{own}/download", headers=uh)
    assert r.status_code == 200 and "attachment" in r.json()["download_url"]
    # 2) 游客不可下载
    assert client.get(f"/materials/{own}/download").status_code == 401
    # 3) 管理员发布一条公共物料;另一个用户"未收藏"时不可下载,收藏后可下载
    pubm = client.post("/materials", json={"type": "music", "oss_key": "dl_pub.mp3"}, headers=ah).json()["id"]
    client.post(f"/materials/{pubm}/set-audit", json={"status": "pass"}, headers=ah)
    client.post(f"/materials/{pubm}/publish", headers=ah)
    client.post("/users/register", json={"name": "dluser", "password": "pw123456"})
    dh = _hdr(_token("dluser", "pw123456"))
    assert client.get(f"/materials/{pubm}/download", headers=dh).status_code == 403   # 公共但未收藏 → 不可下载
    client.post(f"/materials/{pubm}/favorite", headers=dh)
    assert client.get(f"/materials/{pubm}/download", headers=dh).status_code == 200   # 收藏进我的库 → 可下载


# ── 账号管理 + 按用户授权(F8 新)──
def test_admin_lists_and_creates_and_deletes_users():
    ah = _admin_hdr()
    us = client.get("/admin/users", headers=ah).json()["users"]
    assert any(u["name"] == "admin" and u["role"] == "admin" for u in us)
    # 创建(默认普通用户)
    r = client.post("/admin/users", json={"name": "acct_new", "password": "pw123456"}, headers=ah)
    assert r.status_code == 200 and r.json()["role"] == "user"
    uid = r.json()["id"]
    assert client.post("/users/login", json={"name": "acct_new", "password": "pw123456"}).status_code == 200
    # 删除 → 登录失败
    assert client.delete(f"/admin/users/{uid}", headers=ah).status_code == 200
    assert client.post("/users/login", json={"name": "acct_new", "password": "pw123456"}).status_code == 401


def test_admin_users_requires_perm():
    assert client.get("/admin/users").status_code == 401              # guest
    assert client.get("/admin/users", headers=_user_hdr()).status_code == 403   # 普通用户无权


def test_cannot_delete_admin_or_self():
    ah = _admin_hdr()
    assert client.delete("/admin/users/admin", headers=ah).status_code == 400    # 管理员不可删


def test_perm_catalog_has_labeled_permissions():
    cat = client.get("/admin/perm-catalog", headers=_admin_hdr()).json()["catalog"]
    keys = {c["key"] for c in cat}
    assert "materials.publish" in keys and all(c["label"] and c["desc"] for c in cat)


def test_per_user_grant_takes_effect_immediately():
    ah = _admin_hdr()
    # 清理上次跑崩残留
    users = client.get("/admin/users", headers=ah).json()["users"]
    for u in users:
        if u["name"] == "grantee":
            client.delete(f"/admin/users/{u['id']}", headers=ah)
    r = client.post("/admin/users", json={"name": "grantee", "password": "pw123456"}, headers=ah)
    assert r.status_code == 200, f"create user failed: {r.text}"
    uid = r.json()["id"]
    gh = _hdr(_token("grantee", "pw123456"))
    assert client.get("/library/all", headers=gh).status_code == 403          # 授权前:无权
    r = client.post(f"/admin/users/{uid}/perms", json={"permissions": ["library.all"]}, headers=ah)
    assert r.status_code == 200 and "library.all" in r.json()["permissions"]
    assert client.get("/library/all", headers=gh).status_code == 200          # 授权后:即时可用
    # 收回
    client.post(f"/admin/users/{uid}/perms", json={"permissions": []}, headers=ah)
    assert client.get("/library/all", headers=gh).status_code == 403
    # 清理
    client.delete(f"/admin/users/{uid}", headers=ah)


def test_set_user_perms_rejects_unknown():
    ah = _admin_hdr()
    r = client.post("/admin/users", json={"name": "grantee2", "password": "pw123456"}, headers=ah)
    assert r.status_code == 200, f"create user failed: {r.text}"
    uid = r.json()["id"]
    r = client.post(f"/admin/users/{uid}/perms", json={"permissions": ["not.a.real.perm", "audit.rules"]}, headers=ah)
    assert r.json()["permissions"] == ["audit.rules"]   # 目录外权限被丢弃
    # 清理
    client.delete(f"/admin/users/{uid}", headers=ah)


# ── 视频 物料/作品 + ≤20s 强制 ──
def test_submit_video_material_too_long_rejected(monkeypatch):
    from app.api import deps
    monkeypatch.setattr(deps.storage, "video_duration_ms", lambda k: 25000)  # 25 秒
    uh = _user_hdr()
    r = client.post("/audit/submit", data={"type": "video", "video_kind": "material"},
                    files={"file": ("long.mp4", b"vid-material-toolong-1", "video/mp4")}, headers=uh)
    assert r.status_code == 200 and r.json()["status"] == "too_long"
    # 被拒的物料应已删除(去重查不到)
    import hashlib
    assert deps.material_repo.by_content_hash("user01", hashlib.md5(b"vid-material-toolong-1").hexdigest()) is None


def test_submit_video_work_not_length_checked(monkeypatch):
    from app.api import deps
    monkeypatch.setattr(deps.storage, "video_duration_ms", lambda k: 25000)  # 25 秒也放行(作品不限长)
    uh = _user_hdr()
    pid = client.post("/admin/projects", json={"name": "作品项目A"}, headers=_admin_hdr()).json()["id"]
    r = client.post("/audit/submit", data={"type": "video", "video_kind": "work", "project_id": pid},
                    files={"file": ("film.mp4", b"vid-work-ok-1", "video/mp4")}, headers=uh)
    assert r.status_code == 200 and r.json()["status"] == "submitted"
    t = _wait_task(r.json()["task_id"], uh)
    assert t["video_kind"] == "work"
    assert any(s["source_type"] == "video_frame" for s in t["report"]["segments"])


def test_submit_video_material_ok_under_limit():
    uh = _user_hdr()   # FakeStorage.video_duration_ms == 8000(8 秒,合规)
    r = client.post("/audit/submit", data={"type": "video", "video_kind": "material"},
                    files={"file": ("short.mp4", b"vid-material-ok-1", "video/mp4")}, headers=uh)
    assert r.status_code == 200 and r.json()["status"] == "submitted"
    assert _wait_task(r.json()["task_id"], uh)["video_kind"] == "material"


def test_dedup_reserve_blocks_inflight_and_saved():
    # 并发/连点去重:同内容「正在处理中」也算重复(修复检查-建库竞态导致的多次重复提交)
    from app.api import deps
    from app.api.router import _dedup_reserve, _dedup_release
    from app.domain.models import Material, MaterialType, AuditStatus
    owner, h = "user01", "dedup-inflight-hash-1"
    existing, dup = _dedup_reserve(owner, h)
    assert existing is None and dup is False          # 首次:登记成功
    _, dup2 = _dedup_reserve(owner, h)
    assert dup2 is True                                # 第二次(还在处理中)→ 重复
    _dedup_release(owner, h)
    _, dup3 = _dedup_reserve(owner, h)
    assert dup3 is False                               # 释放后可再登记
    _dedup_release(owner, h)
    # 库内已落地同 hash → 也算重复
    deps.material_repo.save(Material(id="dz", type=MaterialType.IMAGE, thumb="", source_timecode=0.0,
                                     embedding=[], audit_status=AuditStatus.REVIEW, source_job="",
                                     oss_key="dz.png", owner_id=owner, content_hash=h))
    ex, dup4 = _dedup_reserve(owner, h)
    assert dup4 is True and ex is not None and ex.id == "dz"


def test_failed_audit_keeps_material_for_retry(monkeypatch):
    # 审核失败:任务标失败 + 暴露原因 + 物料保留(不删)→ 可重试;同内容重新上传会被去重挡住(用重试)
    from app.api import deps
    uh = _user_hdr()

    def _boom(url, hints=""):
        raise RuntimeError("反解炸了")
    monkeypatch.setattr(deps._vision, "describe_image", _boom)   # 图片反解抛错 → 审核失败
    data = b"fail-then-retry-unique-bytes-43"
    r = client.post("/audit/submit", data={"type": "image"},
                    files={"file": ("f.png", data, "image/png")}, headers=uh)
    assert r.json()["status"] == "submitted"
    mid = r.json()["material_id"]
    tid = r.json()["task_id"]
    t = _wait_task(tid, uh)
    assert t["status"] == "failed"
    assert t["error"]                                            # 失败原因被暴露(待审核页能看到)
    assert client.get(f"/materials/{mid}", headers=uh).status_code == 200   # 物料保留,不删
    # 重试失败任务 → 审核成功(需 admin)
    monkeypatch.setattr(deps._vision, "describe_image", lambda url, hints="": "现在正常了")
    ah = _admin_hdr()
    rr = client.post(f"/audit/tasks/{tid}/retry", headers=ah)
    assert rr.status_code == 200
    t2 = _wait_task(tid, uh)
    assert t2["status"] == "done"
    # 同内容再传:被去重挡住(应走重试而非重复上传)
    r3 = client.post("/audit/submit", data={"type": "image"},
                     files={"file": ("f3.png", data, "image/png")}, headers=uh)
    assert r3.json()["status"] == "duplicate"


def test_upload_audio_file_as_music_accepted():
    # 歌曲上传:音频文件选「音乐」类型放行(声音/音乐同属音频家族)→ 建成 music 物料(才走联网搜档案)
    uh = _user_hdr()
    r = client.post("/audit/submit", data={"type": "music"},
                    files={"file": ("晴天.mp3", b"fake-song-bytes-unique-music-1", "audio/mpeg")}, headers=uh)
    assert r.status_code == 200 and r.json()["status"] == "submitted"
    from app.api import deps
    assert deps.material_repo.get(r.json()["material_id"]).type.value == "music"


def test_dedup_skips_dead_material(monkeypatch):
    # 举一反三:历史残留 / 竞态遗留的「死上传」(有失败任务的物料)不该挡住重传 → 清掉残留、放行
    import hashlib
    from app.api import deps
    from app.api.router import _dedup_reserve, _dedup_release
    from app.domain.models import Material, MaterialType, AuditStatus, AuditTask, JobStatus
    owner = "user01"
    data = b"dead-residue-unique-bytes-999"
    chash = hashlib.md5(data).hexdigest()
    deps.material_repo.save(Material(id="deadmat1", type=MaterialType.IMAGE, thumb="",
                                     source_timecode=0.0, embedding=[], audit_status=AuditStatus.PROCESSING,
                                     source_job="", oss_key="audit/dead.png", owner_id=owner,
                                     content_hash=chash))
    deps.task_repo.save(AuditTask(id="deadtask1", owner_id=owner, name="dead.png",
                                  material_type=MaterialType.IMAGE, material_id="deadmat1",
                                  content_hash=chash, status=JobStatus.FAILED, created_ms=1))
    existing, dup = _dedup_reserve(owner, chash)
    assert dup is False and existing is None                     # 死上传不算重复 → 放行
    assert deps.material_repo.get("deadmat1") is None            # 残留物料已清
    assert deps.task_repo.get("deadtask1") is None               # 残留失败任务已清
    _dedup_release(owner, chash)


def test_batch_video_kind_follows_tab(monkeypatch):
    # 批量视频按顶部 tab(请求 video_kind)分,不再按时长自动猜。
    # 关键判别:8 秒短片若选「作品」必须当作品(旧的按时长逻辑会误判成物料)。
    from app.api import deps
    uh = _user_hdr()
    monkeypatch.setattr(deps.storage, "video_duration_ms", lambda k: 8000)    # 8 秒
    pid = client.post("/admin/projects", json={"name": "作品项目B"}, headers=_admin_hdr()).json()["id"]
    r = client.post("/audit/batch", data={"video_kind": "work", "project_id": pid},
                    files=[("files", ("shortfilm.mp4", b"batch-shortwork-9", "video/mp4"))], headers=uh)
    assert _wait_task(r.json()["task_ids"][0], uh)["video_kind"] == "work"
    r2 = client.post("/audit/batch", data={"video_kind": "material"},
                     files=[("files", ("shortmat.mp4", b"batch-shortmat-9", "video/mp4"))], headers=uh)
    assert _wait_task(r2.json()["task_ids"][0], uh)["video_kind"] == "material"


def test_batch_material_video_over_20s_blocked(monkeypatch):
    # 物料 ≤20s 护栏在批量同样生效:>20s 的物料视频被拦(任务失败 + 物料删除)
    import hashlib
    from app.api import deps
    uh = _user_hdr()
    monkeypatch.setattr(deps.storage, "video_duration_ms", lambda k: 25000)   # 25 秒
    payload = b"batch-mat-toolong-9"
    r = client.post("/audit/batch", data={"video_kind": "material"},
                    files=[("files", ("toolongbatch.mp4", payload, "video/mp4"))], headers=uh)
    t = _wait_task(r.json()["task_ids"][0], uh)
    assert t["status"] == "failed" and "20" in (t.get("error") or "")
    assert deps.material_repo.by_content_hash("user01", hashlib.md5(payload).hexdigest()) is None


def test_whitelist_crud_and_perm():
    assert client.get("/admin/whitelist").status_code == 401                     # 游客
    assert client.get("/admin/whitelist", headers=_user_hdr()).status_code == 403  # 普通用户
    ah = _admin_hdr()
    client.post("/admin/whitelist", json={"words": ["白名单测试词", "另一个"]}, headers=ah)
    ws = client.get("/admin/whitelist", headers=ah).json()["words"]
    assert "白名单测试词" in ws and "另一个" in ws
    client.request("DELETE", "/admin/whitelist", params={"word": "白名单测试词"}, headers=ah)
    ws2 = client.get("/admin/whitelist", headers=ah).json()["words"]
    assert "白名单测试词" not in ws2
    # 清理
    client.request("DELETE", "/admin/whitelist", params={"word": "另一个"}, headers=ah)


def test_blockwords_crud_and_perm():
    assert client.get("/admin/blockwords").status_code == 401                       # 游客
    assert client.get("/admin/blockwords", headers=_user_hdr()).status_code == 403  # 普通用户无 audit.rules
    ah = _admin_hdr()
    client.post("/admin/blockwords", json={"words": ["硬禁词A", "硬禁词B"]}, headers=ah)
    ws = client.get("/admin/blockwords", headers=ah).json()["words"]
    assert "硬禁词A" in ws and "硬禁词B" in ws
    client.request("DELETE", "/admin/blockwords", params={"word": "硬禁词A"}, headers=ah)
    assert "硬禁词A" not in client.get("/admin/blockwords", headers=ah).json()["words"]
    # 清理
    client.request("DELETE", "/admin/blockwords", params={"word": "硬禁词B"}, headers=ah)


def test_new_material_processing_then_flips(monkeypatch):
    # 提交后物料初始为「审核中(processing)」,机审完才翻成 pass/review
    ah, uh = _admin_hdr(), _user_hdr()
    mid = client.post("/materials", json={"type": "image", "oss_key": "proc1.png"}, headers=uh).json()["id"]
    m = client.get("/materials", headers=ah).json()
    row = [x for x in m["items"] if x["id"] == mid]
    assert row and row[0]["audit_status"] == "processing"    # 初始=审核中,未审完


def test_processing_not_in_review_queue():
    ah, uh = _admin_hdr(), _user_hdr()
    # 直接建一个 processing 物料(未走审核)→ 不应出现在人工审核队列
    mid = client.post("/materials", json={"type": "image", "oss_key": "proc2.png"}, headers=uh).json()["id"]
    ids = [x["id"] for x in client.get("/audit/queue?size=100", headers=ah).json()["items"]]
    assert mid not in ids                                     # 机审中的不进人工队列


def test_set_audit_rejects_processing():
    ah, uh = _admin_hdr(), _user_hdr()
    mid = client.post("/materials", json={"type": "image", "oss_key": "proc3.png"}, headers=uh).json()["id"]
    r = client.post(f"/materials/{mid}/set-audit", json={"status": "processing"}, headers=ah)
    assert r.status_code == 400                              # 人工不能把状态设成 processing


def test_recheck_requires_audit_perm_and_flips_verdict():
    """重新审核:普通用户无权(403);删规则后管理员重判 → review 翻 pass(只对已存报告重判)。"""
    uh, ah = _user_hdr(), _admin_hdr()
    from app.api import deps
    deps._llm.set_response({"findings": [{"rule": 1, "segment": 1, "reason": "命中"}]})   # 语义判命中该规则
    rid = client.post("/audit/rules", json={"source_type": "any", "condition": "赌博暗词RX",
                                            "action": "block"}, headers=ah).json()["id"]
    tid = client.post("/audit/submit", data={"type": "corpus", "content": "含赌博暗词RX的一段文字"},
                      headers=uh).json()["task_id"]
    assert _wait_task(tid, uh)["verdict"] == "review"   # 机器命中 → 待人工复核
    # 普通用户无审核权限
    assert client.post(f"/audit/tasks/{tid}/recheck", headers=uh).status_code == 403
    # 删掉规则 → 管理员重新审核 → 用当前规则(无)重判 → 通过
    client.delete(f"/audit/rules/{rid}", headers=ah)
    assert client.post(f"/audit/tasks/{tid}/recheck", headers=ah).status_code == 200
    assert _wait_task(tid, uh)["verdict"] == "pass"
    # 未完成/无报告的任务不可重判(404 用不存在的 id)
    assert client.post("/audit/tasks/nope/recheck", headers=ah).status_code == 404


def test_audit_queue_carries_media_and_report():
    """审核队列端点:每个待复核物料带内联媒体签名 URL + 机审报告(卡片内直接看直接判)。"""
    ah, uh = _admin_hdr(), _user_hdr()
    assert client.get("/audit/queue").status_code == 401                      # 游客
    assert client.get("/audit/queue", headers=uh).status_code == 403          # 普通用户
    # 造一条待复核 + 带报告的物料:加 review 规则 → 语义判命中 → 转 review
    from app.api import deps
    deps._llm.set_response({"findings": [{"rule": 1, "segment": 1, "reason": "命中"}]})
    rid = client.post("/audit/rules", json={"source_type": "any", "condition": "队列复核词QX",
                                            "action": "review"}, headers=ah).json()["id"]
    tid = client.post("/audit/submit", data={"type": "corpus", "content": "这段含队列复核词QX"},
                      headers=uh).json()["task_id"]
    assert _wait_task(tid, uh)["verdict"] == "review"
    items = client.get("/audit/queue?size=100", headers=ah).json()["items"]
    assert all("media_url" in m and "report" in m for m in items)             # 卡片字段齐全
    hit = [m for m in items if m.get("report") and any("队列复核词QX" in (t.get("text", ""))
                                                       for t in m["report"]["triggered"])]
    assert hit and hit[0]["report"]["verdict"] == "review"                    # 命中原因内联在报告里
    # 图片物料:media_url 是可内联显示/播放的签名 URL(非空)
    mid = client.post("/materials", json={"type": "image", "oss_key": "queue-img.png"}, headers=ah).json()["id"]
    client.post(f"/materials/{mid}/set-audit", json={"status": "review"}, headers=ah)
    img = [m for m in client.get("/audit/queue?size=100", headers=ah).json()["items"] if m["id"] == mid]
    assert img and img[0]["media_url"]                                        # 图片给真实文件签名 URL
    # 清理
    client.delete(f"/materials/{mid}", headers=ah)
    client.delete(f"/audit/rules/{rid}", headers=ah)
    client.delete(f"/audit/tasks/{tid}", headers=ah)


def test_task_verdict_follows_material_after_manual_review():
    """待审核任务的裁定跟随物料现状:管理员在审核队列改判后,任务页立即反映 pass/block,
    不再停留在机审的『待人工复核』(修复:审核队列已清空但任务仍显示待复核)。"""
    ah, uh = _admin_hdr(), _user_hdr()
    from app.api import deps
    deps._llm.set_response({"findings": [{"rule": 1, "segment": 1, "reason": "命中"}]})
    rid = client.post("/audit/rules", json={"source_type": "any", "condition": "跟随复核词QZ",
                                            "action": "review"}, headers=ah).json()["id"]
    tid = client.post("/audit/submit", data={"type": "corpus", "content": "含跟随复核词QZ的一段"},
                      headers=uh).json()["task_id"]
    t = _wait_task(tid, uh)
    assert t["verdict"] == "review"                                    # 机审 → 待人工复核
    mid = t["material_id"]
    # 管理员在审核队列里放行该物料
    client.post(f"/materials/{mid}/set-audit", json={"status": "pass"}, headers=ah)
    assert client.get(f"/audit/tasks/{tid}", headers=uh).json()["verdict"] == "pass"   # 任务跟随 → 通过
    # 改判为拦截 → 任务也跟随
    client.post(f"/materials/{mid}/set-audit", json={"status": "block"}, headers=ah)
    assert client.get(f"/audit/tasks/{tid}", headers=uh).json()["verdict"] == "block"
    # 列表接口同样跟随
    assert any(x["id"] == tid and x["verdict"] == "block"
               for x in client.get("/audit/tasks", headers=ah).json()["tasks"])
    # 清理
    client.delete(f"/materials/{mid}", headers=ah)
    client.delete(f"/audit/rules/{rid}", headers=ah)
    client.delete(f"/audit/tasks/{tid}", headers=ah)


def test_audit_submit_rejects_wrong_or_bad_file():
    """内容审核单条上传的错误提示:类型不符 / 格式不支持 / 空文件 都要 400 + 明确原因。"""
    uh = _user_hdr()
    # 选「视频」却传图片 → 类型不符
    r = client.post("/audit/submit", data={"type": "video"},
                    files={"file": ("pic.jpg", b"imgbytes", "image/jpeg")}, headers=uh)
    assert r.status_code == 400 and "不符" in r.json()["detail"]
    # 不支持的格式
    r2 = client.post("/audit/submit", data={"type": "image"},
                     files={"file": ("mal.exe", b"x", "application/octet-stream")}, headers=uh)
    assert r2.status_code == 400 and "不支持" in r2.json()["detail"]
    # 空文件
    r3 = client.post("/audit/submit", data={"type": "image"},
                     files={"file": ("empty.png", b"", "image/png")}, headers=uh)
    assert r3.status_code == 400 and ("空" in r3.json()["detail"] or "缺少" in r3.json()["detail"])


def test_audit_submit_rejects_over_1gb(monkeypatch):
    """单文件 >1GB → 413 + 提示。用小上限模拟(不真传 1GB)。"""
    monkeypatch.setattr("app.api.router._MAX_UPLOAD", 10)
    uh = _user_hdr()
    r = client.post("/audit/submit", data={"type": "image"},
                    files={"file": ("big.png", b"0123456789ABCDEF", "image/png")}, headers=uh)  # 16 > 10
    assert r.status_code == 413 and "1GB" in r.json()["detail"]


def test_batch_skips_oversized_and_unsupported_with_reasons(monkeypatch):
    """批量:超 1GB / 格式不支持 的文件被跳过,并按原因分类回报。"""
    monkeypatch.setattr("app.api.router._MAX_UPLOAD", 10)
    uh = _user_hdr()
    r = client.post("/audit/batch", data={"video_kind": "material"},
                    files=[("files", ("big.png", b"0123456789ABCDEF", "image/png")),      # 超限
                           ("files", ("bad.exe", b"y", "application/octet-stream"))],       # 不支持
                    headers=uh)
    d = r.json()
    assert d["skipped_big"] == 1 and d["skipped_type"] == 1 and d["created"] == 0
    assert d["skipped"] == 2


def test_projects_crud_perm_and_dupe():
    ah, uh = _admin_hdr(), _user_hdr()
    assert client.post("/admin/projects", json={"name": "X项目"}).status_code == 401           # 游客
    assert client.post("/admin/projects", json={"name": "X项目"}, headers=uh).status_code == 403  # 普通用户
    pid = client.post("/admin/projects", json={"name": "汽水音乐QC"}, headers=ah).json()["id"]
    assert client.post("/admin/projects", json={"name": "汽水音乐QC"}, headers=ah).status_code == 409  # 重名
    names = [p["name"] for p in client.get("/projects", headers=uh).json()["projects"]]           # 登录即可列
    assert "汽水音乐QC" in names
    empty = client.post("/admin/projects", json={"name": "空项目QC"}, headers=ah).json()["id"]
    assert client.delete(f"/admin/projects/{empty}", headers=ah).status_code == 200               # 空项目可删


def test_submit_work_requires_existing_project_and_lands_in_project_queue():
    uh, ah = _user_hdr(), _admin_hdr()
    # 作品无项目 → 400
    r = client.post("/audit/submit", data={"type": "video", "video_kind": "work"},
                    files={"file": ("w.mp4", b"proj-w-1", "video/mp4")}, headers=uh)
    assert r.status_code == 400 and "项目" in r.json()["detail"]
    # 不存在的项目 → 400
    r2 = client.post("/audit/submit", data={"type": "video", "video_kind": "work", "project_id": "nope"},
                     files={"file": ("w2.mp4", b"proj-w-2", "video/mp4")}, headers=uh)
    assert r2.status_code == 400
    # 合法项目 → 落 project_id;进该项目队列、不进物料栏
    pid = client.post("/admin/projects", json={"name": "队列项目QC"}, headers=ah).json()["id"]
    t = _wait_task(client.post("/audit/submit",
                   data={"type": "video", "video_kind": "work", "project_id": pid},
                   files={"file": ("w3.mp4", b"proj-w-3", "video/mp4")}, headers=uh).json()["task_id"], uh)
    assert t["project_id"] == pid
    mid = t["material_id"]
    # 浏览:库里按项目筛能查到该作品,project="" 的物料栏里没有它
    assert any(m["id"] == mid for m in client.get(f"/library/all?project={pid}", headers=ah).json()["items"])
    assert not any(m["id"] == mid for m in client.get("/library/all?project=", headers=ah).json()["items"])
    # 审核栏:强制转 review → 进该项目审核 tab、不进物料 tab
    client.post(f"/materials/{mid}/set-audit", json={"status": "review"}, headers=ah)
    assert any(m["id"] == mid for m in client.get(f"/audit/queue?project={pid}", headers=ah).json()["items"])
    assert not any(m["id"] == mid for m in client.get("/audit/queue", headers=ah).json()["items"])  # 物料栏无它


def test_delete_project_blocked_when_it_has_work():
    uh, ah = _user_hdr(), _admin_hdr()
    pid = client.post("/admin/projects", json={"name": "有作品QC"}, headers=ah).json()["id"]
    _wait_task(client.post("/audit/submit",
               data={"type": "video", "video_kind": "work", "project_id": pid},
               files={"file": ("wk.mp4", b"proj-has-work-1", "video/mp4")}, headers=uh).json()["task_id"], uh)
    r = client.delete(f"/admin/projects/{pid}", headers=ah)
    assert r.status_code == 400 and "作品" in r.json()["detail"]


# ── 作品必须归属项目:保证任何时候都有可选项目(修「作品上传失败:一个项目都没有」)──
def test_projects_list_self_heals_when_empty(monkeypatch):
    from app.api import deps
    from app.infrastructure.fakes import InMemoryProjectRepo
    monkeypatch.setattr(deps, "project_repo", InMemoryProjectRepo())
    assert deps.project_repo.list() == []                        # 起始空
    ps = client.get("/projects", headers=_user_hdr()).json()["projects"]
    assert len(ps) >= 1                                          # GET 自愈补默认项目 → 下拉永不为空
    ps2 = client.get("/projects", headers=_user_hdr()).json()["projects"]
    assert len(ps2) == len(ps)                                   # 幂等,不重复补


def test_cannot_delete_last_project(monkeypatch):
    from app.api import deps
    from app.infrastructure.fakes import InMemoryProjectRepo
    monkeypatch.setattr(deps, "project_repo", InMemoryProjectRepo())
    ah = _admin_hdr()
    p1 = client.post("/admin/projects", json={"name": "唯一项目QC"}, headers=ah).json()["id"]
    r = client.delete(f"/admin/projects/{p1}", headers=ah)
    assert r.status_code == 400 and "保留" in r.json()["detail"]  # 最后一个不能删(否则作品没项目可选)
    client.post("/admin/projects", json={"name": "第二项目QC"}, headers=ah)
    assert client.delete(f"/admin/projects/{p1}", headers=ah).status_code == 200  # 有多个 → 可删


def test_work_submit_uses_self_healed_project(monkeypatch):
    from app.api import deps
    from app.infrastructure.fakes import InMemoryProjectRepo
    monkeypatch.setattr(deps, "project_repo", InMemoryProjectRepo())
    uh = _user_hdr()
    pid = client.get("/projects", headers=uh).json()["projects"][0]["id"]   # 自愈拿到默认项目
    r = client.post("/audit/submit", data={"type": "video", "video_kind": "work", "project_id": pid},
                    files={"file": ("selfheal.mp4", b"selfheal-work-1", "video/mp4")}, headers=uh)
    assert r.status_code == 200 and r.json()["status"] == "submitted"


def test_project_scoped_rules_filter_and_queue_tabs():
    ah = _admin_hdr()
    pid = client.post("/admin/projects", json={"name": "规则项目QC"}, headers=ah).json()["id"]
    client.post("/audit/rules", json={"source_type": "any", "keywords": ["项目违规词QC"],
                                      "action": "review", "project_id": pid}, headers=ah)
    client.post("/audit/rules", json={"source_type": "any", "keywords": ["标准违规词QC"],
                                      "action": "block"}, headers=ah)   # 标准规则(无项目)
    prules = client.get(f"/audit/rules?project={pid}", headers=ah).json()["rules"]
    assert len(prules) == 1 and prules[0]["project_id"] == pid
    std = client.get("/audit/rules?project=", headers=ah).json()["rules"]
    assert std and all(r["project_id"] == "" for r in std)
    # 不存在项目的规则 → 400
    assert client.post("/audit/rules", json={"keywords": ["x"], "project_id": "nope"},
                       headers=ah).status_code == 400
    # 审核栏 tabs:项目在前、物料栏("")放最后(与上传页项目优先一致)
    keys = [t["key"] for t in client.get("/audit/queue/tabs", headers=ah).json()["tabs"]]
    assert "" in keys and pid in keys
    assert keys[-1] == "" and keys.index(pid) < keys.index("")   # 物料最后、项目在前
