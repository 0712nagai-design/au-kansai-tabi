# -*- coding: utf-8 -*-
import os, sys, time, re, logging, random
from collections import defaultdict, deque
from typing import Deque, Dict, List, Tuple

from flask import Flask, request, abort

# LINE SDK
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    ImageSendMessage, FlexSendMessage
)

# OpenAI v1
from openai import OpenAI
from openai._exceptions import RateLimitError, APIError, APITimeoutError

# =========================
# 必須環境変数
# =========================
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

if not LINE_CHANNEL_SECRET or not LINE_CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("LINEの環境変数が未設定（LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN）")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY が未設定です")

# =========================
# 基本セットアップ
# =========================
app = Flask(__name__)
app.logger.setLevel(logging.INFO)

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
oai = OpenAI(api_key=OPENAI_API_KEY)

# =========================
# プロンプト ホットリロード
# =========================
PROMPT_PATH = os.path.join(os.path.dirname(__file__), "prompt.txt")
_prompt_cache_text = None
_prompt_cache_mtime = 0.0

DEFAULT_PROMPT = (
    "あなたは「AI旅ナビ関西（AI Travel Navi Kansai）」です。"
    "関西（京都・大阪・奈良・神戸・滋賀・和歌山）の旅行プランに精通したプロの旅行コンシェルジュとして、"
    "番号選択式の質問を1問ずつ出し、すべての回答が揃ったら即座に最終プランを1回で提示してください。"
    "最終出力は必ず 1)ホテル候補3件 2)日程表 3)実用ガイド 4)総評・注意点・代替案 5)次の操作メニュー を含める。"
    "禁止：途中の中間メッセージ（了解/少々お待ちください等）、分割出力、Markdownリンク画像。"
    "画像は各ブロック1枚、許可ドメインは【japan-guide.com / upload.wikimedia.org / images.unsplash.com】のみ。"
    "各質問の下には常に『🔄 最初から』を表示。英語/日本語は最初に選ばれた言語で統一。"
    "ユーザーが『最初から/やり直す/restart/reset/start/スタート』と言ったらリセットして言語選択から再開。"
    "LINEで読みやすい改行と絵文字を適度に使う。"
)

def load_system_prompt() -> str:
    global _prompt_cache_text, _prompt_cache_mtime
    try:
        st = os.stat(PROMPT_PATH)
        if st.st_mtime != _prompt_cache_mtime:
            with open(PROMPT_PATH, "r", encoding="utf-8") as f:
                txt = f.read().strip() or DEFAULT_PROMPT
            _prompt_cache_text = txt
            _prompt_cache_mtime = st.st_mtime
            app.logger.info("[PROMPT] reloaded")
    except FileNotFoundError:
        _prompt_cache_text = DEFAULT_PROMPT
        _prompt_cache_mtime = 0.0
    return _prompt_cache_text

# =========================
# 簡易会話状態
# =========================
MAX_TURNS = 20
conversations: Dict[str, Deque[dict]] = defaultdict(lambda: deque(maxlen=MAX_TURNS))
last_user_text: Dict[str, Tuple[str, float]] = {}
last_ai_text:   Dict[str, Tuple[str, float]] = {}

RESTART_WORDS = {"start", "restart", "reset", "スタート", "最初から", "やり直す"}
START_MSG = (
    "🔄 最初から\n"
    "こんにちは！私はAI旅ナビ関西です🧭\n"
    "どちらの言語でご案内しますか？\n"
    "1️⃣ 日本語（Japanese）\n"
    "2️⃣ English（英語）"
)

# =========================
# 画像検出 & 送信ユーティリティ
# =========================
IMG_ALLOW = (
    r"https://(?:www\.)?japan-guide\.com/[^)\s]+",
    r"https://upload\.wikimedia\.org/[^)\s]+",
    r"https://images\.unsplash\.com/[^)\s]+",
    r"https://placehold\.co/[^)\s]+",
)
IMG_MD_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
PLAIN_URL_RE = re.compile(r"(https?://[^\s)]+)")

def extract_image_urls(text: str) -> List[str]:
    urls: List[str] = []
    for m in IMG_MD_RE.finditer(text):
        urls.append(m.group(1))
    # 念のためプレーンURLも拾う
    for m in PLAIN_URL_RE.finditer(text):
        url = m.group(1)
        if any(re.match(pat, url) for pat in IMG_ALLOW):
            if url not in urls:
                urls.append(url)
    # 上限（LINEは大量の画像を嫌う）を5枚に制限
    return urls[:5]

def strip_md_images(text: str) -> str:
    # 画像Markdownを消して本文が詰まるのを防ぐ
    return IMG_MD_RE.sub("", text)

