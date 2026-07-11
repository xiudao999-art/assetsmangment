"""真实向量索引 —— PostgreSQL + pgvector(实现 domain.ports.VectorIndex)。
存物料向量并做余弦近邻(HNSW)。单 worker + 线程池:每次操作用短连接(线程安全、简单)。infra→domain。"""
from __future__ import annotations


class PgVectorIndex:
    def __init__(self, dsn: str, dim: int = 1024) -> None:
        self._dsn = dsn
        self._dim = dim
        self._init_schema()

    def _conn(self):
        import psycopg
        conn = psycopg.connect(self._dsn, autocommit=True, connect_timeout=10)
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        return conn

    @staticmethod
    def _vec(vector: list[float]) -> str:
        # pgvector 字面量 '[..]',配合 ::vector 显式转型(避免 list→double precision[] 无 <=> 算子)
        return "[" + ",".join(repr(float(x)) for x in vector) + "]"

    def _init_schema(self) -> None:
        with self._conn() as c:
            c.execute(
                f"CREATE TABLE IF NOT EXISTS material_vectors "
                f"(material_id text PRIMARY KEY, embedding vector({self._dim}))"
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS mv_hnsw ON material_vectors "
                "USING hnsw (embedding vector_cosine_ops)"
            )

    def add(self, material_id: str, vector: list[float]) -> None:
        if not vector or len(vector) != self._dim:
            return  # 维度不符(如假向量)不入库,避免污染
        with self._conn() as c:
            c.execute(
                "INSERT INTO material_vectors (material_id, embedding) VALUES (%s, %s::vector) "
                "ON CONFLICT (material_id) DO UPDATE SET embedding = EXCLUDED.embedding",
                (material_id, self._vec(vector)),
            )

    def query(self, vector: list[float], k: int = 10) -> list[str]:
        return [mid for mid, _ in self.query_scored(vector, k)]

    def query_scored(self, vector: list[float], k: int = 10) -> list[tuple[str, float]]:
        """返回 (material_id, 余弦距离);距离越小越相关(0=同向)。供按相关度阈值过滤,避免搜出无关物料。"""
        if not vector or len(vector) != self._dim:
            return []
        with self._conn() as c:
            rows = c.execute(
                "SELECT material_id, embedding <=> %s::vector AS dist FROM material_vectors "
                "ORDER BY dist LIMIT %s",
                (self._vec(vector), k),
            ).fetchall()
        return [(r[0], float(r[1])) for r in rows]

    def size(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT count(*) FROM material_vectors").fetchone()[0]

    def delete(self, material_id: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM material_vectors WHERE material_id = %s", (material_id,))
