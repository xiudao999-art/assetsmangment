"""为 PostgreSQL public schema 的业务表和字段补齐中文注释。

默认读取 .env 中的 AM_DATABASE_URL。脚本会先校验当前库中的每张表、每个字段
都有明确的中文注释映射；只要发现遗漏就终止，不会写入部分注释。
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg import sql


TABLE_COMMENTS = {
    "app_user": "应用用户表，保存登录身份、角色和账号状态，密码仅存储加盐哈希。",
    "audit_log": "审计日志表，只追加记录权限操作等关键事件。",
    "audit_report": "审核报告表，保存审核总判定、文本审核明细和命中规则。",
    "audit_rule": "审核规则表，按文本来源和所属项目匹配物料并执行相应审核动作。",
    "audit_task": "审核任务表，保存用户提交的审核任务及其处理状态。",
    "blockword": "绝对禁词表，命中任一在用词条时直接拦截内容。",
    "content_whitelist": "内容安全白名单表，命中在用词条时允许覆盖外部安全服务的违规判定。",
    "material": "物料表，保存图像、表情包、视频、风格、语料、音乐和声音等物料。",
    "material_submission": "素材提报表，记录团队提报素材、上传状态和平台反馈。",
    "material_submission_permission": "素材提报数据权限关联表，记录用户对提报记录的阅读或编辑权限。",
    "material_vectors": "物料向量表，保存物料的语义向量并支持相似度检索。",
    "project": "作品项目表，每个项目维护独立审核规则，作品必须归属项目。",
    "requirement": "需求提报表，保存内部新需求及其处理和验收信息。",
    "role_permission": "角色权限表，记录角色默认拥有的权限。",
    "rule_training_example": "规则训练样本表，保存物料应命中哪些规则的人工标注结果。",
    "rule_training_set": "规则训练集表，按项目保存训练配置、规则快照和训练结果。",
    "user_favorite": "用户收藏关系表，记录用户收藏的公共物料。",
    "user_permission": "用户权限表，记录用户在角色默认权限之外拥有的额外权限。",
    "video_editing_template": "视频剪辑模板表，媒体字段仅保存 OSS 对象键。",
}


COMMON_COLUMN_COMMENTS = {
    "id": "雪花算法生成的主键，接口中以字符串形式返回。",
    "del_flag": "软删除标记，0 表示在用，删除后保存删除操作生成的雪花 ID。",
    "create_by": "创建人标识。",
    "create_time": "创建时间。",
    "update_by": "最后更新人标识。",
    "update_time": "最后更新时间。",
}


COLUMN_COMMENTS = {
    "app_user": {
        "domain_id": "领域用户标识，在未删除记录中唯一。",
        "name": "登录用户名，在未删除记录中唯一。",
        "pwd_hash": "加盐后的密码哈希，禁止保存明文密码。",
        "role": "用户角色：admin 为管理员，user 为普通用户，viewer 为只读用户。",
        "status": "账号状态：active 为启用，disabled 为停用。",
    },
    "audit_log": {
        "event": "审计事件的文字描述。",
        "create_time": "事件记录时间。",
    },
    "audit_report": {
        "report_id": "审核报告标识，由领域层生成。",
        "verdict": "审核总判定：pass 为通过，review 为人工复核，block 为拦截，processing 为处理中。",
        "summary": "审核结果摘要。",
        "segments": "各文本片段的审核结果，使用 JSONB 保存。",
        "triggered": "命中的审核规则列表，使用 JSONB 保存。",
    },
    "audit_rule": {
        "no": "规则编号，在未删除记录中唯一并从 1 递增。",
        "source_type": "参与匹配的文本来源类型，any 表示匹配所有来源。",
        "keywords": "用于快速筛选的关键词列表，使用 JSONB 保存。",
        "condition": "提交给大模型判断的自然语言审核条件。",
        "action": "规则命中后的动作：block 为拦截，review 为转人工复核。",
        "enabled": "是否启用，停用规则不参与审核。",
        "project_id": "所属项目标识，为空表示全局规则。",
        "guidance": "审核尺度说明，可包含正例、反例等判定指导。",
        "match_level": "匹配严格度：literal 为精确匹配，metaphor 为隐喻匹配，regex 为正则匹配。",
        "regex": "正则匹配模式下使用的正则表达式。",
        "exceptions": "允许放行的例外列表，使用 JSONB 保存。",
    },
    "audit_task": {
        "owner_id": "提交任务的用户标识。",
        "name": "待审核文件名称或任务描述。",
        "material_type": "待审核物料类型，如图像、视频、音频或文本。",
        "material_id": "关联的物料标识。",
        "content_hash": "待审核内容的哈希值，用于识别重复内容。",
        "status": "任务状态：pending 为待处理，running 为处理中，done 为完成，failed 为失败。",
        "verdict": "审核判定：pass 为通过，review 为人工复核，block 为拦截。",
        "report_id": "关联的审核报告标识。",
        "created_ms": "任务创建时的毫秒级时间戳。",
        "error": "任务处理失败时的错误信息。",
        "video_kind": "视频业务类型，用于区分物料视频等处理场景。",
        "project_id": "任务所属项目标识。",
        "report_generated_at": "审核报告生成时间，按文本格式保存。",
    },
    "blockword": {"word": "绝对禁词词条。"},
    "content_whitelist": {"word": "内容安全白名单词条。"},
    "material": {
        "type": "物料类型：image 为图像，meme 为表情包，video 为视频，style 为风格，corpus 为语料，music 为音乐，audio 为声音。",
        "thumb": "物料缩略图地址或缩略图数据。",
        "source_timecode": "物料在来源视频中的时间点，单位为秒。",
        "audit_status": "审核状态：processing 为处理中，pass 为通过，review 为人工复核，block 为拦截。",
        "source_job": "生成该物料的来源任务标识。",
        "oss_key": "物料文件在 OSS 中的对象键。",
        "description": "物料描述。",
        "owner_id": "物料所有者的用户标识。",
        "is_public": "是否为所有用户可见的公共物料。",
        "audit_report_id": "关联的审核报告标识。",
        "content_hash": "物料内容哈希值，用于去重。",
        "project_id": "物料所属项目标识。",
        "tags": "物料标签列表，使用 JSONB 保存。",
        "ai_summary": "人工智能生成的物料内容摘要。",
        "ai_scenarios": "人工智能识别的适用场景列表，使用 JSONB 保存。",
        "ai_emotions": "人工智能识别的情绪列表，使用 JSONB 保存。",
        "ai_atmosphere": "人工智能识别的氛围描述。",
        "reject_events": "物料退回历史，使用 JSONB 保存。",
    },
    "material_submission": {
        "team_name": "提报团队名称。",
        "delivery_time": "视频交付时间，按文本格式保存。",
        "drama_name": "短剧名称。",
        "oss_key": "提报素材在 OSS 中的对象键。",
        "video_file_name": "提报视频文件名。",
        "title_name": "素材标题名称。",
        "episode_range": "素材包含的剧集范围，按文本格式保存。",
        "revision_comment": "素材修改意见。",
        "can_upload_status": "可上传状态：1 为可上传，2 为不可上传，空值表示未判定。",
        "publish_status": "发布状态：1 为成功，2 为失败，空值表示待发布。",
        "platform_reject_reason": "平台拒绝审核的原因。",
        "platform_reject_attachments": "平台拒审原因的附件列表，使用 JSONB 保存。",
        "upload_account_name": "实际上传账号名称。",
        "upload_date": "上传日期，格式为年-月-日。",
        "designated_upload_account_name": "指定上传账号名称。",
    },
    "material_submission_permission": {
        "submission_id": "关联的素材提报记录标识。",
        "user_id": "获得数据权限的用户标识。",
        "permission_type": "数据权限类型：read 为只读，read_edit 为可阅读和编辑。",
    },
    "material_vectors": {
        "material_id": "关联的物料标识，也是本表主键。",
        "embedding": "物料内容的语义向量，用于相似度检索。",
    },
    "project": {
        "name": "项目名称，在未删除记录中唯一。",
        "created_ms": "项目创建时的毫秒级时间戳，用于排序。",
    },
    "requirement": {
        "description": "需求内容描述。",
        "urgency": "紧急程度：low 为低，medium 为中，high 为高。",
        "status": "处理状态：not_started 为未开始，in_progress 为处理中，pending_acceptance 为待验收，completed 为已完成，acceptance_failed 为验收未通过。",
        "reply": "需求处理回复或结果说明。",
        "attachments": "需求附件列表，使用 JSONB 保存。",
    },
    "role_permission": {
        "role": "角色标识。",
        "permission": "角色拥有的权限标识。",
    },
    "rule_training_example": {
        "training_set_id": "关联的规则训练集标识。",
        "material_id": "被人工标注的物料标识。",
        "expected_rule_ids": "该物料预期命中的规则标识列表，使用 JSONB 保存。",
        "source_note": "人工标注的来源或备注。",
    },
    "rule_training_set": {
        "project_id": "关联的项目标识，在未删除记录中唯一。",
        "name": "训练集名称。",
        "status": "训练状态：collecting 为收集中，training 为训练中，completed 为已完成，failed 为失败。",
        "rule_snapshot": "训练开始时的项目规则完整快照，使用 JSONB 保存。",
        "max_fp_ratio": "允许的最大多判率，即多命中数量占预期命中总数的比例。",
        "max_iterations": "允许执行的最大重审迭代次数。",
        "training_result": "训练结果摘要，包含迭代次数、最终指标和规则变更，使用 JSONB 保存。",
        "started_at": "最近一次训练开始时间，按国际标准时间格式保存。",
        "completed_at": "最近一次训练完成时间，按国际标准时间格式保存。",
    },
    "user_favorite": {
        "user_id": "收藏物料的用户标识。",
        "material_id": "被收藏的物料标识。",
    },
    "user_permission": {
        "user_id": "用户标识。",
        "permission": "用户额外拥有的权限标识。",
    },
    "video_editing_template": {
        "name": "视频剪辑模板名称，在未删除记录中不区分大小写唯一。",
        "description": "视频剪辑模板说明。",
        "reference_oss_key": "参考成片在 OSS 中的对象键，不保存签名地址或本地路径。",
        "narration_voice": "旁白音色和语音参数配置，使用 JSONB 保存。",
        "bgm_oss_key": "背景音乐在 OSS 中的对象键，为空表示不使用独立背景音乐。",
        "config": "视频剪辑模板的完整配置，使用 JSONB 保存。",
        "status": "模板状态：active 为启用，inactive 为停用。",
        "version": "模板版本号，从 1 开始递增。",
    },
}


def _load_schema(conn: psycopg.Connection) -> dict[str, list[str]]:
    rows = conn.execute(
        """
        SELECT c.table_name, c.column_name
          FROM information_schema.columns c
          JOIN information_schema.tables t
            ON t.table_schema = c.table_schema AND t.table_name = c.table_name
         WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE'
         ORDER BY c.table_name, c.ordinal_position
        """
    ).fetchall()
    schema: dict[str, list[str]] = {}
    for table_name, column_name in rows:
        schema.setdefault(table_name, []).append(column_name)
    return schema


def _validate(schema: dict[str, list[str]]) -> None:
    missing_tables = sorted(set(schema) - set(TABLE_COMMENTS))
    stale_tables = sorted(set(TABLE_COMMENTS) - set(schema))
    missing_columns = [
        f"{table}.{column}"
        for table, columns in schema.items()
        for column in columns
        if column not in COMMON_COLUMN_COMMENTS
        and column not in COLUMN_COMMENTS.get(table, {})
    ]
    if missing_tables or stale_tables or missing_columns:
        details = []
        if missing_tables:
            details.append("缺少表注释映射：" + "、".join(missing_tables))
        if stale_tables:
            details.append("映射中存在数据库没有的表：" + "、".join(stale_tables))
        if missing_columns:
            details.append("缺少字段注释映射：" + "、".join(missing_columns))
        raise RuntimeError("；".join(details))


def apply_comments(dsn: str, check_only: bool = False) -> tuple[int, int]:
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        schema = _load_schema(conn)
        _validate(schema)
        if check_only:
            return len(schema), sum(map(len, schema.values()))

        for table, columns in schema.items():
            conn.execute(
                sql.SQL("COMMENT ON TABLE {} IS {}").format(
                    sql.Identifier("public", table), sql.Literal(TABLE_COMMENTS[table])
                )
            )
            table_comments = COLUMN_COMMENTS.get(table, {})
            for column in columns:
                comment = table_comments.get(column, COMMON_COLUMN_COMMENTS.get(column))
                conn.execute(
                    sql.SQL("COMMENT ON COLUMN {}.{} IS {}").format(
                        sql.Identifier("public", table),
                        sql.Identifier(column),
                        sql.Literal(comment),
                    )
                )
        conn.commit()
        return len(schema), sum(map(len, schema.values()))


def main() -> None:
    parser = argparse.ArgumentParser(description="补齐 PostgreSQL 表和字段的中文注释")
    parser.add_argument("--check", action="store_true", help="只检查映射完整性，不修改数据库")
    args = parser.parse_args()

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    dsn = os.getenv("AM_DATABASE_URL", "")
    if not dsn or dsn.startswith("postgresql://user:pass@localhost"):
        raise RuntimeError("AM_DATABASE_URL 未配置为真实 PostgreSQL 连接串")

    table_count, column_count = apply_comments(dsn, check_only=args.check)
    action = "检查通过" if args.check else "中文注释写入完成"
    print(f"{action}：{table_count} 张表，{column_count} 个字段。")


if __name__ == "__main__":
    main()
