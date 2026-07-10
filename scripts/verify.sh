#!/usr/bin/env bash
# 统一验证 harness —— 一次跑完三层闭环的确定性传感器。
# 全绿 exit 0;任一红 exit 2(供 Claude 的 Stop hook 唤醒我去修)。
# 跑法:bash scripts/verify.sh
set -uo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:$PATH"
V=.venv/bin
fail=0

run() {  # $1=标签  其余=命令
  local name="$1"; shift
  if out=$("$@" 2>&1); then
    echo "  🟢 $name"
  else
    echo "  🔴 $name"
    echo "$out" | tail -6 | sed 's/^/       /'
    fail=1
  fi
}

echo "── 三层闭环传感器 ──"
run "② 架构  lint-imports(分层契约)" $V/lint-imports
run "③ 代码  pytest(单元/集成)"       $V/pytest -q
run "① 产品  behave(可执行验收)"      $V/behave specs/features

if [ $fail -eq 0 ]; then
  echo "✅ 三层闭环全绿"
  exit 0
else
  echo "❌ 有红 —— 按失败路由修对应闭环(①产品/②架构/③代码),再让验证转绿"
  exit 2
fi
