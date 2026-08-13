"""需求提报仓储：内存实现与 PostgreSQL 真源实现。"""
from __future__ import annotations

from dataclasses import replace
from typing import Optional

from app.domain.models import Requirement
from app.infrastructure.snowflake import next_id


class InMemoryRequirementRepo:
    def __init__(self) -> None:
        self._items: dict[str, Requirement] = {}

    def add(self, requirement: Requirement, by: str = "") -> None:
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        old = self._items.get(requirement.id)
        requirement.created_by = old.created_by if old else (requirement.created_by or by)
        requirement.created_time = old.created_time if old else (requirement.created_time or now)
        requirement.updated_by = by or requirement.updated_by or requirement.created_by
        requirement.updated_time = now
        self._items[requirement.id] = replace(requirement, attachments=list(requirement.attachments))

    def get(self, requirement_id: str) -> Optional[Requirement]:
        item = self._items.get(requirement_id)
        return replace(item, attachments=list(item.attachments)) if item else None

    def delete(self, requirement_id: str, by: str = "") -> None:
        self._items.pop(requirement_id, None)

    def list(self, q: str = "", urgency: str = "", status: str = "",
             offset: int = 0, limit: int | None = None) -> list[Requirement]:
        keyword = q.strip().lower()
        items = list(self._items.values())
        if keyword:
            items = [x for x in items if keyword in x.description.lower() or keyword in x.reply.lower()]
        if urgency:
            items = [x for x in items if x.urgency == urgency]
        if status:
            items = [x for x in items if x.status == status]
        items.sort(key=lambda x: (x.created_time, x.id), reverse=True)
        sliced = items[offset:] if limit is None else items[offset:offset + limit]
        return [replace(x, attachments=list(x.attachments)) for x in sliced]

    def count(self, q: str = "", urgency: str = "", status: str = "") -> int:
        return len(self.list(q=q, urgency=urgency, status=status))


class PgRequirementRepo:
    def __init__(self, dsn: str, idgen=None) -> None:
        self._dsn = dsn
        self._idgen = idgen or next_id
        self._init_schema()

    def _conn(self):
        import psycopg
        return psycopg.connect(self._dsn, autocommit=True, connect_timeout=10,
                               options="-c timezone=Asia/Shanghai")

    def _init_schema(self) -> None:
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS requirement (
                    id BIGINT PRIMARY KEY,
                    description TEXT NOT NULL,
                    urgency TEXT NOT NULL DEFAULT 'medium'
                        CHECK (urgency IN ('low','medium','high')),
                    status TEXT NOT NULL DEFAULT 'not_started'
                        CHECK (status IN ('not_started','pending_reply','in_progress','pending_acceptance','completed','acceptance_failed')),
                    reply TEXT NOT NULL DEFAULT '',
                    attachments JSONB NOT NULL DEFAULT '[]'::jsonb,
                    del_flag BIGINT NOT NULL DEFAULT 0,
                    create_by TEXT NOT NULL DEFAULT '',
                    create_time TIMESTAMPTZ NOT NULL DEFAULT now(),
                    update_by TEXT NOT NULL DEFAULT '',
                    update_time TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            # Older deployments already have the generated requirement_status_check
            # constraint, so CREATE TABLE IF NOT EXISTS cannot extend its allowed values.
            c.execute("""
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT 1
                          FROM pg_constraint
                         WHERE conrelid = 'requirement'::regclass
                           AND conname = 'requirement_status_check'
                           AND pg_get_constraintdef(oid) NOT LIKE '%pending_reply%'
                    ) THEN
                        ALTER TABLE requirement DROP CONSTRAINT requirement_status_check;
                        ALTER TABLE requirement ADD CONSTRAINT requirement_status_check
                            CHECK (status IN ('not_started','pending_reply','in_progress','pending_acceptance','completed','acceptance_failed'));
                    END IF;
                END
                $$
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_requirement_live_time ON requirement (create_time DESC) WHERE del_flag=0")
            c.execute("CREATE INDEX IF NOT EXISTS idx_requirement_filter ON requirement (status, urgency) WHERE del_flag=0")
            c.execute("COMMENT ON TABLE requirement IS '内部新需求提报及处理记录'")

    def add(self, requirement: Requirement, by: str = "") -> None:
        from psycopg.types.json import Jsonb
        try:
            rid = int(requirement.id)
        except (TypeError, ValueError):
            rid = self._idgen()
            requirement.id = str(rid)
        with self._conn() as c:
            c.execute("""
                INSERT INTO requirement
                    (id, description, urgency, status, reply, attachments, create_by, update_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET
                    description=EXCLUDED.description, urgency=EXCLUDED.urgency,
                    status=EXCLUDED.status, reply=EXCLUDED.reply,
                    attachments=EXCLUDED.attachments, update_by=EXCLUDED.update_by,
                    update_time=now()
            """, (rid, requirement.description, requirement.urgency, requirement.status,
                  requirement.reply, Jsonb(requirement.attachments),
                  requirement.created_by or by, by or requirement.created_by))

    def get(self, requirement_id: str) -> Optional[Requirement]:
        try:
            rid = int(requirement_id)
        except (TypeError, ValueError):
            return None
        with self._conn() as c:
            row = c.execute("""
                SELECT id,description,urgency,status,reply,attachments,
                       create_by,create_time,update_by,update_time
                  FROM requirement WHERE id=%s AND del_flag=0
            """, (rid,)).fetchone()
        return self._to_model(row) if row else None

    def delete(self, requirement_id: str, by: str = "") -> None:
        try:
            rid = int(requirement_id)
        except (TypeError, ValueError):
            return
        with self._conn() as c:
            c.execute("UPDATE requirement SET del_flag=%s,update_by=%s,update_time=now() WHERE id=%s AND del_flag=0",
                      (self._idgen(), by, rid))

    @staticmethod
    def _where(q: str, urgency: str, status: str):
        where = ["del_flag=0"]
        params: list = []
        if q:
            where.append("(description ILIKE %s OR reply ILIKE %s)")
            params.extend([f"%{q}%", f"%{q}%"])
        if urgency:
            where.append("urgency=%s")
            params.append(urgency)
        if status:
            where.append("status=%s")
            params.append(status)
        return " AND ".join(where), params

    def list(self, q: str = "", urgency: str = "", status: str = "",
             offset: int = 0, limit: int | None = None) -> list[Requirement]:
        where, params = self._where(q, urgency, status)
        sql = ("SELECT id,description,urgency,status,reply,attachments,create_by,create_time,update_by,update_time "
               f"FROM requirement WHERE {where} ORDER BY create_time DESC,id DESC OFFSET %s")
        params.append(offset)
        if limit is not None:
            sql += " LIMIT %s"
            params.append(limit)
        with self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        return [self._to_model(row) for row in rows]

    def count(self, q: str = "", urgency: str = "", status: str = "") -> int:
        where, params = self._where(q, urgency, status)
        with self._conn() as c:
            row = c.execute(f"SELECT COUNT(*) FROM requirement WHERE {where}", params).fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def _to_model(row) -> Requirement:
        return Requirement(
            id=str(row[0]), description=row[1] or "", urgency=row[2] or "medium",
            status=row[3] or "not_started", reply=row[4] or "", attachments=row[5] or [],
            created_by=row[6] or "", created_time=row[7].isoformat() if row[7] else "",
            updated_by=row[8] or "", updated_time=row[9].isoformat() if row[9] else "",
        )
