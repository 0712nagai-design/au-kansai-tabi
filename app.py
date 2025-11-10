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
    MessageEvent, TextMessage, TextSendMessage, ImageSendMessage,
    TemplateSendMessage, ButtonsTemplate, URITemplateAction,
    QuickReply, QuickReplyButton, MessageAction
)

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
    {"key": "region", "title": "地域を教えてください（複数選択→最後に［完了］）", "choices": REGIONS, "multi": True},
    {"key": "date", "title": "出発日を選んでください（例: 2025-03-20 を直接入力）", "choices": {}, "multi": False},
    {"key": "stay", "title": "日程を選んでください。", "choices": {1: "日帰り", 2: "1泊2日", 3: "2泊3日", 4: "3泊以上"}, "multi": False},
    {"key": "theme", "title": "テーマを選んでください（複数選択→［完了］）", "choices": THEMES, "multi": True},
    {"key": "budget", "title": "予算（1人）を選んでください。", "choices": BUDGETS, "multi": False},
    {"key": "hotel", "title": "ホテルタイプを選んでください。", "choices": HOTELS, "multi": False},
    {"key": "transport", "title": "交通手段（複数選択→［完了］）", "choices": TRANSPORT, "multi": True},
    {"key": "companion", "title": "同行者を選んでください。", "choices": COMPANION, "multi": False},
    {"key": "dept", "title": "出発時間帯を選んでください。", "choices": DEPT, "multi": False},
    {"key": "arrv", "title": "帰着時間帯はどのくらい？", "choices": ARRV, "multi": False},
]

