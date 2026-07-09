# ADR-0001:主库与向量检索用 PostgreSQL + pgvector

- 状态:已接受
- 日期:2026-07-09
- 关联需求:物料索引、图片/视频语义搜索

## 决策
关系数据(用户/权限/物料元信息 JSONB)与向量检索统一用 **RDS PostgreSQL + pgvector** 一套系统。

## 理由
- 500 并发、千万级向量,pgvector/pgvectorscale 完全够用,运维最简。
- 关系 + 向量一体,免维护第二套存储与数据同步。

## 后果
- 未来物料破亿 → 向量层平滑迁 Milvus/Zilliz(见 [[0003-ack-gitops-deploy]] 的扩展性)。
- 需装 pgvector 扩展;向量列建 HNSW 索引。
