# -*- coding: utf-8 -*-
"""
AI旅ナビ関西（LINE公式アカウント用） Flaskアプリ 完全版
- LINE webhook: /callback
- ヘルスチェック: /, /healthz, /py
- OpenAI v1 (requestsベース) を利用（aiohttpは不使用）
- セッション（会話履歴）をユーザーごとにメモリ保持（TTL付き）
- "最初から" リセット
- 画像マークダウン `![alt](URL)` を ImageSendMessage に自動変換
- 長文は安全分割（LINE上限対策）
- RateLimit/その他エラーの丁寧なフォールバック
"""

import os
import re
import time
import sys
import logging
from typing import List, Dict, Any

from flask import Flask, request, abort

# LINE SDK
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, ImageSendMessage
)

# OpenAI v1 SDK（aiohttp依存なし）
from openai import OpenAI
from openai import RateLimitError, APIConnectionError, APIError

# =========================
# 環境変数（Render > Settings > Environment）
# =========================
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

if not LINE_CHANNEL_SECRET or not LINE_CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("LINEの環境変数が未設定です（LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN）")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY が未設定です")

# =========================
# OpenAI クライアント
# =========================
client = OpenAI(api_key=OPENAI_API_KEY)

# =========================
# Flask
# =========================
app = Flask(__name__)
app.logger.setLevel(logging.INFO)
logging.getLogger().setLevel(logging.INFO)
logging.info(f"Running Python: {sys.version}")

# =========================
# LINE ハンドラ
# =========================
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# =========================
# システムプロンプト
# =========================
def load_system_prompt() -> str:
    default_prompt = (
        "あなたは「AI旅ナビ関西（AI Travel Navi Kansai）」です。"
        "関西（京都・大阪・奈良・神戸・滋賀・和歌山）の旅行プランを、"
        "選択式の質問を一問ずつ出し、全質問終了後に1回で最終プラン（ホテル3件/日程/実用ガイド/総評/操作メニュー）を提示します。"
        "禁止：進行中の中間メッセージ（了解/少々お待ちください等）、画像のMarkdownリンク、分割出力。"
        "画像は各ブロック1枚、許可ドメイン（japan-guide.com / upload.wikimedia.org / images.unsplash.com / placehold.co）のみを使用。"
        "ユーザーが「最初から」「restart」「reset」等と言ったら会話をリセットして言語選択からやり直します。"
        "常に文体は簡潔でフレンドリー、日本語モードでは日本語、英語モードでは英語のみを用います。"
    )
    try:
        here = os.path.dirname(__file__)
        with open(os.path.join(here, "prompt.txt"), "r", encoding="utf-8") as f:
            txt = f.read().strip()
            return txt or default_prompt
    except FileNotFoundError:
        return default_prompt

SYSTEM_PROMPT = load_system_prompt()

# =========================
# 会話セッション（メモリ）
# =========================
# Renderの単一コンテナ内で有効。スケールアウト時はRedis等が必要。
SESSIONS: Dict[str, Dict[str, Any]] = {}
SESSION_TTL_SEC = 60 * 60 * 2  # 2時間で自動破棄
MAX_MSG_LEN = 4900            # TextSendMessage安全分割閾値
OPENAI_MODEL = "gpt-4o-mini"  # コスパ良い軽量モデル

def now() -> float:
    return time.time()

def cleanup_sessions() -> None:
    """古いセッションを掃除"""
    cutoff = now() - SESSION_TTL_SEC
    dead_keys = [uid for uid, s in SESSIONS.items() if s.get("t", 0) < cutoff]
    for k in dead_keys:
        SESSIONS.pop(k, None)

def new_greeting() -> str:
    """最初の質問（静的に生成：安定のため）"""
    return (
        "🔄 最初から\n"
        "こんにちは！私はAI旅ナビ関西です🧭\n"
        "どちらの言語でご案内しますか？\n"
        "1️⃣ 日本語（Japanese）\n"
        "2️⃣ English（英語）"
    )

def init_session(user_id: str) -> Dict[str, Any]:
    sess = {
        "t": now(),
        "msgs": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "assistant", "content": new_greeting()},
        ],
    }
    SESSIONS[user_id] = sess
    return sess

def get_session(user_id: str) -> Dict[str, Any]:
    cleanup_sessions()
    sess = SESSIONS.get(user_id)
    if not sess:
        sess = init_session(user_id)
    return sess

def reset_session(user_id: str) -> Dict[str, Any]:
    return init_session(user_id)

