"""全量数据迁移:state.json → PostgreSQL(幂等,可重复跑)。
用法: .venv\Scripts\python scripts\migrate_all_to_pg.py [state.json路径]
默认读 .localdata/state.json;PG 连接串从环境变量 AM_DATABASE_URL 取。

维护 old_id→新雪花ID 映射,解决 material 换雪花ID 后 favorites/tasks 等外键引用断裂。
"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.infrastructure.snowflake import next_id
from app.config import settings


def _init_mv(conn) -> None:
    """创建 material_vectors 表(含基础字段),幂等。旧表缺列用 ALTER TABLE 渐进迁移不丢数据。"""
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS material_vectors (
            material_id TEXT PRIMARY KEY,
            embedding   vector(1024)
        )""")
    for col, type_default in [
        ("del_flag", "BIGINT NOT NULL DEFAULT 0"),
        ("create_by", "TEXT NOT NULL DEFAULT ''"),
        ("create_time", "TIMESTAMPTZ NOT NULL DEFAULT now()"),
        ("update_by", "TEXT NOT NULL DEFAULT ''"),
        ("update_time", "TIMESTAMPTZ NOT NULL DEFAULT now()"),
    ]:
        conn.execute(
            f"ALTER TABLE material_vectors ADD COLUMN IF NOT EXISTS {col} {type_default}"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS mv_hnsw ON material_vectors "
        "USING hnsw (embedding vector_cosine_ops)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_mv_live ON material_vectors (del_flag) WHERE del_flag = 0"
    )


