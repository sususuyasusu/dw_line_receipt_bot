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
from linebot.v3.webhooks import (
    FileMessageContent,
    ImageMessageContent,
    MessageEvent,
)

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

# 一時診断カウンタ（受信→発火→保存の流れを外から観測する）
_DIAG = {
    "webhook_calls": 0,
    "last_event_types": None,
    "handler_error": None,
    "image_handler_calls": 0,
    "image_saved": 0,
    "last_saved_file": None,
}


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


# ── LINE Webhook 再送による二重保存を防ぐための「処理済みマーカー」 ──
# LINE は初回配信が失敗（コールドスタート等）すると同じイベントを再送する。
# メッセージ ID ごとに空マーカーを残し、既処理なら保存をスキップする。
# マーカーは _inbox の外（sidecar が触らない _seen/）に置く。
def _seen_folder() -> str:
    parent = DROPBOX_DEST_FOLDER.rsplit("/", 1)[0]
    return f"{parent}/_seen"


def already_seen(message_id: str) -> bool:
    try:
        _dbx().files_get_metadata(f"{_seen_folder()}/{message_id}")
        return True
    except Exception:  # noqa: BLE001  未検出・その他は「未処理」扱い（安全側）
        return False


def mark_seen(message_id: str) -> None:
    try:
        _dbx().files_upload(
            b"",
            f"{_seen_folder()}/{message_id}",
            mode=dropbox.files.WriteMode.overwrite,
        )
    except Exception:  # noqa: BLE001  マーク失敗は致命的でない
        pass


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


@app.get("/debug/counters")
def debug_counters():
    return _DIAG


@app.post("/webhook")
async def webhook(request: Request):
    signature = request.headers.get("X-Line-Signature", "")
    body = (await request.body()).decode("utf-8")
    _DIAG["webhook_calls"] += 1
    try:
        import json as _json
        _DIAG["last_event_types"] = [
            (e.get("type"), (e.get("message") or {}).get("type"))
            for e in _json.loads(body).get("events", [])
        ]
    except Exception:  # noqa: BLE001
        pass
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    except Exception as e:  # noqa: BLE001  診断のため記録して再送出（挙動は不変）
        _DIAG["handler_error"] = f"{type(e).__name__}: {str(e)[:400]}"
        raise
    return {"status": "ok"}


def _fetch_content(message_id: str) -> bytes:
    with ApiClient(configuration) as api_client:
        return MessagingApiBlob(api_client).get_message_content(message_id=message_id)


def _sips_to_jpeg(data: bytes) -> bytes:
    """HEIC 等 Pillow が読めない画像を macOS の sips で JPEG 化する。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        src = Path(tmpdir) / "in.bin"
        out = Path(tmpdir) / "out.jpg"
        src.write_bytes(data)
        result = subprocess.run(
            ["sips", "-s", "format", "jpeg", str(src), "--out", str(out)],
            capture_output=True, text=True,
        )
        if result.returncode != 0 or not out.exists():
            raise RuntimeError(f"sips failed: {result.stderr[:200]}")
        return out.read_bytes()


def _content_to_pdf(content: bytes) -> bytes:
    """受信データを PDF に。PDF はそのまま、画像(HEIC含む)は変換する。"""
    if content[:5] == b"%PDF-":
        return content
    try:
        return image_to_pdf_with_text_layer(content)
    except Exception:  # noqa: BLE001  Pillow不可(HEIC等)→ sipsでJPEG化して再挑戦
        return image_to_pdf_with_text_layer(_sips_to_jpeg(content))


def _save_pdf_and_reply(event: MessageEvent, message_id: str, pdf_bytes: bytes) -> None:
    # ファイル名の時刻は LINE 送信時刻を日本時間で（実行環境のTZに依存させない）
    jst = datetime.timezone(datetime.timedelta(hours=9))
    ts_ms = getattr(event, "timestamp", None)
    ts = (
        datetime.datetime.fromtimestamp(ts_ms / 1000, tz=jst)
        if ts_ms
        else datetime.datetime.now(jst)
    )
    user_suffix = (event.source.user_id or "anon")[:6]
    filename = f"{ts.strftime('%Y%m%d-%H%M%S')}-line-{user_suffix}.pdf"
    dest_path = upload_to_dropbox(pdf_bytes, filename)
    _DIAG["image_saved"] += 1
    _DIAG["last_saved_file"] = filename
    mark_seen(message_id)  # 保存成功→以後の再送はスキップ
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=f"保存しました\n{filename}\n→ {dest_path}")],
            )
        )


@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image(event: MessageEvent):
    _DIAG["image_handler_calls"] += 1
    message_id = event.message.id
    if already_seen(message_id):  # LINE 再送の重複なら二重保存しない
        return
    pdf_bytes = image_to_pdf_with_text_layer(_fetch_content(message_id))
    _save_pdf_and_reply(event, message_id, pdf_bytes)


@handler.add(MessageEvent, message=FileMessageContent)
def handle_file(event: MessageEvent):
    """写真を『ファイル』として送っても保存できるようにする（PDF/HEIC/JPEG等）。"""
    _DIAG["image_handler_calls"] += 1
    message_id = event.message.id
    if already_seen(message_id):
        return
    pdf_bytes = _content_to_pdf(_fetch_content(message_id))
    _save_pdf_and_reply(event, message_id, pdf_bytes)