WELCOME = (
    "🔄 最初から\n"
    "こんにちは！私はAI旅ナビ関西です🧭\n"
    "どちらの言語でご案内しますか？"
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
def _render_question_text(idx: int, state: State) -> str:
    q = Q[idx]
    chosen = state.get("pending_multi_set") if q["multi"] else None
    chosen_str = ""
    if q["multi"] and chosen:
        # すでに選択したラベルを表示
        labels = []
        for n in sorted(chosen):
            labels.append(q["choices"].get(n, str(n)))
        if labels:
            chosen_str = "\n（選択中： " + "、".join(labels) + " ）"
    return q["title"] + chosen_str

def _build_quick_reply(idx: int) -> QuickReply:
    q = Q[idx]
    items = []
    # 選択肢ボタン（最大13）
    for n, label in q.get("choices", {}).items():
        items.append(QuickReplyButton(action=MessageAction(label=f"{n} {label}", text=str(n))))
        if len(items) >= 12:  # 13枠のうち1枠は［完了］/［最初から］で使う
            break
    # 複数選択なら［完了］
    if q["multi"]:
        items.append(QuickReplyButton(action=MessageAction(label="✅ 完了", text="完了")))
    # 共通：最初から
    items.append(QuickReplyButton(action=MessageAction(label="🔁 最初から", text="最初から")))
    return QuickReply(items=items[:13])

def _send_question(reply_token: str, state: State):
    idx = state["step"]
    text = _render_question_text(idx, state)
    line_bot_api.reply_message(
        reply_token,
        TextSendMessage(text=text, quick_reply=_build_quick_reply(idx))
    )

FW_TO_HW = str.maketrans({
    "０":"0","１":"1","２":"2","３":"3","４":"4","５":"5","６":"6","７":"7","８":"8","９":"9",
    "．":".","，":",","、":",","・":",","　":" "
})

def _parse_numbers(s: str) -> Optional[List[int]]:
    if not s: return None
    s = s.translate(FW_TO_HW)
    for sep in [".", "･", "・", "、", "　", "，", " ", "/", "／"]:
        s = s.replace(sep, ",")
    s = re.sub(r",+", ",", s).strip(",")
    if not re.fullmatch(r"[0-9,]+", s):
        return None
    try:
        nums = [int(x) for x in s.split(",") if x != ""]
        return nums if nums else None
    except Exception:
        return None

def answers_brief(a: Dict[str, Any]) -> str:
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
    stay = str(answers.get("stay", "2"))
    table = {"日帰り": 1, "1泊2日": 2, "2泊3日": 3, "3泊以上": 3}
    d = table.get(stay, 2)
    return max(d, 2)

# ---------- 生成プロンプト ----------
def build_hotel_prompt(answers: Dict[str, Any]) -> str:
    answers_json = json.dumps(answers, ensure_ascii=False, indent=2)
    return f"""
以下は「ホテル候補」セクションの出力指示です。
ユーザー回答に従って、宿泊施設のみを出力してください。
極めて重要：「公式サイト：URL」および「Googleマップ：URL」の行に、実際のURLを出力してください。
【ユーザー回答(JSON参照用)】
{answers_json}

出力形式：
① 🏨 ホテル正式名称
特徴：1行要約
🔗 公式：URL
📍 Googleマップ：URL
💰 価格目安：〜円／泊
──────────────────────────────
② 🏨 ホテル正式名称
特徴：1行要約
🔗 公式：URL
📍 Googleマップ：URL
💰 価格目安：〜円／泊
──────────────────────────────
③ 🏨 ホテル正式名称
特徴：1行要約
🔗 公式：URL
📍 Googleマップ：URL
💰 価格目安：〜円／泊
──────────────────────────────
"""

def build_schedule_prompt(answers: Dict[str, Any]) -> str:
    answers_json = json.dumps(answers, ensure_ascii=False, indent=2)
    return f"""
以下は「日程表」セクションの出力指示です。
他の項目は出さず、旅程のみを生成してください。
極めて重要：「公式：URL」および「Googleマップ：URL」の行に、実際のURLを出力してください。
【ユーザー回答(JSON参照用)】
{answers_json}

厳守事項：
- 最終日には「宿泊」ブロックを入れない（チェックイン/宿泊は最終日前日まで）
- 各日6ブロック以上を目標
- 「所要」の前の絵文字は ⌛ を使用すること（例：⌛ 所要：60分）

出力例：
Day1
🕘 9:00–10:30　🏯 観光：施設名（エリア）
短評：見どころ・体験内容を2〜3行で
⌛ 所要：60〜90分　🚶アクセス：交通手段・所要
🔗 公式：URL
📍 Googleマップ：URL
🕰 営業：時間／休：定休
↓
（以降、ブロックごとに「↓」で区切る）
"""

def build_guide_prompt(answers: Dict[str, Any]) -> str:
    answers_json = json.dumps(answers, ensure_ascii=False, indent=2)
    return f"""
以下は「実用ガイド」セクションの出力指示です。
交通・食事・体験・予算・チェックリストのみを出力してください。
極めて重要：「公式：URL」および「Googleマップ：URL」の行に、実際のURLを出力してください。
【ユーザー回答(JSON参照用)】
{answers_json}

出力構成：
1) 🚆 交通（主要3行）
──────────────────────────────
2) 🍱 食事おすすめ（昼3件／夜3件）
🍽 店名（エリア）
短評：料理や雰囲気
💰 価格帯：〜円程度　🕰 営業：時間／休：定休
🔗 公式：URL
📍 Googleマップ：URL
──────────────────────────────
3) 🎟️ 体験予約（3件）
🎯 施設名（エリア）
短評：体験内容や特徴を2〜3行
💰 料金：〜円　⌛ 所要：〜分／予約：要・不要
🔗 公式：URL
📍 Googleマップ：URL
──────────────────────────────
4) 💰 合計予算
──────────────────────────────
5) ✅ チェックリスト
──────────────────────────────
"""

def build_review_prompt(answers: Dict[str, Any]) -> str:
    return """
以下は「総評・注意点・代替案」セクションです。
旅全体の特徴や注意事項を2〜4行でまとめてください。
"""

def build_next_prompt(answers: Dict[str, Any]) -> str:
    return """
以下は「次の操作メニュー」セクションです。
この行のみ出力してください。

🔄 最初から
"""

# ---------- OpenAI 呼び出し ----------
def _call_openai_text(user_prompt: str) -> str:
    res = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.6,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    )
    return (res.choices[0].message.content or "").strip()