def main(state_path: str = ".localdata/state.json") -> None:
    dsn = settings.database_url
    if not dsn or dsn.startswith("postgresql://user:pass@localhost"):
        print("❌ AM_DATABASE_URL 未配置或为占位值,退出。")
        sys.exit(1)

    import psycopg
    from psycopg.types.json import Jsonb

    if not os.path.exists(state_path):
        print(f"⚠️  {state_path} 不存在,跳过。")
        return

    with open(state_path, "r", encoding="utf-8-sig") as f:
        data = json.load(f)

    conn = psycopg.connect(dsn, autocommit=True, connect_timeout=10,
                           options="-c timezone=Asia/Shanghai")

    total = {"users": 0, "materials": 0, "vectors": 0, "favorites": 0, "roles": 0, "user_perms": 0,
             "whitelist": 0, "blockwords": 0, "tasks": 0, "reports": 0}

    # ── 映射表:state.json 旧 id → PG 新雪花 id(str) ──
    id_map: dict[str, str] = {}   # old_uid → new_snowflake_str

    # ── 用户 ──
    for u in data.get("users", []):
        name = u.get("name", "").strip()
        if not name:
            continue
        did = u.get("id", name)
        row = conn.execute(
            "SELECT 1 FROM app_user WHERE domain_id = %s AND del_flag = 0", (did,)
        ).fetchone()
        if row:
            continue
        conn.execute(
            """INSERT INTO app_user (id, domain_id, name, pwd_hash, role, status, create_by, update_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT DO NOTHING""",
            (next_id(), did, name, u.get("pwd_hash", ""), u.get("role", "viewer"),
             u.get("status", "active"), did, did),
        )
        total["users"] += 1

    # ── 物料(建立 old_uuid → 新雪花 映射) ──
    for m in data.get("materials", []):
        old_id = m.get("id", "").strip()
        if not old_id:
            continue
        ch = m.get("content_hash", "")
        oid = m.get("owner_id", "")
        # 幂等:按 content_hash + owner_id 查重
        if ch:
            row = conn.execute(
                "SELECT id FROM material WHERE owner_id = %s AND content_hash = %s AND del_flag = 0 LIMIT 1",
                (oid, ch),
            ).fetchone()
            if row:
                id_map[old_id] = str(row[0])  # 已存在 → 登记映射
                continue

        new_id = next_id()
        id_map[old_id] = str(new_id)
        conn.execute(
            """INSERT INTO material
               (id, type, thumb, source_timecode, audit_status, source_job,
                oss_key, description, owner_id, is_public, audit_report_id,
                content_hash, project_id, tags, ai_summary, ai_scenarios,
                ai_emotions, ai_atmosphere, reject_events, create_by, update_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT DO NOTHING""",
            (new_id, m.get("type", "image"), m.get("thumb", ""),
             m.get("source_timecode", 0), m.get("audit_status", "review"),
             m.get("source_job", ""), m.get("oss_key", ""), m.get("description", ""),
             oid, m.get("is_public", False), m.get("audit_report_id", ""),
             ch, m.get("project_id", ""),
             Jsonb(m.get("tags", [])), m.get("ai_summary", ""),
             Jsonb(m.get("ai_scenarios", [])), Jsonb(m.get("ai_emotions", [])),
             m.get("ai_atmosphere", ""), Jsonb(m.get("reject_events", [])),
             oid, oid),
        )
        total["materials"] += 1

    print(f"  id_map: {len(id_map)} old→new material ID mappings")

    # ── 向量索引:material_vectors(embedding 从 state.json 迁入,映射旧 UUID→新雪花 ID) ──
    _init_mv(conn)
    vectors_migrated = 0
    for m in data.get("materials", []):
        old_id = m.get("id", "").strip()
        emb = m.get("embedding")
        if not old_id or not emb or not isinstance(emb, list) or len(emb) == 0:
            continue
        new_id = id_map.get(old_id)
        if not new_id:
            continue
        # 幂等:已存在则跳过
        row = conn.execute(
            "SELECT 1 FROM material_vectors WHERE material_id = %s AND del_flag = 0 LIMIT 1",
            (new_id,),
        ).fetchone()
        if row:
            continue
        vec_str = "[" + ",".join(repr(float(x)) for x in emb) + "]"
        conn.execute(
            "INSERT INTO material_vectors (material_id, embedding, create_by, update_by) "
            "VALUES (%s, %s::vector, '', '') "
            "ON CONFLICT (material_id) DO UPDATE SET embedding = EXCLUDED.embedding, del_flag = 0, update_time = now()",
            (new_id, vec_str),
        )
        vectors_migrated += 1
    if vectors_migrated:
        total["vectors"] = vectors_migrated

    # ── 收藏(用映射把旧 material UUID → 新雪花 ID) ──
    for pair in data.get("favorites", []):
        uid, old_mid = pair[0], pair[1]
        new_mid = id_map.get(old_mid, old_mid)  # 找不到映射则保留原值(兼容)
        row = conn.execute(
            "SELECT 1 FROM user_favorite WHERE user_id = %s AND material_id = %s AND del_flag = 0 LIMIT 1",
            (uid, new_mid),
        ).fetchone()
        if row:
            continue
        conn.execute(
            """INSERT INTO user_favorite (id, user_id, material_id, create_by, update_by)
               VALUES (%s, %s, %s, %s, '') ON CONFLICT DO NOTHING""",
            (next_id(), uid, new_mid, uid),
        )
        total["favorites"] += 1

    # ── RBAC 角色权限 ──
    for role, perms in data.get("roles", {}).items():
        for perm in perms:
            row = conn.execute(
                "SELECT 1 FROM role_permission WHERE role = %s AND permission = %s AND del_flag = 0 LIMIT 1",
                (role, perm),
            ).fetchone()
            if row:
                continue
            conn.execute(
                """INSERT INTO role_permission (id, role, permission, create_by, update_by)
                   VALUES (%s, %s, %s, '', '') ON CONFLICT DO NOTHING""",
                (next_id(), role, perm),
            )
            total["roles"] += 1

    # ── RBAC 用户级权限 ──
    for uid, perms in data.get("user_perms", {}).items():
        for perm in perms:
            row = conn.execute(
                "SELECT 1 FROM user_permission WHERE user_id = %s AND permission = %s AND del_flag = 0 LIMIT 1",
                (uid, perm),
            ).fetchone()
            if row:
                continue
            conn.execute(
                """INSERT INTO user_permission (id, user_id, permission, create_by, update_by)
                   VALUES (%s, %s, %s, '', '') ON CONFLICT DO NOTHING""",
                (next_id(), uid, perm),
            )
            total["user_perms"] += 1

    # ── 白名单 ──
    for w in data.get("content_whitelist", []):
        w = (w or "").strip()
        if not w:
            continue
        row = conn.execute(
            "SELECT 1 FROM content_whitelist WHERE word = %s AND del_flag = 0 LIMIT 1", (w,)
        ).fetchone()
        if row:
            continue
        conn.execute(
            "INSERT INTO content_whitelist (id, word, create_by, update_by) VALUES (%s, %s, '', '')",
            (next_id(), w),
        )
        total["whitelist"] += 1

    # ── 禁词 ──
    for w in data.get("blockwords", []):
        w = (w or "").strip()
        if not w:
            continue
        row = conn.execute(
            "SELECT 1 FROM blockword WHERE word = %s AND del_flag = 0 LIMIT 1", (w,)
        ).fetchone()
        if row:
            continue
        conn.execute(
            "INSERT INTO blockword (id, word, create_by, update_by) VALUES (%s, %s, '', '')",
            (next_id(), w),
        )
        total["blockwords"] += 1

    # ── 审核任务(用映射把旧 material UUID → 新雪花 ID) ──
    for t in data.get("audit_tasks", []):
        tname = t.get("name", "").strip()
        towner = t.get("owner_id", "").strip()
        if not tname:
            continue
        row = conn.execute(
            "SELECT 1 FROM audit_task WHERE name = %s AND owner_id = %s AND del_flag = 0 LIMIT 1",
            (tname, towner),
        ).fetchone()
        if row:
            continue
        old_mid = t.get("material_id", "")
        new_mid = id_map.get(old_mid, old_mid) if old_mid else ""
        conn.execute(
            """INSERT INTO audit_task
               (id, owner_id, name, material_type, material_id, content_hash,
                status, verdict, report_id, created_ms, error, video_kind, project_id,
                create_by, update_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT DO NOTHING""",
            (next_id(),
             towner, tname, t.get("material_type", "image"),
             new_mid, t.get("content_hash", ""), t.get("status", "pending"),
             t.get("verdict", ""), t.get("report_id", ""), t.get("created_ms", 0),
             t.get("error", ""), t.get("video_kind", "material"), t.get("project_id", ""),
             towner, towner),
        )
        total["tasks"] += 1

    # ── 审核报告 ──
    reports = data.get("audit_reports", {})
    if isinstance(reports, list):
        reports = {(r.get("id") or str(i)): r for i, r in enumerate(reports) if isinstance(r, dict)}
    for rid, rep in reports.items():
        rid = str(rid).strip()
        if not rid:
            continue
        row = conn.execute("SELECT 1 FROM audit_report WHERE report_id = %s", (rid,)).fetchone()
        if row:
            continue
        segs_raw = rep.get("segments", [])
        segs = [{"source_type": s.get("source_type", "transcript"), "text": s.get("text", ""),
                 "begin_ms": s.get("begin_ms"), "end_ms": s.get("end_ms"),
                 "frame_oss_key": s.get("frame_oss_key", "")} for s in segs_raw]
        conn.execute(
            """INSERT INTO audit_report (report_id, verdict, summary, segments, triggered)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (report_id) DO NOTHING""",
            (rid, rep.get("verdict", "processing"), rep.get("summary", ""),
             Jsonb(segs), Jsonb(rep.get("triggered", []))),
        )
        total["reports"] += 1

    conn.close()

    print("✅ 迁移完成:")
    for k, v in total.items():
        if v:
            print(f"  {k}: {v} 条")
    if all(v == 0 for v in total.values()):
        print("  (无新数据,已全部幂等跳过)")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else ".localdata/state.json"
    main(path)
