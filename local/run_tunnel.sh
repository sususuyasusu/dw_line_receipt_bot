#!/bin/bash
# launchd Job B: cloudflared quick tunnel 常駐 + LINE Webhook URL 自動更新
# トンネルが再起動して URL が変わっても、LINE 側の受信先を自動で追従させる
source "$(dirname "$0")/env.sh"

LOG=/tmp/dw-receipt-bot-tunnel.log
: > "$LOG"

~/.local/bin/cloudflared tunnel --url "http://localhost:${PORT:-8788}" --no-autoupdate >> "$LOG" 2>&1 &
TUNNEL_PID=$!

# URL が出るまで最大60秒待つ
URL=""
for i in $(seq 1 30); do
  sleep 2
  URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" | head -1)
  [ -n "$URL" ] && break
done

if [ -z "$URL" ]; then
  echo "$(date) FATAL: tunnel URL not found" >> "$LOG"
  kill "$TUNNEL_PID" 2>/dev/null
  exit 1
fi

echo "$(date) tunnel URL: $URL" >> "$LOG"
echo "$URL" > /tmp/dw-receipt-bot-tunnel-url.txt

# LINE Webhook endpoint を自動更新（トンネルURLのDNS浸透待ちでリトライ）
for j in 1 2 3 4 5 6; do
  sleep 5
  RESP=$(curl -s -m 15 -X PUT 'https://api.line.me/v2/bot/channel/webhook/endpoint' \
    -H "Authorization: Bearer $LINE_CHANNEL_ACCESS_TOKEN" \
    -H 'Content-Type: application/json' \
    -d "{\"endpoint\":\"${URL}/webhook\"}")
  echo "$(date) webhook update try#$j resp: $RESP" >> "$LOG"
  [ "$RESP" = "{}" ] && break
done

# 検証テスト
sleep 3
TEST=$(curl -s -m 20 -X POST 'https://api.line.me/v2/bot/channel/webhook/test' \
  -H "Authorization: Bearer $LINE_CHANNEL_ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"endpoint\":\"${URL}/webhook\"}")
echo "$(date) webhook test: $TEST" >> "$LOG"

# トンネルプロセスを見守る（死んだら launchd が本スクリプトごと再起動→URL再取得→webhook再更新）
wait "$TUNNEL_PID"
