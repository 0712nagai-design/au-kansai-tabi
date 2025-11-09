import os, re, logging, sys
from typing import List, Tuple
from urllib.parse import urlparse, urlunparse, urlencode, parse_qsl

from flask import Flask, request, abort

# LINE SDK
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageSendMessage

# OpenAI v1
from openai import OpenAI


# =========================
# 環境変数
# =========================
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

if not LINE_CHANNEL_SECRET or not LINE_CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("LINEの環境変数が未設定です（LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN）")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY が未設定です")

client = OpenAI(api_key=OPENAI_API_KEY)

# Flask
app = Flask(__name__)
app.logger.setLevel(logging.INFO)

# LINE
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)


# =========================
# プロンプト読込
# =========================
def load_system_prompt() -> str:
    default_prompt = (
        "あなたは「AI旅ナビ関西（AI Travel Navi Kansai）」です。"
        "関西（京都・大阪・奈良・神戸・滋賀・和歌山）の旅行プランを、"
        "選択式の質問→全回答後に最終プラン（ホテル3件/日程/実用ガイド/総評/操作メニュー）を"
        "一度で提示します。禁止：進行中の中間メッセージ、分割出力、Markdownのリンク画像。"
        "写真は 1ブロック1枚まで。写真の行は Markdown 画像（例: ![説明](https://...)）で書いてください。"
        "文章は日本語で簡潔に。"
    )
    path = os.path.join(os.path.dirname(__file__), "prompt.txt")
    try:
        with open(path, "r", encoding="utf-8") as f:
            t = f.read().strip()
            return t if t else default_prompt
    except FileNotFoundError:
        return default_prompt

SYSTEM_PROMPT = load_system_prompt()


# =========================
# ヘルスチェック
# =========================
@app.get("/")
def root():
    return "ok", 200

@app.get("/healthz")
def healthz():
    return "ok", 200

@app.get("/py")
def py_version():
    return sys.version, 200


# =========================
# Webhook
# =========================
@app.post("/callback")
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    app.logger.info(f"[Webhook body] {body[:800]}...")

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        app.logger.exception("Invalid signature")
        abort(400)
    except Exception:
        app.logger.exception("Webhook unhandled error")
        abort(500)
    return "OK", 200


# =========================
# 画像URL抽出 & 整形
# =========================
IMG_MD = re.compile(r"!\[[^\]]*\]\((https?://[^\s)]+)\)")

ALLOW_HOSTS = {
    "upload.wikimedia.org",
    "images.unsplash.com",
    "www.japan-guide.com",
    "placehold.co",
}

def _tune_for_line_image(url: str) -> str:
    """
    LINEが取りにいけるように、サイズを抑えるための微調整。
    ・HTTPS必須
    ・画像直リンク（拡張子は必須ではないが、content-type が image/* になること）
    ・Unsplashは w パラメータで縮小
    """
    try:
        u = urlparse(url)
        if u.scheme != "https":
            return url  # https でないと失敗するので、そのまま返し→後で弾く

        if u.netloc == "images.unsplash.com":
            qs = dict(parse_qsl(u.query))
            # なるべく軽く。幅 1000px 程度
            qs.setdefault("w", "1000")
            u = u._replace(query=urlencode(qs))
            return urlunparse(u)

        return url
    except Exception:
        return url

def extract_images_and_clean(text: str) -> Tuple[str, List[str]]:
    """
    本文から Markdown 画像行を抜き出し、本文からは削除。
    返り値: (画像行を除いた本文, 画像URLリスト)
    """
    urls = []

    def repl(m: re.Match) -> str:
        url = m.group(1).strip()
        tuned = _tune_for_line_image(url)
        urls.append(tuned)
        # 本文側にはURLだけ残す（保険）
        return f"（画像: {tuned}）"

    body = IMG_MD.sub(repl, text)

    # ホスト制限（不許可ドメインは捨てる）
    filtered = []
    for u in urls:
        host = urlparse(u).netloc.lower()
        if any(host.endswith(h) for h in ALLOW_HOSTS):
            filtered.append(u)
    return body, filtered


# =========================
# LINE メッセージ構築
# =========================
MAX_REPLY_MSGS = 5         # LINE 仕様
MAX_TEXT_LEN   = 4800      # 安全マージン

def build_line_messages(full_text: str) -> List:
    """
    OpenAIの応答文字列から、Text と Image を組み合わせて
    1返信あたり5通以内に収めて返す。
    """
    # 画像抽出
    body, img_urls = extract_images_and_clean(full_text)

    # テキストは必要に応じ分割
    texts = [body[i:i+MAX_TEXT_LEN] for i in range(0, len(body), MAX_TEXT_LEN)]
    text_msgs = [TextSendMessage(text=t) for t in texts]

    # 画像は最大2枚だけ（返信上限を超えないように）
    img_msgs = []
    for u in img_urls[:2]:
        if urlparse(u).scheme == "https":
            img_msgs.append(ImageSendMessage(original_content_url=u, preview_image_url=u))

    # 上限調整：テキストが多いときは画像をさらに絞る
    while len(text_msgs) + len(img_msgs) > MAX_REPLY_MSGS:
        if img_msgs:
            img_msgs.pop()
        else:
            # それでも超えるなら最後のテキストを切り詰める
            text_msgs = text_msgs[:MAX_REPLY_MSGS]
            break

    return text_msgs + img_msgs


# =========================
# 会話ハンドラ
# =========================
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event: MessageEvent):
    user_text = (event.message.text or "").strip()

    # Restart
    if user_text.lower() in {"restart", "reset"} or "最初から" in user_text or "やり直す" in user_text:
        reply = (
            "最初からやり直します🔄\n"
            "こんにちは！私はAI旅ナビ関西です🧭\n"
            "どちらの言語でご案内しますか？\n"
            "1️⃣ 日本語（Japanese）\n"
            "2️⃣ English（英語）"
        )
        line_bot_api.reply_message(event.reply_token, [TextSendMessage(text=reply)])
        return

    # OpenAI へ
    try:
        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.7,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_text},
            ],
        )
        content = completion.choices[0].message.content or "（応答なし）"
    except Exception as e:
        app.logger.exception("OpenAI API error")
        content = (
            "サーバ側でエラーが発生しました。\n"
            "少し時間をおいて再度お試しください。\n"
            f"(debug: {type(e).__name__})"
        )

    # LINE 返信（5通以内に整形）
    try:
        msgs = build_line_messages(content)
        line_bot_api.reply_message(event.reply_token, msgs)
    except LineBotApiError:
        app.logger.exception("LineBotApiError while replying")


# =========================
# ローカル起動
# =========================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
