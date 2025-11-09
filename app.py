# -*- coding: utf-8 -*-
import os, re, sys, json, logging
from collections import defaultdict, deque
from datetime import datetime
from typing import Optional, Dict, Any, List

from flask import Flask, request, abort

# LINE SDK
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, ImageSendMessage
)

# OpenAI v1
from openai import OpenAI

# ====== 環境変数 ======
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

if not LINE_CHANNEL_SECRET or not LINE_CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("LINEの環境変数が未設定です（LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN）")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY が未設定です")

client = OpenAI(api_key=OPENAI_API_KEY)

# ====== Flask / LINE 準備 ======
app = Flask(__name__)
app.logger.setLevel(logging.INFO)
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ====== 会話状態（インメモリ） ======
MAX_TURNS = 30
State = Dict[str, Any]
users: Dict[str, State] = defaultdict(dict)
last_to: Dict[str, str] = {}  # reply_token -> user_id（画像push宛先用）

# 再起動ワード
RESTART = {"start", "restart", "reset", "スタート", "最初から", "やり直す"}

# ====== 質問定義 ======
REGIONS = {1: "京都", 2: "大阪", 3: "奈良", 4: "神戸", 5: "滋賀", 6: "和歌山"}
THEMES = {
    1: "グルメ", 2: "歴史文化", 3: "自然癒し", 4: "夜景",
    5: "温泉", 6: "家族", 7: "ショッピング", 8: "体験メイン", 9: "その他"
}
BUDGETS = {1: "~¥5,000", 2: "~¥10,000", 3: "~¥20,000", 4: "¥30,000以上"}
HOTELS = {1: "高級", 2: "中価格", 3: "コスパ", 4: "和風旅館", 5: "こだわらない"}
TRANSPORT = {1: "公共交通", 2: "車", 3: "徒歩中心", 4: "指定なし"}
COMPANION = {1: "ひとり", 2: "カップル", 3: "友人", 4: "家族", 5: "外国人友人", 6: "その他"}
DEPT = {1: "6–8時", 2: "9–11時", 3: "12–14時", 4: "15–17時", 5: "18時以降"}
ARRV = {1: "14–17時", 2: "17–19時", 3: "19–21時", 4: "21時以降", 5: "未定"}

Q = [
    {"key": "lang", "title": "どちらの言語でご案内しますか？",
     "choices": {1: "日本語", 2: "English"}, "multi": False},
    {"key": "region", "title": "地域を教えてください。（複数選択可）",
     "choices": REGIONS, "multi": True},
    {"key": "date", "title": "出発日を YYYY-MM-DD で入力してください（例：2025-03-20）",
     "choices": {}, "multi": False},
    {"key": "stay", "title": "日程を選択してください。",
     "choices": {1: "日帰り", 2: "1泊2日", 3: "2泊3日", 4: "3泊以上"}, "multi": False},
    {"key": "theme", "title": "テーマを選んでください。（複数選択可）",
     "choices": THEMES, "multi": True},
    {"key": "budget", "title": "予算（1人）を選んでください。",
     "choices": BUDGETS, "multi": False},
    {"key": "hotel", "title": "ホテルタイプを選んでください。",
     "choices": HOTELS, "multi": False},
    {"key": "transport", "title": "交通手段を選んでください。（複数選択可）",
     "choices": TRANSPORT, "multi": True},
    {"key": "companion", "title": "同行者を選んでください。",
     "choices": COMPANION, "multi": False},
    {"key": "dept", "title": "出発時間帯を選んでください。",
     "choices": DEPT, "multi": False},
    {"key": "arrv", "title": "帰着時間帯はどのくらいを予定されていますか？",
     "choices": ARRV, "multi": False},
]

WELCOME = (
    "🔄 最初から\n"
    "こんにちは！私はAI旅ナビ関西です🧭\n"
    "どちらの言語でご案内しますか？\n"
    "1️⃣ 日本語（Japanese）\n"
    "2️⃣ English（英語）\n"
    "（再開するには 1 または 2 を送ってください）"
)

# ====== ルーティング ======
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
    try:
        handler.handle(body, sig)
    except InvalidSignatureError:
        abort(400)
    return "OK", 200

# ====== ユーティリティ ======
def _render_question(idx: int) -> str:
    q = Q[idx]
    title = q["title"]
    if q["choices"]:
        lines = [title]
        for n, label in q["choices"].items():
            lines.append(f"{n}\u20E3 {label}")  # 1⃣ 風
    else:
        lines = [title]
    lines.append("\n🔁 最初から")
    return "\n".join(lines)

