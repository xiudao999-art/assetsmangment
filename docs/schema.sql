-- 物料管理系统 DB Schema(RDS PostgreSQL + pgvector)
-- 对应 domain 模型;Phase 2 用 Alembic 迁移执行。

CREATE EXTENSION IF NOT EXISTS vector;

-- 物料(F1/F2)
CREATE TABLE material (
    id              UUID PRIMARY KEY,
    type            TEXT NOT NULL,          -- image/meme/video/style/corpus
    oss_key         TEXT NOT NULL,
    thumb           TEXT,
    source_timecode DOUBLE PRECISION DEFAULT 0,
    owner_id        TEXT,
    audit_status    TEXT NOT NULL DEFAULT 'review',  -- pass/review/block(默认不放行)
    description     TEXT DEFAULT '',
    tags            TEXT[] DEFAULT '{}',
    source_job      UUID,
    created_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_material_audit ON material(audit_status);   -- 搜索按 pass 过滤(REQ-303)

-- 向量索引(F3/F4)—— HNSW,余弦相似度(REQ-401)。material_id 存雪花ID字符串,与 material.id 对应。
-- 遵循全项目 PG 业务表基础字段规范(由 PgVectorIndex 启动时自建)。
CREATE TABLE IF NOT EXISTS material_vectors (
    material_id TEXT PRIMARY KEY,
    embedding   vector(1024),
    del_flag    BIGINT NOT NULL DEFAULT 0,
    create_by   TEXT NOT NULL DEFAULT '',
    create_time TIMESTAMPTZ NOT NULL DEFAULT now(),
    update_by   TEXT NOT NULL DEFAULT '',
    update_time TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_material_vectors_hnsw ON material_vectors
    USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_mv_live ON material_vectors (del_flag) WHERE del_flag = 0;

-- 视频反解任务(F2)
CREATE TABLE video_job (
    id         UUID PRIMARY KEY,
    oss_key    TEXT NOT NULL,
    size_bytes BIGINT,
    status     TEXT NOT NULL DEFAULT 'pending',  -- pending/running/done/failed
    retry      INT DEFAULT 0,
    owner_id   TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- 审核结果(F6)
CREATE TABLE audit_result (
    id          BIGSERIAL PRIMARY KEY,
    material_id UUID REFERENCES material(id) ON DELETE CASCADE,
    scene       TEXT,
    suggestion  TEXT,                 -- pass/review/block
    detail      JSONB,
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- 用户 + RBAC(F7/F8)
CREATE TABLE app_user (
    id       UUID PRIMARY KEY,
    name     TEXT UNIQUE NOT NULL,
    pwd_hash TEXT NOT NULL,           -- 加盐哈希,禁明文(REQ-602)
    role     TEXT DEFAULT 'viewer',
    status   TEXT DEFAULT 'active'
);
CREATE TABLE role            (id TEXT PRIMARY KEY);
CREATE TABLE permission      (id TEXT PRIMARY KEY);
CREATE TABLE role_permission (role_id TEXT, permission_id TEXT, PRIMARY KEY (role_id, permission_id));

-- ══════════════════════════════════════════════════════════════════════════
-- 全项目 PG 业务表基础字段规范(2026-07 起,后续新表照抄):
--   id          BIGINT PRIMARY KEY      雪花算法 ID(app/infrastructure/snowflake.py;
--                                       API 序列化为字符串,防 JS 2^53 精度丢失)
--   del_flag    BIGINT NOT NULL DEFAULT 0   软删:0=在用;删除时置为**新雪花 ID**
--                                       (唯一索引带上 del_flag → 软删行不占唯一位,编号/名称可复用)
--   create_by   TEXT                    创建人(domain 的 created_by 映射到此列,不建冗余列)
--   create_time TIMESTAMPTZ DEFAULT now()
--   update_by   TEXT                    最后操作人
--   update_time TIMESTAMPTZ DEFAULT now()
-- 更新只动业务列 + update_by/update_time;create_* 与 del_flag 不随 upsert 改变。
-- ══════════════════════════════════════════════════════════════════════════

-- 审核规则(F6,真源;由 PgAuditRuleRepo 启动时 CREATE IF NOT EXISTS 自建)
-- 列序:业务列在前,基础字段(del_flag/create_by/create_time/update_by/update_time)在末尾。
CREATE TABLE IF NOT EXISTS audit_rule (
    id          BIGINT PRIMARY KEY,                    -- 雪花ID,API 序列化为字符串
    no          INT NOT NULL DEFAULT 0,                -- 规则编号,在用行内唯一、稳定、从 1 递增
    source_type TEXT NOT NULL DEFAULT 'any',           -- TextSourceType 或 any
    keywords    JSONB NOT NULL DEFAULT '[]'::jsonb,    -- 关键词快筛,命中任一即触发
    condition   TEXT NOT NULL DEFAULT '',              -- 自然语言条件,交大模型判定
    action      TEXT NOT NULL DEFAULT 'block',         -- block=硬拦 | review=转人工
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,          -- 是否启用,停用规则不参与审核
    project_id  TEXT NOT NULL DEFAULT '',              -- 空=全局规则;非空=仅该项目作品生效
    guidance    TEXT NOT NULL DEFAULT '',              -- 审核尺度说明,含好例/坏例,辅助大模型
    match_level TEXT NOT NULL DEFAULT 'metaphor',      -- literal=精确 | metaphor=隐喻 | regex=正则
    regex       TEXT NOT NULL DEFAULT '',              -- match_level=regex 时的正则表达式
    exceptions  JSONB NOT NULL DEFAULT '[]'::jsonb,    -- 可放行例外 [{text,note,by,ms}]
    -- ↓ 全项目 PG 业务表基础字段(统一放末尾)↓
    del_flag    BIGINT NOT NULL DEFAULT 0,             -- 0=在用;删除时置新雪花ID。软删行不占 no 唯一位
    create_by   TEXT NOT NULL DEFAULT '',              -- 创建人,映射 domain.created_by;upsert 不动此列
    create_time TIMESTAMPTZ NOT NULL DEFAULT now(),    -- 创建时间,upsert 不更新此列
    update_by   TEXT NOT NULL DEFAULT '',              -- 最后操作人,每次 upsert/delete 更新
    update_time TIMESTAMPTZ NOT NULL DEFAULT now()     -- 最后操作时间,每次 upsert/delete 更新
);
COMMENT ON TABLE audit_rule IS '审核规则。每条规则按 source_type + project_id 匹配物料，命中后执行 action(block/review)。';
COMMENT ON COLUMN audit_rule.id IS '雪花算法 BIGINT 主键，API 序列化为字符串';
COMMENT ON COLUMN audit_rule.no IS '规则编号，在用行内唯一、稳定、从 1 递增';
COMMENT ON COLUMN audit_rule.source_type IS '匹配的文本来源类型(TextSourceType)，any=匹配所有';
COMMENT ON COLUMN audit_rule.keywords IS '关键词快筛列表，命中任一即触发规则';
COMMENT ON COLUMN audit_rule.condition IS '自然语言条件描述，提交大模型判定';
COMMENT ON COLUMN audit_rule.action IS '命中后动作:block=硬拦, review=转人工';
COMMENT ON COLUMN audit_rule.enabled IS '是否启用，停用规则不参与审核';
COMMENT ON COLUMN audit_rule.project_id IS '所属项目，空=全局规则，非空=仅该项目作品生效';
COMMENT ON COLUMN audit_rule.guidance IS '审核尺度说明，含好例/坏例，辅助大模型判定';
COMMENT ON COLUMN audit_rule.match_level IS '匹配严格度:literal=精确, metaphor=隐喻, regex=正则';
COMMENT ON COLUMN audit_rule.regex IS 'match_level=regex 时的正则表达式';
COMMENT ON COLUMN audit_rule.exceptions IS '可放行例外列表，每项含 text/note/by/ms';
COMMENT ON COLUMN audit_rule.del_flag IS '软删标记:0=在用，删除时置为新雪花ID。软删行不占 no 唯一位';
COMMENT ON COLUMN audit_rule.create_by IS '创建人，映射 domain.created_by';
COMMENT ON COLUMN audit_rule.create_time IS '创建时间，upsert 不更新此列';
COMMENT ON COLUMN audit_rule.update_by IS '最后操作人，每次 upsert/delete 更新';
COMMENT ON COLUMN audit_rule.update_time IS '最后操作时间，每次 upsert/delete 更新';
CREATE UNIQUE INDEX IF NOT EXISTS uq_audit_rule_no ON audit_rule (no, del_flag);
CREATE INDEX IF NOT EXISTS idx_audit_rule_live ON audit_rule (del_flag) WHERE del_flag = 0;

-- 作品项目(由 PgProjectRepo 启动时 CREATE IF NOT EXISTS 自建)
CREATE TABLE IF NOT EXISTS project (
    id          BIGINT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_ms  BIGINT NOT NULL DEFAULT 0,
    del_flag    BIGINT NOT NULL DEFAULT 0,
    create_by   TEXT NOT NULL DEFAULT '',
    create_time TIMESTAMPTZ NOT NULL DEFAULT now(),
    update_by   TEXT NOT NULL DEFAULT '',
    update_time TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_project_name ON project (name, del_flag);
CREATE INDEX IF NOT EXISTS idx_project_live ON project (del_flag) WHERE del_flag = 0;

-- 物料(主表;embedding 向量由 material_vectors(pgvector)独立管理)
CREATE TABLE IF NOT EXISTS material (
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
);
CREATE INDEX IF NOT EXISTS idx_material_owner ON material (owner_id, del_flag);
CREATE INDEX IF NOT EXISTS idx_material_status ON material (audit_status, del_flag);
CREATE INDEX IF NOT EXISTS idx_material_hash ON material (owner_id, content_hash, del_flag);
CREATE INDEX IF NOT EXISTS idx_material_live ON material (del_flag) WHERE del_flag = 0;

-- 用户(由 PgUserRepo 启动时自建;domain_id=领域 id,name=登录名)
CREATE TABLE IF NOT EXISTS app_user (
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
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_app_user_domain_id ON app_user (domain_id, del_flag);
CREATE UNIQUE INDEX IF NOT EXISTS uq_app_user_name ON app_user (name, del_flag);
CREATE INDEX IF NOT EXISTS idx_app_user_live ON app_user (del_flag) WHERE del_flag = 0;

-- 用户收藏(多对多;由 PgFavoriteRepo 启动时自建)
CREATE TABLE IF NOT EXISTS user_favorite (
    id          BIGINT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    material_id TEXT NOT NULL,
    del_flag    BIGINT NOT NULL DEFAULT 0,
    create_by   TEXT NOT NULL DEFAULT '',
    create_time TIMESTAMPTZ NOT NULL DEFAULT now(),
    update_by   TEXT NOT NULL DEFAULT '',
    update_time TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_user_favorite_pair ON user_favorite (user_id, material_id, del_flag);
CREATE INDEX IF NOT EXISTS idx_user_favorite_live ON user_favorite (del_flag) WHERE del_flag = 0;

-- 角色权限(由 PgRbacRepo 启动时自建)
CREATE TABLE IF NOT EXISTS role_permission (
    id          BIGINT PRIMARY KEY,
    role        TEXT NOT NULL,
    permission  TEXT NOT NULL,
    del_flag    BIGINT NOT NULL DEFAULT 0,
    create_by   TEXT NOT NULL DEFAULT '',
    create_time TIMESTAMPTZ NOT NULL DEFAULT now(),
    update_by   TEXT NOT NULL DEFAULT '',
    update_time TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_role_permission_pair ON role_permission (role, permission, del_flag);
CREATE INDEX IF NOT EXISTS idx_role_permission_live ON role_permission (del_flag) WHERE del_flag = 0;

-- 用户级权限(叠加在角色默认权限之上;由 PgRbacRepo 启动时自建)
CREATE TABLE IF NOT EXISTS user_permission (
    id          BIGINT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    permission  TEXT NOT NULL,
    del_flag    BIGINT NOT NULL DEFAULT 0,
    create_by   TEXT NOT NULL DEFAULT '',
    create_time TIMESTAMPTZ NOT NULL DEFAULT now(),
    update_by   TEXT NOT NULL DEFAULT '',
    update_time TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_user_permission_pair ON user_permission (user_id, permission, del_flag);
CREATE INDEX IF NOT EXISTS idx_user_permission_live ON user_permission (del_flag) WHERE del_flag = 0;

-- 待审核任务(由 PgAuditTaskRepo 启动时自建)
CREATE TABLE IF NOT EXISTS audit_task (
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
);
CREATE INDEX IF NOT EXISTS idx_audit_task_owner ON audit_task (owner_id, del_flag);
CREATE INDEX IF NOT EXISTS idx_audit_task_live ON audit_task (del_flag) WHERE del_flag = 0;

-- 审核报告(由 PgAuditReportRepo 启动时自建;report_id 为 domain UUID,非雪花)
CREATE TABLE IF NOT EXISTS audit_report (
    report_id   TEXT PRIMARY KEY,
    verdict     TEXT NOT NULL DEFAULT 'processing',
    summary     TEXT NOT NULL DEFAULT '',
    segments    JSONB NOT NULL DEFAULT '[]'::jsonb,
    triggered   JSONB NOT NULL DEFAULT '[]'::jsonb,
    create_time TIMESTAMPTZ NOT NULL DEFAULT now(),
    update_time TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 内容安全白名单(由 PgWhitelistRepo 启动时自建)
CREATE TABLE IF NOT EXISTS content_whitelist (
    id          BIGINT PRIMARY KEY,
    word        TEXT NOT NULL,
    del_flag    BIGINT NOT NULL DEFAULT 0,
    create_by   TEXT NOT NULL DEFAULT '',
    create_time TIMESTAMPTZ NOT NULL DEFAULT now(),
    update_by   TEXT NOT NULL DEFAULT '',
    update_time TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_content_whitelist_word ON content_whitelist (word, del_flag);
CREATE INDEX IF NOT EXISTS idx_content_whitelist_live ON content_whitelist (del_flag) WHERE del_flag = 0;

-- 绝对禁词(由 PgBlockwordRepo 启动时自建)
CREATE TABLE IF NOT EXISTS blockword (
    id          BIGINT PRIMARY KEY,
    word        TEXT NOT NULL,
    del_flag    BIGINT NOT NULL DEFAULT 0,
    create_by   TEXT NOT NULL DEFAULT '',
    create_time TIMESTAMPTZ NOT NULL DEFAULT now(),
    update_by   TEXT NOT NULL DEFAULT '',
    update_time TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_blockword_word ON blockword (word, del_flag);
CREATE INDEX IF NOT EXISTS idx_blockword_live ON blockword (del_flag) WHERE del_flag = 0;

-- 审计日志(由 PgAuditLog 启动时自建;只追加不删不改)
CREATE TABLE IF NOT EXISTS audit_log (
    id          BIGINT PRIMARY KEY,
    event       TEXT NOT NULL,
    create_time TIMESTAMPTZ NOT NULL DEFAULT now()
);
