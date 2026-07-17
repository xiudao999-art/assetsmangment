"""审核规则可编辑:PUT /audit/rules/{id}(闭环③)。"""
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def _tok(n, p):
    return client.post("/users/login", json={"name": n, "password": p}).json()["token"]


def _admin():
    return {"Authorization": f"Bearer {_tok('admin', 'admin123')}"}


def _user():
    return {"Authorization": f"Bearer {_tok('demo', 'pw123456')}"}


def _add_rule(hdr, **kw):
    body = {"source_type": "any", "keywords": [], "condition": "", "action": "block", "project_id": ""}
    body.update(kw)
    return client.post("/audit/rules", json=body, headers=hdr).json()


def test_update_rule_changes_fields():
    from app.api import deps
    ah = _admin()
    created = _add_rule(ah, keywords=["赌博"], condition="旧条件", action="block")
    rid = created["id"]
    assert rid.isdigit()   # 雪花字符串契约:纯数字 str(BIGINT 序列化,防 JS 精度丢失)
    up = client.put(f"/audit/rules/{rid}", json={"source_type": "transcript", "keywords": ["诈骗", "传销"],
                    "condition": "新条件:出现导流", "action": "review", "project_id": ""}, headers=ah)
    assert up.status_code == 200
    d = up.json()
    assert d["id"] == rid and d["condition"] == "新条件:出现导流" and d["action"] == "review"
    assert d["source_type"] == "transcript" and set(d["keywords"]) == {"诈骗", "传销"}
    got = next(x for x in client.get("/audit/rules", headers=ah).json()["rules"] if x["id"] == rid)
    assert got["condition"] == "新条件:出现导流" and got["action"] == "review"   # 持久
    assert next(r for r in deps.rule_repo.list() if r.id == rid).created_by == "admin"  # created_by 保留


def test_rule_match_level_roundtrip_and_default():
    """严格程度(字面/隐喻):默认隐喻(保持现状);可显式设、PUT 可改、非法值归一为隐喻。"""
    ah = _admin()
    # 不带 match_level → 默认 metaphor(隐喻)
    d = _add_rule(ah, keywords=["赌博"])
    assert d["match_level"] == "metaphor"
    # 显式 literal(字面)→ 落库回读一致
    lit = _add_rule(ah, keywords=["引流"], match_level="literal")
    assert lit["match_level"] == "literal"
    got = next(x for x in client.get("/audit/rules", headers=ah).json()["rules"] if x["id"] == lit["id"])
    assert got["match_level"] == "literal"
    # PUT 改成 metaphor
    up = client.put(f"/audit/rules/{lit['id']}", json={"source_type": "any", "keywords": ["引流"],
                    "condition": "", "action": "block", "project_id": "", "match_level": "metaphor"}, headers=ah)
    assert up.status_code == 200 and up.json()["match_level"] == "metaphor"
    # 非法值 → 归一为 metaphor(安全默认)
    bad = _add_rule(ah, keywords=["x"], match_level="乱写")
    assert bad["match_level"] == "metaphor"


def test_update_rule_not_found():
    r = client.put("/audit/rules/nope", json={"source_type": "any", "keywords": ["x"], "condition": "",
                   "action": "block", "project_id": ""}, headers=_admin())
    assert r.status_code == 404


def test_update_rule_bad_project():
    ah = _admin()
    rid = _add_rule(ah, keywords=["x"])["id"]
    r = client.put(f"/audit/rules/{rid}", json={"source_type": "any", "keywords": ["x"], "condition": "",
                   "action": "block", "project_id": "nonexist"}, headers=ah)
    assert r.status_code == 400


def test_update_rule_requires_perm():
    ah = _admin()
    rid = _add_rule(ah, keywords=["x"])["id"]
    body = {"source_type": "any", "keywords": ["y"], "condition": "", "action": "block", "project_id": ""}
    assert client.put(f"/audit/rules/{rid}", json=body, headers=_user()).status_code == 403
    assert client.put(f"/audit/rules/{rid}", json=body).status_code == 401
