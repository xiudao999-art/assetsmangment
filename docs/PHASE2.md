# Phase 2 接线清单(接真阿里云)

> 业务逻辑已完成且全绿(端口注入)。接真环境 = **只改组合根 `app/api/deps.py` + 填环境变量**,`app/service` 与 `app/domain` 一行不改。

## 1. 环境变量(写入 `.env`,前缀 AM_)
```
AM_USE_FAKES=false
AM_OSS_ENDPOINT=oss-cn-hangzhou.aliyuncs.com
AM_OSS_BUCKET=your-bucket
AM_OSS_ACCESS_KEY_ID=...
AM_OSS_ACCESS_KEY_SECRET=...
AM_DASHSCOPE_API_KEY=sk-...
AM_CONTENT_SAFETY_ACCESS_KEY_ID=...
AM_CONTENT_SAFETY_ACCESS_KEY_SECRET=...
AM_DATABASE_URL=postgresql://user:pass@rds-host:5432/assets
```

## 2. 要替换的 4 个假实现 → 真适配器(实现同一批 domain 端口)
| 端口 | 假实现(现) | 真适配器(Phase 2) | 状态 |
|---|---|---|---|
| `ObjectStorage` | FakeStorage | **`aliyun_oss.OssStorage`**(oss2) | ✅ 已写 |
| `VideoParser`/`Embedder`/`QueryEmbedder` | FakeVideoParser/… | `dashscope_ai.*`(DashScope Qwen-VL + multimodal-embedding) | ⬜ 待填 SDK 调用 |
| `Auditor` | FakePassAuditor | `content_safety.ContentSafetyAuditor`(内容安全) | ⬜ 待填 |
| `MaterialRepo`/`VectorIndex` | InMemory* | `pg.PgMaterialRepo`(psycopg + pgvector) | ⬜ 待填 |
| `UserRepo`/`RbacRepo` | InMemory* | `pg.PgUserRepo`/`PgRbac` | ⬜ 待填 |

## 3. 组合根切换(`app/api/deps.py`)
```python
from app.config import settings
if settings.use_fakes:
    storage = FakeStorage()
else:
    from app.infrastructure.aliyun_oss import OssStorage
    storage = OssStorage()
# 其余端口同理
```

## 4. 建库
```
psql "$AM_DATABASE_URL" -f docs/schema.sql     # 或用 Alembic 迁移
```

## 5. 验证顺序(全绿即可交付)
1. `.venv/bin/lint-imports` 🟢  2. `.venv/bin/pytest -q` 🟢  3. `.venv/bin/behave specs/features` 🟢
4. 起服务 + `k6 run scripts/load_test.js`(NFR-1:P95≤10s)
5. CI `.github/workflows/ci.yml` 全绿 → build → ACR → ACK
6. `verify` + Playwright 生产验收 → REQ 关闭

## 6. 异步化(生产)
`app/api/router.py` 的 `run_job` 现为同步演示;生产改为投递 **Celery** 任务(见 ADR-0004),API 只返回 jobId,Worker 独立扩缩(ADR-0008)。
