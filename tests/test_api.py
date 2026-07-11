"""API 端到端集成测试(闭环③/④)—— 用 FastAPI TestClient 驱动真实应用。"""
from fastapi.testclient import TestClient
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


def _wait_task(task_id, uh, n=150):
    import time
    for _ in range(n):
        t = client.get(f"/audit/tasks/{task_id}", headers=uh).json()
        if t.get("status") in ("done", "failed"):
            return t
        time.sleep(0.02)
    return client.get(f"/audit/tasks/{task_id}", headers=uh).json()


def test_audit_text_keyword_block():  # 提交异步受理 → 「待审核」任务出裁定
    ah, uh = _admin_hdr(), _user_hdr()
    client.post("/audit/rules", json={"source_type": "any", "keywords": ["赌博"], "action": "block"}, headers=ah)
    r = client.post("/audit/submit", data={"type": "corpus", "content": "这是赌博广告要审一下"}, headers=uh)
    assert r.status_code == 200 and r.json()["status"] == "submitted"
    t = _wait_task(r.json()["task_id"], uh)
    assert t["verdict"] == "block"
    assert any("赌博" in x["reason"] for x in t["report"]["triggered"])


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
    assert r.status_code == 200 and r.json()["ai_summary"] and r.json()["ai_emotion"]


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
    uid = client.post("/admin/users", json={"name": "grantee", "password": "pw123456"}, headers=ah).json()["id"]
    gh = _hdr(_token("grantee", "pw123456"))
    assert client.get("/library/all", headers=gh).status_code == 403          # 授权前:无权
    r = client.post(f"/admin/users/{uid}/perms", json={"permissions": ["library.all"]}, headers=ah)
    assert r.status_code == 200 and "library.all" in r.json()["permissions"]
    assert client.get("/library/all", headers=gh).status_code == 200          # 授权后:即时可用
    # 收回
    client.post(f"/admin/users/{uid}/perms", json={"permissions": []}, headers=ah)
    assert client.get("/library/all", headers=gh).status_code == 403


def test_set_user_perms_rejects_unknown():
    ah = _admin_hdr()
    uid = client.post("/admin/users", json={"name": "grantee2", "password": "pw123456"}, headers=ah).json()["id"]
    r = client.post(f"/admin/users/{uid}/perms", json={"permissions": ["not.a.real.perm", "audit.rules"]}, headers=ah)
    assert r.json()["permissions"] == ["audit.rules"]   # 目录外权限被丢弃


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
    r = client.post("/audit/submit", data={"type": "video", "video_kind": "work"},
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


def test_batch_video_kind_follows_tab(monkeypatch):
    # 批量视频按顶部 tab(请求 video_kind)分,不再按时长自动猜。
    # 关键判别:8 秒短片若选「作品」必须当作品(旧的按时长逻辑会误判成物料)。
    from app.api import deps
    uh = _user_hdr()
    monkeypatch.setattr(deps.storage, "video_duration_ms", lambda k: 8000)    # 8 秒
    r = client.post("/audit/batch", data={"video_kind": "work"},
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


def test_recheck_requires_audit_perm_and_flips_verdict():
    """重新审核:普通用户无权(403);删规则后管理员重判 → block 翻 pass(只对已存报告重判)。"""
    uh, ah = _user_hdr(), _admin_hdr()
    rid = client.post("/audit/rules", json={"source_type": "any", "keywords": ["赌博暗词RX"],
                                            "action": "block"}, headers=ah).json()["id"]
    tid = client.post("/audit/submit", data={"type": "corpus", "content": "含赌博暗词RX的一段文字"},
                      headers=uh).json()["task_id"]
    assert _wait_task(tid, uh)["verdict"] == "block"
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
    # 造一条待复核 + 带报告的物料:加 review 规则 → 提交 corpus → 命中转 review
    rid = client.post("/audit/rules", json={"source_type": "any", "keywords": ["队列复核词QX"],
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
    rid = client.post("/audit/rules", json={"source_type": "any", "keywords": ["跟随复核词QZ"],
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
