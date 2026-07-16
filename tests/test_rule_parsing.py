"""粘贴审核文案 → 大模型解析成结构化规则 → 预览确认批量落库(闭环③/④)。
本地用 FakeLlm 插桩验证「解析→归一化→落库」管道;线上真大模型用真实卡审文档实测。"""
from fastapi.testclient import TestClient
from app.main import app
from app.api import deps
from app.infrastructure.fakes import FakeLlm

client = TestClient(app)


def _token(name, password):
    return client.post("/users/login", json={"name": name, "password": password}).json()["token"]


def _hdr(token):
    return {"Authorization": f"Bearer {token}"}


def _admin_hdr():
    return _hdr(_token("admin", "admin123"))


def _user_hdr():
    return _hdr(_token("demo", "pw123456"))


def _new_project(name="解析测试项目"):
    return client.post("/admin/projects", json={"name": name}, headers=_admin_hdr()).json()["id"]


# 一份「脏」响应:坏 source_type、空规则(无词无条件)、非法 action、重复关键词 —— 验证归一化 + 丢弃
_MESSY = {"rules": [
    {"category": "网赚风险类", "source_type": "any", "keywords": ["一夜暴富", "躺赚", "躺赚"],
     "condition": "宣扬不劳而获", "action": "review"},
    {"category": "国家标志类", "source_type": "video_frame", "keywords": ["国旗"],
     "condition": "国旗使用不规范", "action": "block"},
    {"category": "乱来", "source_type": "不存在的类型", "keywords": ["测试词"],
     "condition": "", "action": "毁灭"},                        # source_type→any, action→review
    {"category": "空的", "source_type": "any", "keywords": [], "condition": ""},   # 空规则 → 丢弃
]}


def test_parse_rules_service_normalizes_and_drops_empty(monkeypatch):
    monkeypatch.setattr(deps, "_llm", FakeLlm(response=_MESSY))
    drafts = deps.get_audit_service().parse_rules("随便一段文案")
    assert len(drafts) == 3                                      # 空规则被丢弃
    assert drafts[0]["keywords"] == ["一夜暴富", "躺赚"]           # 去重
    assert drafts[2]["source_type"] == "any"                    # 非法来源类型归一
    assert drafts[2]["action"] == "review"                      # 非法动作归一
    assert deps._llm.calls, "应真的调用了大模型解析"


def test_parse_rules_service_empty_text_returns_empty(monkeypatch):
    monkeypatch.setattr(deps, "_llm", FakeLlm(response=_MESSY))
    assert deps.get_audit_service().parse_rules("   ") == []


def test_parse_endpoint_perm_and_validation(monkeypatch):
    monkeypatch.setattr(deps, "_llm", FakeLlm(response=_MESSY))
    pid = _new_project()
    body = {"text": "文案", "project_id": pid}
    assert client.post("/audit/rules/parse", json=body).status_code == 401           # 游客
    assert client.post("/audit/rules/parse", json=body, headers=_user_hdr()).status_code == 403  # 无 audit.rules
    # 缺项目 / 项目不存在 / 空文案 → 400
    assert client.post("/audit/rules/parse", json={"text": "文案"}, headers=_admin_hdr()).status_code == 400
    assert client.post("/audit/rules/parse", json={"text": "文案", "project_id": "nope"},
                       headers=_admin_hdr()).status_code == 400
    assert client.post("/audit/rules/parse", json={"text": "  ", "project_id": pid},
                       headers=_admin_hdr()).status_code == 400


def test_parse_then_bulk_creates_project_scoped_rules(monkeypatch):
    monkeypatch.setattr(deps, "_llm", FakeLlm(response=_MESSY))
    pid = _new_project()
    # 1) 解析(不落库)
    r = client.post("/audit/rules/parse", json={"text": "整篇卡审文案…", "project_id": pid},
                    headers=_admin_hdr())
    assert r.status_code == 200
    drafts = r.json()["rules"]
    assert len(drafts) == 3
    # 解析后规则库还没多(纯预览)
    assert client.get(f"/audit/rules?project={pid}", headers=_admin_hdr()).json()["rules"] == []
    # 2) 确认批量落库
    r2 = client.post("/audit/rules/bulk", json={"rules": drafts, "project_id": pid}, headers=_admin_hdr())
    assert r2.status_code == 200
    assert r2.json()["created"] == 3
    # 3) 规则归到该项目作用域,标准(全局)作用域看不到
    scoped = client.get(f"/audit/rules?project={pid}", headers=_admin_hdr()).json()["rules"]
    assert len(scoped) == 3
    assert all(x["project_id"] == pid for x in scoped)
    assert client.get("/audit/rules?project=", headers=_admin_hdr()).json()["rules"] == []


def test_bulk_skips_empty_and_validates(monkeypatch):
    monkeypatch.setattr(deps, "_llm", FakeLlm(response=_MESSY))
    pid = _new_project()
    rules = [
        {"source_type": "any", "keywords": ["有效词"], "condition": "", "action": "review"},
        {"source_type": "any", "keywords": [], "condition": "", "action": "block"},   # 空 → 跳过
    ]
    r = client.post("/audit/rules/bulk", json={"rules": rules, "project_id": pid}, headers=_admin_hdr())
    assert r.status_code == 200
    assert r.json()["created"] == 1                              # 空规则被跳过
    # 缺项目 → 400;无权限 → 403
    assert client.post("/audit/rules/bulk", json={"rules": rules}, headers=_admin_hdr()).status_code == 400
    assert client.post("/audit/rules/bulk", json={"rules": rules, "project_id": pid},
                       headers=_user_hdr()).status_code == 403


