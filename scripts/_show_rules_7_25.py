"""查看 Rule #7 和 #25 的完整 guidance。"""
import os, dotenv, psycopg

dotenv.load_dotenv(".env")
dsn = os.getenv("AM_DATABASE_URL")

with psycopg.connect(dsn, autocommit=True) as conn:
    rows = conn.execute(
        "SELECT id, no, condition, guidance FROM audit_rule WHERE del_flag=0 AND no IN (7, 25) ORDER BY no"
    ).fetchall()
    for r in rows:
        g = r[3] or ""
        print(f"=== Rule #{r[1]} ===")
        print(f"  condition: {r[2]}")
        print(f"  guidance ({len(g)} chars):")
        print(g)
        print()