# ---------- 画像/URL 検出 ----------
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

NON_PREVIEW_DOMAINS = re.compile(
    r"(?:japan-guide\.com|upload\.wikimedia\.org|images\.unsplash\.com|placehold\.co|google\.com/maps|goo\.gl/maps)",
    re.I,
)
URL_RE = re.compile(r"https?://[^\s)]+", re.I)

def _extract_preview_urls(text: str, limit=6) -> List[str]:
    urls: List[str] = []
    for m in URL_RE.finditer(text):
        u = m.group(0)
        if NON_PREVIEW_DOMAINS.search(u):
            continue
        if u not in urls:
            urls.append(u)
        if len(urls) >= limit:
            break
    return urls

OFFICIAL_URL_RE = re.compile(r"^(?:🔗\s*)?(?:公式|Official)\s*[:：]\s*(https?://[^\s)]+)", re.M)
MAP_URL_RE = re.compile(r"^(?:📍\s*)?(?:Google ?マップ|Google ?Maps)\s*[:：]\s*(https?://[^\s)]+)", re.M | re.I)

SECTION_SPLIT_RE = re.compile(r"\n[-─]{6,}\n")
FOOD_HEAD_RE  = re.compile(r"^\s*🍽\s*(?P<title>[^（\(\n]+)", re.M)
EXPER_HEAD_RE = re.compile(r"^\s*🎯\s*(?P<title>[^（\(\n]+)", re.M)
DAY_HEAD_RE   = re.compile(r"^Day\s*\d+", re.M | re.I)
BLOCK_SPLIT_RE= re.compile(r"\n\s*↓\s*\n", re.M)
ACT_TITLE_RE  = re.compile(r"^[^\n：:]*[：:]\s*(?P<title>[^\n（(]+)", re.M)

# ======== 文字列サニタイズ / 情報抽出 ========
TIME_RANGE_RE = re.compile(r"(?P<t1>\d{1,2}[:：]\d{2})\s*[–\-~〜]\s*(?P<t2>\d{1,2}[:：]\d{2})")
PRICE_LINE_RE = re.compile(r"^[\s　]*[💰\$]?\s*(?:価格帯|料金|料金目安|目安)\s*[:：]\s*(?P<price>.+)$", re.M)
OPEN_LINE_RE  = re.compile(r"^[\s　]*[🕰⏰]?\s*(?:営業)\s*[:：]\s*(?P<open>.+)$", re.M)

TIME_TOKEN_RE = re.compile(r"\[?\s*\d{1,2}[:：]\d{2}\s*[–\-~〜]\s*\d{1,2}[:：]\d{2}\s*\]?")
TRAIL_TIME_RE = re.compile(r"\b\d{1,2}[:：]\d{2}\b")

def _strip_time(s: str) -> str:
    s = TIME_TOKEN_RE.sub("", s)
    s = TRAIL_TIME_RE.sub("", s)
    s = re.sub(r"\s{2,}", " ", s).strip(" -|[]（）")
    return s.strip()

def _clean_url(u: str) -> str:
    if not u: return ""
    u = u.replace("\u200b", "").replace("\u200c", "").replace("\u200d", "").replace("\ufeff", "")
    u = u.strip().strip("。．、，)）]］>＞")
    if u.startswith("http://"): u = "https://" + u[len("http://"):]
    return u

def _strip_urls_from_text(text: str) -> str:
    lines = []
    for ln in text.splitlines():
        if OFFICIAL_URL_RE.search(ln) or MAP_URL_RE.search(ln):
            continue
        # 余白を広めに：ブロック見出しの前後に空行
        lines.append(ln)
    out = "\n".join(lines).strip()
    out = out.replace("\n↓\n", "\n\n↓\n\n")  # ブロック境界の間隔を広く
    out = out.replace("🕒 所要", "⌛ 所要")   # ⌛に統一
    return out

