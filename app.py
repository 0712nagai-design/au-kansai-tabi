import os
import logging
from flask import Flask, request, abort

# LINE SDK
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

# OpenAI v1 SDK（aiohttpは使いません）
from openai import OpenAI

# -----------------------------
# 設定
# -----------------------------
# 必須の環境変数（Render > Settings > Environment で設定）
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# ここでエラーにしておくと、足りない変数にすぐ気づけます
if not LINE_CHANNEL_SECRET or not LINE_CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("LINEの環境変数が未設定です（LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN）")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY が未設定です")

# OpenAIクライアント
client = OpenAI(api_key=OPENAI_API_KEY)

# Flask
app = Flask(__name__)
app.logger.setLevel(logging.INFO)

# LINE ハンドラ
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)


# -----------------------------
# 補助：プロンプト読み込み
# -----------------------------
def load_system_prompt() -> str:
    """
    ルート直下の prompt.txt をUTF-8で読み込み。
    置いていない場合は安全な既定文を返す。
    """
    default_prompt = (
        "あなたは「AI旅ナビ関西（AI Travel Navi Kansai）」です。"
        "関西（京都・大阪・奈良・神戸・滋賀・和歌山）の旅行プランを、"
        "選択式の質問→最終プラン（ホテル3件/日程/実用ガイド/総評/操作メニュー）で一度に提示します。"
        "禁止：進行中の中間メッセージ（了解/少々お待ちください等）、画像のMarkdownリンク、分割出力。"
        "出力は日本語で簡潔かつフレンドリーに。"
    )
    path = os.path.join(os.path.dirname(__file__), "prompt.txt")
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read().strip()
            # 空ファイル対策
            return text if text else default_prompt
    except FileNotFoundError:
        return default_prompt


SYSTEM_PROMPT = load_system_prompt()


# -----------------------------
# ルーティング
# -----------------------------
@app.route("/", methods=["GET"])
def health():
    # Renderのヘルスチェック・自分確認用
    return "ok", 200


@app.route("/callback", methods=["POST"])
def callback():
    # LINE署名の検証
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
def handle_text_message(event: MessageEvent):
    user_text = (event.message.text or "").strip()

    # Restart キーワード（LINEで「最初から」「restart」など）
    if user_text.lower() in {"restart", "reset"} or "最初から" in user_text or "やり直す" in user_text:
        reply = (
            "最初からやり直します🔄\n"
            "こんにちは！私はAI旅ナビ関西です🧭\n"
            "まず、どちらの言語でご案内しますか？\n"
            "1️⃣ 日本語（Japanese）\n"
            "2️⃣ English（英語）"
        )
        _safe_reply(event.reply_token, reply)
        return

    # OpenAIへ問い合わせ（v1 chat.completions）
    try:
        completion = client.chat.completions.create(
            model="gpt-4o-mini",   # 利用可能なモデルに合わせて変更可
            temperature=0.7,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_text},
            ],
        )
        reply_text = completion.choices[0].message.content
    except Exception as e:
        app.logger.exception("OpenAI API error")
        reply_text = (
            "サーバ側でエラーが発生しました。\n"
            "しばらくしてからもう一度お試しください。\n"
            f"(debug: {type(e).__name__})"
        )

    _safe_reply(event.reply_token, reply_text)


# -----------------------------
# LINE返信（安全ラッパ）
# -----------------------------
def _safe_reply(reply_token: str, text: str) -> None:
    try:
        # LINEの1メッセージ上限に合わせ、長過ぎる時は分割（安全策）
        MAX = 4900  # 実運用は5000未満推奨
        chunks = [text[i:i + MAX] for i in range(0, len(text), MAX)] or [""]
        messages = [TextSendMessage(text=c) for c in chunks]
        line_bot_api.reply_message(reply_token, messages)
    except LineBotApiError:
        app.logger.exception("LineBotApiError while replying")


# -----------------------------
# ローカル実行用
# -----------------------------
if __name__ == "__main__":
    # RenderではProcfileでgunicornが起動するので、ここはローカルテスト用
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
import sys
from flask import Flask

# 既存の app = Flask(__name__) の直後あたりに追加
@app.get("/py")
def py_version():
    return sys.version, 200

@app.get("/healthz")
def healthz():
    return "ok", 200

# 起動時にログにも出す
import logging
logging.getLogger().setLevel(logging.INFO)
logging.info(f"Running Python: {sys.version}")

