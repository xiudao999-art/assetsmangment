"""语义搜索服务(REQ-301/302/303)。只依赖 domain 端口。"""
from __future__ import annotations
from typing import Optional
from app.domain.models import Material, AuditStatus
from app.domain.ports import QueryEmbedder, MaterialRepo, VectorIndex


class SearchService:
    def __init__(self, embedder: QueryEmbedder, repo: MaterialRepo,
                 index: Optional[VectorIndex] = None) -> None:
        self._embedder = embedder
        self._repo = repo
        self._index = index  # 传入真向量索引(pgvector)则走语义近邻;否则关键词回退

    def _public_pass(self, m: Optional[Material]) -> bool:
        return bool(m and m.is_public and m.audit_status == AuditStatus.PASS)

    def search(self, query_text: str) -> list[Material]:
        """REQ-301 向量近邻+按相似度排序;REQ-303 仅公共库范围(已发布 is_public 且 pass)。
        有真向量索引(pgvector)→ multimodal-embedding 查询向量做余弦近邻;否则关键词回退。"""
        qvec = self._embedder.embed_text(query_text)
        if self._index is not None and query_text.strip():
            try:
                if self._index.size() > 0:
                    ids = self._index.query(qvec, k=50)
                    mats = [self._repo.get(i) for i in ids]
                    return [m for m in mats if self._public_pass(m)]  # 按相似度排序 + 仅公共库
            except Exception:
                pass  # 向量库异常 → 回退关键词,不影响可用性
        results = self._repo.search(query_text, only_pass=True)
        return [m for m in results if m.is_public]
