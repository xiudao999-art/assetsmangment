"""待审核任务仓储 —— PostgreSQL 真源实现(实现 domain.ports.AuditTaskRepo)。
遵循全项目 PG 业务表基础字段规范:id(雪花 BIGINT 主键) / del_flag(0=在用,删除时置新雪花 ID,软删)。
风格与 pg_rule_repo 一致:每操作短连接、autocommit、裸 SQL。infra→domain。"""
from __future__ import annotations
import re
from typing import Optional

from app.domain.models import AuditTask, MaterialType, JobStatus
from app.infrastructure.snowflake import next_id

_TABLE_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

_SELECT_COLS = ("id, owner_id, name, material_type, material_id, content_hash, "
                "status, verdict, report_id, created_ms, error, video_kind, project_id")


class PgAuditTaskRepo:
    def __init__(self, dsn: str, table: str = "audit_task", idgen=None) -> None:
        if not _TABLE_RE.match(table):
            raise ValueError(f"非法表名: {table!r}")
        self._dsn = dsn
        self._table = table
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
                    id              BIGINT PRIMARY KEY,
                    owner_id        TEXT NOT NULL DEFAULT '',
                    name            TEXT NOT NULL DEFAULT '',
                    material_type   TEXT NOT NULL DEFAULT 'image',
                    material_id     TEXT NOT NULL DEFAULT '',
                    content_hash    TEXT NOT NULL DEFAULT '',
                    status          TEXT NOT NULL DEFAULT 'pending',
                    verdict         TEXT NOT NULL DEFAULT '',
                    report_id       TEXT NOT NULL DEFAULT '',
                    created_ms      BIGINT NOT NULL DEFAULT 0,
                    error           TEXT NOT NULL DEFAULT '',
                    video_kind      TEXT NOT NULL DEFAULT 'material',
                    project_id      TEXT NOT NULL DEFAULT '',
                    del_flag        BIGINT NOT NULL DEFAULT 0,
                    create_by       TEXT NOT NULL DEFAULT '',
                    create_time     TIMESTAMPTZ NOT NULL DEFAULT now(),
                    update_by       TEXT NOT NULL DEFAULT '',
                    update_time     TIMESTAMPTZ NOT NULL DEFAULT now()
                )""")
            c.execute(f"COMMENT ON TABLE {t} IS '待审核任务。用户提交审核后持久化，页面轮询状态。'")
            c.execute(f"COMMENT ON COLUMN {t}.id IS '雪花算法 BIGINT 主键，API 序列化为字符串'")
            c.execute(f"COMMENT ON COLUMN {t}.owner_id IS '提交用户 ID'")
            c.execute(f"COMMENT ON COLUMN {t}.name IS '文件名或描述'")
            c.execute(f"COMMENT ON COLUMN {t}.material_type IS '物料类型:image/video/audio/text 等'")
            c.execute(f"COMMENT ON COLUMN {t}.status IS '任务状态:pending/running/done/failed'")
            c.execute(f"COMMENT ON COLUMN {t}.verdict IS '审核裁定:pass/review/block'")
            c.execute(f"COMMENT ON COLUMN {t}.report_id IS '指向审核报告的 ID'")
            c.execute(f"COMMENT ON COLUMN {t}.created_ms IS '创建时间戳(毫秒)'")
            c.execute(f"COMMENT ON COLUMN {t}.del_flag IS '软删标记:0=在用，删除时置为新雪花ID'")
            c.execute(f"CREATE INDEX IF NOT EXISTS idx_{t}_owner ON {t} (owner_id, del_flag)")
            c.execute(f"CREATE INDEX IF NOT EXISTS idx_{t}_live ON {t} (del_flag) WHERE del_flag = 0")

    def save(self, task: AuditTask) -> None:
        """插入或按 id 覆盖(upsert)。domain 层可能传 UUID id,自动换发雪花。更新只动业务列。"""
        try:
            tid = int(task.id)
        except (TypeError, ValueError):
            tid = self._idgen()
            task.id = str(tid)

        with self._conn() as c:
            c.execute(
                f"""INSERT INTO {self._table}
                        (id, owner_id, name, material_type, material_id, content_hash,
                         status, verdict, report_id, created_ms, error, video_kind, project_id,
                         create_by, update_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        owner_id = EXCLUDED.owner_id,
                        name = EXCLUDED.name,
                        material_type = EXCLUDED.material_type,
                        material_id = EXCLUDED.material_id,
                        content_hash = EXCLUDED.content_hash,
                        status = EXCLUDED.status,
                        verdict = EXCLUDED.verdict,
                        report_id = EXCLUDED.report_id,
                        error = EXCLUDED.error,
                        video_kind = EXCLUDED.video_kind,
                        project_id = EXCLUDED.project_id,
                        update_by = EXCLUDED.update_by,
                        update_time = now()""",
                (tid, task.owner_id, task.name, task.material_type.value,
                 task.material_id, task.content_hash, task.status.value, task.verdict,
                 task.report_id, task.created_ms, task.error, task.video_kind,
                 task.project_id, task.owner_id, task.owner_id),
            )

    def get(self, task_id: str) -> Optional[AuditTask]:
        try:
            tid = int(task_id)
        except (TypeError, ValueError):
            return None
        with self._conn() as c:
            row = c.execute(
                f"SELECT {_SELECT_COLS} FROM {self._table} "
                f"WHERE id = %s AND del_flag = 0", (tid,)
            ).fetchone()
        return self._to_task(row) if row else None

    def delete(self, task_id: str) -> None:
        """软删:del_flag 置新雪花 ID。"""
        try:
            tid = int(task_id)
        except (TypeError, ValueError):
            return
        with self._conn() as c:
            c.execute(
                f"UPDATE {self._table} SET del_flag = %s, update_time = now() "
                f"WHERE id = %s AND del_flag = 0",
                (self._idgen(), tid),
            )

    def list_for(self, owner_id: str, project_id: str = "", offset: int = 0, limit: int | None = None) -> list[AuditTask]:
        where = "owner_id = %s AND del_flag = 0"
        params: list = [owner_id]
        if project_id:
            where += " AND project_id = %s"
            params.append(project_id)
        sql = f"SELECT {_SELECT_COLS} FROM {self._table} WHERE {where} ORDER BY created_ms DESC, id DESC"
        if limit is not None:
            sql += f" LIMIT {int(limit)} OFFSET {int(offset)}"
        with self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        return [self._to_task(r) for r in rows]

    def list_all(self, project_id: str = "", offset: int = 0, limit: int | None = None) -> list[AuditTask]:
        where = "del_flag = 0"
        params: list = []
        if project_id:
            where += " AND project_id = %s"
            params.append(project_id)
        sql = f"SELECT {_SELECT_COLS} FROM {self._table} WHERE {where} ORDER BY created_ms DESC, id DESC"
        if limit is not None:
            sql += f" LIMIT {int(limit)} OFFSET {int(offset)}"
        with self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        return [self._to_task(r) for r in rows]

    def count_for(self, owner_id: str, project_id: str = "") -> int:
        where = "owner_id = %s AND del_flag = 0"
        params: list = [owner_id]
        if project_id:
            where += " AND project_id = %s"
            params.append(project_id)
        with self._conn() as c:
            row = c.execute(
                f"SELECT COUNT(*) FROM {self._table} WHERE {where}", params
            ).fetchone()
        return row[0] if row else 0

    def count_all(self, project_id: str = "") -> int:
        where = "del_flag = 0"
        params: list = []
        if project_id:
            where += " AND project_id = %s"
            params.append(project_id)
        with self._conn() as c:
            row = c.execute(
                f"SELECT COUNT(*) FROM {self._table} WHERE {where}", params
            ).fetchone()
        return row[0] if row else 0

    @staticmethod
    def _to_task(row) -> AuditTask:
        (rid, owner_id, name, material_type, material_id, content_hash,
         status, verdict, report_id, created_ms, error, video_kind, project_id) = row
        return AuditTask(
            id=str(rid),
            owner_id=owner_id, name=name,
            material_type=MaterialType(material_type),
            material_id=material_id, content_hash=content_hash,
            status=JobStatus(status), verdict=verdict,
            report_id=report_id, created_ms=created_ms,
            error=error, video_kind=video_kind, project_id=project_id,
        )