# =========================
# OpenAI 呼び出し（リトライ付）
# =========================
def call_openai(messages: List[dict], temperature=0.6, max_retry=3) -> str:
    delay = 1.2
    for i in range(max_retry):
        try:
            res = oai.chat.completions.create(
                model="gpt-4o-mini",
                temperature=temperature,
                messages=messages,
            )
            return res.choices[0].message.content
        except (RateLimitError, APITimeoutError, APIError) as e:
            app.logger.warning(f"[OpenAI retry {i+1}/{max_retry}] {type(e).__name__}")
            time.sleep(delay + random.random())
            delay *= 1.8
        except Exception as e:
            app.logger.exception("OpenAI fatal error")
            raise
    # リトライ尽きた
    raise RuntimeError("OpenAI call failed after retries")

# =========================
# ルーティング
# =========================
@app.get("/")
def root_ok(): return "ok", 200

@app.get("/healthz")
def healthz(): return "ok", 200

@app.get("/py")
def py(): return sys.version, 200

@app.post("/callback")
def callback():
    sig = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    app.logger.info(f"[LINE] body={body[:800]}...")
    try:
        handler.handle(body, sig)
    except InvalidSignatureError:
        app.logger.exception("InvalidSignatureError")
        abort(400)
    except Exception:
        app.logger.exception("Webhook handle error")
        abort(500)
    return "OK", 200

# =========================
# メッセージイベント
# =========================
@handler.add(MessageEvent, message=TextMessage)
def on_text(event: MessageEvent):
    uid = event.source.user_id
    user_text = (event.message.text or "").strip()

    # リセット
    if user_text in RESTART_WORDS or user_text.lower() in RESTART_WORDS:
        conversations.pop(uid, None)
        _reply_text(event.reply_token, START_MSG)
        last_user_text.pop(uid, None)
        last_ai_text.pop(uid, None)
        return

    # 初回は言語選択を出す（＋system積む）
    if uid not in conversations or len(conversations[uid]) == 0:
        conversations[uid].clear()
        conversations[uid].append({"role": "system", "content": load_system_prompt()})
        _reply_text(event.reply_token, START_MSG)
        # 初回入力も履歴へ
        conversations[uid].append({"role": "user", "content": user_text})
        last_user_text[uid] = (user_text, time.time())
        return

    # == ループ抑制（同一入力スパムなど） ==
    now = time.time()
    if uid in last_user_text:
        last_u, ts_u = last_user_text[uid]
        if user_text == last_u and (now - ts_u) < 2.0:
            # 2秒以内に全く同じ入力なら無視
            return
    last_user_text[uid] = (user_text, now)

    # 履歴に追記して OpenAI 呼び出し
    messages = list(conversations[uid])
    # system を最新に保つ（ホットリロード）
    messages[0] = {"role": "system", "content": load_system_prompt()}
    messages.append({"role": "user", "content": user_text})

    try:
        ai_text = call_openai(messages, temperature=0.6)
    except Exception as e:
        app.logger.exception("OpenAI API error")
        _reply_text(
            event.reply_token,
            "サーバ側で一時的なエラーが発生しました。\n少し時間をおいてからもう一度お試しください。\n(debug: OpenAIError)"
        )
        return

    # ループ検出（全く同じ応答が続く）
    if uid in last_ai_text:
        last_a, ts_a = last_ai_text[uid]
        if ai_text.strip() == last_a.strip():
            ai_text += "\n\n（続きへ進めるには番号で回答してください。🔄 最初から でリセットできます）"
    last_ai_text[uid] = (ai_text, now)

    # 画像URL抽出 → ImageSendMessage / Flex に分離送信
    img_urls = extract_image_urls(ai_text)
    text_only = strip_md_images(ai_text).strip()

    # 返信（テキスト→画像の順に）
    _reply_text(event.reply_token, text_only or " ")
    if img_urls:
        _push_images(uid, img_urls)

    # 履歴にAI発話を追加（次ターンの文脈用）
    conversations[uid].append({"role": "assistant", "content": ai_text})

# =========================
# 送信ユーティリティ
# =========================
def _reply_text(reply_token: str, text: str) -> None:
    try:
        MAX = 4900
        chunks = [text[i:i+MAX] for i in range(0, len(text), MAX)] or [""]
        line_bot_api.reply_message(reply_token, [TextSendMessage(text=c) for c in chunks])
    except LineBotApiError:
        app.logger.exception("LineBotApiError(reply)")

def _push_images(user_id: str, urls: List[str]) -> None:
    # 画像は「プッシュ」で送る（リプライの上限や順序問題を避けるため）
    try:
        msgs = [ImageSendMessage(original_content_url=u, preview_image_url=u) for u in urls]
        # 5件を超えると怒られる時があるので安全側で2回に分ける
        batch = []
        for m in msgs:
            batch.append(m)
            if len(batch) == 5:
                line_bot_api.push_message(user_id, batch)
                batch = []
        if batch:
            line_bot_api.push_message(user_id, batch)
    except LineBotApiError:
        app.logger.exception("LineBotApiError(push images)")

# =========================
# ローカル起動
# =========================
if __name__ == "__main__":
    logging.getLogger().setLevel(logging.INFO)
    logging.info(f"Running Python: {sys.version}")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
