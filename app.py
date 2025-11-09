import os, re, sys, time, logging
from collections import deque
from urllib.parse import urlparse, urlunparse, urlencode, parse_qsl
from typing import Deque, Dict, List, Tuple

from flask import Flask, request, abort

# LINE
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageSendMessage

# OpenAI v1
from openai import OpenAI
from openai import RateLimitError, APITimeoutError, APIConnectionError, APIStatusError

# ====== 環境変数 ======
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

if not LINE_CHANNEL_SECRET or not LINE_CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("LINEの環境変数が未設定です（LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN）")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY が未設定です")

# OpenAI はタイムアウトを client 生成時に付ける（.create の引数ではなく）
client = OpenAI(api_key=OPENAI_API_KEY, timeout=25)

# ====== Flask / LINE ======
app = Flask(__name__)
app.logger.setLevel(logging.INFO)
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ====== プロンプト ======
def load_system_prompt() -> str:
    default_prompt = (
        "あなたは「AI旅ナビ関西（AI Travel Navi Kansai）」です。"
        "関西（京都・大阪・奈良・神戸・滋賀・和歌山）の旅行プランを、"
        "選択式の質問→全回答後に最終プラン（ホテル3件/日程/実用ガイド/総評/操作メニュー）まで一度で提示。"
        "進行中の中間メッセージ・分割出力・Markdownのリンク画像は禁止。"
        "各ブロックの写真は1枚まで、形式は ![説明](https://...)。番号回答で次の質問に進む。日本語で返答。"
    )
    try:
        with open(os.path.join(os.path.dirname(__file__), "prompt.txt"), "r", encoding="utf-8") as f:
            t = f.read().strip()
            return t or default_prompt
    except FileNotFoundError:
        return default_prompt

SYSTEM_PROMPT = load_system_prompt()

# ====== ヘルスチェック ======
@app.get("/")
def root(): return "ok", 200
@app.get("/healthz")
def healthz(): return "ok", 200
@app.get("/py")
def py(): return sys.version, 200

# ====== 軽量メモリ & 重複対策 ======
USER_HISTORY: Dict[str, Deque[Dict]] = {}                 # userId -> 直近10ターン
PROCESSED_IDS: Deque[str] = deque(maxlen=500)             # 受信済み message.id

def push_hist(uid: str, role: str, content: str, k: int = 10):
    dq = USER_HISTORY.setdefault(uid, deque(maxlen=k)); dq.append({"role": role, "content": content})

def build_msgs(uid: str, user_text: str):
    dq = USER_HISTORY.get(uid) or deque()
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    msgs.extend(list(dq))
    msgs.append({"role": "user", "content": user_text})
    return msgs

# ====== 画像抽出（許可ドメインのみ） ======
IMG_MD = re.compile(r"!\[[^\]]*\]\((https?://[^\s)]+)\)")
ALLOW_HOSTS = {"upload.wikimedia.org", "images.unsplash.com", "www.japan-guide.com", "placehold.co"}

def _tune_url(u: str) -> str:
    try:
        p = urlparse(u)
        if p.scheme != "https": return u
        if p.netloc == "images.unsplash.com":
            qs = dict(parse_qsl(p.query)); qs.setdefault("w", "1000")
            return urlunparse(p._replace(query=urlencode(qs)))
        return u
    except Exception:
        return u

def extract_imgs_and_clean(text: str) -> Tuple[str, List[str]]:
    urls: List[str] = []
    def repl(m):
        url = _tune_url(m.group(1).strip()); urls.append(url)
        return f"（画像: {url}）"   # 本文にもURL残す（保険）
    body = IMG_MD.sub(repl, text)
    filtered = []
    for u in urls:
        host = urlparse(u).netloc.lower()
        if any(host.endswith(h) for h in ALLOW_HOSTS):
            filtered.append(u)
    return body, filtered

MAX_REPLY_MSGS, MAX_TEXT = 5, 4800
def to_line_messages(full_text: str):
    body, img_urls = extract_imgs_and_clean(full_text)
    texts = [TextSendMessage(text=body[i:i+MAX_TEXT]) for i in range(0, len(body), MAX_TEXT)] or [TextSendMessage(text="")]
    imgs = [ImageSendMessage(original_content_url=u, preview_image_url=u) for u in img_urls[:2]]
    while len(texts) + len(imgs) > MAX_REPLY_MSGS:
        if imgs: imgs.pop()
        else: texts = texts[:MAX_REPLY_MSGS]; break
    return texts + imgs

def normalize(t: str) -> str:
    s = (t or "").strip()
    if s in {"1","１"}: return "1を選択"
    if s in {"2","２"}: return "2を選択"
    if s.lower() in {"start","スタート"}: return "開始"
    return s

# ====== Webhook ======
@app.post("/callback")
def callback():
    sig = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, sig)
    except InvalidSignatureError:
        app.logger.exception("Invalid signature"); abort(400)
    except Exception:
        app.logger.exception("Webhook error"); abort(500)
    return "OK", 200

# ====== 会話ハンドラ（重複除去・履歴・リトライ） ======
@handler.add(MessageEvent, message=TextMessage)
def on_message(event: MessageEvent):
    uid = getattr(event.source, "user_id", "unknown")
    mid = getattr(event.message, "id", None)
    text = normalize(event.message.text)

    # Duplicate webhook guard
    if mid and mid in PROCESSED_IDS:
        app.logger.info(f"dup ignore: {mid}"); return
    if mid: PROCESSED_IDS.append(mid)

    # restart
    if text.lower() in {"restart", "reset"} or "最初から" in text or "やり直す" in text:
        USER_HISTORY.pop(uid, None)
        msg = ("最初からやり直します🔄\n"
               "こんにちは！私はAI旅ナビ関西です🧭\n"
               "どちらの言語でご案内しますか？\n"
               "1️⃣ 日本語（Japanese）\n"
               "2️⃣ English（英語）")
        line_bot_api.reply_message(event.reply_token, [TextSendMessage(text=msg)])
        return

    messages = build_msgs(uid, text)

    content, last_err = None, None
    for i in range(3):
        try:
            res = client.chat.completions.create(
                model="gpt-4o-mini",
                temperature=0.7,
                messages=messages,
            )
            content = (res.choices[0].message.content or "").strip()
            break
        except (RateLimitError, APITimeoutError, APIConnectionError, APIStatusError) as e:
            last_err = e; time.sleep(1.5 * (i+1))
        except Exception as e:
            last_err = e; break

    if not content:
        content = ("サーバ側で一時的なエラーが発生しました。\n"
                   "少し時間をおいて再度お試しください。\n"
                   f"(debug: {type(last_err).__name__ if last_err else 'Unknown'})")
    else:
        push_hist(uid, "user", text)
        push_hist(uid, "assistant", content)

    try:
        line_bot_api.reply_message(event.reply_token, to_line_messages(content))
    except LineBotApiError:
        app.logger.exception("LINE reply error")
