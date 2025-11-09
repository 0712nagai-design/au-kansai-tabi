# -*- coding: utf-8 -*-
import os
import re
import sys
import json
import time
import logging
import unicodedata
from collections import defaultdict, deque
from datetime import datetime

from flask import Flask, request, abort

# LINE SDK
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, ImageSendMessage
)

# OpenAI v1
from openai import OpenAI

# =========================
# 環境変数 & 初期化
# =========================
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

if not LINE_CHANNEL_SECRET or not LINE_CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("LINE の環境変数が未設定です（LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN）")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY が未設定です")

client = OpenAI(api_key=OPENAI_API_KEY)

app = Flask(__name__)
app.logger.setLevel(logging.INFO)

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# Renderのgunicornは複数ワーカーを立てるとインメモリ状態が分離されます。
# かならず Procfile を `--workers 1` にしてください（下に例あり）。

# =========================
# ユーザー状態（インメモリ）
# ※Render再起動で消えます。スケールしたい場合は Redis/DB を使ってください。
# =========================
MAX_TURNS = 20
users = defaultdict(lambda: {
    "step": 0,
    "answers": {},            # QAの値を保存
    "history": deque(maxlen=MAX_TURNS)  # OpenAIへ渡す文脈（systemは都度付与）
})

RESTART_WORDS = {"start", "restart", "reset", "最初から", "やり直す", "スタート"}

ALLOWED_IMG_DOMAINS = (
    "images.unsplash.com",
    "upload.wikimedia.org",
    "www.japan-guide.com",
    "japan-guide.com",
    "placehold.co",
)

# =========================
# 質問定義（0〜10）
# =========================
Q = [
    {   # 0 言語
        "key": "lang",
        "title": "どちらの言語でご案内しますか？",
        "options": ["日本語（Japanese）", "English（英語）"],
        "multi": False
    },
    {   # 1 地域
        "key": "region",
        "title": "地域を選んでください。（複数選択可）",
        "options": ["京都", "大阪", "奈良", "神戸", "滋賀", "和歌山"],
        "multi": True
    },
    {   # 2 出発日
        "key": "date",
        "title": "出発日を YYYY-MM-DD で入力してください（例：2025-03-20）",
        "options": None,
        "multi": "free"
    },
    {   # 3 日程
        "key": "days",
        "title": "日程を選んでください。",
        "options": ["日帰り", "1泊2日", "2泊3日", "3泊以上"],
        "multi": False
    },
    {   # 4 テーマ
        "key": "theme",
        "title": "テーマを選んでください。（複数選択可）",
        "options": ["グルメ", "歴史文化", "自然癒し", "夜景", "温泉", "家族", "ショッピング", "体験メイン", "その他"],
        "multi": True
    },
    {   # 5 予算
        "key": "budget",
        "title": "予算（1人）を選んでください。",
        "options": ["~¥5,000", "~¥10,000", "~¥20,000", "¥30,000以上"],
        "multi": False
    },
    {   # 6 ホテルタイプ
        "key": "hotel",
        "title": "ホテルタイプを選んでください。",
        "options": ["高級", "中価格", "コスパ", "和風旅館", "こだわらない"],
        "multi": False
    },
    {   # 7 交通手段
        "key": "transport",
        "title": "交通手段を選んでください。（複数選択可）",
        "options": ["公共交通", "車", "徒歩中心", "指定なし"],
        "multi": True
    },
    {   # 8 同行者
        "key": "party",
        "title": "同行者を選んでください。",
        "options": ["ひとり", "カップル", "友人", "家族", "外国人友人", "その他"],
        "multi": False
    },
    {   # 9 出発時間帯
        "key": "depart",
        "title": "出発時間帯を選んでください。",
        "options": ["6–8時", "9–11時", "12–14時", "15–17時", "18時以降"],
        "multi": False
    },
    {   # 10 帰着時間帯
        "key": "return",
        "title": "帰着時間帯を選んでください。",
        "options": ["14–17時", "17–19時", "19–21時", "21時以降", "未定"],
        "multi": False
    },
]

WELCOME_JA = (
    "🔄 最初から\n"
    "こんにちは！私はAI旅ナビ関西です🧭\n"
    "どちらの言語でご案内しますか？\n"
    "1️⃣ 日本語（Japanese）\n"
    "2️⃣ English（英語）"
)

WELCOME_EN = (
    "🔄 Restart\n"
    "Hi! I'm AI Travel Navi Kansai 🧭\n"
    "Choose your language:\n"
    "1️⃣ Japanese\n"
    "2️⃣ English"
)

