# ADR-0003:部署用 GitHub Actions → ACR → ACK

- 状态:已接受
- 日期:2026-07-09
- 关联需求:可扩展、500 并发

## 决策
CI/CD:**GitHub Actions**(`aliyun/acr-login@v1`)build 镜像 → 推 **ACR** → 部署 **ACK(K8s)**;镜像扫描用云安全中心 `sas-image-scanner`。

## 理由
- K8s 天然满足可扩展性 + 500 并发(HPA 自动扩缩容;DB 用 StatefulSet)。
- 与 OSS/RDS/百炼同处阿里云,内网低延迟。

## 后果
- API 无状态多副本;Celery Worker 独立扩缩;Redis/PG 用托管实例。
- 发布前过 TEA Release-Gate;回滚用 `git revert` 触发流水线。
