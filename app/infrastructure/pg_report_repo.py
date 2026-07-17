"""审核报告仓储 —— PostgreSQL 真源实现(实现隐式 AuditReportRepo 接口:save + get)。
报告 ID 是 domain 层生成的 UUID 字符串,直接做主键(非雪花)。segments/triggered 存 JSONB。
风格与 pg_rule_repo 一致:每操作短连接、autocommit、裸 SQL。infra→domain。"""
from __future__ import annotations
import re
import json
from typing import Optional

from app.domain.models import AuditReport, AuditStatus, TextSegment, TextSourceType

_TABLE_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


class PgAuditReportRepo:
    def __init__(self, dsn: str, table: str = "audit_report", idgen=None) -> None:
        if not _TABLE_RE.match(table):
            raise ValueError(f"非法表名: {table!r}")
        self._dsn = dsn
        self._table = table
        self._idgen = idgen   # 报告不需要雪花 ID,但保留参数兼容
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
                    report_id   TEXT PRIMARY KEY,
                    verdict     TEXT NOT NULL DEFAULT 'processing',
                    summary     TEXT NOT NULL DEFAULT '',
                    segments    JSONB NOT NULL DEFAULT '[]'::jsonb,
                    triggered   JSONB NOT NULL DEFAULT '[]'::jsonb,
                    create_time TIMESTAMPTZ NOT NULL DEFAULT now(),
                    update_time TIMESTAMPTZ NOT NULL DEFAULT now()
                )""")
            c.execute(f"COMMENT ON TABLE {t} IS '审核报告。总判定 + 各文字链路 + 命中的规则。'")
            c.execute(f"COMMENT ON COLUMN {t}.report_id IS 'domain 生成的报告 ID(UUID 字符串)'")
            c.execute(f"COMMENT ON COLUMN {t}.verdict IS '总判定:pass/review/block/processing'")
            c.execute(f"COMMENT ON COLUMN {t}.summary IS '审核摘要'")
            c.execute(f"COMMENT ON COLUMN {t}.segments IS '审核文字段列表 JSONB'")
            c.execute(f"COMMENT ON COLUMN {t}.triggered IS '命中规则列表 JSONB [{{rule_id,source_type,reason,action}}]'")

    def save(self, report_id: str, report: AuditReport) -> None:
        from psycopg.types.json import Jsonb
        with self._conn() as c:
            segs = [{"source_type": s.source_type.value, "text": s.text,
                     "begin_ms": s.begin_ms, "end_ms": s.end_ms,
                     "frame_oss_key": s.frame_oss_key} for s in (report.segments or [])]
            c.execute(
                f"""INSERT INTO {self._table} (report_id, verdict, summary, segments, triggered)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (report_id) DO UPDATE SET
                        verdict = EXCLUDED.verdict,
                        summary = EXCLUDED.summary,
                        segments = EXCLUDED.segments,
                        triggered = EXCLUDED.triggered,
                        update_time = now()""",
                (report_id, report.verdict.value, report.summary,
                 Jsonb(segs), Jsonb(report.triggered)),
            )

    def get(self, report_id: str) -> Optional[AuditReport]:
        rid = (report_id or "").strip()
        if not rid:
            return None
        with self._conn() as c:
            row = c.execute(
                f"SELECT report_id, verdict, summary, segments, triggered "
                f"FROM {self._table} WHERE report_id = %s", (rid,)
            ).fetchone()
        if row is None:
            return None
        return self._to_report(row)

    @staticmethod
    def _to_report(row) -> AuditReport:
        _, verdict, summary, segments, triggered = row
        segs = [TextSegment(
            source_type=TextSourceType(s["source_type"]),
            text=s["text"],
            begin_ms=s.get("begin_ms"),
            end_ms=s.get("end_ms"),
            frame_oss_key=s.get("frame_oss_key", ""),
        ) for s in (segments or [])]
        return AuditReport(
            verdict=AuditStatus(verdict),
            summary=summary,
            segments=segs,
            triggered=list(triggered or []),
        )
