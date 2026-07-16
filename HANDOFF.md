# 会话交接（HANDOFF）— 物料管理系统

> 目的：让**新会话立刻接上**当前进度。读完这份 + `CLAUDE.md` + 记忆文件，就能知道"做过什么、现在什么状态、怎么继续、怎么验证、怎么部署、有哪些红线"。
> 最近更新：2026-07-11。作者：上一段 Claude Code 会话。

---

## 0. 30 秒速览（当前状态）

- 项目：多模态**物料管理系统**（图/表情/视频/风格/语料 + 视频反解 + 语义搜索 + 自动审核 + RBAC 后台）。FastAPI 分层架构。
- 线上：**共享生产 ECS `http://8.149.247.100:8088`**，容器 `assets-api`，**已部署最新代码**（所有改动都已上线并真机验证）。
- 传感器全绿：`lint-imports` 3/3 · `pytest` **182 passed** · `behave` 17 · `make ui-qc` 6/6。
- ⚠️ 2026-07-13 曾因热更 jsonstore 触发线上 state.json 清库,已从备份完整恢复并硬化;**动 state 结构/jsonstore 前务必先 test-load 备份**(见 `ecs-deploy-and-persistence.md`)。
- 最新已上线（详见记忆）：**作品项目维度**（`work-project-dimension.md`）+ **粘贴卡审文案自动拆成规则**（`rule-doc-parsing.md`）+ **审核三波级联 + 裁定模型**（`audit-three-stage-cascade.md`,判定=绝对禁词→阿里云→语义整篇判、取消关键词硬匹配;**机器只出 pass/review 永不 block、新增 processing「审核中」态、block 只由人工拒绝**;线上真模型验过)。
- ⚠️ **工作区未提交 git**：本会话改动都在工作树里（`git status` 一堆 M/??），**没 commit**。最新代码 = 工作树 + 线上容器，不是 git HEAD。
- 真源文档：本文件只是索引，**权威细节在** `CLAUDE.md`（工作规程）+ 用户记忆 `~/.claude/projects/-Users-sunyuexuan-Documents-assets-management/memory/`（下面列了）。

---

## 1. 必读的持久记忆（真源，改动前先读）

路径 `~/.claude/projects/-Users-sunyuexuan-Documents-assets-management/memory/`，`MEMORY.md` 是索引：

| 文件 | 内容（为什么重要） |
|---|---|
| **ecs-deploy-and-persistence.md** | 🔴 **最重要**。线上部署机制、持久化、内容安全、本会话所有功能的落点、Dockerfile 坑、真云能力。**改动/部署前必读。** |
| test-all-before-done.md | 收工前必须真点击跑通全部关键流程（API 绿 + 静态截图不算数）。 |
| materials-listing-pagination.md | 库/搜索是服务端翻页（`MaterialQuery` 契约）。 |
| audit-decoupled-upload-flow.md | 提交即受理、异步审核、待审核页、MD5 去重、`AuditTask`。 |
| rbac-accounts-and-search.md | 按用户授权、账号 CRUD、搜索关键词为主+向量阈值 0.35。 |

---

## 2. 架构与纪律（不能破）

- 分层 `app/{api,service,domain,infrastructure}`，依赖方向 **api > service > domain > infrastructure**；**domain 不得 import infrastructure**。改完跑 `.venv/bin/lint-imports` 必须绿（3 契约）。
- 端口 + 假实现 + config-gated：有云密钥（`.env`）用真阿里云适配器，否则 fakes。测试用 `tests/conftest.py` monkeypatch `deps.*` 强制 fakes，**绝不打真云**。
- 组合根 `app/api/deps.py`：所有 service/infra 在这里装配。`get_audit_service()` 等每次读模块级单例（如 `deps._auditor`），所以测试/QC 可 monkeypatch。
- 前端是**单文件 SPA** `frontend/index.html`（vanilla JS），白 + 葡萄紫（`--mint:#6c2ba0`），Fraunces/Inter/8px。`StaticFiles` 挂 `/ui`，**从磁盘按请求读**（所以前端改动可 `docker cp` 热更、无需重建）。

---

## 3. 本会话做了什么（3 件事，均已上线+验证）

### ① 内容安全「治误伤」闭环：命中词标红 → 一键加白 → 一键重判
问题：阿里云把引用/玩笑（"我本可以成为杀人犯"）误判；白名单要靠猜和手敲。
- **后端**
  - `content_safety.py`：新增 `AliyunAuditor.audit_detail(content)->(verdict, risk_words)`（非破坏，`audit()->str` 契约不变，两处 `AuditStatus(audit())` 调用方不动）；`_moderate_text_detail` 交出阿里云 `reason.riskWords`。
  - `audit_pipeline.py`：`_content_safety._run` 用 `getattr(auditor,'audit_detail')` 兜底，命中项塞 `risk_words`；`run()` 重构为 `_to_segments` + **`_evaluate(job,segments)`** + **`_persist(summary_segments=None 时不重跑摘要)`**；新增 **`recheck(job, old_report)`** = 只对已存报告 segments 重判（不重转写/抽帧/不重复生成帧素材，白名单实时读）。
  - `router.py`：**`POST /audit/tasks/{id}/recheck`**（gate `materials.audit`，后台线程跑 `recheck`）。
