"""将现有全部物料和审核规则归于指定项目（幂等，可重复跑）。

用法:
  .venv\Scripts\python scripts\assign_to_project.py

默认目标项目名：「汽水音乐金币类」
PG 连接串从环境变量 AM_DATABASE_URL（或 .env）取。
"""
from __future__ import annotations
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.infrastructure.snowflake import next_id
from app.infrastructure.pg_project_repo import PgProjectRepo
from app.infrastructure.pg_material_repo import PgMaterialRepo
from app.infrastructure.pg_rule_repo import PgAuditRuleRepo
from app.config import settings

PROJECT_NAME = "汽水音乐金币类"


def main():
    dsn = settings.database_url
    if not dsn:
        print("❌ AM_DATABASE_URL 未配置，请检查 .env")
        sys.exit(1)

    print(f"📋 目标项目: {PROJECT_NAME}")
    print(f"🔗 数据库: {dsn.split('@')[1] if '@' in dsn else dsn}")

    # 1. 创建/获取项目
    project_repo = PgProjectRepo(dsn)
    proj = project_repo.get_by_name(PROJECT_NAME)
    if proj is None:
        from app.domain.models import Project
        import time
        proj = Project(
            id=str(next_id()),
            name=PROJECT_NAME,
            created_by="system",
            created_ms=int(time.time() * 1000),
        )
        project_repo.add(proj)
        print(f"✅ 创建项目: {PROJECT_NAME} (id={proj.id})")
    else:
        print(f"✅ 项目已存在: {PROJECT_NAME} (id={proj.id})")

    pid = proj.id

    # 2. 更新所有物料(无论当前归属,全部归于目标项目)
    import psycopg
    conn = psycopg.connect(dsn, autocommit=True)

    before_mat = conn.execute(
        "SELECT COUNT(*), COUNT(*) FILTER (WHERE project_id != %s) FROM material WHERE del_flag = 0",
        (pid,),
    ).fetchone()
    total_materials = before_mat[0]
    need_update = before_mat[1]
    print(f"\n📦 物料总数: {total_materials}, 需迁移: {need_update}")

    if need_update > 0:
        conn.execute(
            "UPDATE material SET project_id = %s, update_time = now() "
            "WHERE del_flag = 0 AND project_id != %s",
            (pid, pid),
        )
        print(f"✅ 已将 {need_update} 条物料归于项目 {PROJECT_NAME}")
    else:
        print("✅ 物料已全部归属该项目，无需更新")

    # 3. 更新所有审核规则(无论当前是全局还是其他项目,全部归于目标项目)
    before_rules = conn.execute(
        "SELECT COUNT(*), COUNT(*) FILTER (WHERE project_id != %s) FROM audit_rule WHERE del_flag = 0",
        (pid,),
    ).fetchone()
    total_rules = before_rules[0]
    need_update = before_rules[1]
    print(f"\n📏 规则总数: {total_rules}, 需迁移: {need_update}")

    if need_update > 0:
        conn.execute(
            "UPDATE audit_rule SET project_id = %s, update_time = now() "
            "WHERE del_flag = 0 AND project_id != %s",
            (pid, pid),
        )
        print(f"✅ 已将 {need_update} 条规则归于项目 {PROJECT_NAME}")
    else:
        print("✅ 规则已全部归属该项目，无需更新")

    # 4. 更新审核任务
    before_tasks = conn.execute(
        "SELECT COUNT(*), COUNT(*) FILTER (WHERE project_id != %s) FROM audit_task WHERE del_flag = 0",
        (pid,),
    ).fetchone()
    need_update = before_tasks[1]
    if need_update > 0:
        conn.execute(
            "UPDATE audit_task SET project_id = %s, update_time = now() "
            "WHERE del_flag = 0 AND project_id != %s",
            (pid, pid),
        )
        print(f"✅ 已将 {need_update} 条审核任务归于项目 {PROJECT_NAME}")

    # 5. 汇总
    print("\n" + "=" * 60)
    print("🎉 迁移完成！汇总:")
    mat_count = conn.execute("SELECT COUNT(*) FROM material WHERE project_id = %s AND del_flag = 0", (pid,)).fetchone()[0]
    rule_count = conn.execute("SELECT COUNT(*) FROM audit_rule WHERE project_id = %s AND del_flag = 0", (pid,)).fetchone()[0]
    task_count = conn.execute("SELECT COUNT(*) FROM audit_task WHERE project_id = %s AND del_flag = 0", (pid,)).fetchone()[0]
    print(f"   项目: {PROJECT_NAME} (id={pid})")
    print(f"   物料: {mat_count} 条")
    print(f"   规则: {rule_count} 条")
    print(f"   审核任务: {task_count} 条")

    conn.close()


if __name__ == "__main__":
    main()
