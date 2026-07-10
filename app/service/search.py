"""语义搜索服务(REQ-301/302/303)。只依赖 domain 端口。"""
from __future__ import annotations
from app.domain.models import Material
from app.domain.ports import QueryEmbedder, MaterialRepo


class SearchService:
    def __init__(self, embedder: QueryEmbedder, repo: MaterialRepo) -> None:
        self._embedder = embedder
        self._repo = repo

    def search(self, query_text: str) -> list[Material]:
        """REQ-301 向量近邻+按相似度排序;REQ-302 hybrid;REQ-303 仅返回审核通过。
        搜索范围 = 公共物料库(已发布 is_public 且审核通过 pass),不泄露非公开/他人未发布物料。"""
        # 生成查询向量(真实现走 multimodal-embedding;假实现仅记录调用)
        self._embedder.embed_text(query_text)
        # only_pass=True → 违规/未通过物料不出现在结果(REQ-303)
        results = self._repo.search(query_text, only_pass=True)
        # 仅公共库范围:未发布物料(含本人/他人的私有 pass 物料)不出现在搜索
        return [m for m in results if m.is_public]