def _parse_numbers(s: str) -> Optional[List[int]]:
    if not s: return None
    s = s.replace("，", ",").replace("・", ",").replace(" ", "")
    if not re.fullmatch(r"[0-9,]+", s): return None
    try:
        nums = [int(x) for x in s.split(",") if x]
        return nums if nums else None
    except Exception:
        return None

def _validate_and_store(uid: str, step: int, text: str) -> bool:
    """有効なら users[uid]['answers'] に保存して True を返す。無効なら False。"""
    state = users[uid]
    q = Q[step]
    key = q["key"]

    # 言語
    if key == "lang":
        nums = _parse_numbers(text)
        if nums and len(nums) == 1 and nums[0] in (1, 2):
            state["answers"][key] = "ja" if nums[0] == 1 else "en"
            return True
        return False

    # 地域（複数）
    if key == "region":
        nums = _parse_numbers(text)
        if not nums: return False
        bad = [n for n in nums if n not in REGIONS]
        if bad: return False
        state["answers"][key] = [REGIONS[n] for n in sorted(set(nums))]
        return True

    # 日付
    if key == "date":
        try:
            datetime.strptime(text.strip(), "%Y-%m-%d")
            state["answers"][key] = text.strip()
            return True
        except Exception:
            return False

    # 以降は共通（単一 or 複数）
    nums = _parse_numbers(text)
    if not nums: return False

    if q["multi"]:
        bad = [n for n in nums if n not in q["choices"]]
        if bad: return False
        state["answers"][key] = [q["choices"][n] for n in sorted(set(nums))]
        return True
    else:
        if len(nums) != 1 or nums[0] not in q["choices"]:
            return False
        state["answers"][key] = q["choices"][nums[0]]
        return True

def _answers_brief(a: Dict[str, Any]) -> str:
    return json.dumps(a, ensure_ascii=False, indent=2)

SYSTEM_PROMPT = (
    "You are AI Travel Navi Kansai.\n"
    "必ずユーザーの選択（JSON）に厳密に従い、**選ばれていない地域は一切行程に含めない**こと。\n"
    "質問はすべて完了済み。いまから最終出力を**一度に**返す。\n"
    "出力構成：1)ホテル候補3件 2)日程表 3)実用ガイド 4)総評・注意点・代替案 5)次の操作メニュー。\n"
    "画像は各ブロック1枚、許可ドメインは `https://www.japan-guide.com` / `https://upload.wikimedia.org` / "
    "`https://images.unsplash.com` / 不明時は `https://placehold.co/800x500.png?text=施設名`。\n"
    "GoogleマップURLは `https://www.google.com/maps/search/キーワード` 形式。\n"
    "日本語モードでは日本語、英語モードでは英語で出力。分割禁止、中間文言禁止。"
)
# --- 詳細行程テンプレ（強制ルール） ---
ITINERARY_RULES = r"""
【🗓️ 日程テンプレ（厳守）】
- 各日、**少なくとも6ブロック**を必ず入れる（観光3+体験1+食事2 以上を目安）。
- 1ブロック = 時刻・カテゴリ・名称・短評・所要・アクセス・📸画像・リンク・営業時間。
- 各ブロックの区切りは必ず「──────────────────────────────」。
- 時間は60–90分滞在・移動30分を基準に、**9:00開始 / 17:30前後に主要観光終了**を目安。
- **各ブロックに画像1枚必須**。画像URLは下記ドメインのみ：
  https://www.japan-guide.com / https://upload.wikimedia.org / https://images.unsplash.com
  無ければ： https://placehold.co/800x500.png?text={施設名}
- 「昼食」「夕食」は**エリア＋ジャンル記法**（店名は出さない）。
- 営業/拝観時間・休はできるだけ入れる。不明なら「🕰 公式情報なし（要確認）」。
- 雨天時の代替（屋内）を**各日1つ**提案する。

【ブロック例（厳密フォーマット）】
🕘 9:00　🏯 観光：清水寺（東山）
木造舞台から望む京都市街が絶景。朝は比較的空き、撮影に最適。
🕒 所要：約90分　🚶‍♀️アクセス：市バス「五条坂」徒歩10分
📸
![清水寺](https://www.japan-guide.com/g18/3901_top.jpg)
🔗 公式：https://www.kiyomizudera.or.jp/
📍 Googleマップ：https://www.google.com/maps/search/清水寺+京都
🕰 拝観 6:00–18:00（季節変動）／ 休：無休
──────────────────────────────
"""

