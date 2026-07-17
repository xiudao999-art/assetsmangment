"""真实向量索引 —— PostgreSQL + pgvector(实现 domain.ports.VectorIndex)。
存物料向量并做余弦近邻(HNSW)。遵循全项目 PG 业务表基础字段规范:del_flag 软删。
material_id 为主键(TEXT,存雪花ID字符串)。单 worker + 线程池:每次操作短连接。infra→domain。"""
from __future__ import annotations

from app.infrastructure.snowflake import next_id


class PgVectorIndex:
    def __init__(self, dsn: str, dim: int = 1024, idgen=None) -> None:
        self._dsn = dsn
        self._dim = dim
        self._idgen = idgen or next_id
        self._init_schema()

    def _conn(self):
        import psycopg
        conn = psycopg.connect(self._dsn, autocommit=True, connect_timeout=10)
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        return conn

    @staticmethod
    def _vec(vector: list[float]) -> str:
        return "[" + ",".join(repr(float(x)) for x in vector) + "]"

    def _init_schema(self) -> None:
        with self._conn() as c:
            c.execute(f"""
                CREATE TABLE IF NOT EXISTS material_vectors (
                    material_id TEXT PRIMARY KEY,
                    embedding   vector({self._dim})
                )""")
            # 渐进式迁移:旧表缺基础字段则 ALTER TABLE ADD COLUMN(不丢数据)
            for col, type_default in [
                ("del_flag", "BIGINT NOT NULL DEFAULT 0"),
                ("create_by", "TEXT NOT NULL DEFAULT ''"),
                ("create_time", "TIMESTAMPTZ NOT NULL DEFAULT now()"),
                ("update_by", "TEXT NOT NULL DEFAULT ''"),
                ("update_time", "TIMESTAMPTZ NOT NULL DEFAULT now()"),
            ]:
                c.execute(
                    f"ALTER TABLE material_vectors ADD COLUMN IF NOT EXISTS {col} {type_default}"
                )
            c.execute(
                "CREATE INDEX IF NOT EXISTS mv_hnsw ON material_vectors "
                "USING hnsw (embedding vector_cosine_ops)"
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_mv_live ON material_vectors (del_flag) WHERE del_flag = 0"
            )

    def add(self, material_id: str, vector: list[float]) -> None:
        if not vector or len(vector) != self._dim or not any(vector):
            return
        with self._conn() as c:
            c.execute(
                "INSERT INTO material_vectors (material_id, embedding, create_by, update_by) "
                "VALUES (%s, %s::vector, '', '') "
                "ON CONFLICT (material_id) DO UPDATE SET "
                "embedding = EXCLUDED.embedding, "
                "del_flag = 0, "
                "update_time = now()",
                (material_id, self._vec(vector)),
            )

    def query(self, vector: list[float], k: int = 10) -> list[str]:
        return [mid for mid, _ in self.query_scored(vector, k)]

    def query_scored(self, vector: list[float], k: int = 10) -> list[tuple[str, float]]:
        if not vector or len(vector) != self._dim:
            return []
        with self._conn() as c:
            rows = c.execute(
                "SELECT material_id, embedding <=> %s::vector AS dist FROM material_vectors "
                "WHERE del_flag = 0 ORDER BY dist LIMIT %s",
                (self._vec(vector), k),
            ).fetchall()
        return [(r[0], float(r[1])) for r in rows]

    def size(self) -> int:
        with self._conn() as c:
            return c.execute(
                "SELECT count(*) FROM material_vectors WHERE del_flag = 0"
            ).fetchone()[0]

    def delete(self, material_id: str) -> None:
        """软删:del_flag 置新雪花 ID。"""
        with self._conn() as c:
            c.execute(
                "UPDATE material_vectors SET del_flag = %s, update_time = now() "
                "WHERE material_id = %s AND del_flag = 0",
                (self._idgen(), material_id),
            )