def _spacious_join(blocks: List[str]) -> str:
    # 各ブロックの間に空行を多めに
    cleaned = [ _strip_urls_from_text(b).strip() for b in blocks if b.strip() ]
    return ("\n\n\n").join(cleaned)

# ======== 送信用ヘルパー ========
def _push_messages_in_chunks(uid: str, msgs, size: int = 5):
    for i in range(0, len(msgs), size):
        chunk = msgs[i:i+size]
        line_bot_api.push_message(uid, chunk if len(chunk) > 1 else chunk[0])

def _send_hotels_as_buttons(reply_token: str, hotels_text: str):
    blocks = re.split(r"\n[- ─]{6,}\n|\n{2,}", hotels_text.strip())
    msgs = []
    for b in blocks:
        if not b.strip(): continue
        first_line = next((ln.strip() for ln in b.splitlines() if ln.strip()), "")
        title = re.sub(r"^\s*[①-⑳]?\s*[🏨\d\.\)\）\s]*", "", first_line) or "ホテル"
        off = OFFICIAL_URL_RE.search(b)
        mp  = MAP_URL_RE.search(b)
        actions = []
        if off: actions.append(URITemplateAction(label="公式サイトを見る", uri=_clean_url(off.group(1))))
        if mp:  actions.append(URITemplateAction(label="Googleマップ", uri=_clean_url(mp.group(1))))
        if not actions: continue
        msgs.append(
            TemplateSendMessage(
                alt_text=title,
                template=ButtonsTemplate(title=title[:40], text="リンクを選択してください", actions=actions[:4])
            )
        )
    if msgs:
        line_bot_api.reply_message(reply_token, msgs[:5])
    else:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=hotels_text))

def _extract_blocks_by_head(section_text: str, head_re: re.Pattern):
    lines = section_text.splitlines()
    idxs = [i for i, ln in enumerate(lines) if head_re.search(ln)]
    blocks = []
    for j, start in enumerate(idxs):
        end = idxs[j+1] if j+1 < len(idxs) else len(lines)
        blocks.append("\n".join(lines[start:end]).strip())
    return blocks

def _title_from_head_block(block: str, head_re: re.Pattern) -> str:
    m = head_re.search(block or "")
    return (m.group("title").strip() if m else "スポット")

def _build_buttons_from_blocks(blocks, head_re):
    msgs = []
    for b in blocks:
        title = _strip_time(_title_from_head_block(b, head_re))
        off = OFFICIAL_URL_RE.search(b)
        mp  = MAP_URL_RE.search(b)
        if not (off or mp):
            continue
        # サブ：営業時間 / 料金目安
        sub = []
        mopen = OPEN_LINE_RE.search(b)
        if mopen: sub.append(f"営業: {mopen.group('open').strip()}")
        mprice = PRICE_LINE_RE.search(b)
        if mprice: sub.append(f"目安: {mprice.group('price').strip()}")
        subtitle = " / ".join(sub) if sub else "リンクを選択してください"

        msgs.append(
            TemplateSendMessage(
                alt_text=title,
                template=ButtonsTemplate(
                    title=title[:40],
                    text=subtitle[:60],
                    actions=[
                        URITemplateAction(label="公式サイトを見る", uri=_clean_url(off.group(1))) if off else None,
                        URITemplateAction(label="Googleマップ", uri=_clean_url(mp.group(1))) if mp else None
                    ][:2]
                )
            )
        )
    return msgs

def _split_days(schedule_text: str):
    parts = []
    positions = [(m.group(0).strip(), m.start()) for m in DAY_HEAD_RE.finditer(schedule_text)]
    for i, (title, start) in enumerate(positions):
        end = positions[i+1][1] if i+1 < len(positions) else len(schedule_text)
        parts.append((title, schedule_text[start:end]))
    return parts

def _blocks_in_day(day_text: str):
    return [b.strip() for b in BLOCK_SPLIT_RE.split(day_text.strip()) if b.strip()]