- **前端**：`riskWordsHTML`/`trigItem` 把命中词标红成可点 chip；管理员点 chip=`wlFromReport` 一键加白（仅 `ME.role==='admin'`）；待审核任务加「重新审核」`recheckTask`；白名单页加词数统计+筛选。
- **实测**：真 Aliyun `comment_detection` **确实返回 riskWords**（`Data.Reason={"riskLevel":"medium","riskTips":"暴恐_恐怖分子","riskWords":"杀人"}`；广告类 riskWords="加我,加我微信"）。子串匹配，加"杀人"即覆盖"杀人犯"。

### ② 阿里云内容审核调研
- 增强版检测走 OpenAPI `green20220302`；**增强版自定义词库只能控制台管、无公开 OpenAPI** → 本地白名单路线是对的。文档 `docs/content-safety-tuning.md`。API 全景/词库/回流机制记在 `ecs-deploy-and-persistence.md`。

### ③ 审核队列改「卡内直接判」+ 卡内含 AI 报告、只标问题
用户要：卡片里直接看媒体、看 AI 审核报告、看**哪里有问题（没问题的不展示）**，直接通过/拦截，不跳转。
- **后端** `router.py`：新增 **`GET /audit/queue`**（gate `materials.audit`）一次拉齐待复核物料 + `media_url=_media_url(m)`（真实文件签名 URL，可内联 `<img>/<video controls>/<audio controls>`）+ `report`（`_report_out`）。新增 `_media_url(m)` helper。
- **前端**：`sec-review` 的 `loadQueue` 渲染 **`queueCard`**：`mediaEmbed(m)`（按类型内联媒体）+ **`auditBlock(m)`** + 通过/拦截，不进抽屉。
  - `auditBlock(m)` **只展示"哪里有问题"**：有机审命中→`relevantTrig(m)`（帧图片只显命中本帧 `frame_oss_key==oss_key` 的）过 `trigItem` 标红定位；无命中但有 AI 反解→显示 `m.description`（Qwen-VL 画面分析）为「AI 内容分析」；都没有→「暂无 AI 报告」。**故意不展示干净的全量文字链路**。
- **安全修复（对抗式 review 抓到的存储型 XSS）**：用户名开放注册无过滤 → 拼进 innerHTML 会打到管理员。已对 `owner_name` 和媒体 URL 全部 `esc()`（`esc` 转义 `&<>"`，对属性也安全）。修了 `queueCard` + `cell(ctx==='all')`。
- **注意**：审核队列里**多数是审核抽出的帧图片**（`oss_key` 前缀 `frames/`，`_save_frame_material` 存 review 但**不挂 audit_report_id**→无正式 report，靠 description 兜底）。当前线上队列约 21 条，多为帧图片。

---

## 4. 关键文件地图（本会话动过的）

```
app/infrastructure/content_safety.py  audit_detail / _moderate_text_detail / _apply_whitelist / _parse_risk_words
app/domain/ports.py                    Auditor.audit_detail 文档（可选方法）
app/service/audit_pipeline.py          _content_safety(risk_words) / _evaluate / recheck / _persist / _save_frame_material
app/api/router.py                      /audit/tasks/{id}/recheck / GET /audit/queue / _media_url / _report_out / _mat_out
frontend/index.html                    queueCard/mediaEmbed/auditBlock/relevantTrig/riskWordsHTML/trigItem/wlFromReport/recheckTask/loadWhitelist(count+filter)
Dockerfile                             ENV UV_INDEX_URL=阿里云 PyPI 镜像（不然 ECS 直连 pypi.org 卡死）
tests/test_api.py                      /audit/queue、recheck 端点+403、whitelist CRUD
tests/test_audit_pipeline.py           risk_words 入报告、recheck 只重判不重抽帧
tests/test_mvp_services.py             audit_detail 返回(verdict,words)、白名单降级
docs/content-safety-tuning.md          严格度/白名单/阿里云控制台路径说明
```

---

## 5. 怎么验证（收工前必跑，"测全才收工"）

```bash
cd /Users/sunyuexuan/Documents/assets-management
.venv/bin/lint-imports                       # 架构 3 契约必绿
.venv/bin/python -m pytest -q                # 129 passed
.venv/bin/behave specs/features              # 17 scenarios
make ui-qc                                   # 真点击回归 6/6（登录/收藏/管理员/审核/发布）
```
本会话的临时真点击 QC 脚本在 `/tmp/`（`queue_qc.py` 审核队列卡、`riskword_qc.py` 治误伤闭环、`live_riskword_qc2.py` 线上真 Aliyun 闭环）——**新会话 `/tmp` 可能已清空**，需要时照着 `tests/ui_qc.py` 的模式（in-process uvicorn + Playwright）重写。
> 铁律：**功能对不对必须真点击**（`make ui-qc` 或 Playwright），API 绿灯 + 静态截图不算数。前端改动尤其。登录用真登录 `#g-name`/`#g-pw`/`#g-submit`，注册要填 `#g-pw2`（确认密码）。

