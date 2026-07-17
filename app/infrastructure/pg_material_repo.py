"""物料仓储 —— PostgreSQL 真源实现(实现 domain.ports.MaterialRepo)。
遵循全项目 PG 业务表基础字段规范:id(雪花 BIGINT 主键) / del_flag(0=在用,删除时置新雪花 ID,软删)。
embedding 向量由 PgVectorIndex 独立管理,物料表不存向量。
风格与 pg_rule_repo 一致:每操作短连接、autocommit、裸 SQL。infra→domain。"""
from __future__ import annotations
import json
import re
from typing import Optional

from app.domain.models import Material, MaterialType, AuditStatus
from app.domain.query import MaterialQuery, haystack, matches_keyword
from app.infrastructure.snowflake import next_id

_TABLE_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

_SELECT_COLS = (
    "id, type, thumb, source_timecode, audit_status, source_job, "
    "oss_key, description, owner_id, is_public, audit_report_id, "
    "content_hash, project_id, tags, ai_summary, ai_scenarios, "
    "ai_emotions, ai_atmosphere, reject_events"
)


def _to_int_ids(ids) -> list[int]:
    """将字符串 ID 集合转为 int 列表(雪花 BIGINT),跳过不能转换的(UUID 等)。"""
    result = []
    for x in ids:
        try:
            result.append(int(x))
        except (TypeError, ValueError):
            pass
    return result


