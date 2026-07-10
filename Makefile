# 物料中台 —— 本地 harness 入口(从功能设计到产品验证的闭环)
V := .venv/bin
.PHONY: help verify arch test bdd ui-qc serve deploy design

help:            ## 显示所有命令
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/ →/' | sort

# ── 验证闭环(确定性传感器)──
verify:          ## 三层闭环:架构②+代码③+产品① 一次跑完(Stop hook 自动调这个)
	@bash scripts/verify.sh
arch:            ## ② 架构契约(domain 不碰 infra 等)
	@$(V)/lint-imports
test:            ## ③ 单元/集成
	@$(V)/pytest -q
bdd:             ## ① 可执行验收(EARS→behave)
	@$(V)/behave specs/features
ui-qc:           ## 前端真点击 QC(起服务+播种+断言,前端改动必跑)
	@bash scripts/ui_qc.sh

# ── 开发 ──
serve:           ## 本地起服务(localhost:8099)
	@AM_OSS_ACCESS_KEY_ID= AM_OSS_BUCKET= $(V)/uvicorn app.main:app --reload --port 8099

# ── 交付 ──
deploy:          ## 推 GitHub(触发 CI:sensors→build→ACR→ECS)
	@git push origin main

# ── 产品/架构设计闭环(BMAD + 设计技能,在 Claude Code 内)──
design:          ## 打印产品/架构设计闭环该调哪些技能
	@echo "产品设计①:bmad-agent-pm → bmad-prd(EARS/REQ-id)→ bmad-testarch-atdd(生成 behave)"
	@echo "界面设计 :frontend-design 技能 + shadcn MCP → 每屏 make ui-qc 真点击"
	@echo "架构设计②:bmad-agent-architect + c4-model-skill → workspace.dsl + ADR;arch 守门"
	@echo "代码闭环③:bmad-create-story → bmad-dev-story(先测后码)→ /code-review + make verify"
	@echo "验证驱动④:make verify(每次收工 Stop hook 自动跑)+ make ui-qc + TEA-nfr/trace/release-gate"
