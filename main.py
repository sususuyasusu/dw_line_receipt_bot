"""
LINE Bot: 受信画像 → 透明テキスト層付き PDF → Dropbox _inbox/ にアップロード
"""
import datetime
import os
import subprocess
import tempfile
from pathlib import Path

import dropbox
from fastapi import FastAPI, HTTPException, Request
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    MessagingApiBlob,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import ImageMessageContent, MessageEvent

CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
DROPBOX_REFRESH_TOKEN = os.environ["DROPBOX_REFRESH_TOKEN"]
DROPBOX_APP_KEY = os.environ["DROPBOX_APP_KEY"]
DROPBOX_APP_SECRET = os.environ["DROPBOX_APP_SECRET"]
DROPBOX_DEST_FOLDER = os.environ.get(
    "DROPBOX_DEST_FOLDER",
    "/D& W/社内/証憑写真/_inbox",
)
DROPBOX_ROOT_NAMESPACE_ID = os.environ.get("DROPBOX_ROOT_NAMESPACE_ID")

handler = WebhookHandler(CHANNEL_SECRET)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)

app = FastAPI()


def _dbx() -> dropbox.Dropbox:
    dbx = dropbox.Dropbox(
        app_key=DROPBOX_APP_KEY,
        app_secret=DROPBOX_APP_SECRET,
        oauth2_refresh_token=DROPBOX_REFRESH_TOKEN,
    )
    if DROPBOX_ROOT_NAMESPACE_ID:
        from dropbox.common import PathRoot
        dbx = dbx.with_path_root(PathRoot.root(DROPBOX_ROOT_NAMESPACE_ID))
    return dbx


def image_to_pdf_with_text_layer(image_bytes: bytes) -> bytes:
    with tempfile.TemporaryDirectory() as tmpdir:
        img_path = Path(tmpdir) / "input.jpg"
        img_path.write_bytes(image_bytes)
        out_base = Path(tmpdir) / "output"
        result = subprocess.run(
            ["tesseract", str(img_path), str(out_base), "-l", "jpn", "pdf"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"tesseract failed: {result.stderr}")
        return out_base.with_suffix(".pdf").read_bytes()


def upload_to_dropbox(pdf_bytes: bytes, filename: str) -> str:
    dest = f"{DROPBOX_DEST_FOLDER}/{filename}"
    _dbx().files_upload(
        pdf_bytes,
        dest,
        mode=dropbox.files.WriteMode.add,
        autorename=True,
    )
    return dest


@app.get("/")
def root():
    return {"status": "ok", "service": "dw_line_receipt_bot"}


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(request: Request):
    signature = request.headers.get("X-Line-Signature", "")
    body = (await request.body()).decode("utf-8")
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    return {"status": "ok"}


@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image(event: MessageEvent):
    message_id = event.message.id
    with ApiClient(configuration) as api_client:
        blob_api = MessagingApiBlob(api_client)
        image_bytes = blob_api.get_message_content(message_id=message_id)

    pdf_bytes = image_to_pdf_with_text_layer(image_bytes)

    now = datetime.datetime.now()
    user_suffix = (event.source.user_id or "anon")[:6]
    filename = f"{now.strftime('%Y%m%d-%H%M%S')}-line-{user_suffix}.pdf"
    dest_path = upload_to_dropbox(pdf_bytes, filename)

    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[
                    TextMessage(text=f"保存しました\n{filename}\n→ {dest_path}")
                ],
            )
        )
