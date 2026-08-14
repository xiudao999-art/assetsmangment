"""In-memory, JSON-file, and PostgreSQL repositories for video-editing templates."""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.domain.models import VideoEditingTemplate

_TABLE_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_SELECT_COLS = (
    "id, name, description, reference_oss_key, narration_voice, bgm_oss_key, "
    "config, status, version, create_by, create_time, update_by, update_time"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _matches(template: VideoEditingTemplate, name: str, status: str) -> bool:
    return ((not name or name.casefold() in template.name.casefold())
            and (not status or template.status == status))


class InMemoryVideoEditingTemplateRepo:
    def __init__(self) -> None:
        self._items: dict[str, VideoEditingTemplate] = {}

    def save(self, template: VideoEditingTemplate, by: str = "") -> None:
        previous = self._items.get(template.id)
        now = _now()
        template.created_time = previous.created_time if previous else (template.created_time or now)
        template.updated_by = by or template.updated_by or template.created_by
        template.updated_time = now
        self._items[template.id] = template

    def get(self, template_id: str) -> Optional[VideoEditingTemplate]:
        return self._items.get(str(template_id))

    def get_by_name(self, name: str) -> Optional[VideoEditingTemplate]:
        key = (name or "").strip().casefold()
        return next((x for x in self._items.values() if x.name.casefold() == key), None)

    def list(self, name: str = "", status: str = "", offset: int = 0,
             limit: int | None = None) -> list[VideoEditingTemplate]:
        items = [x for x in self._items.values() if _matches(x, name, status)]
        items.sort(key=lambda x: int(x.id), reverse=True)
        return items[offset:] if limit is None else items[offset:offset + limit]

    def count(self, name: str = "", status: str = "") -> int:
        return sum(1 for x in self._items.values() if _matches(x, name, status))


class JsonVideoEditingTemplateRepo(InMemoryVideoEditingTemplateRepo):
    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self._path = Path(path)
        self._lock = threading.RLock()
        if self._path.exists():
            payload = json.loads(self._path.read_text(encoding="utf-8-sig"))
            for row in payload.get("templates", []):
                template = VideoEditingTemplate(**row)
                self._items[template.id] = template

    def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=self._path.name + ".", suffix=".tmp", dir=self._path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"templates": [asdict(x) for x in self._items.values()]}, stream,
                          ensure_ascii=False, indent=2)
            os.replace(temporary, self._path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def save(self, template: VideoEditingTemplate, by: str = "") -> None:
        with self._lock:
            super().save(template, by=by)
            self._flush()


class PgVideoEditingTemplateRepo:
    def __init__(self, dsn: str, table: str = "video_editing_template") -> None:
        if not _TABLE_RE.match(table):
            raise ValueError(f"Invalid table name: {table!r}")
        self._dsn = dsn
        self._table = table
        self._init_schema()

    def _conn(self):
        from app.infrastructure.pg_pool import connection
        return connection(self._dsn)

    def _init_schema(self) -> None:
        t = self._table
        with self._conn() as c:
            c.execute(f"""
                CREATE TABLE IF NOT EXISTS {t} (
                    id BIGINT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    reference_oss_key TEXT NOT NULL DEFAULT '',
                    narration_voice JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    bgm_oss_key TEXT NOT NULL DEFAULT '',
                    config JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive')),
                    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
                    del_flag BIGINT NOT NULL DEFAULT 0,
                    create_by TEXT NOT NULL DEFAULT '',
                    create_time TIMESTAMPTZ NOT NULL DEFAULT now(),
                    update_by TEXT NOT NULL DEFAULT '',
                    update_time TIMESTAMPTZ NOT NULL DEFAULT now()
                )""")
            c.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS uq_{t}_live_name ON {t} (lower(name)) WHERE del_flag = 0")
            c.execute(f"CREATE INDEX IF NOT EXISTS idx_{t}_live_status ON {t} (status, id DESC) WHERE del_flag = 0")
            c.execute(f"COMMENT ON TABLE {t} IS '视频剪辑模板表，媒体字段仅保存 OSS 对象键。'")
            c.execute(f"COMMENT ON COLUMN {t}.reference_oss_key IS '参考成片在 OSS 中的对象键，不保存签名地址或本地路径。'")
            c.execute(f"COMMENT ON COLUMN {t}.bgm_oss_key IS '背景音乐在 OSS 中的对象键，为空表示不使用独立背景音乐。'")

    def save(self, template: VideoEditingTemplate, by: str = "") -> None:
        from psycopg import errors
        from psycopg.types.json import Jsonb
        try:
            with self._conn() as c:
                c.execute(f"""
                    INSERT INTO {self._table}
                        (id, name, description, reference_oss_key, narration_voice,
                         bgm_oss_key, config, status, version, create_by, update_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        name = EXCLUDED.name,
                        description = EXCLUDED.description,
                        reference_oss_key = EXCLUDED.reference_oss_key,
                        narration_voice = EXCLUDED.narration_voice,
                        bgm_oss_key = EXCLUDED.bgm_oss_key,
                        config = EXCLUDED.config,
                        status = EXCLUDED.status,
                        version = EXCLUDED.version,
                        update_by = EXCLUDED.update_by,
                        update_time = now()
                """, (
                    int(template.id), template.name, template.description,
                    template.reference_oss_key, Jsonb(template.narration_voice or {}),
                    template.bgm_oss_key, Jsonb(template.config or {}), template.status,
                    int(template.version), template.created_by,
                    by or template.updated_by or template.created_by,
                ))
        except errors.UniqueViolation as exc:
            raise ValueError("Template name already exists") from exc

    def get(self, template_id: str) -> Optional[VideoEditingTemplate]:
        try:
            value = int(template_id)
        except (TypeError, ValueError):
            return None
        with self._conn() as c:
            row = c.execute(
                f"SELECT {_SELECT_COLS} FROM {self._table} WHERE id = %s AND del_flag = 0",
                (value,),
            ).fetchone()
        return self._to_model(row) if row else None

    def get_by_name(self, name: str) -> Optional[VideoEditingTemplate]:
        with self._conn() as c:
            row = c.execute(
                f"SELECT {_SELECT_COLS} FROM {self._table} WHERE lower(name) = lower(%s) AND del_flag = 0",
                ((name or "").strip(),),
            ).fetchone()
        return self._to_model(row) if row else None

    def list(self, name: str = "", status: str = "", offset: int = 0,
             limit: int | None = None) -> list[VideoEditingTemplate]:
        where = "del_flag = 0"
        params: list = []
        if name:
            where += " AND name ILIKE %s"
            params.append(f"%{name}%")
        if status:
            where += " AND status = %s"
            params.append(status)
        sql = f"SELECT {_SELECT_COLS} FROM {self._table} WHERE {where} ORDER BY id DESC"
        if limit is not None:
            sql += " LIMIT %s OFFSET %s"
            params.extend([int(limit), int(offset)])
        with self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        return [self._to_model(row) for row in rows]

    def count(self, name: str = "", status: str = "") -> int:
        where = "del_flag = 0"
        params: list = []
        if name:
            where += " AND name ILIKE %s"
            params.append(f"%{name}%")
        if status:
            where += " AND status = %s"
            params.append(status)
        with self._conn() as c:
            row = c.execute(f"SELECT COUNT(*) FROM {self._table} WHERE {where}", params).fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def _to_model(row) -> VideoEditingTemplate:
        return VideoEditingTemplate(
            id=str(row[0]), name=row[1], description=row[2] or "",
            reference_oss_key=row[3] or "", narration_voice=row[4] or {},
            bgm_oss_key=row[5] or "", config=row[6] or {}, status=row[7] or "active",
            version=int(row[8] or 1), created_by=row[9] or "",
            created_time=row[10].isoformat() if row[10] else "", updated_by=row[11] or "",
            updated_time=row[12].isoformat() if row[12] else "",
        )