# 解析响应带「严格程度」:严重类给 metaphor、普通给 literal、缺失/非法归一为 metaphor
_LEVELS = {"rules": [
    {"category": "国家政治类", "source_type": "any", "condition": "影射领导人", "keywords": ["领导人"],
     "action": "block", "match_level": "metaphor"},
    {"category": "网赚类", "source_type": "any", "condition": "宣扬躺赚", "keywords": ["躺赚"],
     "action": "review", "match_level": "literal"},
    {"category": "缺级", "source_type": "any", "condition": "某条件", "keywords": ["词"],
     "action": "review"},                                          # 缺 match_level → metaphor 默认
    {"category": "乱级", "source_type": "any", "condition": "另一条件", "keywords": ["词2"],
     "action": "review", "match_level": "乱写"},                    # 非法 → metaphor
]}


def test_parse_rules_normalizes_match_level(monkeypatch):
    monkeypatch.setattr(deps, "_llm", FakeLlm(response=_LEVELS))
    drafts = deps.get_audit_service().parse_rules("文案")
    assert [d["match_level"] for d in drafts] == ["metaphor", "literal", "metaphor", "metaphor"]


def test_rule_gets_sequential_no():
    pid = _new_project()
    a = client.post("/audit/rules", json={"source_type": "any", "condition": "甲", "action": "review",
                                          "project_id": pid}, headers=_admin_hdr()).json()
    b = client.post("/audit/rules", json={"source_type": "any", "condition": "乙", "action": "review",
                                          "project_id": pid}, headers=_admin_hdr()).json()
    assert isinstance(a["no"], int) and a["no"] >= 1
    assert b["no"] == a["no"] + 1                       # 递增
    # 编辑不改编号
    edited = client.put(f"/audit/rules/{a['id']}", json={"source_type": "any", "condition": "甲改",
                        "action": "review", "project_id": pid}, headers=_admin_hdr()).json()
    assert edited["no"] == a["no"]


def test_bulk_rules_get_distinct_no():
    pid = _new_project()
    rules = [{"source_type": "any", "keywords": ["x"], "condition": "一", "action": "review"},
             {"source_type": "any", "keywords": ["y"], "condition": "二", "action": "review"}]
    client.post("/audit/rules/bulk", json={"rules": rules, "project_id": pid}, headers=_admin_hdr())
    got = client.get(f"/audit/rules?project={pid}", headers=_admin_hdr()).json()["rules"]
    nos = sorted(r["no"] for r in got)
    assert len(set(nos)) == 2 and nos[1] == nos[0] + 1   # 两个不同、递增


def test_norm_level_allows_regex():
    from app.api.router import _norm_level
    assert _norm_level("regex") == "regex"
    assert _norm_level("literal") == "literal"
    assert _norm_level("乱写") == "metaphor"           # 非法/缺省仍收敛隐喻


def test_compile_regex_endpoint(monkeypatch):
    monkeypatch.setattr(deps, "_llm", FakeLlm(response={
        "keywords": ["躺赚", "日入"], "regex": r"躺.{0,2}赚|日\s*入\s*\d+"}))
    body = {"text": "真钱躺赚 日入过百 稳赚不赔"}
    assert client.post("/audit/rules/compile-regex", json=body).status_code == 401           # 游客
    assert client.post("/audit/rules/compile-regex", json=body, headers=_user_hdr()).status_code == 403  # 无 audit.rules
    r = client.post("/audit/rules/compile-regex", json=body, headers=_admin_hdr())
    assert r.status_code == 200
    assert r.json()["regex"].startswith("躺") and "躺赚" in r.json()["keywords"]


def test_create_regex_rule_roundtrip():
    pid = _new_project()
    body = {"source_type": "any", "keywords": ["躺赚"], "match_level": "regex",
            "regex": r"躺.{0,2}赚", "action": "review", "project_id": pid}
    created = client.post("/audit/rules", json=body, headers=_admin_hdr()).json()
    assert created["match_level"] == "regex" and created["regex"] == r"躺.{0,2}赚"
    got = client.get(f"/audit/rules?project={pid}", headers=_admin_hdr()).json()["rules"]
    assert got[0]["match_level"] == "regex" and got[0]["regex"] == r"躺.{0,2}赚"


def test_bulk_persists_match_level(monkeypatch):
    monkeypatch.setattr(deps, "_llm", FakeLlm(response=_MESSY))
    pid = _new_project()
    rules = [{"source_type": "any", "keywords": ["严重词"], "condition": "影射", "action": "block",
              "match_level": "metaphor"},
             {"source_type": "any", "keywords": ["普通词"], "condition": "字面", "action": "review",
              "match_level": "literal"}]
    r = client.post("/audit/rules/bulk", json={"rules": rules, "project_id": pid}, headers=_admin_hdr())
    assert r.status_code == 200 and r.json()["created"] == 2
    scoped = client.get(f"/audit/rules?project={pid}", headers=_admin_hdr()).json()["rules"]
    assert sorted(x["match_level"] for x in scoped) == ["literal", "metaphor"]