# =========================
# 画像マークダウン → LINEメッセージ変換
# =========================
IMG_PAT = re.compile(r'!\[[^\]]*\]\((https?://[^\s)]+)\)')
ALLOWED_IMG_PREFIX = (
    "https://www.japan-guide.com",
    "https://upload.wikimedia.org",
    "https://images.unsplash.com",
    "https://placehold.co",
)

def build_line_messages_from_markdown(text: str) -> List[Any]:
    """
    本文中の `![alt](url)` を検出して、
    テキストはTextSendMessage、画像はImageSendMessageに変換。
    """
    msgs: List[Any] = []
    pos = 0

    def push_text(chunk: str):
        chunk = (chunk or "").strip()
        if not chunk:
            return
        for i in range(0, len(chunk), MAX_MSG_LEN):
            msgs.append(TextSendMessage(text=chunk[i:i+MAX_MSG_LEN]))

    for m in IMG_PAT.finditer(text):
        url = m.group(1)
        # 前テキスト
        push_text(text[pos:m.start()])
        # 画像（許可ドメインのみ）
        if url.startswith(ALLOWED_IMG_PREFIX):
            msgs.append(ImageSendMessage(original_content_url=url, preview_image_url=url))
        else:
            # 安全策：URLをテキストで通知
            push_text(f"📸 画像URL: {url}")
        pos = m.end()

    # 残りテキスト
    push_text(text[pos:])

    # LINEの1 replyは最大5メッセージ程度が安全
    return msgs[:5] if msgs else [TextSendMessage(text="")]

def reply_text_or_images(reply_token: str, content: str) -> None:
    """OpenAI応答を行に合わせて送信"""
    try:
        messages = build_line_messages_from_markdown(content)
        line_bot_api.reply_message(reply_token, messages)
    except LineBotApiError:
        app.logger.exception("LineBotApiError while replying")

# =========================
# ルーティング
# =========================
@app.get("/")
def index():
    return "ok", 200

@app.get("/healthz")
def healthz():
    return "ok", 200

@app.get("/py")
def py_version():
    return sys.version, 200

@app.post("/callback")
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    app.logger.info(f"[LINE Webhook] body={body[:1000]}...")

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        app.logger.exception("Invalid signature")
        abort(400)
    except Exception:
        app.logger.exception("Unhandled error")
        abort(500)

    return "OK", 200

# =========================
# メッセージイベント
# =========================
@handler.add(MessageEvent, message=TextMessage)
def on_text_message(event: MessageEvent):
    user_id = event.source.user_id if hasattr(event.source, "user_id") else "anonymous"
    user_text = (event.message.text or "").strip()
    app.logger.info(f"[MSG] {user_id=} text={user_text!r}")

    # リセットワード
    if user_text.lower() in {"restart", "reset"} or ("最初から" in user_text) or ("やり直す" in user_text):
        sess = reset_session(user_id)
        greeting = sess["msgs"][-1]["content"]
        reply_text_or_images(event.reply_token, greeting)
        return

    # セッション取得
    sess = get_session(user_id)
    sess["t"] = now()

    # 初回起動用キーワード（念のため）
    if user_text in {"スタート", "start", "開始", "ここをクリック"} and len(sess["msgs"]) <= 2:
        greeting = sess["msgs"][-1]["content"]
        reply_text_or_images(event.reply_token, greeting)
        return

    # OpenAI への問い合わせ（履歴ごと）
    msgs = sess["msgs"] + [{"role": "user", "content": user_text}]

    try:
        completion = client.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=0.7,
            messages=msgs,
        )
        assistant_text = completion.choices[0].message.content or ""
    except RateLimitError as e:
        app.logger.warning(f"OpenAI RateLimit: {e}")
        assistant_text = (
            "サーバが混み合っています。少し時間をおいてもう一度お試しください。\n"
            "(debug: RateLimitError)"
        )
    except (APIConnectionError, APIError) as e:
        app.logger.exception("OpenAI API connectivity error")
        assistant_text = (
            "接続エラーが発生しました。ネットワーク状況をご確認の上、再度お試しください。\n"
            f"(debug: {type(e).__name__})"
        )
    except Exception as e:
        app.logger.exception("OpenAI API unexpected error")
        assistant_text = (
            "サーバ側でエラーが発生しました。しばらくしてからもう一度お試しください。\n"
            f"(debug: {type(e).__name__})"
        )

    # 「🔄 最初から」を常時フッターとして付ける（重複しないよう簡易対策）
    if "最初から" not in assistant_text:
        assistant_text = f"{assistant_text}\n\n🔄 最初から"

    # 履歴を更新
    sess["msgs"] = msgs + [{"role": "assistant", "content": assistant_text}]
    # 応答
    reply_text_or_images(event.reply_token, assistant_text)

# =========================
# ローカル実行
# =========================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)




