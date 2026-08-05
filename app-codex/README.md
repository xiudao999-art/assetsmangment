# app-codex

独立的 Codex SDK 执行微服务，提供：

- `POST /codex/sessions`：创建 Codex thread 并执行首轮问题。
- Redis 全局 FIFO 队列，默认最多同时运行 2 个 SDK 调用。
- 每次 SDK 调用结束后随机冷却 2–5 秒再释放名额。
- `GET /health`：存活检查。

## 本地启动

在 `app-codex` 目录安装依赖并启动：

```powershell
python -m pip install -e ".[dev]"
$env:CODEX_PROJECT_ROOT="D:\project"
$env:CODEX_REDIS_URL="redis://localhost:6379/0"
$env:CODEX_TOKEN_SECRET="与主服务的 AM_TOKEN_SECRET 相同"
uvicorn app_codex.main:app --host 0.0.0.0 --port 8010
```

请求格式保持不变：

```http
POST /codex/sessions
Authorization: Bearer <主服务登录接口返回的 token>
Content-Type: application/json

{"question":"检查项目代码","project":"assetsmangment"}
```

`project` 可以是 `CODEX_PROJECT_ROOT` 下的相对路径或绝对路径，超出根目录会返回 400。

## Docker

容器必须能访问 Redis，并把待操作的项目目录挂载到 `CODEX_PROJECT_ROOT`：

```powershell
docker build -t app-codex .
docker run --rm -p 8010:8010 `
  -e CODEX_PROJECT_ROOT=/projects `
  -e CODEX_REDIS_URL=redis://host.docker.internal:6379/0 `
  -e CODEX_TOKEN_SECRET=your-shared-secret `
  -v D:\project:/projects `
  app-codex
```

