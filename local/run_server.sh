#!/bin/bash
# launchd Job A: FastAPI サーバー常駐（ポート8788）
# Dropbox はローカルFSでなく API 経由なので TCC(CloudStorage) 制約に当たらない
source "$(dirname "$0")/env.sh"
export PATH="$HOME/Library/Python/3.9/bin:/usr/bin:/bin:/usr/sbin:/sbin"
cd "$(dirname "$0")/.."
exec python3 -m uvicorn main:app --host 127.0.0.1 --port "${PORT:-8788}"
