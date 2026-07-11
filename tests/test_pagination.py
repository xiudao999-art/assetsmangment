"""服务端翻页/筛选测试(闭环③/④):repo.query 谓词+分页+total,service 分页,API 契约。
全 hermetic:内存 fake + JSON repo(tmp_path),不触云。"""
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.domain.models import Material, MaterialType, AuditStatus
from app.domain.query import MaterialQuery
from app.service.library import LibraryService
from app.service.search import SearchService
from app.infrastructure.fakes import (
    InMemoryMaterialRepo, InMemoryFavoriteRepo, FakeQueryEmbedder,
)
from app.infrastructure.jsonstore import Store, JsonMaterialRepo


def _m(mid, *, type=MaterialType.IMAGE, status=AuditStatus.PASS, owner="",
       public=False, tags=None, desc="", emotion=""):
    return Material(id=mid, type=type, thumb=f"{mid}#t", source_timecode=0.0,
                    embedding=[0.1] * 8, audit_status=status, source_job="",
                    oss_key=f"{mid}.png", description=desc, owner_id=owner,
                    is_public=public, tags=tags or [], ai_emotion=emotion)


@pytest.fixture(params=["mem", "json"])
def repo(request, tmp_path):
    """两种实现都跑,证明 query 行为一致(未来换真表只需让新 adapter 也过这套)。"""
    if request.param == "mem":
        return InMemoryMaterialRepo()
    return JsonMaterialRepo(Store(str(tmp_path / "s.json")))


# ── repo.query:谓词过滤 ──
def test_query_type_filter(repo):
    repo.save(_m("a", type=MaterialType.IMAGE))
    repo.save(_m("b", type=MaterialType.VIDEO))
    repo.save(_m("c", type=MaterialType.IMAGE))
    items, total = repo.query(MaterialQuery(type="image"))
    assert total == 2 and {m.id for m in items} == {"a", "c"}


def test_query_status_filter(repo):
    repo.save(_m("p", status=AuditStatus.PASS))
    repo.save(_m("r", status=AuditStatus.REVIEW))
    repo.save(_m("k", status=AuditStatus.BLOCK))
    items, total = repo.query(MaterialQuery(status="review"))
    assert total == 1 and items[0].id == "r"


def test_query_tag_exact(repo):
    repo.save(_m("a", tags=["项目A", "春季"]))
    repo.save(_m("b", tags=["项目B"]))
    items, total = repo.query(MaterialQuery(tag="项目A"))
    assert total == 1 and items[0].id == "a"


def test_query_keyword_drops_nonmatches(repo):
    repo.save(_m("a", desc="一只可爱的猫"))
    repo.save(_m("b", emotion="温馨"))
    repo.save(_m("c", desc="普通的狗"))
    items, total = repo.query(MaterialQuery(keyword="猫"))
    assert total == 1 and items[0].id == "a"   # 非命中被丢弃(区别于旧 search 保留全部)
    items2, total2 = repo.query(MaterialQuery(keyword="温馨"))
    assert total2 == 1 and items2[0].id == "b"


def test_query_public_pass_isolation(repo):
    repo.save(_m("pp", status=AuditStatus.PASS, public=True))   # 公共+过审 → 命中
    repo.save(_m("pr", status=AuditStatus.PASS, public=False))  # 过审未公开
    repo.save(_m("bp", status=AuditStatus.BLOCK, public=True))  # 公开但被拦
    items, total = repo.query(MaterialQuery(public_only=True, pass_only=True))
    assert total == 1 and items[0].id == "pp"


def test_query_mine_or_semantics(repo):
    repo.save(_m("own", owner="u1"))
    repo.save(_m("fav", owner="u2"))
    repo.save(_m("other", owner="u3"))
    q = MaterialQuery(owner_id="u1", include_ids=frozenset({"fav"}), owner_or_include=True)
    items, total = repo.query(q)
    assert total == 2 and {m.id for m in items} == {"own", "fav"}
    # 叠加类型仍是 AND
    repo.save(_m("own_vid", type=MaterialType.VIDEO, owner="u1"))
    items2, total2 = repo.query(MaterialQuery(
        owner_id="u1", include_ids=frozenset({"fav"}), owner_or_include=True, type="video"))
    assert total2 == 1 and items2[0].id == "own_vid"


# ── repo.query:分页 ──
def test_query_pagination_window(repo):
    for i in range(10):
        repo.save(_m(f"m{i:02d}"))
    page1, total = repo.query(MaterialQuery(offset=0, limit=3))
    assert total == 10 and len(page1) == 3
    tail, total2 = repo.query(MaterialQuery(offset=9, limit=3))
    assert total2 == 10 and len(tail) == 1
    over, total3 = repo.query(MaterialQuery(offset=20, limit=3))
    assert total3 == 10 and over == []
    # 分页序稳定、不重叠
    page2, _ = repo.query(MaterialQuery(offset=3, limit=3))
    assert {m.id for m in page1}.isdisjoint({m.id for m in page2})


def test_query_limit_none_returns_all(repo):
    for i in range(5):
        repo.save(_m(f"x{i}"))
    items, total = repo.query(MaterialQuery(limit=None))
    assert total == 5 and len(items) == 5


def test_query_total_invariant_across_window(repo):
    for i in range(7):
        repo.save(_m(f"t{i}", type=MaterialType.IMAGE))
    _, t0 = repo.query(MaterialQuery(type="image", offset=0, limit=2))
    _, t1 = repo.query(MaterialQuery(type="image", offset=4, limit=2))
    assert t0 == t1 == 7   # total 不随窗口变


