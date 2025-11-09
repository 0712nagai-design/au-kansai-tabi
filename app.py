# -*- coding: utf-8 -*-
import os, re, sys, json, logging
from collections import defaultdict, deque
from datetime import datetime
from typing import Optional, Dict, Any, List

from flask import Flask, request, abort
import re, json
from datetime import datetime
import json
from typing import Dict, Any

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
def _required_days(answers: dict) -> int:
    """日程の選択肢から、必要な日数を返す（最低2に丸め）"""
    m = str(answers.get("schedule", "1"))
    table = {"1": 1, "2": 2, "3": 3}
    d = table.get(m, 3)
    return max(d, 2)  # 1泊2日以上を前提に最低2日に

def answers_brief(answers: Dict[str, Any]) -> str:
    """ユーザー回答を短く並べる（欠損に強い安全版）"""
    def pick(key, default="未選択"):
        v = answers.get(key)
        if v is None or v == "":
            return default
        if isinstance(v, list):
            return "、".join(map(str, v)) if v else default
        return str(v)

    return (
        f"- 地域：{pick('area')}\n"
        f"- 出発日：{pick('date')}\n"
        f"- 日程：{pick('stay')}\n"
        f"- テーマ：{pick('theme')}\n"
        f"- 予算：{pick('budget')}\n"
        f"- ホテルタイプ：{pick('hotel')}\n"
        f"- 交通手段：{pick('transport')}\n"
        f"- 同行者：{pick('companion')}\n"
        f"- 出発時間帯：{pick('depart_time')}\n"
        f"- 帰着時間帯：{pick('return_time')}\n"
    )


    regions = pick_list(a.get("region", ""), region_map)
    themes  = pick_list(a.get("theme", ""), theme_map)
    traffic = pick_list(a.get("traffic", ""), traffic_map)

    s = []
    s.append(f"- 地域：{regions}")
    s.append(f"- 出発日：{a.get('date','未選択')}")
    s.append(f"- 日程：{ {'1':'日帰り','2':'1泊2日','3':'2泊3日','4':'3泊以上'}.get(str(a.get('schedule','')), '未選択') }")
    s.append(f"- テーマ：{themes}")
    s.append(f"- 予算：{ {'1':'~¥5,000','2':'~¥10,000','3':'~¥20,000','4':'¥30,000以上'}.get(str(a.get('budget','')), '未選択') }")
    s.append(f"- ホテルタイプ：{ hotel_map.get(str(a.get('hotel','')), '未選択') }")
    s.append(f"- 交通手段：{traffic}")
    s.append(f"- 同行者：{ party_map.get(str(a.get('party','')), '未選択') }")
    s.append(f"- 出発時間帯：{ dep_band_map.get(str(a.get('dep_band','')), '未選択') }")
    s.append(f"- 帰着時間帯：{ arr_band_map.get(str(a.get('arr_band','')), '未選択') }")
    return "\n".join(s)

def _count_days_in_text(text: str) -> int:
    """生成文から日数らしき見出し数を数える（不足検出に使う）"""
    a = len(re.findall(r"\*\*?\s*\d+日目", text))
    b = len(re.findall(r"🗓️?\s*Day\s*\d+", text, flags=re.IGNORECASE))
    return max(a, b)

def build_final_prompt(answers: Dict[str, Any]) -> str:
    lang = answers.get("lang", "ja")
    locale_hint = "Japanese output." if lang == "ja" else "English output."
    brief = answers_brief(answers)

    return f"""
{locale_hint}
あなたは「AI旅ナビ関西」です。以下の利用者条件に基づき、**実用性の高い旅プラン**を1回で出力します。

回答JSON（要約）:
{json.dumps(answers, ensure_ascii=False, indent=2)}
回答サマリ:
{brief}

出力は次の順で**一度に**:
1️⃣ ホテル候補3件（名称・特徴・価格目安・公式URL・Googleマップ検索URL・写真1枚）
2️⃣ 日程表（出発〜帰着まで、**各日6ブロック以上**）
3️⃣ 実用ガイド（交通 / 食事おすすめ3+3〈**店名必須**〉/ 体験予約〈**施設名と料金必須**〉/ 予算 / チェックリスト）
4️⃣ 総評・注意点・代替案
5️⃣ 次の操作メニュー

【ITINERARY_RULES】
- 各ブロックは**固有名詞を必須**（例：東大寺、春日大社、ならまち、竹林の小径、黒門市場、白浜温泉、海遊館）
- 見出し：`🕘 9:00–10:30　🏯 観光：春日大社（奈良公園）`
- 本文：見どころ/体験内容/小さなコツ（2–3行）
- アクセス（公共交通・徒歩中心／所要）
- 所要：60–90分を基準（移動は30分刻みで自然に）
- 写真：**japan-guide.com / upload.wikimedia.org / images.unsplash.com / placehold.co** のいずれか1枚  
  書式：
  📸
  ![説明](https://…)
- 公式サイトURL と Googleマップ検索URL（生URLで1行ずつ）
- 営業/拝観時間・休業日（分かる範囲）
- 雨天時代替を**各日1件**入れる（例：「※雨天時は◯◯ミュージアムへ」）
- 1泊2日なら **Day1 / Day2** を必ず出力。泊数に応じて日数分を作成。
- 各日の区切りは `──────────────────────────────`

【HOTEL_RULES】
- 3件とも：名称/特徴/価格目安/公式URL/GoogleマップURL/写真1枚（許可ドメイン）

【FOOD_RULES】
- **昼3件／夜3件**：店名必須・短評・価格帯・営業時間/定休日・公式URL・写真1枚  
  例：『和彩○○ — 和食 / ¥1,000–¥2,000 / 🕰 11:00–20:00 / 休：水』

【EXPERIENCE_RULES】
- 体験は**1件以上**：施設名・公式URL・推定料金・写真1枚。予約推奨なら明記。

【BUDGET_RULES】
- 1名あたり：宿泊 / 交通 / 食事 / 体験の小計と合計を**数値**で。

【LINK_RULES】
- URLは**生URL**のみ（Markdownリンク禁止）

【IMAGE_RULES】
- 1ブロック1枚必須。許可外ドメインは使わない。なければ
  `https://placehold.co/800x500.png?text=施設名` を使用。

【OUTPUT_STYLE】
- 改行多めで可読性重視。全ブロックを `──────────────────────────────` で区切る。
- 日本語モードは日本語、英語モードは英語で一貫。
"""


def _call_openai_plan(answers: dict) -> str:
    """OpenAI へ最終プランを生成させる"""
    sys_prompt = _build_final_prompt(answers)
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user",
         "content": "以下の回答に基づいて、上記仕様どおり**一度で完成**の旅程を提示してください。\n回答JSON:\n" +
                    json.dumps(answers, ensure_ascii=False, indent=2)}
    ]
    res = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.6,
        messages=messages,
    )
    text = res.choices[0].message.content or ""

    # 念のため、日数不足を検出したら追記指示を自動で付け足す
    need = _required_days(answers)
    got = _count_days_in_text(text)
    if got < need:
        text += f"\n\n（補足）上記は {got} 日分です。{need} 日分の体裁になるよう続きも含めて提示してください。"
    return text




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








