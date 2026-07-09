# PRD — 物料管理系统

> 产品需求文档。每条需求带唯一 **REQ-id**,是追溯脊柱的锚点:PRD → behave(`@REQ-id`)→ C4 元素 → story → 测试 → CI。
> 验收标准用 **EARS**(WHEN…THEN 系统 SHALL… / IF…THEN… / WHERE…),每条对应一个可执行 behave 场景。

## 定位与质量目标
- **定位**:内容运营的"物料中台"——上传视频即自动拆解成可检索、已审核的多模态物料库。
- **质量目标**:① 500 并发上传受理 P95 ≤ 10s;② 反解召回准确率(人工抽检)≥ 85%;③ 违规物料零漏放。

## Epic 与优先级
| Epic | 能力 | 优先级 | REQ 前缀 |
|---|---|---|---|
| E2/E5 | 视频反解物料 + 反解智能体〔核心〕 | P0 | REQ-2xx |
| E6 | 自动内容审核 | P0 | REQ-5xx |
| E1 | 物料管理 CRUD | P0 | REQ-1xx |
| E3 | 语义搜索 | P1 | REQ-3xx |
| E4 | 大量物料索引 | P1 | REQ-4xx |
| E7 | 用户管理 | P1 | REQ-6xx |
| E8 | 功能权限 RBAC 后台 | P1 | REQ-7xx |

**MVP**:E2+E5+E6+E1+E3(上传视频→反解→审核→存物料→搜得到)。

---

## E1 物料管理(REQ-1xx)
- **REQ-101**(事件) WHEN 用户上传物料文件 THEN 系统 SHALL 将文件存入 OSS 并落库元数据(类型/标签/owner/审核态),返回物料 ID。
- **REQ-102**(事件) WHEN 用户请求物料 THEN 系统 SHALL 返回受时限 OSS 签名 URL。
- **REQ-103**(状态) IF 物料被删除 THEN 系统 SHALL 使其不可访问且不可检索。

## E2 视频反解物料〔核心〕(REQ-2xx)
- **REQ-201**(事件) WHEN 用户上传视频 THEN 系统 SHALL 受理并在 10 秒内返回任务 ID,反解异步进行。
- **REQ-202**(事件) WHEN 反解完成 THEN 系统 SHALL 产出 ≥1 条物料,每条含类型/缩略/来源时间码/向量。
- **REQ-203**(可选) WHERE 视频 >100MB 或 >10min 系统 SHALL 分段抽帧并限制 max_frames。
- **REQ-204**(状态) IF 反解调用失败 THEN 系统 SHALL 重试 ≤3 次,仍失败则标记 job=failed 且保留原视频。

## E5 反解智能体(REQ-2xx 共用)
- **REQ-211**(事件) WHEN 调用图像反解 THEN 智能体 SHALL 返回结构化物料候选(字段齐全:类型/描述/标签)。
- **REQ-212**(状态) IF 反解结果缺必填字段 THEN 系统 SHALL 判为失败并触发重试或人工。

## E6 自动内容审核(REQ-5xx)
- **REQ-501**(事件) WHEN 生成一条物料 THEN 系统 SHALL 调内容安全审核画面+文本,写回 pass/review/block。
- **REQ-502**(状态) WHERE 审核=block 系统 SHALL 使该物料默认不可检索、不可下载。
- **REQ-503**(状态) IF 审核超时/失败 THEN 系统 SHALL 标记 review 进人工复核,**不得默认放行**。

## E3 语义搜索(REQ-3xx)
- **REQ-301**(事件) WHEN 用户输入文本查询 THEN 系统 SHALL 生成 embedding 做 pgvector 近邻 + 元数据过滤,按相似度排序返回。
- **REQ-302**(事件) WHEN 查询含专有名词 THEN 系统 SHALL 用 hybrid(向量+BM25)提升命中。
- **REQ-303**(状态) WHERE 物料审核=block 系统 SHALL 不出现在任何搜索结果。

## E4 大量物料索引(REQ-4xx)
- **REQ-401**(非功能) WHERE 物料量 >千万 系统 SHALL 用 HNSW 索引保证向量近邻查询 P95 ≤ 200ms。
- **REQ-402**(事件) WHEN 新物料入库 THEN 系统 SHALL 增量写入向量索引。

## E7 用户管理(REQ-6xx)
- **REQ-601**(事件) WHEN 用户登录并凭据正确 THEN 系统 SHALL 签发受时限 token。
- **REQ-602**(状态) IF 密码存储 THEN 系统 SHALL 加盐哈希,禁止明文。

## E8 功能权限 RBAC 后台(REQ-7xx)
- **REQ-701**(状态) IF 用户无某功能权限 THEN 系统 SHALL 拒绝(403)并记审计。
- **REQ-702**(事件) WHEN 管理员在后台修改角色权限 THEN 系统 SHALL 即时生效于后续请求。

---

## 非功能需求(交 TEA-NFR 评估)
- **NFR-1** 500 并发上传受理 P95 ≤ 10s。
- **NFR-2** 向量查询 P95 ≤ 200ms(千万级)。
- **NFR-3** 审核违规零漏放。
- **NFR-4** 越权访问一律拒绝并审计。