# =========================
# 文字正規化（数字入力の揺れ対策）
# =========================
def normalize_indices_text(s: str) -> str:
    # 全角 -> 半角 + 絵文字の数字を除去して数字と , - のみ残す
    s = unicodedata.normalize("NFKC", s)
    # 1️⃣ のようなキートップは数字だけ残るようにフィルタ
    buf = []
    for ch in s:
        if ch.isdigit() or ch in ",-":
            buf.append(ch)
    s = "".join(buf)
    s = s.replace("，", ",")
    return s

# =========================
# 質問の整形
# =========================
def render_question(step: int) -> str:
    q = Q[step]
    title = q["title"]
    opts = q["options"]

    tail = "\n\n↩️ 最初から"
    if opts is None:  # 自由入力
        return f"{title}{tail}"

    # 番号付きリスト
    lines = [title]
    for i, o in enumerate(opts, 1):
        lines.append(f"{i}️⃣ {o}")
    return "\n".join(lines) + tail

# =========================
# 入力のパース
# =========================
def parse_answer(text: str, step: int):
    q = Q[step]
    raw = (text or "").strip()

    # リスタート
    if raw.lower() in RESTART_WORDS or raw in RESTART_WORDS:
        return "__RESTART__"

    # 日付
    if q["multi"] == "free":
        try:
            _ = datetime.strptime(raw, "%Y-%m-%d")
            return raw
        except Exception:
            return None

    # 番号選択
    norm = normalize_indices_text(raw)
    if not norm:
        return None

    try:
        if q["multi"]:
            idxs = [int(x) for x in norm.split(",") if x]
            if not idxs:
                return None
            if min(idxs) < 1 or max(idxs) > len(q["options"]):
                return None
            return [q["options"][i-1] for i in idxs]
        else:
            i = int(norm)
            if 1 <= i <= len(q["options"]):
                return q["options"][i-1]
            return None
    except Exception:
        return None

# =========================
# プロンプト読み込み
# =========================
def load_system_prompt() -> str:
    default_prompt = (
        "あなたは「AI旅ナビ関西（AI Travel Navi Kansai）」です。"
        "関西（京都・大阪・奈良・神戸・滋賀・和歌山）の観光・体験・グルメ・宿泊に精通した"
        "プロの旅行コンシェルジュとして、次のルールで**LINE向けに**出力してください。"
        "1) すべての質問が完了したら即時に最終プランを **1回** だけで提示。"
        "2) 最終出力の構成は必ず『①ホテル候補(3件) → ②日程表 → ③実用ガイド(交通/食事/体験/予算/チェックリスト) → ④総評/注意点/代替案 → ⑤次の操作メニュー』の順。"
        "3) 画像は各ブロック1枚。ドメインは images.unsplash.com / upload.wikimedia.org / japan-guide.com / placehold.co のみ。"
        "   Markdown 画像の形式で構いません（例：📸\\n![説明](URL)）。"
        "4) 途中経過の『了解しました/少々お待ちください』などの中間メッセージは禁止。"
        "5) 出力は改行多めで読みやすく、絵文字を適度に使用。"
        "6) 英語モードの場合は英語表記（時間 9:00 AM / 5:30 PM, 地名は英語）。"
        "7) ホテルには必ず『公式URL』と『GoogleマップURL』を付けること。"
        "8) 画像URLは行頭の📸の直後に1行で置く。"
        "9) 文字数はLINEで読みやすいよう、冗長な説明は避けつつも情報を十分に。"
    )
    p = os.path.join(os.path.dirname(__file__), "prompt.txt")
    try:
        with open(p, "r", encoding="utf-8") as f:
            t = f.read().strip()
            return t if t else default_prompt
    except FileNotFoundError:
        return default_prompt

SYSTEM_PROMPT = load_system_prompt()

def build_user_brief(ans: dict) -> str:
    """OpenAIに渡す要約（ユーザー回答の再掲）"""
    return json.dumps(ans, ensure_ascii=False)

# =========================
# 画像URL抽出 → LINE ImageMessage で送信
# （Markdown画像をLINEで確実に表示させるため）
# =========================
IMG_MD_RE = re.compile(r"!\[[^\]]*\]\((https?://[^\s)]+)\)")
URL_RE = re.compile(r"(https?://[^\s)]+)")

def extract_image_urls(text: str) -> list[str]:
    urls = []

    # Markdown画像
    for m in IMG_MD_RE.finditer(text):
        urls.append(m.group(1))

    # 予備：素のURL行からも拾う
    for m in URL_RE.finditer(text):
        u = m.group(1)
        if any(dom in u for dom in ALLOWED_IMG_DOMAINS):
            urls.append(u)

    # 重複を順序保持で排除
    seen = set()
    out = []
    for u in urls:
        if any(dom in u for dom in ALLOWED_IMG_DOMAINS):
            if u not in seen:
                seen.add(u)
                out.append(u)
    return out[:10]  # 送信は最大10枚

