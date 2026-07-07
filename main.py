"""
LINE Bot: 受信画像 → 透明テキスト層付き PDF → Dropbox _inbox/ にアップロード
"""
import datetime
import io
import os
import subprocess
import tempfile
from pathlib import Path

import dropbox
from PIL import Image, ImageOps
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


MAX_DIMENSION = 2200  # 長辺の最大ピクセル。Render Free 512MB に収める


def _preprocess_image(image_bytes: bytes) -> bytes:
    """大きい画像でも Render Free tier (512MB) で Tesseract が完走できるよう、
    EXIF 回転を適用してから RGB JPEG (品質88・長辺<=2200px) に正規化する。"""
    with Image.open(io.BytesIO(image_bytes)) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        w, h = im.size
        long_side = max(w, h)
        if long_side > MAX_DIMENSION:
            scale = MAX_DIMENSION / long_side
            im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=88, optimize=True)
        return buf.getvalue()


def _image_to_pdf_pillow(normalized_jpeg: bytes) -> bytes:
    """Tesseract が無い環境（Mac常駐）用: Pillow で画像のみの PDF を生成。
    テキスト層は付かないが、検索性は receipt-photo-sidecar の .md が担保する。"""
    with Image.open(io.BytesIO(normalized_jpeg)) as im:
        if im.mode != "RGB":
            im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, format="PDF", resolution=150.0)
        return buf.getvalue()


def image_to_pdf_with_text_layer(image_bytes: bytes) -> bytes:
    normalized = _preprocess_image(image_bytes)
    with tempfile.TemporaryDirectory() as tmpdir:
        img_path = Path(tmpdir) / "input.jpg"
        img_path.write_bytes(normalized)
        out_base = Path(tmpdir) / "output"
        try:
            result = subprocess.run(
                ["tesseract", str(img_path), str(out_base), "-l", "jpn", "pdf"],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except FileNotFoundError:
            return _image_to_pdf_pillow(normalized)
        if result.returncode != 0:
            raise RuntimeError(f"tesseract failed: {result.stderr[:500]}")
        pdf_bytes = out_base.with_suffix(".pdf").read_bytes()
        # 健全性検証: 完全な PDF は末尾近くに %%EOF を含む
        if b"%%EOF" not in pdf_bytes[-1024:]:
            raise RuntimeError("tesseract produced truncated PDF (no %%EOF marker)")
        return pdf_bytes


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


@app.get("/debug/env")
def debug_env():
    return {
        "secret_head": (CHANNEL_SECRET[:4] + "...") if CHANNEL_SECRET else None,
        "secret_len": len(CHANNEL_SECRET) if CHANNEL_SECRET else 0,
        "token_head": (CHANNEL_ACCESS_TOKEN[:8] + "...") if CHANNEL_ACCESS_TOKEN else None,
        "token_len": len(CHANNEL_ACCESS_TOKEN) if CHANNEL_ACCESS_TOKEN else 0,
        "dropbox_root_ns": DROPBOX_ROOT_NAMESPACE_ID,
        "dropbox_dest": DROPBOX_DEST_FOLDER,
        "expected_secret_head": "62fc",
        "expected_token_head": "Ytb4BbD8",
    }


@app.get("/debug/selftest")
def debug_selftest():
    """Dropbox接続の自己診断（一時的）。トークン失効/権限を切り分ける。"""
    result = {"dropbox_account": None, "dropbox_list": None}
    try:
        acct = _dbx().users_get_current_account()
        result["dropbox_account"] = {"ok": True, "email": acct.email}
    except Exception as e:  # noqa: BLE001
        result["dropbox_account"] = {"ok": False, "error": type(e).__name__, "detail": str(e)[:300]}
    try:
        listing = _dbx().files_list_folder(DROPBOX_DEST_FOLDER)
        result["dropbox_list"] = {"ok": True, "entries": len(listing.entries)}
    except Exception as e:  # noqa: BLE001
        result["dropbox_list"] = {"ok": False, "error": type(e).__name__, "detail": str(e)[:300]}
    return result


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
