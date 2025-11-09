# -*- coding: utf-8 -*-
import os, re, sys, json, logging
from collections import defaultdict, deque
from datetime import datetime
from typing import Optional, Dict, Any, List

from flask import Flask, request, abort

# LINE SDK
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageSendMessage

# OpenAI v1
from openai import OpenAI

# ====================== 環境変数 ======================
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

if not LINE_CHANNEL_SECRET or not LINE_CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("LINEの環境変数が未設定です（LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN）")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY が未設定です")

client = OpenAI(api_key=OPENAI_API_KEY)

# ====================== Flask / LINE ======================
app = Flask(__name__)
app.logger.setLevel(logging.INFO)
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ====================== 会話状態 ======================
MAX_TURNS = 30
State = Dict[str, Any]
users: Dict[str, State] = defaultdict(dict)

RESTART = {"start", "restart", "reset", "スタート", "最初から", "やり直す"}

# ====================== 質問定義 ======================
REGIONS = {1: "京都", 2: "大阪", 3: "奈良", 4: "神戸", 5: "滋賀", 6: "和歌山"}
THEMES = {
    1: "グルメ", 2: "歴史文化", 3: "自然癒し", 4: "夜景",
    5: "温泉", 6: "家族", 7: "ショッピング", 8: "体験メイン", 9: "その他"
}
BUDGETS = {1: "~¥5,000", 2: "~¥10,000", 3: "~¥20,000", 4: "¥30,000以上"}
HOTELS  = {1: "高級", 2: "中価格", 3: "コスパ", 4: "和風旅館", 5: "こだわらない"}
TRANSPORT = {1: "公共交通", 2: "車", 3: "徒歩中心", 4: "指定なし"}
COMPANION = {1: "ひとり", 2: "カップル", 3: "友人", 4: "家族", 5: "外国人友人", 6: "その他"}
DEPT = {1: "6–8時", 2: "9–11時", 3: "12–14時", 4: "15–17時", 5: "18時以降"}
ARRV = {1: "14–17時", 2: "17–19時", 3: "19–21時", 4: "21時以降", 5: "未定"}

Q = [
    {"key": "lang", "title": "どちらの言語でご案内しますか？", "choices": {1: "日本語", 2: "English"}, "multi": False},
    {"key": "region", "title": "地域を教えてください。（複数選択可）", "choices": REGIONS, "multi": True},
    {"key": "date", "title": "出発日を YYYY-MM-DD で入力してください（例：2025-03-20）", "choices": {}, "multi": False},
    {"key": "stay", "title": "日程を選択してください。", "choices": {1: "日帰り", 2: "1泊2日", 3: "2泊3日", 4: "3泊以上"}, "multi": False},
    {"key": "theme", "title": "テーマを選んでください。（複数選択可）", "choices": THEMES, "multi": True},
    {"key": "budget", "title": "予算（1人）を選んでください。", "choices": BUDGETS, "multi": False},
    {"key": "hotel", "title": "ホテルタイプを選んでください。", "choices": HOTELS, "multi": False},
    {"key": "transport", "title": "交通手段を選んでください。（複数選択可）", "choices": TRANSPORT, "multi": True},
    {"key": "companion", "title": "同行者を選んでください。", "choices": COMPANION, "multi": False},
    {"key": "dept", "title": "出発時間帯を選んでください。", "choices": DEPT, "multi": False},
    {"key": "arrv", "title": "帰着時間帯はどのくらいを予定されていますか？", "choices": ARRV, "multi": False},
]

WELCOME = (
    "🔄 最初から\n"
    "こんにちは！私はAI旅ナビ関西です🧭\n"
    "どちらの言語でご案内しますか？\n"
    "1️⃣ 日本語（Japanese）\n"
    "2️⃣ English（英語）"
)

# ====================== ルーティング ======================
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

# ====================== ユーティリティ ======================
def _render_question(idx: int) -> str:
    q = Q[idx]
    lines = [q["title"]]
    if q["choices"]:
        for n, label in q["choices"].items():
            lines.append(f"{n}\u20E3 {label}")  # 1⃣ の見た目
    lines.append("🔁 最初から")
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
    if "answers" not in state: state["answers"] = {}

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

    # 共通（単一/複数）
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

def answers_brief(a: Dict[str, Any]) -> str:
    """回答の短い要約（キー名はこの実装のものに合わせる）"""
    def pick(v, default="未選択"):
        if v is None or v == "": return default
        if isinstance(v, list): return "、".join(map(str, v)) if v else default
        return str(v)

    return (
        f"- 地域：{pick(a.get('region'))}\n"
        f"- 出発日：{pick(a.get('date'))}\n"
        f"- 日程：{pick(a.get('stay'))}\n"
        f"- テーマ：{pick(a.get('theme'))}\n"
        f"- 予算：{pick(a.get('budget'))}\n"
        f"- ホテルタイプ：{pick(a.get('hotel'))}\n"
        f"- 交通手段：{pick(a.get('transport'))}\n"
        f"- 同行者：{pick(a.get('companion'))}\n"
        f"- 出発時間帯：{pick(a.get('dept'))}\n"
        f"- 帰着時間帯：{pick(a.get('arrv'))}\n"
    )

def _count_days_in_text(text: str) -> int:
    a = len(re.findall(r"\*\*\s*\d+日目", text))
    b = len(re.findall(r"Day\s*\d+", text, flags=re.I))
    return max(a, b)

