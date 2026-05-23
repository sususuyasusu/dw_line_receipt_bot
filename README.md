# dw_line_receipt_bot

LINEで送信された画像を、透明テキスト層付き PDF に変換して Dropbox `_inbox/` に保存するBot。

## 構成

```
LINE → Webhook → FastAPI → Tesseract (jpn) → 透明テキスト層付きPDF → Dropbox API → _inbox/
```

## 必要な環境変数

| 変数 | 取得元 |
|---|---|
| `LINE_CHANNEL_SECRET` | LINE Developers Console > チャネル基本設定 |
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE Developers Console > Messaging API設定 |
| `DROPBOX_APP_KEY` | Dropbox App Console |
| `DROPBOX_APP_SECRET` | Dropbox App Console |
| `DROPBOX_REFRESH_TOKEN` | OAuth フロー後に取得 |
| `DROPBOX_DEST_FOLDER` | 保存先パス（デフォルト: `/D&W (Detale and Works)/社内/証憑写真/_inbox`） |

## デプロイ

Render.com に Docker でデプロイ:
1. GitHub に push
2. Render で New Web Service → このリポジトリを指定
3. 環境変数を5つ設定
4. デプロイ後の URL を LINE Webhook に登録

## ローカル動作確認

```bash
docker build -t dw-line-receipt-bot .
docker run -p 8000:8000 --env-file .env dw-line-receipt-bot
# 別ターミナルで Cloudflare Tunnel
cloudflared tunnel --url http://localhost:8000
```

## 関連

Vault: `Knowledge/line-bot-receipt.md` / `Knowledge/receipt-photo-pipeline.md`