---

## 6. 怎么部署（红线：只碰 assets-api，别动别人容器）

线上是**共享生产 ECS**（同机还跑 ragflow/crm/es/minio/mysql/redis 等别人的容器）。**只准管 `assets-api` 容器、只用端口 8088**；绝不动 docker daemon/nginx/别人的容器。详见 `ecs-deploy-and-persistence.md`。

- **前端改动（推荐，零停机）**：`scp` 新 `frontend/index.html` → `/opt/assets/src/frontend/index.html`，再 `docker cp /opt/assets/src/frontend/index.html assets-api:/app/frontend/index.html`（StaticFiles 按请求读磁盘，立即生效，无需重建）。
- **后端改动**：打 `*.tgz`（app+frontend+…）→ scp → 解压到 `/opt/assets/src` → `docker build -t assets-api:latest .`（**构建慢时 `nohup … > build.log 2>&1 &` 后台跑 + 轮询**，别让 SSH 超时打断）→ 备份 `/opt/assets/data/state.json` → `docker rm -f assets-api` + `docker run …`（`docker restart` **不重读 `--env-file`**，改 .env 必须 rm+run）。run 命令带 `--network assets-net -p 8088:8000 -v /opt/assets/data:/data --env-file /opt/assets/.env`（完整命令在记忆里）。
- **坑**：Dockerfile 的 uv 装依赖必须走阿里云 PyPI 镜像（已加 `UV_INDEX_URL`）；`ghcr.io/astral-sh/uv` 冷缓存拉 ~26MB 极慢；ECS 宿主机**没 curl**（验证从本地打线上 URL）。
- SSH/SCP 用 expect（上会话在 `/tmp/ecs.exp`、`/tmp/scp_ecs.exp`，**含 ECS root 密码，新会话没有需重建**——密码见 `.env`/记忆，别 echo 别提交）。

---

## 7. 🔴 红线与约束（务必遵守）

1. **共享生产 ECS**：只管 assets-api / 8088，别碰别人容器、docker daemon、nginx。
2. **别污染生产数据**：QC 在线上造的账号/物料/白名单词/任务**必须收尾清理**（用户多次强调）。审核队列里的真实待审核物料**不要乱点通过/拦截**（会改真实状态）——只读验证。
3. **持久化**：线上数据活在宿主机卷 `/opt/assets/data/state.json`，**重部署别删它**；单文件内存态→**必须单 worker**。
4. **密钥待轮换（别 echo/提交）**：对话里泄露过 OSS 主账号 AccessKey（`LTAI5tQk…`）、DashScope key、ECS 密码、ACR 密码、GitHub PAT。**主账号 AK 权限过大，长期应换 RAM 子用户最小权限**。密钥只放 `.env` / `/opt/assets/.env`。
5. **设计系统**：标题 Fraunces、正文 Inter；禁紫色渐变/通用 grid 套路；单一葡萄紫强调色；8px 节奏；白色主题。
6. 工作区**未提交 git**（无远端 CI 那条路手动补 `--network assets-net`）。要不要提交由用户定。

---

## 8. 线上事实（接手即可用）

- URL `http://8.149.247.100:8088`（前端 `/ui/`）。
- 账号：seeded 管理员 + 普通用户，见 `app/api/deps.py` 播种段（`admin` 是唯一管理员，其余普通用户）。QC 脚本里有明文，别写进会提交的文件。
- 真云已接：OSS、Qwen-VL（qwen3-vl-plus 视频/图像反解）、multimodal-embedding-v1、paraformer-v2 ASR、pgvector 语义搜索、**内容安全增强版**（`AM_CONTENT_SAFETY_MODE=balanced`）。都 config-gated。
- pgvector 容器 `assets-pgvector`（网络 `assets-net`）。

---

## 9. 待办 / 下一步（可接着干）

- **审核队列帧图片噪音**：16 张审核抽出的帧图片各占一张卡、且无正式 report。可选优化：把帧 `audit_report_id` 挂到父视频报告（`_persist` 里按 `source_job==job.id` 回填），或**审核队列按父视频聚合**（复核一次视频、标出问题帧），而不是每帧一卡。用户提过"可以说一声就做"。
- **审核队列翻页**：目前 `?size=100` 无翻页控件（和旧版一样），>100 条会只显前 100；其他列表都有服务端翻页 + `renderPager` 可复用。
- **主账号 AK → RAM 子用户最小权限**（安全待办）。
- 「一键补摘要」按钮、DashScope/RDS 扩容（早先提过，未做）。

---

## 10. 给新会话的第一步建议

1. 读 `CLAUDE.md` + 上面第 1 节的记忆文件（尤其 `ecs-deploy-and-persistence.md`）。
2. 跑第 5 节的传感器确认当前绿。
3. 要改功能：先在工作树改 → 传感器 + 真点击 QC → 按第 6 节部署（前端优先零停机 `docker cp`）→ 线上真点击验证 → 清理测试数据 → 更新记忆文件。
4. 有歧义先问用户，别猜大方向。
