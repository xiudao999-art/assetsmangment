"""API 端到端集成测试(闭环③/④)—— 用 FastAPI TestClient 驱动真实应用。"""
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
USER = {"Authorization": "Bearer token-user01-exp"}


def _admin_token():
    r = client.post("/users/login", json={"name": "admin", "password": "admin123"})
    return r.json()["token"]


def _admin_hdr():
    return {"Authorization": f"Bearer {_admin_token()}"}


def test_health():
    assert client.get("/health").json()["status"] == "ok"


def test_user_register_and_login():  # REQ-601
    client.post("/users/register", json={"name": "api_u", "password": "pw123456"})
    r = client.post("/users/login", json={"name": "api_u", "password": "pw123456"})
    assert r.status_code == 200 and "token" in r.json() and r.json()["user"]["role"] == "user"


def test_login_wrong_password_401():
    client.post("/users/register", json={"name": "api_u2", "password": "right"})
    assert client.post("/users/login", json={"name": "api_u2", "password": "wrong"}).status_code == 401


def test_material_crud():  # REQ-101/102/103
    mid = client.post("/materials", json={"type": "image", "oss_key": "api1.png"}).json()["id"]
    assert "Expires" in client.get(f"/materials/{mid}").json()["signed_url"]
    client.delete(f"/materials/{mid}")
    assert client.get(f"/materials/{mid}").status_code == 404


def test_video_parse_then_searchable():  # REQ-201 → 反解产物可搜(F2→F3)
    r = client.post("/videos", json={"oss_key": "v1.mp4", "size_bytes": 1000})
    assert r.json()["material_count"] >= 1
    assert client.get("/search", params={"q": ""}).json()["count"] >= 1


def test_grant_requires_admin():  # 审核/授权是管理员专属
    assert client.post("/admin/grant", json={"role": "editor", "permission": "x"}).status_code == 403


def test_admin_grant():  # REQ-702(带管理员鉴权)
    r = client.post("/admin/grant", json={"role": "editor", "permission": "materials.edit"}, headers=_admin_hdr())
    assert r.status_code == 200


def test_set_audit_requires_admin():  # 普通用户不能审核
    mid = client.post("/materials", json={"type": "music", "oss_key": "s.mp3"}).json()["id"]
    assert client.post(f"/materials/{mid}/set-audit", json={"status": "pass"}, headers=USER).status_code == 403


def test_publish_public_favorite_flow():  # 音乐类型 + 发布 + 公共库 + 收藏
    ah = _admin_hdr()
    mid = client.post("/materials", json={"type": "music", "oss_key": "song.mp3"}).json()["id"]
    client.post(f"/materials/{mid}/set-audit", json={"status": "pass"}, headers=ah)   # 管理员审核通过
    client.post(f"/materials/{mid}/publish", headers=ah)                               # 管理员发布到公共库
    pub = client.get("/library/public", headers=USER).json()
    assert any(m["id"] == mid for m in pub["items"])                                   # 所有人可见公共库
    client.post(f"/materials/{mid}/favorite", headers=USER)                            # 用户收藏
    mine = client.get("/library/mine", headers=USER).json()
    assert any(m["id"] == mid and m["is_favorited"] for m in mine["items"])            # 进我的物料库
