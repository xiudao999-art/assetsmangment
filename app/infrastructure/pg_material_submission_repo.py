"""素材提报仓储 —— PostgreSQL 真源实现。"""
from __future__ import annotations

import re
from typing import Optional

from psycopg.types.json import Jsonb

from app.domain.models import MaterialSubmission
from app.infrastructure.snowflake import next_id

_TABLE_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_SELECT_COLS = (
    "id, team_name, delivery_time, drama_name, oss_key, video_file_name, "
    "title_name, episode_range, revision_comment, can_upload_status, "
    "upload_account_name, upload_date, publish_status, platform_reject_reason, "
    "platform_reject_attachments, create_by, create_time, update_by, update_time"
)


class PgMaterialSubmissionRepo:
    def __init__(self, dsn: str, table: str = "material_submission", idgen=None) -> None:
        if not _TABLE_RE.match(table):
            raise ValueError(f"非法表名: {table!r}")
        self._dsn = dsn
        self._table = table
        self._permission_table = f"{table}_permission"
        self._idgen = idgen or next_id
        self._init_schema()

    def _conn(self):
        import psycopg
        return psycopg.connect(self._dsn, autocommit=True, connect_timeout=10,
                               options="-c timezone=Asia/Shanghai")

    def _init_schema(self) -> None:
        t = self._table
        with self._conn() as c:
            c.execute(f"""
                CREATE TABLE IF NOT EXISTS {t} (
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
                    upload_date                 TEXT NOT NULL DEFAULT '',
                    publish_status              SMALLINT,
                    platform_reject_reason      TEXT NOT NULL DEFAULT '',
                    platform_reject_attachments JSONB NOT NULL DEFAULT '[]'::jsonb,
                    del_flag                    BIGINT NOT NULL DEFAULT 0,
                    create_by                   TEXT NOT NULL DEFAULT '',
                    create_time                 TIMESTAMPTZ NOT NULL DEFAULT now(),
                    update_by                   TEXT NOT NULL DEFAULT '',
                    update_time                 TIMESTAMPTZ NOT NULL DEFAULT now()
                )""")
            c.execute(f"""
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = '{t}'
                          AND column_name = 'uploadable_status'
                    ) AND NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = '{t}'
                          AND column_name = 'can_upload_status'
                    ) THEN
                        EXECUTE 'ALTER TABLE {t} RENAME COLUMN uploadable_status TO can_upload_status';
                    END IF;
                END $$;
            """)
            c.execute(f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS upload_account_name TEXT NOT NULL DEFAULT ''")
            c.execute(f"""
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema = 'public' AND table_name = '{t}' AND column_name = 'upload_time'
                    ) AND NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema = 'public' AND table_name = '{t}' AND column_name = 'upload_date'
                    ) THEN
                        EXECUTE 'ALTER TABLE {t} RENAME COLUMN upload_time TO upload_date';
                    END IF;
                END $$;
            """)
            c.execute(f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS upload_date TEXT NOT NULL DEFAULT ''")
            c.execute(f"""
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT 1 FROM information_schema.tables
                        WHERE table_schema = 'public'
                          AND table_name = 'upload_account'
                    ) AND EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = '{t}'
                          AND column_name = 'upload_account_id'
                    ) THEN
                        EXECUTE '
                            UPDATE {t} ms
                               SET upload_account_name = COALESCE(ua.name, '''')
                              FROM upload_account ua
                             WHERE ms.upload_account_name = ''''
                               AND ms.upload_account_id <> 0
                               AND ua.id = ms.upload_account_id
                               AND ua.del_flag = 0
                        ';
                    END IF;
                END $$;
            """)
            c.execute(f"DROP INDEX IF EXISTS idx_{t}_account")
            c.execute(f"ALTER TABLE {t} ALTER COLUMN can_upload_status DROP NOT NULL")
            c.execute(f"ALTER TABLE {t} ALTER COLUMN can_upload_status DROP DEFAULT")
            c.execute(f"ALTER TABLE {t} ALTER COLUMN publish_status DROP NOT NULL")
            c.execute(f"ALTER TABLE {t} ALTER COLUMN publish_status DROP DEFAULT")
            c.execute(f"UPDATE {t} SET publish_status = NULL WHERE publish_status = 0")
            c.execute(f"ALTER TABLE {t} DROP COLUMN IF EXISTS upload_account_id")
            c.execute("DROP TABLE IF EXISTS upload_account")
            c.execute(f"COMMENT ON TABLE {t} IS '素材提报表。记录团队提报素材、可上传状态与平台反馈。'")
            c.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{t}_account_name ON {t} (upload_account_name, del_flag)"
            )
            c.execute(f"CREATE INDEX IF NOT EXISTS idx_{t}_live ON {t} (del_flag) WHERE del_flag = 0")
            p = self._permission_table
            c.execute(f"""
                CREATE TABLE IF NOT EXISTS {p} (
                    submission_id  BIGINT NOT NULL REFERENCES {t}(id) ON DELETE CASCADE,
                    user_id        TEXT NOT NULL,
                    permission_type TEXT NOT NULL CHECK (permission_type IN ('read', 'read_edit')),
                    create_by      TEXT NOT NULL DEFAULT '',
                    create_time    TIMESTAMPTZ NOT NULL DEFAULT now(),
                    update_by      TEXT NOT NULL DEFAULT '',
                    update_time    TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (submission_id, user_id)
                )""")
            c.execute(f"CREATE INDEX IF NOT EXISTS idx_{p}_user ON {p} (user_id, permission_type)")
            c.execute(f"""
                INSERT INTO {p} (submission_id, user_id, permission_type, create_by, update_by)
                SELECT id, create_by, 'read_edit', create_by, create_by
                  FROM {t}
                 WHERE del_flag = 0 AND create_by <> '' AND create_by <> 'admin'
                ON CONFLICT (submission_id, user_id) DO UPDATE
                    SET permission_type = 'read_edit', update_time = now()
            """)
            c.execute(f"DELETE FROM {p} WHERE user_id = 'admin'")

    def add(self, submission: MaterialSubmission, by: str = "") -> None:
        try:
            sid = int(submission.id)
        except (TypeError, ValueError):
            sid = self._idgen()
            submission.id = str(sid)
        with self._conn() as c:
            c.execute(
                f"""INSERT INTO {self._table}
                        (id, team_name, delivery_time, drama_name, oss_key, video_file_name,
                         title_name, episode_range, revision_comment, can_upload_status,
                         upload_account_name, upload_date, publish_status, platform_reject_reason,
                         platform_reject_attachments, create_by, update_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        team_name = EXCLUDED.team_name,
                        delivery_time = EXCLUDED.delivery_time,
                        drama_name = EXCLUDED.drama_name,
                        oss_key = EXCLUDED.oss_key,
                        video_file_name = EXCLUDED.video_file_name,
                        title_name = EXCLUDED.title_name,
                        episode_range = EXCLUDED.episode_range,
                        revision_comment = EXCLUDED.revision_comment,
                        can_upload_status = EXCLUDED.can_upload_status,
                        upload_account_name = EXCLUDED.upload_account_name,
                        upload_date = EXCLUDED.upload_date,
                        publish_status = EXCLUDED.publish_status,
                        platform_reject_reason = EXCLUDED.platform_reject_reason,
                        platform_reject_attachments = EXCLUDED.platform_reject_attachments,
                        update_by = EXCLUDED.update_by,
                        update_time = now()""",
                (
                    sid, submission.team_name, submission.delivery_time, submission.drama_name,
                    submission.oss_key, submission.video_file_name, submission.title_name,
                    submission.episode_range, submission.revision_comment,
                    submission.can_upload_status, submission.upload_account_name,
                    submission.upload_date, submission.publish_status, submission.platform_reject_reason,
                    Jsonb(submission.platform_reject_attachments),
                    submission.created_by or by, by or submission.created_by,
                ),
            )

    def get(self, submission_id: str) -> Optional[MaterialSubmission]:
        try:
            sid = int(submission_id)
        except (TypeError, ValueError):
            return None
        with self._conn() as c:
            row = c.execute(
                f"SELECT {_SELECT_COLS} FROM {self._table} WHERE id = %s AND del_flag = 0",
                (sid,),
            ).fetchone()
        return self._to_submission(row) if row else None

    def delete(self, submission_id: str, by: str = "") -> None:
        try:
            sid = int(submission_id)
        except (TypeError, ValueError):
            return
        with self._conn() as c:
            c.execute(
                f"UPDATE {self._table} SET del_flag = %s, update_by = %s, update_time = now() "
                f"WHERE id = %s AND del_flag = 0",
                (self._idgen(), by, sid),
            )
            c.execute(f"DELETE FROM {self._permission_table} WHERE submission_id = %s", (sid,))

    def permission_of(self, submission_id: str, user_id: str) -> str:
        try:
            sid = int(submission_id)
        except (TypeError, ValueError):
            return ""
        with self._conn() as c:
            row = c.execute(
                f"SELECT permission_type FROM {self._permission_table} "
                "WHERE submission_id = %s AND user_id = %s",
                (sid, user_id),
            ).fetchone()
        return row[0] if row else ""

    def permissions_for(self, submission_id: str) -> dict[str, str]:
        try:
            sid = int(submission_id)
        except (TypeError, ValueError):
            return {}
        with self._conn() as c:
            rows = c.execute(
                f"SELECT user_id, permission_type FROM {self._permission_table} WHERE submission_id = %s",
                (sid,),
            ).fetchall()
        return {str(row[0]): str(row[1]) for row in rows}

    def permissions_for_user(self, user_id: str) -> dict[str, str]:
        with self._conn() as c:
            rows = c.execute(
                f"SELECT submission_id, permission_type FROM {self._permission_table} WHERE user_id = %s",
                (user_id,),
            ).fetchall()
        return {str(row[0]): str(row[1]) for row in rows}
    def replace_permissions(self, submission_id: str, grants: dict[str, str], by: str = "") -> None:
        try:
            sid = int(submission_id)
        except (TypeError, ValueError):
            return
        clean = {str(uid): value for uid, value in grants.items()
                 if uid and str(uid) != "admin" and value in ("read", "read_edit")}
        with self._conn() as c:
            with c.transaction():
                c.execute(f"DELETE FROM {self._permission_table} WHERE submission_id = %s", (sid,))
                for user_id, permission_type in clean.items():
                    c.execute(
                        f"INSERT INTO {self._permission_table} "
                        "(submission_id, user_id, permission_type, create_by, update_by) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (sid, user_id, permission_type, by, by),
                    )

    def replace_user_permissions(self, user_id: str, grants: dict[str, str], by: str = "") -> int:
        if not user_id or user_id == "admin":
            return 0
        clean: dict[int, str] = {}
        for submission_id, permission_type in grants.items():
            try:
                sid = int(submission_id)
            except (TypeError, ValueError):
                continue
            if permission_type in ("read", "read_edit"):
                clean[sid] = permission_type
        with self._conn() as c:
            with c.transaction():
                rows = c.execute(
                    f"SELECT submission_id, permission_type FROM {self._permission_table} WHERE user_id = %s",
                    (user_id,),
                ).fetchall()
                before = {int(row[0]): str(row[1]) for row in rows}
                owner_rows = c.execute(
                    f"SELECT id FROM {self._table} WHERE del_flag = 0 AND create_by = %s",
                    (user_id,),
                ).fetchall()
                for row in owner_rows:
                    clean[int(row[0])] = "read_edit"
                submission_ids = list(clean)
                if submission_ids:
                    values_sql = ", ".join(["(%s, %s, %s, %s, %s)"] * len(submission_ids))
                    params: list = []
                    for sid in submission_ids:
                        params.extend((sid, user_id, clean[sid], by, by))
                    c.execute(
                        f"""INSERT INTO {self._permission_table}
                                (submission_id, user_id, permission_type, create_by, update_by)
                            VALUES {values_sql}
                            ON CONFLICT (submission_id, user_id) DO UPDATE SET
                                permission_type = EXCLUDED.permission_type,
                                update_by = EXCLUDED.update_by,
                                update_time = now()""",
                        params,
                    )
                    c.execute(
                        f"DELETE FROM {self._permission_table} "
                        "WHERE user_id = %s AND NOT (submission_id = ANY(%s::bigint[]))",
                        (user_id, submission_ids),
                    )
                else:
                    c.execute(f"DELETE FROM {self._permission_table} WHERE user_id = %s", (user_id,))
        return sum(1 for sid in set(before) | set(clean) if before.get(sid, "") != clean.get(sid, ""))
    def submission_ids_for_user(self, user_id: str, require_edit: bool = False) -> set[str]:
        allowed = ("read_edit",) if require_edit else ("read", "read_edit")
        placeholders = ",".join(["%s"] * len(allowed))
        with self._conn() as c:
            rows = c.execute(
                f"SELECT submission_id FROM {self._permission_table} "
                f"WHERE user_id = %s AND permission_type IN ({placeholders})",
                (user_id, *allowed),
            ).fetchall()
        return {str(row[0]) for row in rows}

    def list_upload_account_names(self, keyword: str = "", limit: int | None = None) -> list[str]:
        where = "del_flag = 0 AND upload_account_name <> ''"
        params: list = []
        if keyword:
            where += " AND upload_account_name ILIKE %s"
            params.append(f"%{keyword}%")
        sql = (
            f"SELECT upload_account_name, MAX(id) AS max_id "
            f"FROM {self._table} WHERE {where} "
            f"GROUP BY upload_account_name ORDER BY max_id DESC"
        )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        with self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        return [row[0] for row in rows if row and row[0]]

    def list_drama_names(self, keyword: str = "", limit: int | None = None) -> list[str]:
        where = "del_flag = 0 AND drama_name <> ''"
        params: list = []
        if keyword:
            where += " AND drama_name ILIKE %s"
            params.append(f"%{keyword}%")
        sql = (
            f"SELECT drama_name, MAX(id) AS max_id "
            f"FROM {self._table} WHERE {where} "
            f"GROUP BY drama_name ORDER BY max_id DESC"
        )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        with self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        return [row[0] for row in rows if row and row[0]]

    def list(self, team_name: str = "", drama_name: str = "", video_file_name: str = "",
             title_name: str = "", can_upload_status: int | None = None,
             can_upload_status_empty: bool = False,
             upload_account_name: str = "", publish_status: int | None = None,
             publish_status_empty: bool = False,
             offset: int = 0, limit: int | None = None) -> list[MaterialSubmission]:
        where = "del_flag = 0"
        params: list = []
        if team_name:
            where += " AND team_name ILIKE %s"
            params.append(f"%{team_name}%")
        if drama_name:
            where += " AND drama_name ILIKE %s"
            params.append(f"%{drama_name}%")
        if video_file_name:
            where += " AND video_file_name ILIKE %s"
            params.append(f"%{video_file_name}%")
        if title_name:
            where += " AND title_name ILIKE %s"
            params.append(f"%{title_name}%")
        if can_upload_status is not None:
            where += " AND can_upload_status = %s"
            params.append(can_upload_status)
        elif can_upload_status_empty:
            where += " AND can_upload_status IS NULL"
        if upload_account_name:
            where += " AND upload_account_name = %s"
            params.append(upload_account_name)
        if publish_status is not None:
            where += " AND publish_status = %s"
            params.append(publish_status)
        elif publish_status_empty:
            where += " AND publish_status IS NULL"
        sql = f"SELECT {_SELECT_COLS} FROM {self._table} WHERE {where} ORDER BY id ASC"
        if limit is not None:
            sql += f" LIMIT {int(limit)} OFFSET {int(offset)}"
        with self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        return [self._to_submission(r) for r in rows]

    def count(self, team_name: str = "", drama_name: str = "", video_file_name: str = "",
              title_name: str = "", can_upload_status: int | None = None,
              can_upload_status_empty: bool = False,
              upload_account_name: str = "", publish_status: int | None = None,
              publish_status_empty: bool = False) -> int:
        where = "del_flag = 0"
        params: list = []
        if team_name:
            where += " AND team_name ILIKE %s"
            params.append(f"%{team_name}%")
        if drama_name:
            where += " AND drama_name ILIKE %s"
            params.append(f"%{drama_name}%")
        if video_file_name:
            where += " AND video_file_name ILIKE %s"
            params.append(f"%{video_file_name}%")
        if title_name:
            where += " AND title_name ILIKE %s"
            params.append(f"%{title_name}%")
        if can_upload_status is not None:
            where += " AND can_upload_status = %s"
            params.append(can_upload_status)
        elif can_upload_status_empty:
            where += " AND can_upload_status IS NULL"
        if upload_account_name:
            where += " AND upload_account_name = %s"
            params.append(upload_account_name)
        if publish_status is not None:
            where += " AND publish_status = %s"
            params.append(publish_status)
        elif publish_status_empty:
            where += " AND publish_status IS NULL"
        with self._conn() as c:
            row = c.execute(f"SELECT COUNT(*) FROM {self._table} WHERE {where}", params).fetchone()
        return row[0] if row else 0

    @staticmethod
    def _to_submission(row) -> MaterialSubmission:
        return MaterialSubmission(
            id=str(row[0]),
            team_name=row[1] or "",
            delivery_time=row[2] or "",
            drama_name=row[3] or "",
            oss_key=row[4] or "",
            video_file_name=row[5] or "",
            title_name=row[6] or "",
            episode_range=row[7] or "",
            revision_comment=row[8] or "",
            can_upload_status=int(row[9]) if row[9] is not None else None,
            upload_account_name=row[10] or "",
            upload_date=row[11] or "",
            publish_status=int(row[12]) if row[12] is not None else None,
            platform_reject_reason=row[13] or "",
            platform_reject_attachments=row[14] or [],
            created_by=row[15] or "",
            created_time=row[16].isoformat() if row[16] else "",
            updated_by=row[17] or "",
            updated_time=row[18].isoformat() if row[18] else "",
        )
