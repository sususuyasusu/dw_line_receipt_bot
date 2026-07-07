#!/bin/bash
# トンネル外部疎通ウォッチドッグ（launchd で5分毎起動）
# トンネルURLに外から届かなければ cloudflared を kill
# → run_tunnel.sh の wait が返る → launchd が再起動 → 新URL → LINE webhook 自動更新
URL=$(cat /tmp/dw-receipt-bot-tunnel-url.txt 2>/dev/null)
[ -z "$URL" ] && exit 0
R=$(curl -s -m 20 "$URL/healthz" 2>/dev/null)
if [ "$R" != '{"status":"ok"}' ]; then
  echo "$(date) watchdog: tunnel dead ($URL) -> restarting" >> /tmp/dw-receipt-bot-tunnel.log
  pkill -f "cloudflared tunnel --url http://localhost:8788"
fi
