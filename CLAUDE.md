# CLAUDE.md — 物料管理系统工作规程

## 项目
图/表情包/视频/风格/语料管理 + 上传视频智能反解成物料 + 图片视频搜索 + 大量物料索引 + 自动审核 + 用户功能权限后台。支持 500 并发,部署阿里云 ACK。

## 技术栈(硬约定)
- 后端 **FastAPI/Python 3.11**,分层 `app/{api,service,domain,infrastructure}`。
- DB **PostgreSQL + pgvector**;缓存/队列 **Redis**;异步 **Celery**。
- 存储 **阿里云 OSS**;AI **百炼 Qwen-VL + multimodal-embedding**;审核 **阿里云内容安全**。
- 部署 **GitHub Actions → ACR → ACK**。

## 分层纪律(架构闭环②,import-linter 强制)
`api > service > domain > infrastructure`,**domain 不得 import infrastructure**。改动前后跑 `.venv/bin/lint-imports` 必须绿。架构真源在 `docs/architecture/workspace.dsl`,决策进 `docs/architecture/adr/`。

## 四层闭环工作规程(必须遵守)
1. **产品**:BMAD Analyst→PM→UX 出 PRD(EARS,带 REQ-id)→ TEA-ATDD 生成 `specs/features/*.feature`。
2. **架构**:BMAD Architect + c4-model-skill 更新 `workspace.dsl` + ADR;`c4 inspect` + `lint-imports` 守门。
3. **代码**:BMAD SM 切 story → Dev **先写失败测试再实现**(tdd-guard)→ `/code-review` + `/security-review`。
4. **验证**:CI 跑 `lint-imports`+`behave`+`pytest`+Playwright+k6;**BMAD QA 之外必须 Playwright 真机验证**(防"标记完成却跑不通")。任一红按失败路由回对应闭环。
   - **前端改动必跑 UI QC**:`tests/ui_qc.mjs`(webapp-testing 技能)——真在浏览器点一遍关键流程(登录/收藏→我的库/管理员发布/审核)。**API 测试 + 静态截图不算数**,必须真点击。

## 追溯脊柱(REQ-id)
一个 REQ-id 贯穿 PRD → behave(`@REQ-xxx`)→ C4 元素 → story → 测试 → CI。任何测试红,顺 REQ-id 回溯到精确需求/决策。

## 设计系统(前端,frontend-design + shadcn/Magic)
- 字体:标题 Fraunces,正文 Inter;**禁用**紫色渐变、通用 grid 卡片等默认套路。
- 栅格:8px 节奏;单一强调色;暗色/亮色双主题。
- 组件优先 shadcn/ui,动效用 Magic UI;每屏改完用 Playwright 截图自检。

## 本地 harness 配置(已装 · 闭环地图)
**已装**:BMAD(BMM+TEA,56 skills)· 技能 frontend-design/theme-factory/c4-model-skill/software-design-skills/webapp-testing · MCP context7/github/playwright/db/shadcn · 传感器 lint-imports/pytest/behave/playwright(.venv)。

**闭环怎么自动转起来(核心)**:
- **验证自动化 = Stop hook**:`.claude/settings.json` 里配了 Stop hook,每次我收工自动跑 `make verify`(架构②+代码③+产品①三传感器),**红了自动唤醒我按失败路由修**——这才是"闭环",不靠手动。
- **前端改动 = PostToolUse 提醒**:改 `frontend/**` 会提示跑 `make ui-qc`。

**四个设计闭环 → 对应命令/技能**(`make design` 可打印):
| 闭环 | 产出(技能)| 验证(传感器/命令)|
|---|---|---|
| ①产品设计 | bmad-prd(EARS)→ bmad-testarch-atdd | `make bdd`(behave) |
| 界面设计 | frontend-design + shadcn MCP | **`make ui-qc`(真点击,别只截图)** |
| ②架构设计 | bmad-architecture + c4-model-skill(workspace.dsl+ADR)| `make arch`(lint-imports)|
| ③代码开发 | bmad-dev-story(先测后码)+ /code-review | `make test`(pytest)|
| ④验证驱动 | TEA-nfr/trace/release-gate | **`make verify`(Stop hook 自动跑)** |

**铁律**:功能对不对**必须真点击验证**(`make ui-qc`),API 绿灯 + 静态截图不算数。

## 协作与成本
- **每个 BMAD 工作流开新 chat**(防上下文污染);规划期可用 Web Bundles 省 token。
- 小改走 Quick Flow,不上全套敏捷仪式。

## 本地启动

```powershell
# 前提: PostgreSQL(127.0.0.1:5432, 库 assets + pgvector) + Redis 已运行
.venv\Scripts\python app\main.py    # → http://localhost:8099
```

