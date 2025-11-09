import os, re, logging, sys, time
from typing import List, Tuple, Deque, Dict
from collections import deque
from urllib.parse import urlparse, urlunparse, urlencode, parse_qsl

from flask import Flask, request, abort

# LINE SDK
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageSendMessage

# OpenAI v1
from openai import OpenAI
from openai import RateLimitError, APIError, APITimeoutError


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
        "写真は 1ブロック1枚まで。写真の行は Markdown 画像（例: ![説明](https://...)）で書く。"
        "ユーザーが番号で回答したら次の質問へ進み、最後は即座に最終出力。"
        "返答は日本語で簡潔に。"
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
# 軽量メモリ履歴 & 重複除去
# =========================
# Renderの1プロセス内のみ（永続ではありません）
USER_HISTORY: Dict[str, Deque[Dict]] = {}        # userId -> deque([...]) 直近10 turn
PROCESSED_MESSAGE_IDS: Deque[str] = deque(maxlen=500)  # 重複 webhook 対策

def push_history(user_id: str, role: str, content: str, max_len: int = 10):
    dq = USER_HISTORY.setdefault(user_id, deque(maxlen=max_len))
    dq.append({"role": role, "content": content})

def build_messages(user_id: str, user_text: str):
    dq = USER_HISTORY.get(user_id) or deque()
    # システム
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    # 直近履歴
    msgs.extend(list(dq))
    # 今回のユーザー発話
    msgs.append({"role": "user", "content": user_text})
    return msgs


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
    try:
        u = urlparse(url)
        if u.scheme != "https":
            return url
        if u.netloc == "images.unsplash.com":
            qs = dict(parse_qsl(u.query))
            qs.setdefault("w", "1000")
            u = u._replace(query=urlencode(qs))
            return urlunparse(u)
        return url
    except Exception:
        return url

def extract_images_and_clean(text: str) -> Tuple[str, List[str]]:
    urls = []

    def repl(m: re.Match) -> str:
        url = _tune_for_line_image(m.group(1).strip())
        urls.append(url)
        # 本文側はURL表記だけ残す（保険）
        return f"（画像: {url}）"

    body = IMG_MD.sub(repl, text)
    filtered = []
    for u in urls:
        host = urlparse(u).netloc.lower()
        if any(host.endswith(h) for h in ALLOW_HOSTS):
            filtered.append(u)
    return body, filtered


# =========================
# LINE メッセージ構築
# =========================
MAX_REPLY_MSGS = 5
MAX_TEXT_LEN   = 4800

def build_line_messages(full_text: str) -> List:
    body, img_urls = extract_images_and_clean(full_text)
    text_parts = [body[i:i+MAX_TEXT_LEN] for i in range(0, len(body), MAX_TEXT_LEN)] or [""]
    text_msgs = [TextSendMessage(text=t) for t in text_parts]
    img_msgs = []
    for u in img_urls[:2]:
        if urlparse(u).scheme == "https":
            img_msgs.append(ImageSendMessage(original_content_url=u, preview_image_url=u))

    while len(text_msgs) + len(img_msgs) > MAX_REPLY_MSGS:
        if img_msgs:
            img_msgs.pop()
        else:
            text_msgs = text_msgs[:MAX_REPLY_MSGS]
            break
    return text_msgs + img_msgs


# =========================
# 便利な前処理（番号の意図を明確化）
# =========================
def normalize_shortcuts(t: str) -> str:
    s = t.strip()
    if s in {"1", "１"}:
        return "1を選択"
    if s in {"2", "２"}:
        return "2を選択"
    if s.lower() in {"start", "スタート"}:
        return "開始"
    return s


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
# 会話ハンドラ（重複除去・履歴・リトライ）
# =========================
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event: MessageEvent):
    user_text = normalize_shortcuts(event.message.text or "")
    user_id = getattr(event.source, "user_id", "unknown")
    msg_id = getattr(event.message, "id", None)

    # 重複Webhook除去（同じ message.id は無視）
    if msg_id and msg_id in PROCESSED_MESSAGE_IDS:
        app.logger.info(f"Duplicate message ignored: {msg_id}")
        return
    if msg_id:
        PROCESSED_MESSAGE_IDS.append(msg_id)

    # Restart
    if user_text.lower() in {"restart", "reset"} or "最初から" in user_text or "やり直す" in user_text:
        USER_HISTORY.pop(user_id, None)
        reply = (
            "最初からやり直します🔄\n"
            "こんにちは！私はAI旅ナビ関西です🧭\n"
            "どちらの言語でご案内しますか？\n"
            "1️⃣ 日本語（Japanese）\n"
            "2️⃣ English（英語）"
        )
        line_bot_api.reply_message(event.reply_token, [TextSendMessage(text=reply)])
        return

    # OpenAI 呼び出し（リトライ 2回、指数バックオフ）
    messages = build_messages(user_id, user_text)
    last_err = None
    content = None
    for i in range(3):  # 試行 最大3回
        try:
            completion = client.chat.completions.create(
                model="gpt-4o-mini",
                temperature=0.7,
                timeout=25,  # 秒
                messages=messages,
            )
            content = (completion.choices[0].message.content or "").strip()
            break
        except (RateLimitError, APITimeoutError, APIError) as e:
            last_err = e
            app.logger.warning(f"OpenAI transient error (try {i+1}): {type(e).__name__}")
            time.sleep(1.5 * (i + 1))
        except Exception as e:
            last_err = e
            app.logger.exception("OpenAI hard error")
            break

    if not content:
        content = (
            "サーバ側で一時的なエラーが発生しました。\n"
            "少し時間をおいて再度お試しください。\n"
            f"(debug: {type(last_err).__name__ if last_err else 'UnknownError'})"
        )
        # 失敗でも履歴は進めない
    else:
        # 履歴更新（直近10ターン）
        push_history(user_id, "user", user_text)
        push_history(user_id, "assistant", content)

    # 返信（5通以内）
    try:
        msgs = build_line_messages(content)
        line_bot_api.reply_message(event.reply_token, msgs)
    except LineBotApiError:
        app.logger.exception("LineBotApiError while replying")
