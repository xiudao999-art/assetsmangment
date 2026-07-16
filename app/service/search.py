"""语义搜索服务(REQ-301/302/303)。只依赖 domain 端口。"""
from __future__ import annotations
from typing import Optional
from app.domain.models import Material, AuditStatus
from app.domain.ports import QueryEmbedder, MaterialRepo, VectorIndex
from app.domain.query import MaterialQuery

_CANDIDATE_CAP = 200  # 候选窗口上限:封住向量 k 与排序开销,深翻页时 k 增长但不超此


class SearchService:
    def __init__(self, embedder: QueryEmbedder, repo: MaterialRepo,
                 index: Optional[VectorIndex] = None, max_distance: float = 0.5) -> None:
        self._embedder = embedder
        self._repo = repo
        self._index = index  # 传入真向量索引(pgvector)则走语义近邻;否则纯关键词
        self._max_dist = max_distance  # 语义近邻的相关度阈值(余弦距离);超过=无关,不返回

    def _public_pass(self, m: Optional[Material]) -> bool:
        return bool(m and m.is_public and m.audit_status == AuditStatus.PASS)

    def _passes(self, m, type, tag) -> bool:
        return bool(self._public_pass(m)
                    and (type is None or m.type.value == type)
                    and (tag is None or tag in (m.tags or [])))

    def search(self, query_text: str, *, type: Optional[str] = None, tag: Optional[str] = None,
               offset: int = 0, limit: Optional[int] = None) -> tuple[list[Material], int]:
        """REQ-301/303:公共库范围内检索,返回 (当页, 总数)。
        **关键词命中优先且权威**——真的按关键词匹配(内容/情绪/氛围/场景/标签子串)。
        只有关键词一个都没命中时,才用 multimodal-embedding 语义近邻兜底,且**按相关度阈值过滤**
        (太远的近邻视为无关、不返回),避免"搜一个词却搜出无关物料"。空查询=浏览公共库。"""
        q = (query_text or "").strip()

        # 空查询 = 浏览公共库,直接分页(可扩展、total 精确)
        if not q:
            return self._repo.query(MaterialQuery(
                public_only=True, pass_only=True, type=type, tag=tag,
                offset=offset, limit=limit))

        qvec = self._embedder.embed_text(q)   # REQ-301:文本查询即生成 embedding

        # 关键词候选(权威):repo 做 公共/pass/type/tag/keyword 子串过滤(不命中即丢弃)
        kw, _ = self._repo.query(MaterialQuery(
            public_only=True, pass_only=True, keyword=q, type=type, tag=tag,
            offset=0, limit=_CANDIDATE_CAP))

        ranked = list(kw)
        # 只有关键词零命中,才用语义近邻兜底(且距离阈值内);有命中则不掺入无关近邻
        if not ranked and self._index is not None:
            try:
                if self._index.size() > 0:
                    scored = self._index.query_scored(qvec, k=_CANDIDATE_CAP)
                    for mid, dist in scored:
                        if not (dist <= self._max_dist):
                            continue  # 太远、或 NaN(历史零向量)→ 无关,丢弃(修 fail-open)
                        m = self._repo.get(mid)
                        if self._passes(m, type, tag):
                            ranked.append(m)
            except Exception:
                pass  # 向量库异常 → 只用关键词

        total = len(ranked)
        end = None if limit is None else offset + limit
        return ranked[offset:end], total