def reply_with_text_and_images(reply_token: str, text: str):
    """長文テキストを分割送信し、続けて画像を個別バブルで送信"""
    try:
        MAX = 4900
        chunks = [text[i:i+MAX] for i in range(0, len(text), MAX)] or [""]
        line_bot_api.reply_message(reply_token, [TextSendMessage(text=c) for c in chunks])
    except LineBotApiError:
        app.logger.exception("LineBotApiError while replying text")
        return

    # 画像は別 push にする（reply の 1 回で同時送信すると制限に引っかかるため）
    try:
        urls = extract_image_urls(text)
        if not urls:
            return
        time.sleep(0.8)  # 軽いディレイ（レート負荷低減）
        msgs = [ImageSendMessage(original_content_url=u, preview_image_url=u) for u in urls]
        # 5件ずつに分割して push（LINEは一度に5件程度が安全）
        for i in range(0, len(msgs), 5):
            line_bot_api.push_message(
                users_last_to[reply_token],  # 後述の map でユーザーIDを取得
                msgs[i:i+5]
            )
            time.sleep(0.6)
    except Exception:
        app.logger.exception("send images failed")

# reply_token -> user_id を覚えておく（画像 push 用）
users_last_to = {}

# =========================
# ルーティング
# =========================
@app.get("/")
def health():
    return "ok", 200

@app.get("/healthz")
def healthz():
    return "ok", 200

@app.get("/py")
def py():
    return sys.version, 200

@app.post("/callback")
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    app.logger.info(f"[LINE] body={body[:1000]}...")
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        app.logger.exception("Invalid signature")
        abort(400)
    except Exception:
        app.logger.exception("Webhook handle error")
        abort(500)
    return "OK", 200

# =========================
# メッセージハンドラ
# =========================
@handler.add(MessageEvent, message=TextMessage)
def on_text(event: MessageEvent):
    uid = event.source.user_id
    txt = (event.message.text or "").strip()
    users_last_to[event.reply_token] = uid  # 画像pushの宛先

    # リスタート
    if txt.lower() in RESTART_WORDS or txt in RESTART_WORDS:
        users.pop(uid, None)
        _reply(event.reply_token, WELCOME_JA)
        return

    # 初回は言語選択
    if uid not in users or users[uid]["step"] == 0:
        users[uid]["step"] = 0
        _reply(event.reply_token, WELCOME_JA)
        # 次の入力で0番の回答を取りに行く
        users[uid]["step"] = 0
        return

    step = users[uid]["step"]
    ans = parse_answer(txt, step)

    # 無効回答
    if ans is None:
        _reply(event.reply_token, "入力形式が正しくありません。もう一度番号でお答えください。\n\n" + render_question(step))
        return

    # リスタート指示
    if ans == "__RESTART__":
        users.pop(uid, None)
        _reply(event.reply_token, WELCOME_JA)
        return

    # 保存
    users[uid]["answers"][Q[step]["key"]] = ans
    step += 1
    users[uid]["step"] = step

    # まだ質問が残っている
    if step < len(Q):
        _reply(event.reply_token, render_question(step))
        return

    # ===== 全質問揃った → OpenAI に依頼して最終プランを生成 =====
    answers = users[uid]["answers"].copy()
    lang = "ja" if answers.get("lang") in ["日本語（Japanese）", "1", 1] else "en"

    sys_prompt = SYSTEM_PROMPT
    brief = build_user_brief(answers)

    try:
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content":
                ( "以下の回答に基づき、指示に従って**最終プランを1回で**提示してください。\n"
                  "回答: " + brief )
            },
        ]
        # すでに残っている簡易履歴（任意）
        hist = list(users[uid]["history"])
        messages = [{"role":"system","content":sys_prompt}] + hist + [{"role":"user","content": "回答: " + brief}]

        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.6,
            messages=messages,
        )
        out = completion.choices[0].message.content
    except Exception as e:
        app.logger.exception("OpenAI API error")
        _reply(event.reply_token, "サーバ側で一時的なエラーが発生しました。少し時間をおいて再試行してください。\n(debug: %s)" % type(e).__name__)
        return

    # 返信（長文は自動分割）+ 画像を別送
    reply_with_text_and_images(event.reply_token, out)

    # 次回は冒頭からに戻す（連投防止）
    users.pop(uid, None)

# 短文返信（分割なし）
def _reply(reply_token: str, text: str):
    try:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=text))
    except LineBotApiError:
        app.logger.exception("reply failed")


# =========================
# ローカル起動
# =========================
if __name__ == "__main__":
    # Procfileでgunicornを起動する本番とは別。ローカル検証用。
    logging.getLogger().setLevel(logging.INFO)
    logging.info(f"Running Python: {sys.version}")
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
