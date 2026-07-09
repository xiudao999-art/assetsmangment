# ADR-0007:向量索引 HNSW 与规模化

- 状态:已接受 · 日期:2026-07-09 · 关联:REQ-401/402

## 决策
`material_vector.embedding vector(1024)` 建 **pgvector HNSW 索引**;搜索 = 向量近邻 + 元数据过滤(含 `audit_status='pass'`)+ 可选 hybrid(BM25)。千万级下目标 P95 ≤ 200ms。

## 理由
pgvector+HNSW 在千万级、500 并发够用,且与关系数据同库,免二套存储。

## 后果
- 调参 m/ef_construction/ef_search;必要时分区。
- 物料破亿或延迟不达标 → 向量层平滑迁 **Milvus/Zilliz**(应用只换 infra 客户端)。
