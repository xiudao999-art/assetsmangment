from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg

from app.config import settings
from app.infrastructure.pg_submission_schema import ensure_submission_tables


def main() -> None:
    dsn = settings.database_url
    if not dsn or dsn.startswith("postgresql://user:pass@localhost"):
        raise RuntimeError("AM_DATABASE_URL 未配置为真实 PostgreSQL 连接串，无法建素材提报表。")
    ensure_submission_tables(dsn)
    print("OK: material_submission")
    q = """
        SELECT table_name, column_name, data_type, udt_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name IN ('material_submission')
        ORDER BY table_name, ordinal_position
    """
    with psycopg.connect(dsn, autocommit=True, connect_timeout=10, options="-c timezone=Asia/Shanghai") as conn:
        rows = conn.execute(q).fetchall()
    for table_name, column_name, data_type, udt_name in rows:
        print(f"{table_name}.{column_name}:{data_type}/{udt_name}")


if __name__ == "__main__":
    main()
