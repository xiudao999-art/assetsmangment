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
