"""用户仓储 —— PostgreSQL 真源实现(实现 domain.ports.UserRepo)。
遵循全项目 PG 业务表基础字段规范:id(雪花 BIGINT 主键) / del_flag(0=在用,删除时置新雪花 ID,软删)。
domain User.id(domain_id 列)与 name 分列存储;get() 按 domain_id 查,get_by_name() 按 name 查。
风格与 pg_rule_repo 一致:每操作短连接、autocommit、裸 SQL。infra→domain。"""
from __future__ import annotations
import re
from typing import Optional

from app.domain.models import User
from app.infrastructure.snowflake import next_id

_TABLE_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

_SELECT_COLS = "id, domain_id, name, pwd_hash, role, status"


class PgUserRepo:
    def __init__(self, dsn: str, table: str = "app_user", idgen=None) -> None:
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
                    id          BIGINT PRIMARY KEY,
                    domain_id   TEXT NOT NULL,
                    name        TEXT NOT NULL,
                    pwd_hash    TEXT NOT NULL,
                    role        TEXT DEFAULT 'viewer',
                    status      TEXT DEFAULT 'active',
                    del_flag    BIGINT NOT NULL DEFAULT 0,
                    create_by   TEXT NOT NULL DEFAULT '',
                    create_time TIMESTAMPTZ NOT NULL DEFAULT now(),
                    update_by   TEXT NOT NULL DEFAULT '',
                    update_time TIMESTAMPTZ NOT NULL DEFAULT now()
                )""")
            c.execute(f"COMMENT ON TABLE {t} IS '用户。密码只存加盐哈希，禁明文。domain_id 为领域 id,name 为登录名。'")
            c.execute(f"COMMENT ON COLUMN {t}.id IS '雪花算法 BIGINT 主键'")
            c.execute(f"COMMENT ON COLUMN {t}.domain_id IS '领域 User.id(UUID 或字符串),在用行内唯一'")
            c.execute(f"COMMENT ON COLUMN {t}.name IS '用户名(登录名),在用行内唯一'")
            c.execute(f"COMMENT ON COLUMN {t}.pwd_hash IS '加盐密码哈希'")
            c.execute(f"COMMENT ON COLUMN {t}.role IS '角色:admin/user/viewer'")
            c.execute(f"COMMENT ON COLUMN {t}.status IS '状态:active/disabled'")
            c.execute(f"COMMENT ON COLUMN {t}.del_flag IS '软删标记:0=在用，删除时置为新雪花ID'")
            c.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS uq_{t}_domain_id ON {t} (domain_id, del_flag)")
            c.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS uq_{t}_name ON {t} (name, del_flag)")
            c.execute(f"CREATE INDEX IF NOT EXISTS idx_{t}_live ON {t} (del_flag) WHERE del_flag = 0")

    def save(self, user: User) -> None:
        """插入或按 domain_id 覆盖(upsert,匹配在用行)。"""
        with self._conn() as c:
            c.execute(
                f"""INSERT INTO {self._table}
                        (id, domain_id, name, pwd_hash, role, status, create_by, update_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (domain_id, del_flag) WHERE del_flag = 0 DO UPDATE SET
                        name = EXCLUDED.name,
                        pwd_hash = EXCLUDED.pwd_hash,
                        role = EXCLUDED.role,
                        status = EXCLUDED.status,
                        update_by = EXCLUDED.update_by,
                        update_time = now()""",
                (self._idgen(), user.id, user.name, user.pwd_hash, user.role, user.status,
                 user.id, user.id),
            )

    def get_by_name(self, name: str) -> Optional[User]:
        n = (name or "").strip()
        if not n:
            return None
        with self._conn() as c:
            row = c.execute(
                f"SELECT {_SELECT_COLS} FROM {self._table} "
                f"WHERE name = %s AND del_flag = 0 LIMIT 1", (n,)
            ).fetchone()
        return self._to_user(row) if row else None

    def get(self, user_id: str) -> Optional[User]:
        """按 domain User.id 查找在用行。"""
        uid = (user_id or "").strip()
        if not uid:
            return None
        with self._conn() as c:
            row = c.execute(
                f"SELECT {_SELECT_COLS} FROM {self._table} "
                f"WHERE domain_id = %s AND del_flag = 0 LIMIT 1", (uid,)
            ).fetchone()
        return self._to_user(row) if row else None

    def list(self) -> list[User]:
        with self._conn() as c:
            rows = c.execute(
                f"SELECT {_SELECT_COLS} FROM {self._table} WHERE del_flag = 0 ORDER BY name"
            ).fetchall()
        return [self._to_user(r) for r in rows]

    def delete(self, user_id: str) -> None:
        """软删:del_flag 置新雪花 ID。按 domain User.id 删除在用行。"""
        uid = (user_id or "").strip()
        if not uid:
            return
        with self._conn() as c:
            c.execute(
                f"UPDATE {self._table} SET del_flag = %s, update_time = now() "
                f"WHERE domain_id = %s AND del_flag = 0",
                (self._idgen(), uid),
            )

    @staticmethod
    def _to_user(row) -> User:
        # row: (pg_id, domain_id, name, pwd_hash, role, status)
        _, domain_id, name, pwd_hash, role, status = row
        return User(id=domain_id, name=name, pwd_hash=pwd_hash, role=role, status=status)
