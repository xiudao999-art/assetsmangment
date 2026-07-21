"""列出当前所有在用规则，便于审查 condition 是否冗余。"""
import os, json, dotenv, psycopg

dotenv.load_dotenv(".env")
dsn = os.getenv("AM_DATABASE_URL")

with psycopg.connect(dsn, autocommit=True) as conn:
    rows = conn.execute(
        "SELECT id, no, keywords, condition, guidance, match_level, source_type, action "
        "FROM audit_rule WHERE del_flag=0 ORDER BY no"
    ).fetchall()

    for r in rows:
        cond = r[3] or ""
        guide = r[4] or ""
        print(f"=== Rule #{r[1]} (id={r[0]}) ===")
        print(f"  source_type: {r[6]}")
        print(f"  match_level: {r[5]}")
        print(f"  action: {r[7]}")
        print(f"  keywords: {json.dumps(r[2], ensure_ascii=False)}")
        print(f"  condition ({len(cond)} chars): {cond}")
        print(f"  guidance ({len(guide)} chars): {guide}")
        print()