def _title_from_block(block: str):
    m = ACT_TITLE_RE.search(block)
    if m: return m.group("title").strip()
    first = next((ln.strip() for ln in block.splitlines() if ln.strip()), "")
    return (first[:40] or "スポット")

def _build_schedule_buttons_for_day(day_title: str, day_body: str):
    msgs = []
    first_block = True
    for block in _blocks_in_day(day_body):
        off = OFFICIAL_URL_RE.search(block)
        mp  = MAP_URL_RE.search(block)
        if not (off or mp):
            continue

        # タイトル：時間＋場所（先頭だけに "DayX | " プレフィックス）
        mtime = TIME_RANGE_RE.search(block)
        time_label = (f"{mtime.group('t1').replace('：',':')}–{mtime.group('t2').replace('：',':')}" if mtime else "")
        place = _strip_time(_title_from_block(block))
        title = f"{(day_title + ' | ') if first_block else ''}{(time_label + ' ') if time_label else ''}{place}".strip()

        # サブ：営業時間 / 料金
        sub = []
        mopen = OPEN_LINE_RE.search(block)
        if mopen: sub.append(f"営業: {mopen.group('open').strip()}")
        mprice = PRICE_LINE_RE.search(block)
        if mprice: sub.append(f"目安: {mprice.group('price').strip()}")
        subtitle = " / ".join(sub) if sub else "リンクを選択してください"

        actions = []
        if off: actions.append(URITemplateAction(label="公式サイトを見る", uri=_clean_url(off.group(1))))
        if mp:  actions.append(URITemplateAction(label="Googleマップ", uri=_clean_url(mp.group(1))))
        msgs.append(
            TemplateSendMessage(
                alt_text=place[:60],
                template=ButtonsTemplate(
                    title=title[:40],
                    text=subtitle[:60],
                    actions=actions[:4]
                )
            )
        )
        first_block = False
    return msgs

# ======== 必要日数まで日程表を追生成 ========
def _generate_full_schedule(answers: Dict[str, Any]) -> str:
    schedule = _call_openai_text(build_schedule_prompt(answers))
    need = _required_days(answers)
    got  = _count_days_in_text(schedule)
    guard = 0
    while got < need and guard < 4:
        cont_prompt = (
            build_schedule_prompt(answers)
            + f"\n補足：すでに Day1〜Day{got} まで作成済み。"
              f"続きの Day{got+1} 以降のみを、同じフォーマットで出力してください。"
              f"過去の日を繰り返さないこと。"
        )
        extra = _call_openai_text(cont_prompt)
        schedule = (schedule.rstrip() + "\n" + extra.lstrip()).strip()
        got = _count_days_in_text(schedule)
        guard += 1
    return schedule

# ======== ホテル 3件保証 ========
def _ensure_three_hotels(answers: Dict[str, Any]) -> str:
    hotels = _call_openai_text(build_hotel_prompt(answers))
    def _count_blocks(t: str) -> int:
        return len([b for b in re.split(r"\n[-─]{6,}\n|\n{2,}", (t or "").strip()) if b.strip()])
    if _count_blocks(hotels) >= 3:
        return hotels
    retry_prompt = build_hotel_prompt(answers) + "\n必ず 3 件、欠番なく出力してください。"
    return _call_openai_text(retry_prompt)

