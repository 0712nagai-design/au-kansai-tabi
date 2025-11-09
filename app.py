import os
import sys
import logging
from collections import defaultdict, deque
from flask import Flask, request, abort

# LINE SDK
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageSendMessage
import re

# OpenAI v1 SDK
from openai import OpenAI

# -----------------------------
# 環境変数
# -----------------------------
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

if not LINE_CHANNEL_SECRET or not LINE_CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("LINEの環境変数が未設定です（LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN）")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY が未設定です")

# OpenAI / Flask / LINE 準備
client = OpenAI(api_key=OPENAI_API_KEY)

app = Flask(__name__)
app.logger.setLevel(logging.INFO)

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# -----------------------------
# システムプロンプト
# -----------------------------
def load_system_prompt() -> str:
    default_prompt = (
        "あなたは「AI旅ナビ関西（AI Travel Navi Kansai）」です。"
        "関西（京都・大阪・奈良・神戸・滋賀・和歌山）の旅行プランに精通したプロの旅行コンシェルジュとして、"
        "ユーザーに選択式の質問を1問ずつ出し、すべての回答が揃ったら即座に最終プランを1回で提示してください。"
        "最終出力には必ず 1)ホテル候補3件 2)日程表 3)実用ガイド 4)総評・注意点・代替案 5)次の操作メニュー を含めます。"
        "禁止事項：進行中の中間メッセージ（了解/少々お待ちください等）、画像のMarkdownリンク、分割出力。"
        "画像は各ブロック1枚、許可ドメイン（japan-guide / upload.wikimedia.org / images.unsplash.com）のみ。"
        "質問は一問ずつ番号選択式、各質問の下に常に『🔄 最初から』ボタン表現を付与。"
        "英語モードと日本語モードは選択後に統一。"
        "ユーザーが『最初から/やり直す/restart/reset/start/スタート』と言ったら、全回答を破棄して言語選択からやり直す。"
        "出力はLINEで読みやすい改行・絵文字・囲み記号を適度に用いる。"
    )
    path = os.path.join(os.path.dirname(__file__), "prompt.txt")
    try:
        with open(path, "r", encoding="utf-8") as f:
            txt = f.read().strip()
            return txt if txt else default_prompt
    except FileNotFoundError:
        return default_prompt

SYSTEM_PROMPT = load_system_prompt()

# -----------------------------
# 会話状態（簡易インメモリ）
# Render は再起動することがあるため永続ではありません。
# 安定運用するなら Redis/SQLite への置き換えを検討。
# -----------------------------
MAX_TURNS = 20  # 直近20ターンを保持
conversations: dict[str, deque] = defaultdict(lambda: deque(maxlen=MAX_TURNS))

RESTART_WORDS = {"start", "restart", "reset", "スタート", "最初から", "やり直す"}

START_MSG = (
    "🔄 最初から\n"
    "こんにちは！私はAI旅ナビ関西です🧭\n"
    "どちらの言語でご案内しますか？\n"
    "1️⃣ 日本語（Japanese）\n"
    "2️⃣ English（英語）"
)

# -----------------------------
# ルーティング
# -----------------------------
@app.get("/")
def root_ok():
    return "ok", 200

@app.get("/healthz")
def healthz():
    return "ok", 200

@app.get("/py")
def py_version():
    return sys.version, 200

@app.route("/callback", methods=["POST"])
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
        app.logger.exception("Unhandled error while handling webhook")
        abort(500)
    return "OK", 200

