"""大量物料索引服务(REQ-401/402)。只依赖 domain 端口。"""
from __future__ import annotations
from app.domain.models import Material
from app.domain.ports import VectorIndex


class IndexService:
    def __init__(self, index: VectorIndex) -> None:
        self._index = index

    def index_material(self, material: Material) -> None:
        """REQ-402:新物料入库 → 增量写入向量索引。"""
        self._index.add(material.id, material.embedding)

    def query(self, vector: list[float], k: int = 10) -> list[str]:
        """REQ-401:通过 HNSW 索引近邻查询(真实现 P95≤200ms 由 k6 验证)。"""
        return self._index.query(vector, k)
