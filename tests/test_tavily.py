"""Tavily 联网搜索适配器:打桩 httpx → 把返回拼成简报文本;打不通 → 空简报兜底(不阻塞审核)。"""


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_tavily_search_builds_brief(monkeypatch):
    import app.infrastructure.tavily as tv
    payload = {"answer": "《晴天》是周杰伦的怀旧情歌",
               "results": [{"title": "歌曲解析", "content": "适合校园回忆场景"},
                           {"title": "乐评", "content": "旋律温暖动人"}]}
    monkeypatch.setattr(tv.httpx, "post", lambda *a, **k: _Resp(payload))
    brief = tv.TavilySearch("key").search("歌曲《晴天》 情绪 场景")
    assert "怀旧情歌" in brief                 # 概述并入简报
    assert "校园回忆场景" in brief             # 结果内容并入简报


def test_tavily_search_failsafe(monkeypatch):
    import app.infrastructure.tavily as tv

    def _boom(*a, **k):
        raise RuntimeError("网络炸了")
    monkeypatch.setattr(tv.httpx, "post", _boom)
    assert tv.TavilySearch("key").search("x") == ""      # 打不通 → 空简报,不抛
    assert tv.TavilySearch("key").search("   ") == ""     # 空查询直接空
