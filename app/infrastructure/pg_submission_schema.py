"""素材提报 PG 表初始化。"""
from __future__ import annotations

import re


_TABLE_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


def ensure_submission_tables(
    dsn: str,
    submission_table: str = "material_submission",
) -> None:
    if not _TABLE_RE.match(submission_table):
        raise ValueError(f"非法表名: {submission_table!r}")
    import psycopg

    with psycopg.connect(
        dsn,
        autocommit=True,
        connect_timeout=10,
        options="-c timezone=Asia/Shanghai",
    ) as c:
        c.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {submission_table} (
                id                          BIGINT PRIMARY KEY,
                team_name                   TEXT NOT NULL DEFAULT '',
                delivery_time               TEXT NOT NULL DEFAULT '',
                drama_name                  TEXT NOT NULL DEFAULT '',
                oss_key                     TEXT NOT NULL DEFAULT '',
                video_file_name             TEXT NOT NULL DEFAULT '',
                title_name                  TEXT NOT NULL DEFAULT '',
                episode_range               TEXT NOT NULL DEFAULT '',
                revision_comment            TEXT NOT NULL DEFAULT '',
                can_upload_status           SMALLINT,
                upload_account_name         TEXT NOT NULL DEFAULT '',
                publish_status              SMALLINT,
                platform_reject_reason      TEXT NOT NULL DEFAULT '',
                platform_reject_attachments JSONB NOT NULL DEFAULT '[]'::jsonb,
                del_flag                    BIGINT NOT NULL DEFAULT 0,
                create_by                   TEXT NOT NULL DEFAULT '',
                create_time                 TIMESTAMPTZ NOT NULL DEFAULT now(),
                update_by                   TEXT NOT NULL DEFAULT '',
                update_time                 TIMESTAMPTZ NOT NULL DEFAULT now()
            )"""
        )
        c.execute(
            f"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = '{submission_table}'
                      AND column_name = 'uploadable_status'
                ) AND NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = '{submission_table}'
                      AND column_name = 'can_upload_status'
                ) THEN
                    EXECUTE 'ALTER TABLE {submission_table} RENAME COLUMN uploadable_status TO can_upload_status';
                END IF;
            END $$;
            """
        )
        c.execute(f"ALTER TABLE {submission_table} ADD COLUMN IF NOT EXISTS upload_account_name TEXT NOT NULL DEFAULT ''")
        c.execute(f"ALTER TABLE {submission_table} ALTER COLUMN can_upload_status DROP NOT NULL")
        c.execute(f"ALTER TABLE {submission_table} ALTER COLUMN can_upload_status DROP DEFAULT")
        c.execute(f"ALTER TABLE {submission_table} ALTER COLUMN publish_status DROP NOT NULL")
        c.execute(f"ALTER TABLE {submission_table} ALTER COLUMN publish_status DROP DEFAULT")
        c.execute(f"UPDATE {submission_table} SET publish_status = NULL WHERE publish_status = 0")
        c.execute(
            f"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'public'
                      AND table_name = 'upload_account'
                ) AND EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = '{submission_table}'
                      AND column_name = 'upload_account_id'
                ) THEN
                    EXECUTE '
                        UPDATE {submission_table} ms
                           SET upload_account_name = COALESCE(ua.name, '''')
                          FROM upload_account ua
                         WHERE ms.upload_account_name = ''''
                           AND ms.upload_account_id <> 0
                           AND ua.id = ms.upload_account_id
                           AND ua.del_flag = 0
                    ';
                END IF;
            END $$;
            """
        )
        c.execute(f"DROP INDEX IF EXISTS idx_{submission_table}_account")
        c.execute(f"ALTER TABLE {submission_table} DROP COLUMN IF EXISTS upload_account_id")
        c.execute("DROP TABLE IF EXISTS upload_account")
        c.execute(
            f"COMMENT ON TABLE {submission_table} IS '素材提报表。记录团队提报素材、可上传状态与平台反馈。'"
        )
        c.execute(f"COMMENT ON COLUMN {submission_table}.id IS '雪花算法 BIGINT 主键，API 序列化为字符串'")
        c.execute(f"COMMENT ON COLUMN {submission_table}.team_name IS '团队名称'")
        c.execute(f"COMMENT ON COLUMN {submission_table}.delivery_time IS '视频交付时间，按文本存储'")
        c.execute(f"COMMENT ON COLUMN {submission_table}.drama_name IS '剧名'")
        c.execute(f"COMMENT ON COLUMN {submission_table}.oss_key IS '素材 OSS 对象键'")
        c.execute(f"COMMENT ON COLUMN {submission_table}.video_file_name IS '视频文件名'")
        c.execute(f"COMMENT ON COLUMN {submission_table}.title_name IS '标题名'")
        c.execute(f"COMMENT ON COLUMN {submission_table}.episode_range IS '集数区间，按文本存储'")
        c.execute(f"COMMENT ON COLUMN {submission_table}.revision_comment IS '修改意见'")
        c.execute(f"COMMENT ON COLUMN {submission_table}.can_upload_status IS '可上传状态:1=可上传,2=不可上传；可为空'")
        c.execute(f"COMMENT ON COLUMN {submission_table}.upload_account_name IS '上传账号名称，直接存文本'")
        c.execute(f"COMMENT ON COLUMN {submission_table}.publish_status IS '发布状态:1=成功,2=失败；可为空'")
        c.execute(f"COMMENT ON COLUMN {submission_table}.platform_reject_reason IS '平台拒审理由'")
        c.execute(
            f"COMMENT ON COLUMN {submission_table}.platform_reject_attachments IS '平台拒审理由附件，JSONB 数组；元素仅保留 oss_key 等附件信息，不含文件名字段'"
        )
        c.execute(f"COMMENT ON COLUMN {submission_table}.del_flag IS '软删标记:0=在用，删除时置为新雪花 ID'")
        c.execute(f"COMMENT ON COLUMN {submission_table}.create_by IS '创建人'")
        c.execute(f"COMMENT ON COLUMN {submission_table}.create_time IS '创建时间'")
        c.execute(f"COMMENT ON COLUMN {submission_table}.update_by IS '最后操作人'")
        c.execute(f"COMMENT ON COLUMN {submission_table}.update_time IS '最后操作时间'")
        c.execute(f"CREATE INDEX IF NOT EXISTS idx_{submission_table}_live ON {submission_table} (del_flag) WHERE del_flag = 0")