**Windows 注意**: 虚拟环境路径是 `.venv\Scripts\`（非 `bin/`），且所有 Python 命令需设 `PYTHONUTF8=1`（防 GBK 解码错误）。`scripts/verify.sh` 已适配跨平台路径。

**PyCharm 注意**: `config.py` 的 `env_file=".env"` 相对 CWD 解析。PyCharm 需在 Run Configuration 里把 **Working directory** 设为项目根目录 `D:\project\assetsmangment`，否则 `.env` 找不到 → 走内存/JSON 假实现而非 PG。

### 常用命令（Windows）

| 命令 | 用途 |
|---|---|
| `.venv\Scripts\python app\main.py` | 启动开发服务器 (localhost:8099, hot-reload) |
| `bash scripts/verify.sh` | 三层闭环验证（架构+代码+产品） |
| `.venv\Scripts\lint-imports` | 架构契约检查 |
| `.venv\Scripts\pytest -q` | 单元/集成测试（271 tests） |
| `.venv\Scripts\pytest tests/test_pg_repos.py -q` | PG 仓储集成测试（35 tests，需 `AM_DATABASE_URL`） |
| `.venv\Scripts\python scripts/migrate_all_to_pg.py` | state.json → PG 全量数据迁移（幂等） |
| `.venv\Scripts\behave specs/features` | BDD 验收测试 |
| `make serve` | Linux/Mac 启动（Windows 用上面那条 `python app\main.py`） |

### 三层验证状态

| 传感器 | 命令 |
|---|---|
| ② 架构 | `lint-imports`（3 contracts） |
| ③ 代码 | `pytest`（271 tests） |
| ① 产品 | `behave`（7 features / 17 scenarios） |

**测试注意**: conftest 已 monkeypatch 全部仓储为内存实现（含 user/favorites/rbac/audit_log），不再依赖 state.json，消除了测试间状态污染和 409 问题。PG 仓储集成测试在 `tests/test_pg_repos.py`（35 tests），用一次性表隔离，需要 `AM_DATABASE_URL` 指向真实 PG。

## 部署（单实例 Docker Compose）

服务器目录结构：
```
${ASSETS_ROOT}  (默认 /software/project/python/assets)
├── restart.sh                  ← 手动上传一次
├── docker-compose.yml          ← 手动上传一次
├── .env                        ← 手动创建一次（按 .env-test 填真实密钥）
├── assetsmangment.zip          ← 每次部署上传
├── assetsmangment/             ← 脚本自动解压（Dockerfile + app/ + frontend/）
├── data/                       ← state.json 持久化
├── logs/assetsmangment/        ← 日志
└── backups/                    ← 旧版备份
```

**首次初始化**: `scp deploy/restart.sh deploy/docker-compose.yml` 到服务器主目录，创建 `.env`，`chmod +x restart.sh`。

**每次部署**: 本地 `deploy\pack.cmd` 打包 → `scp assetsmangment.zip` → 服务器 `sh restart.sh`。脚本自动：停服 → 备份 → 解压 → `docker compose build` → `docker compose up -d`。

**注意**: PG/Redis 在 compose 外（宿主机或其他容器），应用用 bridge 网络 + 端口映射 `8088:8000`。阿里云 SDK 依赖钉版本：`alibabacloud-green20220302==3.2.4` + `alibabacloud_tea_openapi>=0.4.5`，升版需对齐。

### PG 真源迁移（✅ 已全部完成，2026-07-17）

| 模块 | 表 | 仓储 | 状态 |
|---|---|---|---|
| 审核规则 | `audit_rule` | `PgAuditRuleRepo` | ✅ 已迁移 |
| 作品项目 | `project` | `PgProjectRepo` | ✅ 已迁移 |
| 物料 | `material` | `PgMaterialRepo` | ✅ 已迁移 |
| 用户 | `app_user` | `PgUserRepo` | ✅ 已迁移 |
| 收藏 | `user_favorite` | `PgFavoriteRepo` | ✅ 已迁移 |
| RBAC | `role_permission` + `user_permission` | `PgRbacRepo` | ✅ 已迁移 |
| 审核任务 | `audit_task` | `PgAuditTaskRepo` | ✅ 已迁移 |
| 审核报告 | `audit_report` | `PgAuditReportRepo` | ✅ 已迁移 |
| 白名单 | `content_whitelist` | `PgWhitelistRepo` | ✅ 已迁移 |
| 禁词 | `blockword` | `PgBlockwordRepo` | ✅ 已迁移 |
| 审计日志 | `audit_log` | `PgAuditLog` | ✅ 已迁移 |
| 向量索引 | `material_vectors` | `PgVectorIndex` | ✅ 已迁移(含基础字段) |

**切换逻辑**：配置 `AM_DATABASE_URL` 后，所有仓储 fail-fast 切换到 PG 真源（不静默回退 JSON）。未配置则走 JSON/内存（`AM_DATA_DIR` 有无决定持久化或纯内存）。

**数据迁移**：`scripts/migrate_all_to_pg.py` 从 `state.json` 全量迁入 PG（幂等可重复跑）。

全项目 PG 表遵循统一基础字段规范：`id`（雪花 BIGINT 主键）、`del_flag`（0=在用，软删置新雪花 ID）、`create_by`/`create_time`/`update_by`/`update_time`。domain 层的 `id` 均为 `str`（雪花 int64 序列化，防 JS 2^53 精度丢失）。

### Task Janitor（定时任务补偿，`app/service/task_janitor.py`）

审核任务异步跑在 `ThreadPoolExecutor` 里。服务重启或外部 API 挂死 → 任务永远卡在 `PENDING/RUNNING`，物料永远 `PROCESSING`。JSON 存储仅在加载时内存修复，PG 存储无启动恢复。

**两层补偿**（FastAPI `lifespan` 启动，`app/main.py`）：
1. **启动恢复（同步）**：`task_janitor.start()` → 扫全部 `PENDING/RUNNING` 任务。物料已审核完成（`audit_status != processing`）→ 任务标 `DONE` 并同步裁定（`_persist` 成功但 `_finish_task` 写失败的保护）；物料仍 `processing` → 任务标 `FAILED` + 清理物料。
2. **运行时常驻扫描（daemon 线程）**：每 `AM_JANITOR_SCAN_INTERVAL_S`（默认 300s）扫一次，把 `created_ms` 超过 `AM_JANITOR_STUCK_TIMEOUT_S`（默认 1800s）的 `PENDING/RUNNING` 任务按同上逻辑修复。fail 前 re-read 防竞态（audit pool 刚好完成的跳过）。

**约定**：只依赖 domain ports（`AuditTaskRepo`/`MaterialRepo`/`ObjectStorage`），零 FastAPI 耦合。每层 try/except 隔离——单个失败不中断扫描。

## 审核规则系统

### 规则来源类型（`source_type`）

| 类型 | 含义 | 审核内容 |
|---|---|---|
| `transcript` | 语音转写 | 阿里云 ASR 转写口播文字 |
| `video_frame` | 视频关键帧画面 | OSS 截图 → Qwen-VL 反解成中文描述 |
| `image_content` | 图像反解画面 | Qwen-VL 直接描述图片内容 |
| `original_text` | 上传原文 | 用户上传的语料/文案 |
| `any` | 不限 | 以上全部生效 |

**支持逗号多选**：`video_frame,image_content` 表示同时对视频帧和静态图片生效。向后兼容单值（`transcript` = 只对口播）。`applies_to()` 是唯一真源，三个仓储的 `list_for()` 都委托给它。

### 匹配严格程度（`match_level`）

| 级别 | 含义 | 大模型行为 |
|---|---|---|
| `literal` | 字面判定 | 只看表面意思，宁可漏不可误伤。不联想、不引申、不结合语境推测 |
| `metaphor` | 隐喻判定 | 影射/暗示/谐音/代称也要揪出（仅用于政治/国家标志等严重项） |
| `regex` | 正则精确 | 不走大模型，纯正则命中——审核时零 LLM 调用 |

**选择原则**：事实性检查（免责声明有无、二维码有无）用 `literal`；语义判断（是否网赚话术、是否拉踩）用 `metaphor`；关键词精确匹配用 `regex`。

### 大模型假阳性过滤（`_reason_says_pass`）

大模型有时会在 `findings` 里输出"不违规/符合要求"的条目——不是它判错了，是它**多嘴汇报**。两层防御：

1. **Prompt 收紧**：`_RULE_JUDGE_SYS` 明确禁止输出"不违规"条目
2. **代码兜底**：`_semantic_judge` 里 `_reason_says_pass()` 检查 reason 是否含"不违规/不应命中/不符合/未违反"等否定 token，命中直接丢弃

### 报告 Segment 命中标注

`_report_out()` 为每个 segment 附加 `triggered_rules` 字段（按 `begin_ms` 时间范围匹配），前端直接渲染"命中: #N 规则描述"而非笼统的"· 命中"。

### 规则调优经验

- 用 **guidance** 告诉大模型"什么不算违规"比改 condition 更有效——condition 定义边界，guidance 消歧义
- 视觉规则（二维码/低俗/明星/服装/美颜）统一用 `video_frame,image_content`，不区分视频帧和图片
- `block` → `review` 是安全降级：机器不直接拦截，交人工终判
- 每次改规则后跑 `recheck` 验证实际效果，大模型会找到你意想不到的角度
