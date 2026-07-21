"""Rule #9: 去掉 condition 中的"仅限这两类"，移入 guidance。"""
import os, dotenv, psycopg

dotenv.load_dotenv(".env")
dsn = os.getenv("AM_DATABASE_URL")

with psycopg.connect(dsn, autocommit=True) as conn:
    row = conn.execute(
        "SELECT id, condition, guidance FROM audit_rule WHERE no=9 AND del_flag=0"
    ).fetchone()

    new_cond = "画面中出现侮辱性手势（如竖中指）或明确的站外导流/诱导点击的箭头图标"
    old_guide = row[2] or ""
    extra = "仅限竖中指等侮辱性手势和指向站外App/诱导下载的箭头图标这两类，不要扩展到其他手势或UI元素。"
    new_guide = old_guide.rstrip() + "\n" + extra if old_guide else extra

    conn.execute(
        "UPDATE audit_rule SET condition=%s, guidance=%s, update_time=now() WHERE id=%s",
        (new_cond, new_guide, row[0]),
    )

    print(f"Rule #9 updated")
    print(f"  condition: {row[1]}")
    print(f"         →  {new_cond}")
    print(f"  guidance: +{len(extra)} chars")