def _count_blocks(text: str) -> int:
    # 区切り線の数＋時刻記号の有無で粗くブロック数を推定
    return text.count("──────────────────────────────") + (1 if "🕘" in text or "🕒" in text else 0)

def _needs_more_detail(text: str) -> bool:
    # 12ブロック未満なら“薄い”とみなして再生成
    return _count_blocks(text) < 12

def _build_final_prompt(answers: Dict[str, Any]) -> str:
    lang = answers.get("lang", "ja")
    locale_hint = "Japanese output." if lang == "ja" else "English output."
    return f"""
{locale_hint}
あなたは「AI旅ナビ関西」です。以下の利用者条件に基づき、濃密な最終旅行プランを1回で出力します。
回答JSON:
{json.dumps(answers, ensure_ascii=False, indent=2)}

出力は必ずこの順で構成：
1️⃣ ホテル候補3件（画像・URL付き）
2️⃣ 日程表（1日目〜最終日、各日6ブロック以上、下記テンプレ厳守）
3️⃣ 実用ガイド（交通・食事・体験・予算・持ち物）
4️⃣ 総評・注意点・代替案（雨天代替含む）
5️⃣ 次の操作メニュー

{ITINERARY_RULES}

禁止事項：
- 分割出力、途中で止まる文言
- Markdown画像リンク以外の形式
- 同じスポットの重複
"""

import re
import json

# --- 必要日数を answers から推定 ---
def _required_days(answers: dict) -> int:
    """
    answers['schedule'] が:
      1=日帰り, 2=1泊2日, 3=2泊3日, 4=3泊以上（最低4日分で作らせる）
    の想定。文字列/数値どちらでも耐性あり。
    """
    raw = str(answers.get("schedule", "")).strip()
    mapping = {
        "1": 1, "日帰り": 1, "daytrip": 1,
        "2": 2, "1泊2日": 2,
        "3": 3, "2泊3日": 3,
        "4": 4, "3泊以上": 4,
    }
    return mapping.get(raw, 2)  # 不明なら2日想定で保守的に

# --- 出力本文に含まれる“日付見出し”のカウント ---
DAY_JP_RE = re.compile(r"(?:\*\*|\*|^)\s*([第]?\s*\d+\s*日目)\b", re.MULTILINE)
DAY_EN_RE = re.compile(r"(?:^|\n)\s*🗓️?\s*Day\s*(\d+)\b", re.IGNORECASE)

def _count_days_in_text(text: str) -> int:
    n1 = len(set(m.group(1) for m in DAY_JP_RE.finditer(text)))
    n2 = len(set(m.group(1) for m in DAY_EN_RE.finditer(text)))
    return max(n1, n2)

# --- 生成プロンプトを組み立て（あなたの既存の SYSTEM_PROMPT を活かす） ---
def _build_final_prompt(answers: dict, required_days: int) -> list[dict]:
    lang = answers.get("lang", "ja")
    locale_hint = "Japanese output." if str(lang).lower() in {"ja", "1", "japanese"} else "English output."

    # ここはあなたの answers_brief など既存の要約関数があればそれを使ってOK
    brief = json.dumps(answers, ensure_ascii=False)

    extra_rules = (
        f"必須要件：**日程表はちょうど {required_days} 日分**を出力すること。"
        "各日には【朝・午前・昼・午後・夕方・夜】の少なくとも4ブロックを入れ、"
        "各ブロックに《開始時間／所要時間／移動手段》を明記。"
        "もし回答が不足していても、推定で埋めて構いません。"
    )

    return [
        {"role": "system", "content": SYSTEM_PROMPT + "\n" + locale_hint + "\n" + extra_rules},
        {"role": "user", "content": "以下の回答に基づき、最適な旅プランを一回で提示してください。\n回答JSON:\n" + brief}
    ]

