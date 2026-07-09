"""API 端到端集成测试(闭环③/④)—— 用 FastAPI TestClient 驱动真实应用。"""
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_health():
    assert client.get("/health").json()["status"] == "ok"


def test_user_register_and_login():  # REQ-601
    client.post("/users/register", json={"name": "api_u", "password": "pw123456"})
    r = client.post("/users/login", json={"name": "api_u", "password": "pw123456"})
    assert r.status_code == 200 and "token" in r.json()


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


def test_admin_grant():  # REQ-702
    assert client.post("/admin/grant", json={"role": "editor", "permission": "materials.edit"}).status_code == 200