# ---------- 5セクション順送り（最終仕様） ----------
def send_plan_parts(reply_token: str, uid: str, answers: Dict[str, Any]):
    # ① ホテル（説明 → ボタン）※説明は余白広め
    hotels = _ensure_three_hotels(answers)
    h_blocks = [b for b in re.split(r"\n[-─]{6,}\n|\n{2,}", hotels.strip()) if b.strip()]
    line_bot_api.push_message(uid, TextSendMessage(text=_spacious_join(h_blocks)))
    _send_hotels_as_buttons(reply_token, hotels)

    # ② 日程表（Dayごとに テキスト(余白広め・URL除去) → ボタン）
    schedule = _generate_full_schedule(answers)
    for day_title, day_body in _split_days(schedule):
        day_blocks = _blocks_in_day(day_body)
        line_bot_api.push_message(uid, TextSendMessage(text=_spacious_join(day_blocks)))
        day_btns = _build_schedule_buttons_for_day(day_title, day_body)
        if day_btns:
            _push_messages_in_chunks(uid, day_btns, size=5)

    # ③ 実用ガイド：交通→食事(説明→ボタン)→体験(説明→ボタン)→合計予算→チェックリスト
    guide = _call_openai_text(build_guide_prompt(answers))
    sections = SECTION_SPLIT_RE.split(guide)

    def _find(name):
        return next((s for s in sections if name in s), "")

    text_transport = _find("交通（")
    text_food = _find("食事おすすめ")
    text_exp  = _find("体験予約")
    text_budget = _find("合計予算")
    text_check  = _find("チェックリスト")

    if text_transport.strip():
        line_bot_api.push_message(uid, TextSendMessage(text=_strip_urls_from_text(text_transport)))

    if text_food.strip():
        food_blocks = _extract_blocks_by_head(text_food, FOOD_HEAD_RE)[:3]
        if food_blocks:
            line_bot_api.push_message(uid, TextSendMessage(text=_spacious_join(food_blocks)))
            _push_messages_in_chunks(uid, _build_buttons_from_blocks(food_blocks, FOOD_HEAD_RE), size=5)

    if text_exp.strip():
        exp_blocks = _extract_blocks_by_head(text_exp, EXPER_HEAD_RE)[:3]
        if exp_blocks:
            line_bot_api.push_message(uid, TextSendMessage(text=_spacious_join(exp_blocks)))
            _push_messages_in_chunks(uid, _build_buttons_from_blocks(exp_blocks, EXPER_HEAD_RE), size=5)

    if text_budget.strip():
        line_bot_api.push_message(uid, TextSendMessage(text=_strip_urls_from_text(text_budget)))
    if text_check.strip():
        line_bot_api.push_message(uid, TextSendMessage(text=_strip_urls_from_text(text_check)))

    # ④ 総評
    review = _call_openai_text(build_review_prompt(answers))
    line_bot_api.push_message(uid, TextSendMessage(text=review))

    # ⑤ 次の操作メニュー
    nxt = _call_openai_text(build_next_prompt(answers))
    line_bot_api.push_message(uid, TextSendMessage(text=nxt))

SYSTEM_PROMPT = (
    "You are AI Travel Navi Kansai.\n"
    "以下の利用者回答（JSON）に厳密に従って、選択されていない地域は一切含めず、"
    "最終プランを**一度だけ**返します。中間メッセージ・分割出力は禁止。\n"
    "出力順：1)ホテル候補3件 2)日程表 3)実用ガイド 4)総評・注意点・代替案 5)次の操作メニュー。\n"
    "画像は各ブロック1枚。許可ドメイン：https://www.japan-guide.com / "
    "https://upload.wikimedia.org / https://images.unsplash.com 。無い場合は "
    "https://placehold.co/800x500.png?text={施設名} を使用。URLは生URL（Markdownリンク禁止）。\n"
    "日程表と実用ガイドでは**画像URLを一切出さない**（📸行も出さない）。\n"
    "日本語モード（ja）は日本語、英語モード（en）は英語で一貫出力。\n"
    "食事と体験は**固有の店名・施設名**を必ず記載し、各項目に Google マップ検索URL と営業時間・定休の情報を付けること。\n"
    "体験は**最低3つ**提示すること（候補として3件、各々に料金目安・所要時間・予約要否を明記）。\n"
    "重要：**旅程の最終日には宿泊/チェックインのブロックを入れない。** 宿泊は最終日前日までに限定する。\n"
    "表記：『所要』の前は必ず ⌛ を用いる。\n"
)

# ---------- 画像・URL 抽出系（そのまま） ----------
def _extract_official_urls(text: str, limit: int = 12) -> List[str]:
    urls: List[str] = []
    for m in OFFICIAL_URL_RE.finditer(text):
        u = m.group(1)
        if NON_PREVIEW_DOMAINS.search(u):
            continue
        if u not in urls:
            urls.append(u)
        if len(urls) >= limit:
            break
    return urls