# -----------------------------
# メッセージイベント
# -----------------------------
@handler.add(MessageEvent, message=TextMessage)
def on_message(event: MessageEvent):
    user_id = event.source.user_id
    user_text = (event.message.text or "").strip()

    # リセット系
    if user_text.lower() in RESTART_WORDS or user_text in RESTART_WORDS:
        conversations.pop(user_id, None)  # 履歴クリア
        _safe_reply(event.reply_token, START_MSG)
        return

    # 初回ユーザーは言語選択から
    if user_id not in conversations or len(conversations[user_id]) == 0:
        _safe_reply(event.reply_token, START_MSG)
        # 初回は履歴に system だけ積んでおく
        conversations[user_id].clear()
        conversations[user_id].append({"role": "system", "content": SYSTEM_PROMPT})
        # ユーザー発話も履歴化（以降の文脈用）
        conversations[user_id].append({"role": "user", "content": user_text})
        return

    # 既に会話中：履歴にユーザー発話を追加
    conversations[user_id].append({"role": "user", "content": user_text})

    # OpenAI へ：system を先頭に、当該ユーザーの履歴を丸ごと送る
    messages = list(conversations[user_id])
    if messages[0].get("role") != "system":
        messages.insert(0, {"role": "system", "content": SYSTEM_PROMPT})

    try:
        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.6,
            messages=messages,
        )
        reply = completion.choices[0].message.content
    except Exception as e:
        app.logger.exception("OpenAI API error")
        reply = (
            "サーバ側で一時的なエラーが発生しました。\n"
            "少し時間をおいてからもう一度お試しください。\n"
            "（debug: " + type(e).__name__ + "）"
        )

    # 返信＆履歴に AI 応答を追記
    _safe_reply(event.reply_token, reply)
    conversations[user_id].append({"role": "assistant", "content": reply})

# -----------------------------
# LINE 返信（自動分割）
# -----------------------------
def _safe_reply(reply_token: str, text: str) -> None:
    try:
        MAX = 4900  # LINEの1メッセージ上限に安全マージン
        chunks = [text[i:i + MAX] for i in range(0, len(text), MAX)] or [""]
        messages = [TextSendMessage(text=c) for c in chunks]
        line_bot_api.reply_message(reply_token, messages)
    except LineBotApiError:
        app.logger.exception("LineBotApiError while replying")
IMG_PAT = re.compile(r'!\[[^\]]*\]\((https?://[^\s)]+)\)')
def _safe_reply(reply_token: str, text: str) -> None:
    try:
        messages = build_line_messages_from_markdown(text)
        line_bot_api.reply_message(reply_token, messages)
    except LineBotApiError:
        app.logger.exception("LineBotApiError while replying")

ALLOWED_IMG = ("https://www.japan-guide.com",
               "https://upload.wikimedia.org",
               "https://images.unsplash.com",
               "https://placehold.co")

def build_line_messages_from_markdown(text: str):
    """
    Markdown内の画像 `![alt](url)` を検出して、
    テキストは TextSendMessage、画像は ImageSendMessage に分解する。
    返すのは LINE 送信用メッセージ配列（最大5件に収める）。
    """
    msgs = []
    pos = 0
    for m in IMG_PAT.finditer(text):
        url = m.group(1)
        # 画像の前のテキスト
        chunk = text[pos:m.start()].strip()
        if chunk:
            # 5000未満で安全に分割
            MAX = 4900
            for i in range(0, len(chunk), MAX):
                msgs.append(TextSendMessage(text=chunk[i:i+MAX]))
        # 許可ドメインのみ画像として送る（それ以外はURL文字列にして送信）
        if url.startswith(ALLOWED_IMG):
            msgs.append(ImageSendMessage(original_content_url=url, preview_image_url=url))
        else:
            msgs.append(TextSendMessage(text=f"画像URL: {url}"))
        pos = m.end()

    # 残りのテキスト
    tail = text[pos:].strip()
    if tail:
        MAX = 4900
        for i in range(0, len(tail), MAX):
            msgs.append(TextSendMessage(text=tail[i:i+MAX]))

    # LINEのreplyは一度に最大5メッセージ（公式上限）なので絞る
    return msgs[:5] if msgs else [TextSendMessage(text="")]

# -----------------------------
# ローカル実行
# -----------------------------
if __name__ == "__main__":
    logging.getLogger().setLevel(logging.INFO)
    logging.info(f"Running Python: {sys.version}")
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)