# ── service 层 ──
def _lib():
    repo, fav = InMemoryMaterialRepo(), InMemoryFavoriteRepo()
    return LibraryService(repo, fav), repo, fav


def test_service_mine_paging_and_favorites():
    lib, repo, fav = _lib()
    repo.save(_m("own", owner="u1"))
    repo.save(_m("pub", owner="u2", public=True, status=AuditStatus.PASS))
    fav.add("u1", "pub")
    repo.save(_m("noise", owner="u3"))
    items, total = lib.mine("u1")
    assert total == 2 and {m.id for m in items} == {"own", "pub"}
    # 分页
    p1, t = lib.mine("u1", offset=0, limit=1)
    assert t == 2 and len(p1) == 1


def test_service_all_status_filter_and_paging():
    lib, repo, _ = _lib()
    for i in range(6):
        repo.save(_m(f"r{i}", status=AuditStatus.REVIEW))
    repo.save(_m("p", status=AuditStatus.PASS))
    items, total = lib.all(status="review", offset=0, limit=4)
    assert total == 6 and len(items) == 4


def test_service_public_filters_pass_public():
    lib, repo, _ = _lib()
    repo.save(_m("a", public=True, status=AuditStatus.PASS))
    repo.save(_m("b", public=False, status=AuditStatus.PASS))
    items, total = lib.public()
    assert total == 1 and items[0].id == "a"


def test_service_search_pages_and_dedup():
    repo, fav = InMemoryMaterialRepo(), InMemoryFavoriteRepo()
    for i in range(5):
        repo.save(_m(f"c{i}", public=True, status=AuditStatus.PASS, desc="猫咪"))
    svc = SearchService(FakeQueryEmbedder(), repo)   # index=None → 关键词路径
    p1, total = svc.search("猫", offset=0, limit=2)
    p2, total2 = svc.search("猫", offset=2, limit=2)
    assert total == total2 == 5 and len(p1) == 2
    assert {m.id for m in p1}.isdisjoint({m.id for m in p2})   # 分页不重叠


def test_service_search_empty_browses_public():
    repo = InMemoryMaterialRepo()
    repo.save(_m("a", public=True, status=AuditStatus.PASS))
    repo.save(_m("priv", public=False, status=AuditStatus.PASS))
    svc = SearchService(FakeQueryEmbedder(), repo)
    items, total = svc.search("")
    assert total == 1 and items[0].id == "a"


def test_service_search_no_match_empty():
    repo = InMemoryMaterialRepo()
    repo.save(_m("a", public=True, status=AuditStatus.PASS, desc="猫"))
    svc = SearchService(FakeQueryEmbedder(), repo)
    items, total = svc.search("不存在的词")
    assert items == [] and total == 0   # 命中空 → 空页(不再倒出整库)


def test_service_search_keyword_authoritative_no_unrelated():
    # 真的按关键词匹配:搜"猫"只返回含"猫"的,不掺无关的"狗"
    repo = InMemoryMaterialRepo()
    repo.save(_m("cat", public=True, status=AuditStatus.PASS, desc="一只可爱的猫"))
    repo.save(_m("dog", public=True, status=AuditStatus.PASS, desc="一只忠诚的狗"))
    svc = SearchService(FakeQueryEmbedder(), repo)
    items, total = svc.search("猫")
    assert total == 1 and items[0].id == "cat"


def test_service_search_type_narrows():
    repo = InMemoryMaterialRepo()
    repo.save(_m("img", type=MaterialType.IMAGE, public=True, status=AuditStatus.PASS, desc="海边"))
    repo.save(_m("vid", type=MaterialType.VIDEO, public=True, status=AuditStatus.PASS, desc="海边"))
    svc = SearchService(FakeQueryEmbedder(), repo)
    items, total = svc.search("海边", type="video")
    assert total == 1 and items[0].id == "vid"


# ── API 层(TestClient;deps.material_repo 已换 fake、_vector_search=False)──
client = TestClient(app)


def _hdr(name, pw):
    return {"Authorization": "Bearer " + client.post(
        "/users/login", json={"name": name, "password": pw}).json()["token"]}


def test_api_public_paged_shape():
    r = client.get("/library/public", params={"page": 1, "size": 2}).json()
    assert {"total", "page", "size", "count", "items"} <= set(r.keys())
    assert len(r["items"]) <= 2 and r["total"] >= len(r["items"])


def test_api_search_paged_shape():
    r = client.get("/search", params={"q": "", "page": 1, "size": 2}).json()
    assert {"total", "page", "size", "results"} <= set(r.keys())
    assert len(r["results"]) <= 2


def test_api_mine_keeps_flags_and_filters():
    uh = _hdr("demo", "pw123456")
    mid = client.post("/materials", json={"type": "image", "oss_key": "pg1.png"},
                      headers=uh).json()["id"]
    got = client.get("/library/mine", params={"page": 1, "size": 50, "type": "image"},
                     headers=uh).json()
    assert any(m["id"] == mid and m["is_mine"] for m in got["items"])
    # 类型不匹配则查不到
    none = client.get("/library/mine", params={"page": 1, "size": 50, "type": "video"},
                      headers=uh).json()
    assert not any(m["id"] == mid for m in none["items"])


def test_api_size_and_page_clamped():
    assert client.get("/library/public", params={"size": 10000}).status_code == 422
    assert client.get("/library/public", params={"page": 0}).status_code == 422


def test_api_invalid_type_400():
    assert client.get("/library/public", params={"type": "nonsense"}).status_code == 400
