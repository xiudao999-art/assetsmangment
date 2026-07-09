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

-- 向量索引(F3/F4)—— HNSW,余弦相似度(REQ-401)
CREATE TABLE material_vector (
    material_id UUID PRIMARY KEY REFERENCES material(id) ON DELETE CASCADE,
    embedding   vector(1024)
);
CREATE INDEX idx_material_vector_hnsw ON material_vector
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

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
