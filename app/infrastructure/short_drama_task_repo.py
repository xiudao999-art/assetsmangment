"""Repositories for short-drama tasks. PostgreSQL is the maintained runtime source."""
from __future__ import annotations

import re
import threading
from datetime import UTC, datetime

from app.domain.models import ShortDramaTask

_TABLE_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_SELECT_COLS = (
    "id, online_time, expiration_time, drama_name, task_type, tags, pre_upload_teams, theme, task_status, task_id, "
    "requirements, cover_oss_key, cloud_material_url, topic_editing_requirements, "
    "submission_activity_time, settlement_mode, commission_validity_period, "
    "settlement_period, data_image_oss_key, quality_case, remarks, "
    "create_by, create_time, update_by, update_time"
)
_SORT_FIELDS = {
    "online_time", "expiration_time", "drama_name", "task_type", "theme", "task_status", "task_id",
    "settlement_mode", "settlement_period", "created_time",
}
_SORT_COLUMNS = {field: field for field in _SORT_FIELDS}
_SORT_COLUMNS["created_time"] = "create_time"
_OPTION_FIELDS = {
    "drama_name", "task_type", "theme", "settlement_mode",
    "commission_validity_period", "settlement_period", "tags",
    "pre_upload_teams", "online_time", "expiration_time", "task_id",
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _matches(item: ShortDramaTask, drama_name: str, task_status: str,
             task_type: str, theme: str, online_time: str = "", expiration_time: str = "",
             task_id: str = "", remarks: str = "",
             tag: str = "", pre_upload_team: str = "",
             actual_upload_drama_names: list[str] | None = None) -> bool:
    tag_key = tag.casefold()
    team_key = pre_upload_team.casefold()
    item_drama = item.drama_name.casefold()
    actual_match = actual_upload_drama_names is None or any(
        (name_key := str(name or "").strip().casefold())
        and (name_key in item_drama or item_drama in name_key)
        for name in actual_upload_drama_names
    )
    return (
        (not drama_name or drama_name.casefold() in item.drama_name.casefold())
        and (not task_status or task_status == item.task_status)
        and (not task_type or task_type.casefold() in item.task_type.casefold())
        and (not theme or theme.casefold() in item.theme.casefold())
        and (not online_time or online_time.casefold() in item.online_time.casefold())
        and (not expiration_time or expiration_time.casefold() in item.expiration_time.casefold())
        and (not task_id or task_id.casefold() in item.task_id.casefold())
        and (not remarks or remarks.casefold() in item.remarks.casefold())
        and (not tag_key or any(tag_key in value.casefold() for value in item.tags))
        and (not team_key or any(team_key in value.casefold()
                                 for value in item.pre_upload_teams))
        and actual_match
    )


class InMemoryShortDramaTaskRepo:
    """Non-persistent development/test fallback; production uses PostgreSQL."""

    def __init__(self) -> None:
        self._items: dict[str, ShortDramaTask] = {}
        self._lock = threading.RLock()

    def save(self, item: ShortDramaTask, by: str = "") -> None:
        with self._lock:
            duplicate = self.get_by_drama_name(item.drama_name)
            if duplicate and duplicate.id != item.id:
                raise ValueError("剧名已存在")
            previous = self._items.get(item.id)
            now = _now()
            item.created_time = previous.created_time if previous else (item.created_time or now)
            item.created_by = previous.created_by if previous else item.created_by
            item.updated_by = by or item.updated_by or item.created_by
            item.updated_time = now
            self._items[item.id] = item

    def bulk_upsert(self, items: list[ShortDramaTask], by: str = "") -> tuple[int, int]:
        created = updated = 0
        with self._lock:
            for item in items:
                previous = self.get_by_drama_name(item.drama_name)
                if previous:
                    item.id = previous.id
                    item.created_by = previous.created_by
                    item.created_time = previous.created_time
                    updated += 1
                else:
                    created += 1
                self.save(item, by=by)
        return created, updated

    def get(self, item_id: str) -> ShortDramaTask | None:
        return self._items.get(str(item_id))

    def get_by_drama_name(self, drama_name: str) -> ShortDramaTask | None:
        key = (drama_name or "").strip().casefold()
        return next((x for x in self._items.values() if x.drama_name.casefold() == key), None)

    def delete(self, item_id: str) -> bool:
        with self._lock:
            return self._items.pop(str(item_id), None) is not None

    def list(self, drama_name: str = "", task_status: str = "", task_type: str = "",
             theme: str = "", online_time: str = "", expiration_time: str = "",
             task_id: str = "", remarks: str = "", tag: str = "",
             pre_upload_team: str = "", sort_by: str = "created_time",
             sort_order: str = "desc", actual_upload_drama_names: list[str] | None = None,
             offset: int = 0,
             limit: int | None = None) -> list[ShortDramaTask]:
        items = [x for x in self._items.values()
                 if _matches(x, drama_name, task_status, task_type, theme, online_time,
                             expiration_time, task_id, remarks, tag, pre_upload_team,
                             actual_upload_drama_names)]
        items.sort(key=lambda x: int(x.id), reverse=True)
        checked_sort = sort_by if sort_by in _SORT_FIELDS else "created_time"
        items.sort(
            key=lambda x: str(getattr(x, checked_sort, "") or "").casefold(),
            reverse=sort_order == "desc",
        )
        return items[offset:] if limit is None else items[offset:offset + limit]

    def count(self, drama_name: str = "", task_status: str = "", task_type: str = "",
              theme: str = "", online_time: str = "", expiration_time: str = "",
              task_id: str = "", remarks: str = "", tag: str = "",
              pre_upload_team: str = "",
              actual_upload_drama_names: list[str] | None = None) -> int:
        return sum(1 for x in self._items.values()
                   if _matches(x, drama_name, task_status, task_type, theme, online_time,
                               expiration_time, task_id, remarks, tag, pre_upload_team,
                               actual_upload_drama_names))

    def list_options(self, field: str, keyword: str = "", limit: int = 200) -> list[str]:
        if field not in _OPTION_FIELDS:
            raise ValueError("不支持的短剧任务选项字段")
        key = (keyword or "").strip().casefold()
        values: list[str] = []
        for item in sorted(self._items.values(), key=lambda x: x.updated_time, reverse=True):
            raw_values = (getattr(item, field, []) if field in {"tags", "pre_upload_teams"}
                          else [getattr(item, field, "")])
            values.extend(str(value or "").strip() for value in raw_values)
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            normalized = value.casefold()
            if not value or normalized in seen or (key and key not in normalized):
                continue
            seen.add(normalized)
            result.append(value)
            if len(result) >= limit:
                break
        return result


class PgShortDramaTaskRepo:
    def __init__(self, dsn: str, table: str = "short_drama_task") -> None:
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
                    online_time TEXT NOT NULL DEFAULT '',
                    expiration_time TEXT NOT NULL DEFAULT '',
                    drama_name TEXT NOT NULL,
                    task_type TEXT NOT NULL DEFAULT '',
                    tags JSONB NOT NULL DEFAULT '[]'::jsonb,
                    pre_upload_teams JSONB NOT NULL DEFAULT '[]'::jsonb,
                    theme TEXT NOT NULL DEFAULT '',
                    task_status TEXT NOT NULL DEFAULT '未上线'
                        CHECK (task_status IN ('未上线', '已上线', '已结束')),
                    task_id TEXT NOT NULL DEFAULT '',
                    requirements TEXT NOT NULL DEFAULT '',
                    cover_oss_key TEXT NOT NULL DEFAULT '',
                    cloud_material_url TEXT NOT NULL DEFAULT '',
                    topic_editing_requirements TEXT NOT NULL DEFAULT '',
                    submission_activity_time TEXT NOT NULL DEFAULT '',
                    settlement_mode TEXT NOT NULL DEFAULT '',
                    commission_validity_period TEXT NOT NULL DEFAULT '',
                    settlement_period TEXT NOT NULL DEFAULT '',
                    data_image_oss_key TEXT NOT NULL DEFAULT '',
                    quality_case TEXT NOT NULL DEFAULT '',
                    remarks TEXT NOT NULL DEFAULT '',
                    del_flag BIGINT NOT NULL DEFAULT 0,
                    create_by TEXT NOT NULL DEFAULT '',
                    create_time TIMESTAMPTZ NOT NULL DEFAULT now(),
                    update_by TEXT NOT NULL DEFAULT '',
                    update_time TIMESTAMPTZ NOT NULL DEFAULT now()
                )""")
            c.execute(
                f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS "
                "pre_upload_teams JSONB NOT NULL DEFAULT '[]'::jsonb"
            )
            c.execute(
                f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS "
                "expiration_time TEXT NOT NULL DEFAULT ''"
            )
            c.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS uq_{t}_live_drama_name "
                f"ON {t} (lower(drama_name)) WHERE del_flag = 0"
            )
            c.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{t}_live_status "
                f"ON {t} (task_status, id DESC) WHERE del_flag = 0"
            )
            c.execute(f"COMMENT ON TABLE {t} IS '短剧任务表，素材提报的上游任务。'")
            c.execute(f"COMMENT ON COLUMN {t}.tags IS '标签 JSON 数组，例如 [\"爆剧\",\"新剧\"]。'")
            c.execute(f"COMMENT ON COLUMN {t}.cover_oss_key IS '封面 OSS 对象键。'")
            c.execute(f"COMMENT ON COLUMN {t}.data_image_oss_key IS '数据图 OSS 对象键。'")

    @staticmethod
    def _params(item: ShortDramaTask, by: str) -> tuple:
        from psycopg.types.json import Jsonb
        return (
            int(item.id), item.online_time, item.expiration_time,
            item.drama_name, item.task_type,
            Jsonb(item.tags or []), Jsonb(item.pre_upload_teams or []),
            item.theme, item.task_status, item.task_id,
            item.requirements, item.cover_oss_key, item.cloud_material_url,
            item.topic_editing_requirements, item.submission_activity_time,
            item.settlement_mode, item.commission_validity_period,
            item.settlement_period, item.data_image_oss_key, item.quality_case,
            item.remarks, item.created_by or by, by or item.updated_by or item.created_by,
        )

    def save(self, item: ShortDramaTask, by: str = "") -> None:
        from psycopg import errors
        try:
            with self._conn() as c:
                c.execute(self._upsert_id_sql(), self._params(item, by))
        except errors.UniqueViolation as exc:
            raise ValueError("剧名已存在") from exc

    def _upsert_id_sql(self) -> str:
        t = self._table
        return f"""
            INSERT INTO {t}
                (id, online_time, expiration_time, drama_name, task_type, tags, pre_upload_teams, theme, task_status,
                 task_id, requirements, cover_oss_key, cloud_material_url,
                 topic_editing_requirements, submission_activity_time, settlement_mode,
                 commission_validity_period, settlement_period, data_image_oss_key,
                 quality_case, remarks, create_by, update_by)
            VALUES ({', '.join(['%s'] * 23)})
            ON CONFLICT (id) DO UPDATE SET
                online_time=EXCLUDED.online_time, expiration_time=EXCLUDED.expiration_time,
                drama_name=EXCLUDED.drama_name,
                task_type=EXCLUDED.task_type, tags=EXCLUDED.tags,
                pre_upload_teams=EXCLUDED.pre_upload_teams, theme=EXCLUDED.theme,
                task_status=EXCLUDED.task_status, task_id=EXCLUDED.task_id,
                requirements=EXCLUDED.requirements, cover_oss_key=EXCLUDED.cover_oss_key,
                cloud_material_url=EXCLUDED.cloud_material_url,
                topic_editing_requirements=EXCLUDED.topic_editing_requirements,
                submission_activity_time=EXCLUDED.submission_activity_time,
                settlement_mode=EXCLUDED.settlement_mode,
                commission_validity_period=EXCLUDED.commission_validity_period,
                settlement_period=EXCLUDED.settlement_period,
                data_image_oss_key=EXCLUDED.data_image_oss_key,
                quality_case=EXCLUDED.quality_case, remarks=EXCLUDED.remarks,
                update_by=EXCLUDED.update_by, update_time=now()
        """

    def bulk_upsert(self, items: list[ShortDramaTask], by: str = "") -> tuple[int, int]:
        if not items:
            return 0, 0
        names = [x.drama_name.casefold() for x in items]
        with self._conn() as c:
            rows = c.execute(
                f"SELECT id, lower(drama_name), create_by, create_time FROM {self._table} "
                "WHERE del_flag = 0 AND lower(drama_name) = ANY(%s)", (names,),
            ).fetchall()
            existing = {row[1]: row for row in rows}
            updated = 0
            for item in items:
                previous = existing.get(item.drama_name.casefold())
                if previous:
                    item.id = str(previous[0])
                    item.created_by = previous[2] or ""
                    item.created_time = previous[3].isoformat() if previous[3] else ""
                    updated += 1
            with c.cursor() as cursor:
                cursor.executemany(
                    self._upsert_drama_sql(),
                    [self._params(x, by) for x in items],
                )
        return len(items) - updated, updated

    def _upsert_drama_sql(self) -> str:
        t = self._table
        return f"""
            INSERT INTO {t}
                (id, online_time, expiration_time, drama_name, task_type, tags, pre_upload_teams, theme, task_status,
                 task_id, requirements, cover_oss_key, cloud_material_url,
                 topic_editing_requirements, submission_activity_time, settlement_mode,
                 commission_validity_period, settlement_period, data_image_oss_key,
                 quality_case, remarks, create_by, update_by)
            VALUES ({', '.join(['%s'] * 23)})
            ON CONFLICT (lower(drama_name)) WHERE del_flag = 0 DO UPDATE SET
                online_time=EXCLUDED.online_time, expiration_time=EXCLUDED.expiration_time,
                task_type=EXCLUDED.task_type,
                tags=EXCLUDED.tags, pre_upload_teams={t}.pre_upload_teams,
                theme=EXCLUDED.theme, task_status=EXCLUDED.task_status,
                task_id=EXCLUDED.task_id, requirements=EXCLUDED.requirements,
                cover_oss_key=EXCLUDED.cover_oss_key,
                cloud_material_url=EXCLUDED.cloud_material_url,
                topic_editing_requirements=EXCLUDED.topic_editing_requirements,
                submission_activity_time=EXCLUDED.submission_activity_time,
                settlement_mode=EXCLUDED.settlement_mode,
                commission_validity_period=EXCLUDED.commission_validity_period,
                settlement_period=EXCLUDED.settlement_period,
                data_image_oss_key=EXCLUDED.data_image_oss_key,
                quality_case=EXCLUDED.quality_case, remarks=EXCLUDED.remarks,
                update_by=EXCLUDED.update_by, update_time=now()
        """

    def get(self, item_id: str) -> ShortDramaTask | None:
        try:
            value = int(item_id)
        except (TypeError, ValueError):
            return None
        with self._conn() as c:
            row = c.execute(
                f"SELECT {_SELECT_COLS} FROM {self._table} WHERE id=%s AND del_flag=0",
                (value,),
            ).fetchone()
        return self._to_model(row) if row else None

    def get_by_drama_name(self, drama_name: str) -> ShortDramaTask | None:
        with self._conn() as c:
            row = c.execute(
                f"SELECT {_SELECT_COLS} FROM {self._table} "
                "WHERE lower(drama_name)=lower(%s) AND del_flag=0",
                ((drama_name or "").strip(),),
            ).fetchone()
        return self._to_model(row) if row else None

    def delete(self, item_id: str) -> bool:
        try:
            value = int(item_id)
        except (TypeError, ValueError):
            return False
        from app.infrastructure.snowflake import next_id
        with self._conn() as c:
            result = c.execute(
                f"UPDATE {self._table} SET del_flag=%s, update_time=now() "
                "WHERE id=%s AND del_flag=0", (next_id(), value),
            )
        return result.rowcount > 0

    @staticmethod
    def _where(drama_name: str, task_status: str, task_type: str, theme: str,
               online_time: str = "", expiration_time: str = "", task_id: str = "", tag: str = "",
               pre_upload_team: str = "", remarks: str = "",
               actual_upload_drama_names: list[str] | None = None) -> tuple[str, list]:
        where = "del_flag = 0"
        params: list = []
        for column, value, exact in (
            ("drama_name", drama_name, False), ("task_status", task_status, True),
            ("task_type", task_type, False), ("theme", theme, False),
            ("online_time", online_time, False),
            ("expiration_time", expiration_time, False), ("task_id", task_id, False),
            ("remarks", remarks, False),
        ):
            if value:
                where += f" AND {column} {'=' if exact else 'ILIKE'} %s"
                params.append(value if exact else f"%{value}%")
        for column, value in (("tags", tag), ("pre_upload_teams", pre_upload_team)):
            if value:
                where += (
                    f" AND EXISTS (SELECT 1 FROM jsonb_array_elements_text({column}) option "
                    "WHERE option ILIKE %s)"
                )
                params.append(f"%{value}%")
        if actual_upload_drama_names is not None:
            if not actual_upload_drama_names:
                where += " AND FALSE"
            else:
                where += (
                    " AND EXISTS (SELECT 1 FROM unnest(%s::text[]) upload_drama "
                    "WHERE upload_drama ILIKE '%%' || drama_name || '%%' "
                    "OR drama_name ILIKE '%%' || upload_drama || '%%')"
                )
                params.append(actual_upload_drama_names)
        return where, params

    def list(self, drama_name: str = "", task_status: str = "", task_type: str = "",
             theme: str = "", online_time: str = "", expiration_time: str = "",
             task_id: str = "", remarks: str = "", tag: str = "",
             pre_upload_team: str = "", sort_by: str = "created_time",
             sort_order: str = "desc", actual_upload_drama_names: list[str] | None = None,
             offset: int = 0,
             limit: int | None = None) -> list[ShortDramaTask]:
        where, params = self._where(
            drama_name, task_status, task_type, theme, online_time, expiration_time,
            task_id, tag,
            pre_upload_team, remarks, actual_upload_drama_names,
        )
        checked_sort = sort_by if sort_by in _SORT_FIELDS else "created_time"
        sort_column = _SORT_COLUMNS[checked_sort]
        checked_order = "ASC" if sort_order == "asc" else "DESC"
        sql = (
            f"SELECT {_SELECT_COLS} FROM {self._table} WHERE {where} "
            f"ORDER BY {sort_column} {checked_order}, id DESC"
        )
        if limit is not None:
            sql += " LIMIT %s OFFSET %s"
            params.extend([int(limit), int(offset)])
        with self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        return [self._to_model(row) for row in rows]

    def count(self, drama_name: str = "", task_status: str = "", task_type: str = "",
              theme: str = "", online_time: str = "", expiration_time: str = "",
              task_id: str = "", remarks: str = "", tag: str = "",
              pre_upload_team: str = "",
              actual_upload_drama_names: list[str] | None = None) -> int:
        where, params = self._where(
            drama_name, task_status, task_type, theme, online_time, expiration_time,
            task_id, tag,
            pre_upload_team, remarks, actual_upload_drama_names,
        )
        with self._conn() as c:
            row = c.execute(f"SELECT COUNT(*) FROM {self._table} WHERE {where}", params).fetchone()
        return int(row[0]) if row else 0

    def list_options(self, field: str, keyword: str = "", limit: int = 200) -> list[str]:
        if field not in _OPTION_FIELDS:
            raise ValueError("不支持的短剧任务选项字段")
        params: list = []
        if field in {"tags", "pre_upload_teams"}:
            value_sql = f"jsonb_array_elements_text({field})"
            source_sql = f"SELECT {value_sql} AS value, update_time FROM {self._table} WHERE del_flag=0"
        else:
            source_sql = f"SELECT {field} AS value, update_time FROM {self._table} WHERE del_flag=0"
        where = "value <> ''"
        if keyword:
            where += " AND value ILIKE %s"
            params.append(f"%{keyword}%")
        params.append(int(limit))
        sql = (
            "SELECT value FROM ("
            "SELECT DISTINCT ON (lower(value)) value, update_time "
            f"FROM ({source_sql}) AS source WHERE {where} "
            "ORDER BY lower(value), update_time DESC"
            ") AS options ORDER BY update_time DESC, value LIMIT %s"
        )
        with self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        return [str(row[0]) for row in rows]

    @staticmethod
    def _to_model(row) -> ShortDramaTask:
        return ShortDramaTask(
            id=str(row[0]), online_time=row[1] or "", expiration_time=row[2] or "",
            drama_name=row[3] or "", task_type=row[4] or "", tags=list(row[5] or []),
            pre_upload_teams=list(row[6] or []), theme=row[7] or "",
            task_status=row[8] or "未上线", task_id=row[9] or "",
            requirements=row[10] or "", cover_oss_key=row[11] or "",
            cloud_material_url=row[12] or "", topic_editing_requirements=row[13] or "",
            submission_activity_time=row[14] or "", settlement_mode=row[15] or "",
            commission_validity_period=row[16] or "", settlement_period=row[17] or "",
            data_image_oss_key=row[18] or "", quality_case=row[19] or "",
            remarks=row[20] or "", created_by=row[21] or "",
            created_time=row[22].isoformat() if row[22] else "", updated_by=row[23] or "",
            updated_time=row[24].isoformat() if row[24] else "",
        )
