"""RBAC 权限仓储 —— PostgreSQL 真源实现(实现 domain.ports.RbacRepo)。
两张表:role_permission(角色→权限) + user_permission(用户→权限),均遵循全项目 PG 基础字段规范。
风格与 pg_rule_repo 一致:每操作短连接、autocommit、裸 SQL。infra→domain。"""
from __future__ import annotations
import re

from app.infrastructure.snowflake import next_id

_TABLE_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


class PgRbacRepo:
    def __init__(self, dsn: str, role_table: str = "role_permission",
                 user_table: str = "user_permission", idgen=None) -> None:
        if not _TABLE_RE.match(role_table):
            raise ValueError(f"非法表名: {role_table!r}")
        if not _TABLE_RE.match(user_table):
            raise ValueError(f"非法表名: {user_table!r}")
        self._dsn = dsn
        self._rt = role_table
        self._ut = user_table
        self._idgen = idgen or next_id
        self._init_schema()

    def _conn(self):
        import psycopg
        return psycopg.connect(self._dsn, autocommit=True, connect_timeout=10,
                               options="-c timezone=Asia/Shanghai")

    def _init_schema(self) -> None:
        with self._conn() as c:
            for t, desc, col1, col2 in [
                (self._rt, "角色权限。角色拥有哪些权限。",
                 ("role", "角色标识"), ("permission", "权限标识")),
                (self._ut, "用户权限。在角色默认权限之上叠加的额外权限。",
                 ("user_id", "用户 ID"), ("permission", "权限标识")),
            ]:
                c.execute(f"""
                    CREATE TABLE IF NOT EXISTS {t} (
                        id          BIGINT PRIMARY KEY,
                        {col1[0]}   TEXT NOT NULL,
                        {col2[0]}   TEXT NOT NULL,
                        del_flag    BIGINT NOT NULL DEFAULT 0,
                        create_by   TEXT NOT NULL DEFAULT '',
                        create_time TIMESTAMPTZ NOT NULL DEFAULT now(),
                        update_by   TEXT NOT NULL DEFAULT '',
                        update_time TIMESTAMPTZ NOT NULL DEFAULT now()
                    )""")
                c.execute(f"COMMENT ON TABLE {t} IS '{desc}'")
                c.execute(f"COMMENT ON COLUMN {t}.id IS '雪花算法 BIGINT 主键'")
                c.execute(f"COMMENT ON COLUMN {t}.{col1[0]} IS '{col1[1]}'")
                c.execute(f"COMMENT ON COLUMN {t}.{col2[0]} IS '{col2[1]}'")
                c.execute(f"COMMENT ON COLUMN {t}.del_flag IS '软删标记:0=在用，删除时置为新雪花ID'")
                c.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS uq_{t}_pair "
                          f"ON {t} ({col1[0]}, {col2[0]}, del_flag)")
                c.execute(f"CREATE INDEX IF NOT EXISTS idx_{t}_live "
                          f"ON {t} (del_flag) WHERE del_flag = 0")

    # ── 角色权限 ──
    def permissions_of(self, role: str) -> set[str]:
        with self._conn() as c:
            rows = c.execute(
                f"SELECT permission FROM {self._rt} "
                f"WHERE role = %s AND del_flag = 0", (role,)
            ).fetchall()
        return {r[0] for r in rows}

    def grant(self, role: str, permission: str) -> None:
        with self._conn() as c:
            c.execute(
                f"""INSERT INTO {self._rt} (id, role, permission, create_by, update_by)
                    VALUES (%s, %s, %s, '', '')
                    ON CONFLICT (role, permission, del_flag) WHERE del_flag = 0 DO NOTHING""",
                (self._idgen(), role, permission),
            )

    def revoke(self, role: str, permission: str) -> None:
        with self._conn() as c:
            c.execute(
                f"UPDATE {self._rt} SET del_flag = %s, update_time = now() "
                f"WHERE role = %s AND permission = %s AND del_flag = 0",
                (self._idgen(), role, permission),
            )

    # ── 用户级权限(叠加在角色默认权限之上) ──
    def user_permissions(self, user_id: str) -> set[str]:
        with self._conn() as c:
            rows = c.execute(
                f"SELECT permission FROM {self._ut} "
                f"WHERE user_id = %s AND del_flag = 0", (user_id,)
            ).fetchall()
        return {r[0] for r in rows}

    def set_user_permissions(self, user_id: str, permissions: set[str]) -> None:
        """原子替换:逐条软删旧(各独立雪花 del_flag) + 插新(同一连接内完成)。"""
        with self._conn() as c:
            # 逐条软删旧权限:每条给独立雪花 del_flag(符合规范)
            rows = c.execute(
                f"SELECT id FROM {self._ut} WHERE user_id = %s AND del_flag = 0",
                (user_id,),
            ).fetchall()
            for (pk,) in rows:
                c.execute(
                    f"UPDATE {self._ut} SET del_flag = %s, update_time = now() "
                    f"WHERE id = %s",
                    (self._idgen(), pk),
                )
            # 插新权限
            for perm in permissions:
                c.execute(
                    f"""INSERT INTO {self._ut} (id, user_id, permission, create_by, update_by)
                        VALUES (%s, %s, %s, '', '')
                        ON CONFLICT (user_id, permission, del_flag) WHERE del_flag = 0 DO NOTHING""",
                    (self._idgen(), user_id, perm),
                )
