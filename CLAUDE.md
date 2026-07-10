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