def _split_long_text(text: str, maxlen=4900) -> List[str]:
    if len(text) <= maxlen:
        return [text]
    parts, buf, count = [], [], 0
    for line in text.splitlines(True):
        if count + len(line) > maxlen:
            parts.append("".join(buf)); buf, count = [line], len(line)
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
        users[uid] = {"step": 0, "answers": {}, "hist": deque(maxlen=MAX_TURNS)}
        # 複数選択一時保存
        users[uid]["pending_multi_set"] = set()
        _reply_text(event.reply_token, WELCOME)
        # 最初の質問をクイックリプライで
        _send_question(event.reply_token, users[uid])
        return

    # 初回
    if uid not in users or not users[uid]:
        users[uid] = {"step": 0, "answers": {}, "hist": deque(maxlen=MAX_TURNS), "pending_multi_set": set()}
        _reply_text(event.reply_token, WELCOME)
        _send_question(event.reply_token, users[uid])
        return

    state = users[uid]
    step = state["step"]
    q = Q[step]

    # --- 入力処理（ボタン前提） ---
    if q["multi"]:
        # 複数選択：数字→蓄積、完了→確定
        if text == "完了":
            if state["pending_multi_set"]:
                # 確定して次へ
                chosen = sorted(set(int(x) for x in state["pending_multi_set"]))
                state["answers"][q["key"]] = [q["choices"][n] for n in chosen if n in q["choices"]]
                state["pending_multi_set"] = set()
                state["step"] += 1
                if state["step"] < len(Q):
                    _send_question(event.reply_token, state)
                else:
                    # 全質問終了
                    answers = state["answers"].copy()
                    try:
                        send_plan_parts(event.reply_token, uid, answers)
                    except Exception as e:
                        app.logger.exception("OpenAI API error")
                        _reply_text(event.reply_token, f"サーバ側で一時的なエラーが発生しました。\n(debug: {type(e).__name__})")
                        return
                    users.pop(uid, None)
                return
            else:
                # 何も選ばれていない
                _send_question(event.reply_token, state)
                return
        else:
            nums = _parse_numbers(text)
            if nums:
                # 選択を一時セットに追加
                for n in nums:
                    if n in q["choices"]:
                        state["pending_multi_set"].add(n)
            _send_question(event.reply_token, state)
            return
    else:
        # 単一選択
        nums = _parse_numbers(text)
        if nums and len(nums) == 1 and nums[0] in q.get("choices", {nums[0]: str(nums[0])}):
            state["answers"][q["key"]] = ( "ja" if (q["key"]=="lang" and nums[0]==1)
                                           else "en" if (q["key"]=="lang" and nums[0]==2)
                                           else q["choices"][nums[0]] )
            state["step"] += 1
            if state["step"] < len(Q):
                _send_question(event.reply_token, state)
                return
        else:
            # 日付入力など
            if q["key"] == "date":
                try:
                    datetime.strptime(text.strip(), "%Y-%m-%d")
                    state["answers"][q["key"]] = text.strip()
                    state["step"] += 1
                    if state["step"] < len(Q):
                        _send_question(event.reply_token, state)
                        return
                except Exception:
                    _send_question(event.reply_token, state)
                    return
            else:
                _send_question(event.reply_token, state)
                return

    # === 全質問終了 → 指定順に送信 ===
    answers = state["answers"].copy()
    try:
        send_plan_parts(event.reply_token, uid, answers)
    except Exception as e:
        app.logger.exception("OpenAI API error")
        _reply_text(event.reply_token, f"サーバ側で一時的なエラーが発生しました。\n(debug: {type(e).__name__})")
        return

    users.pop(uid, None)

# ====================== ローカル実行 ======================
if __name__ == "__main__":
    logging.getLogger().setLevel(logging.INFO)
    logging.info(f"Running Python: {sys.version}")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