def _required_days(answers: dict) -> int:
    """stay から必要日数を返す（最低2日）"""
    stay = str(answers.get("stay", "2"))
    table = {"日帰り": 1, "1泊2日": 2, "2泊3日": 3, "3泊以上": 3}
    d = table.get(stay, 2)
    return max(d, 2)

# ---------- 生成プロンプト ----------
def build_final_prompt(answers: Dict[str, Any]) -> str:
    lang = answers.get("lang", "ja")
    locale_hint = "Japanese output." if lang == "ja" else "English output."
    brief = answers_brief(answers)

    return f"""
{locale_hint}
あなたは「AI旅ナビ関西」です。以下の利用者条件に基づき、**実用性の高い旅プラン**を1回で出力します。

回答JSON:
{json.dumps(answers, ensure_ascii=False, indent=2)}
回答サマリ:
{brief}

出力はこの順で**一度に**:
1️⃣ ホテル候補3件（名称・特徴・価格目安・公式URL・Googleマップ検索URL・写真1枚）
2️⃣ 日程表（出発〜帰着まで、**各日6ブロック以上**）
3️⃣ 実用ガイド（交通 / 食事おすすめ3+3〈店名必須〉/ 体験予約〈施設名と料金必須〉/ 予算 / チェックリスト）
4️⃣ 総評・注意点・代替案
5️⃣ 次の操作メニュー

【ITINERARY_RULES】
- 各ブロックは**固有名詞を必須**（例：東大寺、春日大社、ならまち、海遊館、白浜温泉 等）
- 見出し：`🕘 9:00–10:30　🏯 観光：春日大社（奈良公園）`
- 本文：見どころ/体験内容/小さなコツ（2–3行）
- アクセス（公共交通・徒歩中心／所要）
- 所要：60–90分基準、移動は30分刻み
- 画像：**japan-guide / upload.wikimedia.org / images.unsplash.com / placehold.co** のいずれか1枚  
  書式：
  📸
  ![説明](https://…)
- 公式サイトURLとGoogleマップ検索URL（生URL）
- 営業/拝観時間・休（分かる範囲）
- 雨天代替を**各日1件**
- 1泊2日なら **Day1 / Day2** を必ず出力。泊数に応じて日数分。
- 区切りは `──────────────────────────────`

【FOOD / EXPERIENCE / BUDGET / IMAGE / LINK ルール】は先述に従う。
日本語モードは日本語、英語モードは英語で一貫。分割禁止・中間メッセージ禁止。
"""

# ---------- OpenAI 呼び出し ----------
def _call_openai_plan(answers: dict) -> str:
    sys_policy = "Follow the user's instructions exactly and produce a single, complete itinerary."
    user_prompt = build_final_prompt(answers)

    res = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.6,
        messages=[
            {"role": "system", "content": sys_policy},
            {"role": "user", "content": user_prompt},
        ],
    )
    text = res.choices[0].message.content or ""

    need = _required_days(answers)
    got = _count_days_in_text(text)
    if got < need:
        text += f"\n\n（補足）現在 {got} 日分です。{need} 日分になるよう続きも含めて出力してください。"
    return text

# ---------- 画像検出・送信 ----------
IMG_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:japan-guide\.com|upload\.wikimedia\.org|images\.unsplash\.com|placehold\.co)/[^\s)]+",
    re.I,
)

def _detect_image_urls(text: str, limit=5) -> List[str]:
    urls = []
    for m in IMG_URL_RE.finditer(text):
        urls.append(m.group(0))
        if len(urls) >= limit:
            break
    return urls

def _split_long_text(text: str, maxlen=4900) -> List[str]:
    if len(text) <= maxlen:
        return [text]
    parts, buf, count = [], [], 0
    for line in text.splitlines(True):
        if count + len(line) > maxlen:
            parts.append("".join(buf))
            buf, count = [line], len(line)
        else:
            buf.append(line); count += len(line)
    if buf:
        parts.append("".join(buf))
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

# ====================== メインハンドラ ======================
@handler.add(MessageEvent, message=TextMessage)
def on_message(event: MessageEvent):
    uid = event.source.user_id
    text = (event.message.text or "").strip()

    # リスタート
    if text in RESTART or text.lower() in RESTART:
        users.pop(uid, None)
        _reply_text(event.reply_token, WELCOME)
        return

    # 初期化
    if uid not in users or not users[uid]:
        users[uid] = {"step": 0, "answers": {}, "hist": deque(maxlen=MAX_TURNS)}
        _reply_text(event.reply_token, _render_question(0))
        return

    state = users[uid]
    step = state["step"]

    # 現在ステップの入力検証
    if not _validate_and_store(uid, step, text):
        _reply_text(event.reply_token, _render_question(step))
        return

    # 次のステップへ
    step += 1
    state["step"] = step

    # まだ質問が残っている
    if step < len(Q):
        _reply_text(event.reply_token, _render_question(step))
        return

    # 全質問終了 → 生成
    answers = state["answers"].copy()
    try:
        plan = _call_openai_plan(answers)
    except Exception as e:
        app.logger.exception("OpenAI API error")
        _reply_text(event.reply_token, f"サーバ側で一時的なエラーが発生しました。\n(debug: {type(e).__name__})")
        return

    _reply_text(event.reply_token, plan)
    imgs = _detect_image_urls(plan, limit=5)
    if imgs:
        _push_images(uid, imgs)

    # セッション終了
    users.pop(uid, None)

# ====================== ローカル実行 ======================
if __name__ == "__main__":
    logging.getLogger().setLevel(logging.INFO)
    logging.info(f"Running Python: {sys.version}")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