# --- OpenAI呼び出し（足りない日数なら自動で再生成） ---
def _call_openai_plan(answers: dict) -> str:
    need_days = _required_days(answers)

    # 1回目生成
    messages = _build_final_prompt(answers, need_days)
    res = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.6,
        messages=messages,
    )
    text = res.choices[0].message.content or ""

    # 日数チェック
    have_days = _count_days_in_text(text)
    if have_days >= need_days:
        return text

    # 2回目：修正依頼（不足日数を明示）
    missing = need_days - have_days
    fix_messages = messages + [
        {
            "role": "user",
            "content": (
                "直前の出力では日程が不足しています。"
                f"**あと {missing} 日分**を追加し、合計で **{need_days} 日分**にしてください。"
                "全体を自然に時系列でつなげ、すでに出した日も含めて**最初から最後まで通しの1回出力**で再提示してください。"
                "各日には【朝・午前・昼・午後・夕方・夜】のブロックを入れ、開始時間・所要時間・移動手段を必ず記載。"
            )
        }
    ]
    res2 = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.5,
        messages=fix_messages,
    )
    text2 = res2.choices[0].message.content or ""
    # 念のためもう一度カウントして、まだ足りなければ2→3回目…に広げてもOK。ここでは2回で終了。
    return text2



IMG_URL_RE = re.compile(r"https?://(?:www\.)?(?:japan-guide\.com|upload\.wikimedia\.org|images\.unsplash\.com|placehold\.co)/[^\s)]+", re.I)

def _detect_image_urls(text: str, limit=5) -> List[str]:
    urls = []
    for m in IMG_URL_RE.finditer(text):
        urls.append(m.group(0))
        if len(urls) >= limit: break
    return urls

def _split_long_text(text: str, maxlen=4900) -> List[str]:
    if len(text) <= maxlen: return [text]
    parts, buf = [], []
    count = 0
    for line in text.splitlines(True):
        if count + len(line) > maxlen:
            parts.append("".join(buf))
            buf, count = [line], len(line)
        else:
            buf.append(line); count += len(line)
    if buf: parts.append("".join(buf))
    return parts

def _reply_text(reply_token: str, text: str):
    chunks = _split_long_text(text)
    msgs = [TextSendMessage(text=c) for c in chunks]
    line_bot_api.reply_message(reply_token, msgs)

def _push_images(uid: str, urls: List[str]):
    for u in urls:
        try:
            line_bot_api.push_message(uid, ImageSendMessage(original_content_url=u, preview_image_url=u))
        except LineBotApiError:
            app.logger.exception("Image push failed: %s", u)

# ====== メインハンドラ ======
@handler.add(MessageEvent, message=TextMessage)
def on_message(event: MessageEvent):
    uid = event.source.user_id
    text = (event.message.text or "").strip()
    last_to[event.reply_token] = uid

    # ─ リスタート ─
    if text.lower() in RESTART or text in RESTART:
        users.pop(uid, None)
        _reply_text(event.reply_token, WELCOME)
        return

    # ─ 初期化 ─
    if uid not in users or not users[uid]:
        users[uid] = {"step": 0, "answers": {}, "hist": deque(maxlen=MAX_TURNS)}

    state = users[uid]
    step = state["step"]

    # ─ 現在ステップに対して入力を検証 ─
    valid = _validate_and_store(uid, step, text)
    if not valid:
        # 無効入力：現在の質問を再表示
        _reply_text(event.reply_token, _render_question(step))
        return

    # ─ 有効なら次へ進める ─
    step += 1
    state["step"] = step

    # ✅ ここが修正ポイント：
    # 言語選択後（step==1）は必ず地域質問に進むため、
    # 次の入力を待たずに質問を送信
    if step == 1:
        _reply_text(event.reply_token, _render_question(step))
        return

    # ─ まだ質問が残っている場合 ─
    if step < len(Q):
        _reply_text(event.reply_token, _render_question(step))
        return

    # ─ 全質問終了 → OpenAI で最終プラン作成 ─
    answers = state["answers"].copy()
    try:
        plan = _call_openai_plan(answers)
    except Exception as e:
        app.logger.exception("OpenAI API error")
        _reply_text(
            event.reply_token,
            f"サーバ側で一時的なエラーが発生しました。\n(debug: {type(e).__name__})",
        )
        return

    _reply_text(event.reply_token, plan)
    imgs = _detect_image_urls(plan, limit=5)
    if imgs:
        _push_images(uid, imgs)

    users.pop(uid, None)

  

# ====== ローカル実行 ======
if __name__ == "__main__":
    logging.getLogger().setLevel(logging.INFO)
    logging.info(f"Running Python: {sys.version}")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)



