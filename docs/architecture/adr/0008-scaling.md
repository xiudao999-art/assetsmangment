# ADR-0008:500 并发扩缩容策略

- 状态:已接受 · 日期:2026-07-09 · 关联:NFR-1

## 决策
- **API**:无状态多副本,ACK **HPA** 按 CPU/QPS 扩缩。
- **Worker**:Celery 反解重活单独队列、独立 Deployment/HPA。
- **PG**:pgbouncer 连接池,读多可加只读副本。
- **Redis**:缓存热点查询、签名 URL、限流计数。
- **OSS**:前端**直传签名 URL**,大文件不经 API。
- **API 层**:限流 + 强制分页。

## 理由
把重活(反解)与在线请求解耦,各自水平扩展,才能在 500 并发稳住 P95。

## 验证
`k6` 500 VU 压测 + `bmad-testarch-nfr` 出报告作为发布 gate。