class PgMaterialRepo:
    def __init__(self, dsn: str, table: str = "material", idgen=None) -> None:
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
                    type            TEXT NOT NULL DEFAULT 'image',
                    thumb           TEXT NOT NULL DEFAULT '',
                    source_timecode DOUBLE PRECISION DEFAULT 0,
                    audit_status    TEXT NOT NULL DEFAULT 'review',
                    source_job      TEXT NOT NULL DEFAULT '',
                    oss_key         TEXT NOT NULL DEFAULT '',
                    description     TEXT NOT NULL DEFAULT '',
                    owner_id        TEXT NOT NULL DEFAULT '',
                    is_public       BOOLEAN NOT NULL DEFAULT FALSE,
                    audit_report_id TEXT NOT NULL DEFAULT '',
                    content_hash    TEXT NOT NULL DEFAULT '',
                    project_id      TEXT NOT NULL DEFAULT '',
                    tags            JSONB NOT NULL DEFAULT '[]'::jsonb,
                    ai_summary      TEXT NOT NULL DEFAULT '',
                    ai_scenarios    JSONB NOT NULL DEFAULT '[]'::jsonb,
                    ai_emotions     JSONB NOT NULL DEFAULT '[]'::jsonb,
                    ai_atmosphere   TEXT NOT NULL DEFAULT '',
                    reject_events   JSONB NOT NULL DEFAULT '[]'::jsonb,
                    del_flag        BIGINT NOT NULL DEFAULT 0,
                    create_by       TEXT NOT NULL DEFAULT '',
                    create_time     TIMESTAMPTZ NOT NULL DEFAULT now(),
                    update_by       TEXT NOT NULL DEFAULT '',
                    update_time     TIMESTAMPTZ NOT NULL DEFAULT now()
                )""")
            c.execute(f"COMMENT ON TABLE {t} IS '物料。图像/表情包/视频/风格/语料/音乐/声音。'")
            c.execute(f"COMMENT ON COLUMN {t}.id IS '雪花算法 BIGINT 主键，API 序列化为字符串'")
            c.execute(f"COMMENT ON COLUMN {t}.type IS '物料类型:image/meme/video/style/corpus/music/audio'")
            c.execute(f"COMMENT ON COLUMN {t}.audit_status IS '审核状态:processing/pass/review/block'")
            c.execute(f"COMMENT ON COLUMN {t}.tags IS '标签列表 JSONB'")
            c.execute(f"COMMENT ON COLUMN {t}.ai_scenarios IS '适用情境列表 JSONB'")
            c.execute(f"COMMENT ON COLUMN {t}.ai_emotions IS '情绪列表 JSONB'")
            c.execute(f"COMMENT ON COLUMN {t}.reject_events IS '退回历史 JSONB [{{ms,reason,by}}]'")
            c.execute(f"COMMENT ON COLUMN {t}.del_flag IS '软删标记:0=在用，删除时置为新雪花ID'")
            c.execute(f"CREATE INDEX IF NOT EXISTS idx_{t}_owner ON {t} (owner_id, del_flag)")
            c.execute(f"CREATE INDEX IF NOT EXISTS idx_{t}_status ON {t} (audit_status, del_flag)")
            c.execute(f"CREATE INDEX IF NOT EXISTS idx_{t}_hash ON {t} (owner_id, content_hash, del_flag)")
            c.execute(f"CREATE INDEX IF NOT EXISTS idx_{t}_live ON {t} (del_flag) WHERE del_flag = 0")

    # ── CRUD ──
    def save(self, material: Material) -> None:
        from psycopg.types.json import Jsonb
        # domain 层可能传 UUID id;PG 用雪花 BIGINT → 先尝试转换,失败则换发雪花
        try:
            rid = int(material.id)
        except (TypeError, ValueError):
            # 旧 UUID 或非整数 id → 换发雪花(新入库);已存在的按 content_hash 找
            existing = None
            if material.content_hash and material.owner_id:
                existing = self.by_content_hash(material.owner_id, material.content_hash)
            rid = int(existing.id) if existing else self._idgen()
            material.id = str(rid)  # 回写:调用方后续用 material.id 操作(如 index_material)

        with self._conn() as c:
            c.execute(
                f"""INSERT INTO {self._table}
                        (id, type, thumb, source_timecode, audit_status, source_job,
                         oss_key, description, owner_id, is_public, audit_report_id,
                         content_hash, project_id, tags, ai_summary, ai_scenarios,
                         ai_emotions, ai_atmosphere, reject_events,
                         create_by, update_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        type = EXCLUDED.type,
                        thumb = EXCLUDED.thumb,
                        source_timecode = EXCLUDED.source_timecode,
                        audit_status = EXCLUDED.audit_status,
                        source_job = EXCLUDED.source_job,
                        oss_key = EXCLUDED.oss_key,
                        description = EXCLUDED.description,
                        owner_id = EXCLUDED.owner_id,
                        is_public = EXCLUDED.is_public,
                        audit_report_id = EXCLUDED.audit_report_id,
                        content_hash = EXCLUDED.content_hash,
                        project_id = EXCLUDED.project_id,
                        tags = EXCLUDED.tags,
                        ai_summary = EXCLUDED.ai_summary,
                        ai_scenarios = EXCLUDED.ai_scenarios,
                        ai_emotions = EXCLUDED.ai_emotions,
                        ai_atmosphere = EXCLUDED.ai_atmosphere,
                        reject_events = EXCLUDED.reject_events,
                        update_by = EXCLUDED.update_by,
                        update_time = now()""",
                (rid, material.type.value, material.thumb,
                 material.source_timecode, material.audit_status.value,
                 material.source_job, material.oss_key, material.description,
                 material.owner_id, material.is_public, material.audit_report_id,
                 material.content_hash, material.project_id,
                 Jsonb(material.tags), material.ai_summary,
                 Jsonb(material.ai_scenarios), Jsonb(material.ai_emotions),
                 material.ai_atmosphere, Jsonb(material.reject_events),
                 material.owner_id, material.owner_id),
            )

    def get(self, material_id: str) -> Optional[Material]:
        try:
            mid = int(material_id)
        except (TypeError, ValueError):
            return None
        with self._conn() as c:
            row = c.execute(
                f"SELECT {_SELECT_COLS} FROM {self._table} "
                f"WHERE id = %s AND del_flag = 0", (mid,)
            ).fetchone()
        return self._to_material(row) if row else None

    def delete(self, material_id: str) -> None:
        try:
            mid = int(material_id)
        except (TypeError, ValueError):
            return
        with self._conn() as c:
            c.execute(
                f"UPDATE {self._table} SET del_flag = %s, update_time = now() "
                f"WHERE id = %s AND del_flag = 0",
                (self._idgen(), mid),
            )

    def list(self) -> list[Material]:
        with self._conn() as c:
            rows = c.execute(
                f"SELECT {_SELECT_COLS} FROM {self._table} "
                f"WHERE del_flag = 0 ORDER BY id"
            ).fetchall()
        return [self._to_material(r) for r in rows]

    @staticmethod
    def _haystack_sql() -> str:
        """返回 SQL 表达式:拼接所有文本字段+JSONB数组为搜索用 haystack。
        CONCAT_WS 忽略 NULL;jsonb_array_elements_text 展开数组;COALESCE 兜底空串。"""
        return (
            "CONCAT_WS(' ', thumb, description, ai_summary, ai_atmosphere, "
            "COALESCE((SELECT string_agg(value, ' ') FROM jsonb_array_elements_text(tags)), ''), "
            "COALESCE((SELECT string_agg(value, ' ') FROM jsonb_array_elements_text(ai_emotions)), ''), "
            "COALESCE((SELECT string_agg(value, ' ') FROM jsonb_array_elements_text(ai_scenarios)), ''))"
        )

    # ── 搜索 ──
    def search(self, query_text: str, only_pass: bool = True) -> list[Material]:
        """关键词搜索:在文本字段中做 ILIKE 子串匹配。
        与 JSON/内存实现语义对齐(keyword in haystack)。"""
        if not query_text:
            return []

        t = self._table
        wheres = ["del_flag = 0"]
        params: list = []

        if only_pass:
            wheres.append("audit_status = 'pass'")

        wheres.append(f"{self._haystack_sql()} LIKE %s")
        params.append(f"%{query_text}%")

        where = " AND ".join(wheres)
        with self._conn() as c:
            rows = c.execute(
                f"SELECT {_SELECT_COLS} FROM {t} WHERE {where} "
                f"ORDER BY id", params
            ).fetchall()
        return [self._to_material(r) for r in rows]

    # ── 服务端翻页/筛选 ──
    def query(self, spec: MaterialQuery) -> tuple[list[Material], int]:
        """将 MaterialQuery 翻译为 SQL WHERE + LIMIT/OFFSET + COUNT(*)。
        与 domain.query.matches 的谓词语义完全对齐。"""
        t = self._table
        wheres = ["del_flag = 0"]
        params: list = []

        # ── 归属门 ──
        if spec.owner_or_include:
            # 我的库: owner 匹配 OR id IN include_ids
            parts = []
            if spec.owner_id is not None:
                parts.append(f"owner_id = %s")
                params.append(spec.owner_id)
            if spec.include_ids is not None and len(spec.include_ids) > 0:
                ids_int = _to_int_ids(spec.include_ids)
                if ids_int:
                    placeholders = ",".join(["%s"] * len(ids_int))
                    parts.append(f"id IN ({placeholders})")
                    params.extend(ids_int)
            if parts:
                wheres.append(f"({' OR '.join(parts)})")
            else:
                wheres.append("FALSE")
        else:
            if spec.owner_id is not None:
                wheres.append("owner_id = %s")
                params.append(spec.owner_id)
            if spec.include_ids is not None:
                ids_int = _to_int_ids(spec.include_ids)
                if ids_int:
                    placeholders = ",".join(["%s"] * len(ids_int))
                    wheres.append(f"id IN ({placeholders})")
                    params.extend(ids_int)

        # ── 筛选 ──
        if spec.public_only:
            wheres.append("is_public = TRUE")
        if spec.pass_only:
            wheres.append("audit_status = 'pass'")
        if spec.status is not None:
            wheres.append("audit_status = %s")
            params.append(spec.status)
        if spec.type is not None:
            wheres.append("type = %s")
            params.append(spec.type)
        if spec.tag is not None:
            # 精确命中 tags JSONB 数组
            wheres.append("tags @> %s::jsonb")
            params.append(json.dumps([spec.tag]))
        if spec.project_id is not None:
            wheres.append("project_id = %s")
            params.append(spec.project_id)
        if spec.keyword:
            wheres.append(f"{self._haystack_sql()} LIKE %s")
            params.append(f"%{spec.keyword}%")

        where = " AND ".join(wheres)

        # ── COUNT(*) + 分页数据在同一连接内完成(防翻页时数据变更导致 total 不准) ──
        with self._conn() as c:
            total = c.execute(
                f"SELECT COUNT(*) FROM {t} WHERE {where}", params
            ).fetchone()[0]

            if spec.limit is not None:
                sql = f"SELECT {_SELECT_COLS} FROM {t} WHERE {where} ORDER BY id LIMIT %s OFFSET %s"
                params_data = list(params) + [spec.limit, spec.offset]
            else:
                sql = f"SELECT {_SELECT_COLS} FROM {t} WHERE {where} ORDER BY id"
                params_data = list(params)

            rows = c.execute(sql, params_data).fetchall()
        return [self._to_material(r) for r in rows], total

    # ── 去重 ──
    def by_content_hash(self, owner_id: str, content_hash: str) -> Optional[Material]:
        if not content_hash:
            return None
        with self._conn() as c:
            row = c.execute(
                f"SELECT {_SELECT_COLS} FROM {self._table} "
                f"WHERE owner_id = %s AND content_hash = %s AND del_flag = 0 LIMIT 1",
                (owner_id, content_hash),
            ).fetchone()
        return self._to_material(row) if row else None

    # ── 行 → 领域对象 ──
    @staticmethod
    def _to_material(row) -> Material:
        (rid, mtype, thumb, source_timecode, audit_status, source_job,
         oss_key, description, owner_id, is_public, audit_report_id,
         content_hash, project_id, tags, ai_summary, ai_scenarios,
         ai_emotions, ai_atmosphere, reject_events) = row
        return Material(
            id=str(rid),
            type=MaterialType(mtype),
            thumb=thumb,
            source_timecode=source_timecode,
            embedding=[],  # 向量由 PgVectorIndex 独立管理
            audit_status=AuditStatus(audit_status),
            source_job=source_job,
            oss_key=oss_key,
            description=description,
            owner_id=owner_id,
            is_public=bool(is_public),
            audit_report_id=audit_report_id,
            content_hash=content_hash,
            project_id=project_id,
            tags=list(tags or []),
            ai_summary=ai_summary,
            ai_scenarios=list(ai_scenarios or []),
            ai_emotions=list(ai_emotions or []),
            ai_atmosphere=ai_atmosphere,
            reject_events=list(reject_events or []),
        )
