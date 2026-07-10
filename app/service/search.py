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

    @staticmethod
    def _kw_match(m: Material, q: str) -> bool:
        # 物料的 情绪/氛围/标签 出现在查询里(如 emotion=温馨 命中查询"温馨治愈的画面")→ 强信号
        fields = [m.ai_emotion, m.ai_atmosphere] + list(m.tags or [])
        if any(f and f in q for f in fields):
            return True
        # 或查询整体命中摘要/描述
        hay = " ".join([m.ai_summary, m.description, m.ai_scene, m.ai_emotion,
                        m.ai_atmosphere] + list(m.tags or []))
        return bool(q and q in hay)

    def search(self, query_text: str) -> list[Material]:
        """REQ-301/303:公共库范围内混合检索——关键词命中(情绪/氛围/标签,强信号)优先,
        再叠 multimodal-embedding 向量近邻(语义);两者去重合并。空查询=浏览全部。"""
        q = (query_text or "").strip()
        qvec = self._embedder.embed_text(q) if q else None   # 生成查询向量(REQ-301)
        kw = [m for m in self._repo.list() if self._public_pass(m) and self._kw_match(m, q)] if q else []
        vec: list = []
        if self._index is not None and qvec is not None:
            try:
                if self._index.size() > 0:
                    ids = self._index.query(qvec, k=50)
                    vec = [m for m in (self._repo.get(i) for i in ids) if self._public_pass(m)]
            except Exception:
                pass  # 向量库异常 → 只用关键词/回退
        if kw or vec:
            seen, out = set(), []
            for m in kw + vec:   # 关键词命中在前,向量补充
                if m and m.id not in seen:
                    seen.add(m.id)
                    out.append(m)
            return out
        # 空查询或都未命中 → 公共库关键词回退
        return [m for m in self._repo.search(q, only_pass=True) if m.is_public]
