#!/usr/bin/env bash
# UI QC 编排:起本地服务(禁OSS走假存储)→ 播种可收藏/可审核/可发布数据 → 跑真点击断言。
set -uo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:$PATH"
PORT=${PORT:-8099}; B=http://localhost:$PORT

AM_OSS_ACCESS_KEY_ID= AM_OSS_BUCKET= .venv/bin/uvicorn app.main:app --port "$PORT" --log-level warning &
SRV=$!
trap 'kill $SRV 2>/dev/null' EXIT
sleep 3

# 真登录拿签名 token(token 已 HMAC 签名,不能再伪造)
tok() { curl -s -X POST $B/users/login -H 'Content-Type: application/json' -d "$1" | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])"; }
AT=$(tok '{"name":"admin","password":"admin123"}'); UT=$(tok '{"name":"demo","password":"pw123456"}')
U="Authorization: Bearer $UT"; A="Authorization: Bearer $AT"
curl -s -X POST $B/materials -H "$U" -H 'Content-Type: application/json' -d '{"type":"music","oss_key":"m.mp3"}' >/dev/null
curl -s -X POST $B/videos -H "$A" -H 'Content-Type: application/json' -d '{"oss_key":"a.mp4"}' >/dev/null
curl -s -X POST $B/videos -H "$A" -H 'Content-Type: application/json' -d '{"oss_key":"b.mp4"}' >/dev/null
FIRST=$(curl -s -H "$A" $B/library/all | python3 -c "import sys,json;d=json.load(sys.stdin);print([m['id'] for m in d['items'] if m['owner_id']=='admin'][0])")
curl -s -X POST $B/materials/$FIRST/set-audit -H "$A" -H 'Content-Type: application/json' -d '{"status":"pass"}' >/dev/null
curl -s -X POST $B/materials/$FIRST/publish -H "$A" >/dev/null

.venv/bin/python tests/ui_qc.py "$B"
