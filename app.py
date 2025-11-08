# app.py — LINE接続の切り分け用・堅牢ミニマム
import os
import json
import logging
from flask import Flask, request, abort

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = app.logger

# --- 環境変数チェック（足りなければ起動時に落として原因を明確化）
CHANNEL_SECRET = os.getenv("CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN")
if not CHANNEL_SECRET:
    raise RuntimeError("環境変数 CHANNEL_SECRET が未設定です")
if not CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("環境変数 CHANNEL_ACCESS_TOKEN が未設定です")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

@app.get("/")
def health():
    # 値は出さないが「設定の有無」は見えるようにする
    flags = {
        "CHANNEL_SECRET_set": bool(CHANNEL_SECRET),
        "CHANNEL_ACCESS_TOKEN_set": bool(CHANNEL_ACCESS_TOKEN),
    }
    return {"status": "ok", "env": flags}, 200

@app.post("/callback")
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    # 受信イベントはログに（PII回避のため先頭だけ）
    logger.info("Headers: %s", {k: request.headers.get(k) for k in ["X-Line-Signature", "Content-Type"]})
    logger.info("Body head: %s", body[:400])

    # 署名検証 → 失敗なら 400
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logger.error("Invalid signature (CHANNEL_SECRET が違う可能性)")
        abort(400)
    except Exception as e:
        logger.exception("Unexpected error in handler.handle: %s", e)
        abort(500)

    return "OK", 200

@handler.add(MessageEvent, message=TextMessage)
def on_text(event: MessageEvent):
    text = (event.message.text or "").strip()
    reply_text = f"受け取りました：{text}"

    # reply_token は一回しか使えないので try で保護
    try:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply_text)
        )
    except LineBotApiError as e:
        # 代表的な落ち方をログに出す（Invalid reply token 等）
        logger.exception("LineBotApiError: %s", e)
    except Exception as e:
        logger.exception("Unexpected error while reply_message: %s", e)

# Render の本番は gunicorn が起動、下はローカル実行用
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))


