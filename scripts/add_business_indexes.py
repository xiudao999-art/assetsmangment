"""Create PostgreSQL indexes derived from production repository query paths.

The migration is idempotent and uses CONCURRENTLY so it can be run against the
live database configured by AM_DATABASE_URL in .env without blocking writes for
the duration of an index build.
"""
from __future__ import annotations

import os

import psycopg
from dotenv import load_dotenv


INDEX_DDLS = (
    # Audit-task list/count paths: owner/project filters plus newest-first paging.
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_audit_task_live_created "
    "ON audit_task (created_ms DESC, id DESC) WHERE del_flag = 0",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_audit_task_live_owner_created "
    "ON audit_task (owner_id, created_ms DESC, id DESC) WHERE del_flag = 0",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_audit_task_live_project_created "
    "ON audit_task (project_id, created_ms DESC, id DESC) WHERE del_flag = 0",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_audit_task_live_name_trgm "
    "ON audit_task USING gin (name gin_trgm_ops) WHERE del_flag = 0",

    # MaterialQuery paths: stable id paging after the most frequent filters.
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_material_live_owner_id "
    "ON material (owner_id, id) WHERE del_flag = 0",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_material_live_project_id "
    "ON material (project_id, id) WHERE del_flag = 0",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_material_live_status_id "
    "ON material (audit_status, id) WHERE del_flag = 0",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_material_live_type_id "
    "ON material (type, id) WHERE del_flag = 0",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_material_public_pass_id "
    "ON material (id) WHERE del_flag = 0 AND is_public AND audit_status = 'pass'",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_material_live_tags_gin "
    "ON material USING gin (tags jsonb_path_ops) WHERE del_flag = 0",

    # Submission list/count paths: equality filters retain the requested id order.
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_material_submission_live_id "
    "ON material_submission (id) WHERE del_flag = 0",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_material_submission_live_creator_id "
    "ON material_submission (create_by, id) WHERE del_flag = 0",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_material_submission_live_can_upload_id "
    "ON material_submission (can_upload_status, id) WHERE del_flag = 0",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_material_submission_live_publish_id "
    "ON material_submission (publish_status, id) WHERE del_flag = 0",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_material_submission_live_designated_id "
    "ON material_submission (designated_upload_account_name, id) WHERE del_flag = 0",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_material_submission_live_account_id "
    "ON material_submission (upload_account_name, id) WHERE del_flag = 0",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_material_submission_live_created_id "
    "ON material_submission (create_time DESC, id) WHERE del_flag = 0",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_material_submission_live_upload_date_id "
    "ON material_submission ((NULLIF(BTRIM(upload_date), '')), id) WHERE del_flag = 0",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_material_submission_live_team_id "
    "ON material_submission ((NULLIF(BTRIM(team_name), '')), id) WHERE del_flag = 0",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_material_submission_trash_created_id "
    "ON material_submission (create_time DESC, id) WHERE del_flag <> 0 AND oss_del_flag = 0",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_material_submission_trash_upload_date_id "
    "ON material_submission ((NULLIF(BTRIM(upload_date), '')), id) WHERE del_flag <> 0 AND oss_del_flag = 0",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_material_submission_trash_team_id "
    "ON material_submission ((NULLIF(BTRIM(team_name), '')), id) WHERE del_flag <> 0 AND oss_del_flag = 0",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_material_submission_permission_user_submission "
    "ON material_submission_permission (user_id, submission_id)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_material_submission_live_team_trgm "
    "ON material_submission USING gin (team_name gin_trgm_ops) WHERE del_flag = 0",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_material_submission_live_drama_trgm "
    "ON material_submission USING gin (drama_name gin_trgm_ops) WHERE del_flag = 0",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_material_submission_live_video_name_trgm "
    "ON material_submission USING gin (video_file_name gin_trgm_ops) WHERE del_flag = 0",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_material_submission_live_title_trgm "
    "ON material_submission USING gin (title_name gin_trgm_ops) WHERE del_flag = 0",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_material_submission_live_account_trgm "
    "ON material_submission USING gin (upload_account_name gin_trgm_ops) WHERE del_flag = 0",

    # Training examples are always read per set, newest first.
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_rule_training_example_live_set_created "
    "ON rule_training_example (training_set_id, create_time DESC) WHERE del_flag = 0",
)


def main() -> None:
    load_dotenv(".env")
    dsn = os.getenv("AM_DATABASE_URL", "").strip()
    if not dsn:
        raise SystemExit("AM_DATABASE_URL is not configured in .env")

    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
        conn.execute("SET lock_timeout = '5s'")
        conn.execute("SET statement_timeout = '30min'")
        conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        for ddl in INDEX_DDLS:
            name = ddl.split("EXISTS ", 1)[1].split(" ", 1)[0]
            print(f"creating {name} ...", flush=True)
            conn.execute(ddl)
        conn.execute("ANALYZE audit_task")
        conn.execute("ANALYZE material")
        conn.execute("ANALYZE material_submission")
        conn.execute("ANALYZE rule_training_example")

    print(f"ready: {len(INDEX_DDLS)} indexes")


if __name__ == "__main__":
    main()
