#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# 物料管理系统 — 一键安装剩余 harness / skill / MCP / 传感器
# 用法:在项目根目录执行  bash scripts/setup.sh
# 幂等:可重复运行;已装的会跳过。需要密钥的地方用占位符,自行替换。
# ─────────────────────────────────────────────────────────────
set -uo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:$PATH"

say() { printf "\n\033[1;36m▶ %s\033[0m\n" "$1"; }
ok()  { printf "  \033[1;32m✓ %s\033[0m\n" "$1"; }
warn(){ printf "  \033[1;33m! %s\033[0m\n" "$1"; }

# ── 0. 前置检查 ───────────────────────────────────────────────
say "0/6 前置检查"
command -v node >/dev/null && ok "node $(node -v)" || warn "缺 node"
command -v uv   >/dev/null && ok "uv $(uv --version)" || { warn "缺 uv,安装中"; curl -LsSf https://astral.sh/uv/install.sh | sh; export PATH="$HOME/.local/bin:$PATH"; }
command -v claude >/dev/null && ok "claude CLI 就绪" || warn "缺 claude CLI(MCP 步骤会跳过)"

# ── 1. Python 传感器(import-linter / behave / pytest)─────────
say "1/6 Python 传感器"
[ -d .venv ] || uv venv --python 3.11
uv pip install -q import-linter behave pytest pytest-asyncio
ok "import-linter $(.venv/bin/lint-imports --version 2>/dev/null || echo '?')"
ok "behave $(.venv/bin/behave --version 2>/dev/null || echo '?')"
ok "pytest $(.venv/bin/pytest --version 2>/dev/null || echo '?')"

# ── 2. 项目依赖(FastAPI 等)──────────────────────────────────
say "2/6 项目依赖"
uv pip install -q -e ".[dev]" 2>/dev/null && ok "项目依赖已装" || warn "项目依赖装失败(可稍后重试)"

# ── 3. 首批 MCP → 项目 .mcp.json ─────────────────────────────
say "3/6 配置 MCP(写入项目 .mcp.json)"
if command -v claude >/dev/null; then
  # Playwright:真机验证
  claude mcp add -s project playwright -- npx -y @playwright/mcp@latest 2>/dev/null && ok "playwright" || warn "playwright 已存在/跳过"
  # GitHub:官方远程(OAuth)
  claude mcp add -s project --transport http github https://api.githubcopilot.com/mcp/ 2>/dev/null && ok "github" || warn "github 已存在/跳过"
  # Context7:实时文档(需 API key,先不带 key 也能用基础功能)
  claude mcp add -s project context7 -- npx -y @upstash/context7-mcp 2>/dev/null && ok "context7" || warn "context7 已存在/跳过"
  # DBHub:DB schema(DSN 待 RDS 开通后替换占位符)
  claude mcp add -s project db -- npx -y @bytebase/dbhub --dsn "postgresql://user:pass@localhost:5432/assets" 2>/dev/null && ok "db(DSN 待替换)" || warn "db 已存在/跳过"
  # shadcn:UI 组件真源
  npx -y shadcn@latest mcp init --client claude 2>/dev/null && ok "shadcn" || warn "shadcn 需在有 components.json 后再 init"
else
  warn "无 claude CLI,MCP 请在 Claude Code 内用 /mcp 或手动写 .mcp.json"
fi

# ── 4. 架构/设计技能 → .claude/skills ────────────────────────
say "4/6 安装技能(clone 到 .claude/skills)"
mkdir -p .claude/skills
clone_skill() {  # $1=repo  $2=目录名
  if [ -d ".claude/skills/$2" ]; then warn "$2 已存在,跳过"; else
    git clone --depth 1 "$1" ".claude/skills/$2" 2>/dev/null && ok "$2" || warn "$2 clone 失败(检查网络/地址)"
    rm -rf ".claude/skills/$2/.git"
  fi
}
clone_skill https://github.com/cheriftj/c4-model-skill c4-model-skill
clone_skill https://github.com/yonatankarp/software-design-skills software-design-skills
# Anthropic 官方设计/QC 技能(界面设计 + 主题 + 真点击测试)
if [ ! -d .claude/skills/frontend-design ]; then
  tmp=$(mktemp -d); git clone --depth 1 https://github.com/anthropics/skills "$tmp/s" 2>/dev/null \
    && for s in frontend-design theme-factory webapp-testing; do cp -r "$tmp/s/skills/$s" .claude/skills/ 2>/dev/null && ok "$s"; done \
    || warn "anthropic skills clone 失败(检查网络)"; rm -rf "$tmp"
else warn "frontend-design 已存在,跳过"; fi

# Python Playwright(UI QC 用)
uv pip install -q playwright 2>/dev/null && .venv/bin/playwright install chromium 2>/dev/null && ok "playwright + chromium" || warn "playwright 装失败"

# ── 5. 验证 ──────────────────────────────────────────────────
say "5/6 验证协作链"
echo "  BMAD skills 数:$(ls .claude/skills 2>/dev/null | grep -c bmad- || echo 0)"
echo "  自定义技能:$(ls .claude/skills 2>/dev/null | grep -E 'c4-model|software-design' | tr '\n' ' ')"
command -v claude >/dev/null && { echo "  MCP 列表:"; claude mcp list 2>/dev/null | sed 's/^/    /'; }

# ── 6. 完成 ──────────────────────────────────────────────────
say "6/6 完成"
cat <<'EOF'
  下一步(在 Claude Code 内):
    1) /plugin marketplace add anthropics/skills   → 启用 frontend-design
    2) 调 bmad-help 问"从哪开始"
    3) 用 Analyst/PM 起草"视频反解物料"的 PRD(第一个 REQ)
  可选(按需):k6 压测 / Structurizr MCP / Magic MCP / BMB 造 ai-effect-validator
EOF
