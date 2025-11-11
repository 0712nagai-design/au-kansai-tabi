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
    TemplateSendMessage, ButtonsTemplate, URITemplateAction, MessageAction,
    FlexSendMessage
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

RESTART = {"start", "restart", "reset", "スタート", "最初から", "やり直す", "最初から"}

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
    {"key": "region", "title": "地域を選んでください（複数可）", "choices": REGIONS, "multi": True},
    {"key": "date", "title": "出発日を入力してください（例: 2025-03-20）", "choices": {}, "multi": False},
    {"key": "stay", "title": "日程を選んでください", "choices": {1: "日帰り", 2: "1泊2日", 3: "2泊3日", 4: "3泊以上"}, "multi": False},
    {"key": "theme", "title": "テーマを選んでください（複数可）", "choices": THEMES, "multi": True},
    {"key": "budget", "title": "予算（1人）を選んでください", "choices": BUDGETS, "multi": False},
    {"key": "hotel", "title": "ホテルタイプを選んでください", "choices": HOTELS, "multi": False},
    {"key": "transport", "title": "交通手段を選んでください（複数可）", "choices": TRANSPORT, "multi": True},
    {"key": "companion", "title": "同行者を選んでください", "choices": COMPANION, "multi": False},
    {"key": "dept", "title": "出発時間帯を選んでください", "choices": DEPT, "multi": False},
    {"key": "arrv", "title": "帰着時間帯を選んでください", "choices": ARRV, "multi": False},
]

WELCOME = "🔄 最初から\nこんにちは！私はAI旅ナビ関西です🧭\nどちらの言語でご案内しますか？"

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

# ====================== Flex（質問カード） ======================
def _flex_question_card(title: str, choices: Dict[int, str], multi: bool, selected_labels: List[str]) -> Dict[str, Any]:
    """
    Flex Bubble JSON を返す。
    - 2列グリッドの大きめボタン
    - 複数選択時はフッターに「✅ 完了」
    - ヘッダーに「選択中」を表示
    """
    # ボタン配列（2列グリッド）
    def _choice_button(num: int, label: str) -> Dict[str, Any]:
        # 選択済みは色を強調
        chosen = (label in selected_labels)
        return {
            "type": "button",
            "style": "primary" if chosen else "secondary",
            "height": "md",
            "action": {"type": "message", "label": f"{num} {label}", "text": str(num)},
            "color": "#34C759" if chosen else "#E9EDF1",
        }

    buttons: List[Dict[str, Any]] = []
    for n, lab in choices.items():
        buttons.append(_choice_button(n, lab))

    # 2個ずつで行に分割
    rows: List[Dict[str, Any]] = []
    for i in range(0, len(buttons), 2):
        row = {
            "type": "box", "layout": "horizontal", "spacing": "md",
            "contents": buttons[i:i+2]
        }
        if len(row["contents"]) == 1:  # 端数時は幅埋め
            row["contents"].append({"type": "filler"})
        rows.append(row)

    header_sub = ""
    if multi:
        header_sub = f"（選択中：{'、'.join(selected_labels) if selected_labels else 'なし'}）"

    footer_contents: List[Dict[str, Any]] = []
    if multi:
        footer_contents.append({
            "type": "button", "style": "primary", "height": "md",
            "action": {"type": "message", "label": "✅ 完了", "text": "完了"},
            "color": "#2ECC71"
        })
    # 共通：最初から
    footer_contents.append({
        "type": "button", "style": "link",
        "action": {"type": "message", "label": "↪ 最初から", "text": "最初から"}
    })

    bubble = {
        "type": "bubble",
        "size": "giga",
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "lg",
            "contents": [
                {"type": "text", "text": title, "wrap": True, "weight": "bold", "size": "xl"},
                {"type": "text", "text": header_sub, "wrap": True, "size": "sm", "color": "#6B7280"} if header_sub else {"type": "filler"},
                {"type": "separator"},
                {"type": "box", "layout": "vertical", "spacing": "md", "contents": rows}
            ]
        },
        "footer": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": footer_contents}
    }
    return bubble

def _render_question(idx: int, state: State) -> FlexSendMessage:
    q = Q[idx]
    selected = state.get("multi_temp", {}).get(q["key"], [])
    bubble = _flex_question_card(q["title"], q.get("choices", {}), q["multi"], selected)
    return FlexSendMessage(alt_text=q["title"], contents=bubble)

# ====================== ユーティリティ ======================
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
    if not re.fullmatch(r"[0-9,]+", s): return None
    try:
        nums = [int(x) for x in s.split(",") if x != ""]
        return nums if nums else None
    except Exception:
        return None

def _label_to_num(choices: Dict[int, str], text: str) -> Optional[int]:
    text = text.strip()
    for n, label in choices.items():
        if text == str(n) or text == label:
            return n
    return None

def _validate_and_store(uid: str, step: int, text: str) -> bool:
    state = users[uid]
    q = Q[step]; key = q["key"]
    state.setdefault("answers", {})
    state.setdefault("multi_temp", {})

    # 直接ラベル/番号対応
    if q["choices"]:
        n = _label_to_num(q["choices"], text)
        if n is not None:
            if q["multi"]:
                sel = state["multi_temp"].setdefault(key, [])
                label = q["choices"][n]
                if label not in sel:
                    sel.append(label)
                return True
            else:
                state["answers"][key] = q["choices"][n] if key != "lang" else ("ja" if n == 1 else "en")
                return True

    # 日付
    if key == "date":
        try:
            datetime.strptime(text.strip(), "%Y-%m-%d")
            state["answers"][key] = text.strip()
            return True
        except Exception:
            return False

    # 複数選択の完了
    if q["multi"] and text.strip() == "完了":
        picked = state["multi_temp"].get(key, [])
        if not picked: return False
        state["answers"][key] = picked
        return True

    # 数字入力（後方互換）
    nums = _parse_numbers(text)
    if nums:
        if q["multi"]:
            bad = [n for n in nums if n not in q["choices"]]
            if bad: return False
            labels = [q["choices"][n] for n in nums]
            state["multi_temp"][key] = sorted(set(state["multi_temp"].get(key, []) + labels), key=labels.index)
            return True
        else:
            if len(nums) != 1 or nums[0] not in q["choices"]:
                return False
            state["answers"][key] = q["choices"][nums[0]] if key != "lang" else ("ja" if nums[0] == 1 else "en")
            return True

    return False

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
💰 料金：〜円　⌛ 所要：〜分／予約：要・不要　🕰 営業：時間／休：定休
🔗 公式：URL
📍 Googleマップ：URL
──────────────────────────────
4) 💰 合計予算
──────────────────────────────
5) ✅ チェックリスト
──────────────────────────────
"""

def build_review_prompt(answers: Dict[str, Any]) -> str:
    return "以下は「総評・注意点・代替案」セクションです。旅全体の特徴や注意事項を2〜4行でまとめてください。"

def build_next_prompt(answers: Dict[str, Any]) -> str:
    return "以下は「次の操作メニュー」セクションです。この行のみ出力してください。\n\n🔄 最初から"

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
IMG_URL_RE = re.compile(r"https?://(?:www\.)?(?:japan-guide\.com|upload\.wikimedia\.org|images\.unsplash\.com|placehold\.co)/[^\s)]+", re.I)
NON_PREVIEW_DOMAINS = re.compile(r"(?:japan-guide\.com|upload\.wikimedia\.org|images\.unsplash\.com|placehold\.co|google\.com/maps|goo\.gl/maps)", re.I)
URL_RE = re.compile(r"https?://[^\s)]+", re.I)

OFFICIAL_URL_RE = re.compile(r"^(?:🔗\s*)?(?:公式|Official)\s*[:：]\s*(https?://[^\s)]+)", re.M)
MAP_URL_RE = re.compile(r"^(?:📍\s*)?(?:Google ?マップ|Google ?Maps)\s*[:：]\s*(https?://[^\s)]+)", re.M | re.I)

SECTION_SPLIT_RE = re.compile(r"\n[-─]{6,}\n")
FOOD_HEAD_RE  = re.compile(r"^\s*🍽\s*(?P<title>[^（\(\n]+)", re.M)
EXPER_HEAD_RE = re.compile(r"^\s*🎯\s*(?P<title>[^（\(\n]+)", re.M)
DAY_HEAD_RE   = re.compile(r"^Day\s*\d+", re.M | re.I)
BLOCK_SPLIT_RE= re.compile(r"\n\s*↓\s*\n", re.M)
ACT_TITLE_RE  = re.compile(r"^[^\n：:]*[：:]\s*(?P<title>[^\n（(]+)", re.M)

# ======== 文字列サニタイズ / 情報抽出 ========
TIME_RANGE_RE = re.compile(r"\b(\d{1,2}[:：]\d{2})\s*[–\-~〜]\s*(\d{1,2}[:：]\d{2})\b")
PRICE_RE = re.compile(r"(?:💰|料金|価格帯)\s*[:：]\s*([^\n／]+)")
HOURS_RE = re.compile(r"(?:🕰|営業時間|営業)\s*[:：]\s*([^\n]+)")

def _strip_links(text: str) -> str:
    text = OFFICIAL_URL_RE.sub("", text)
    text = MAP_URL_RE.sub("", text)
    text = re.sub(r"\n{2,}", "\n\n", text).strip()
    return text + "\n" + ("─"*30)

def _clean_url(u: str) -> str:
    if not u: return ""
    u = u.replace("\u200b", "").replace("\u200c", "").replace("\u200d", "").replace("\ufeff", "")
    u = u.strip().strip("。．、，)）]］>＞")
    if u.startswith("http://"): u = "https://" + u[len("http://"):]
    return u

# ======== 送信用ヘルパー ========
def _push_messages_in_chunks(uid: str, msgs, size: int = 5):
    for i in range(0, len(msgs), size):
        chunk = msgs[i:i+size]
        line_bot_api.push_message(uid, chunk if len(chunk) > 1 else chunk[0])

def _send_hotels_as_buttons(uid: str, hotels_text: str):
    blocks = re.split(r"\n[- ─]{6,}\n|\n{2,}", hotels_text.strip())
    msgs = []
    for b in blocks:
        b = b.strip()
        if not b: continue
        first_line = next((ln.strip() for ln in b.splitlines() if ln.strip()), "")
        title = re.sub(r"^\s*[①-⑳]?\s*[🏨\d\.\)\）\s]*", "", first_line) or "ホテル"
        off = OFFICIAL_URL_RE.search(b)
        mp  = MAP_URL_RE.search(b)
        price = ""
        m = re.search(r"価格目安[:：]\s*([^\n]+)", b)
        if m: price = m.group(1).strip()
        actions = []
        if off: actions.append(URITemplateAction(label="公式サイト", uri=_clean_url(off.group(1))))
        if mp:  actions.append(URITemplateAction(label="Googleマップ", uri=_clean_url(mp.group(1))))
        if not actions: continue
        msgs.append(
            TemplateSendMessage(
                alt_text=title,
                template=ButtonsTemplate(
                    title=title[:40],
                    text=(f"価格目安：{price}" if price else "リンクを選択してください"),
                    actions=actions[:4]
                )
            )
        )
    if msgs: _push_messages_in_chunks(uid, msgs, size=5)

def _extract_blocks_by_head(section_text: str, head_re: re.Pattern):
    lines = section_text.splitlines()
    idxs = [i for i, ln in enumerate(lines) if head_re.search(ln)]
    blocks = []
    for j, start in enumerate(idxs):
        end = idxs[j+1] if j+1 < len(idxs) else len(lines)
        blocks.append("\n".join(lines[start:end]).strip())
    return blocks

def _info_from_block(block: str):
    mtime = TIME_RANGE_RE.search(block)
    time_range = mtime.group(0).replace("：", ":") if mtime else ""
    mtitle = ACT_TITLE_RE.search(block)
    name = (mtitle.group("title").strip() if mtitle else "スポット")
    mh = HOURS_RE.search(block)
    hp = mh.group(1).strip() if mh else ""
    mp = PRICE_RE.search(block)
    price = mp.group(1).strip() if mp else ""
    subtitle_parts = []
    if hp: subtitle_parts.append(f"営業時間：{hp}")
    if price: subtitle_parts.append(f"目安：{price}")
    subtitle = " ／ ".join(subtitle_parts) if subtitle_parts else "リンクを選択してください"
    return time_range, name, subtitle

def _split_days(schedule_text: str):
    parts = []
    positions = [(m.group(0).strip(), m.start()) for m in DAY_HEAD_RE.finditer(schedule_text)]
    for i, (title, start) in enumerate(positions):
        end = positions[i+1][1] if i+1 < len(positions) else len(schedule_text)
        parts.append((title, schedule_text[start:end]))
    return parts

def _blocks_in_day(day_text: str):
    return [b.strip() for b in BLOCK_SPLIT_RE.split(day_text.strip()) if b.strip()]

def _send_schedule_buttons_for_day(uid: str, day_title: str, day_body: str):
    msgs = []
    for block in _blocks_in_day(day_body):
        off = OFFICIAL_URL_RE.search(block)
        mp  = MAP_URL_RE.search(block)
        if not (off or mp): continue
        time_range, name, subtitle = _info_from_block(block)
        # タイトルは「10:00–12:00 〇〇」形式（“観光・体験”などは入れない）
        title = f"{time_range} {name}".strip()
        actions = []
        if off: actions.append(URITemplateAction(label="公式サイト", uri=_clean_url(off.group(1))))
        if mp:  actions.append(URITemplateAction(label="Googleマップ", uri=_clean_url(mp.group(1))))
        msgs.append(
            TemplateSendMessage(
                alt_text=title[:240],
                template=ButtonsTemplate(
                    title=title[:40],
                    text=subtitle[:60] if subtitle else "リンクを選択してください",
                    actions=actions[:4]
                )
            )
        )
    if msgs: _push_messages_in_chunks(uid, msgs, size=5)

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

# ---------- 指定順で送信 ----------
def send_plan_parts(reply_token: str, uid: str, answers: Dict[str, Any]):
    # ① ホテル：テキスト → ボタン(push)
    hotels = _call_openai_text(build_hotel_prompt(answers))
    line_bot_api.reply_message(reply_token, TextSendMessage(text=hotels.strip() + "\n" + ("─"*30)))
    _send_hotels_as_buttons(uid, hotels)

    # ② 日程表：各Day テキスト（URL行除去）→ ボタン(push)
    schedule = _generate_full_schedule(answers)
    for day_title, day_body in _split_days(schedule):
        body_clean = _strip_links(day_body.strip())
        line_bot_api.push_message(uid, TextSendMessage(text=body_clean))
        _send_schedule_buttons_for_day(uid, day_title, day_body)

    # ③ 実用ガイド：食事→ボタン→体験→ボタン→予算→チェック
    guide = _call_openai_text(build_guide_prompt(answers))
    sections = SECTION_SPLIT_RE.split(guide)

    # 食事
    food_idx = next((i for i, s in enumerate(sections) if "食事おすすめ" in s), None)
    if food_idx is not None:
        food_blocks_all = _extract_blocks_by_head(sections[food_idx], FOOD_HEAD_RE)[:3]
        if food_blocks_all:
            line_bot_api.push_message(uid, TextSendMessage(text=_strip_links("\n\n".join(food_blocks_all))))
            msgs = []
            for b in food_blocks_all:
                _, name, subtitle = _info_from_block(b)
                off = OFFICIAL_URL_RE.search(b); mp = MAP_URL_RE.search(b)
                actions = []
                if off: actions.append(URITemplateAction(label="公式サイト", uri=_clean_url(off.group(1))))
                if mp:  actions.append(URITemplateAction(label="Googleマップ", uri=_clean_url(mp.group(1))))
                msgs.append(TemplateSendMessage(
                    alt_text=name,
                    template=ButtonsTemplate(title=name[:40], text=subtitle[:60] or "リンクを選択してください", actions=actions[:4])
                ))
            if msgs: _push_messages_in_chunks(uid, msgs, size=5)

    # 体験
    exp_idx  = next((i for i, s in enumerate(sections) if "体験予約" in s), None)
    if exp_idx is not None:
        exp_blocks_all = _extract_blocks_by_head(sections[exp_idx], EXPER_HEAD_RE)[:3]
        if exp_blocks_all:
            line_bot_api.push_message(uid, TextSendMessage(text=_strip_links("\n\n".join(exp_blocks_all))))
            msgs = []
            for b in exp_blocks_all:
                _, name, subtitle = _info_from_block(b)
                off = OFFICIAL_URL_RE.search(b); mp = MAP_URL_RE.search(b)
                actions = []
                if off: actions.append(URITemplateAction(label="公式サイト", uri=_clean_url(off.group(1))))
                if mp:  actions.append(URITemplateAction(label="Googleマップ", uri=_clean_url(mp.group(1))))
                msgs.append(TemplateSendMessage(
                    alt_text=name,
                    template=ButtonsTemplate(title=name[:40], text=subtitle[:60] or "リンクを選択してください", actions=actions[:4])
                ))
            if msgs: _push_messages_in_chunks(uid, msgs, size=5)

    # 合計予算 & チェックリスト
    budget_idx = next((i for i, s in enumerate(sections) if "合計予算" in s), None)
    checklist_idx = next((i for i, s in enumerate(sections) if "チェックリスト" in s), None)
    if budget_idx is not None:
        line_bot_api.push_message(uid, TextSendMessage(text=sections[budget_idx].strip()))
    if checklist_idx is not None:
        line_bot_api.push_message(uid, TextSendMessage(text=sections[checklist_idx].strip()))

    # ④ 総評
    review = _call_openai_text(build_review_prompt(answers))
    line_bot_api.push_message(uid, TextSendMessage(text=review))

    # ⑤ 次の操作
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
    "日程表と実用ガイドでは**画像URLを一切出さない**。\n"
    "日本語モード（ja）は日本語、英語モード（en）は英語で一貫出力。\n"
    "食事と体験は固有の店名・施設名を必ず記載し、各項目にGoogleマップURLと営業時間・定休を付けること。\n"
    "体験は最低3つ提示（料金目安・所要時間・予約要否を明記）。\n"
    "重要：旅程の最終日には宿泊/チェックインのブロックを入れない。\n"
)

# ---------- テキスト分割 ----------
def _split_long_text(text: str, maxlen=4900) -> List[str]:
    if len(text) <= maxlen: return [text]
    parts, buf, count = [], [], 0
    for line in text.splitlines(True):
        if count + len(line) > maxlen:
            parts.append("".join(buf)); buf, count = [line], len(line)
        else:
            buf.append(line); count += len(line)
    if buf: parts.append("".join(buf))
    return parts

def _reply_text(reply_token: str, text: str):
    chunks = _split_long_text(text)
    line_bot_api.reply_message(reply_token, [TextSendMessage(text=c) for c in chunks])

# ====================== メインハンドラ ======================
@handler.add(MessageEvent, message=TextMessage)
def on_message(event: MessageEvent):
    uid = event.source.user_id
    text = (event.message.text or "").strip()

    # リスタート
    if text in RESTART or text.lower() in RESTART:
        users[uid] = {"step": 0, "answers": {}, "hist": deque(maxlen=MAX_TURNS), "multi_temp": {}}
        line_bot_api.reply_message(event.reply_token, _render_question(0, users[uid]))
        return

    # 初回
    if uid not in users or not users[uid]:
        users[uid] = {"step": 0, "answers": {}, "hist": deque(maxlen=MAX_TURNS), "multi_temp": {}}
        line_bot_api.reply_message(event.reply_token, _render_question(0, users[uid]))
        return

    state = users[uid]
    step = state["step"]

    # 入力検証
    if not _validate_and_store(uid, step, text):
        line_bot_api.reply_message(event.reply_token, _render_question(step, state))
        return

    # 複数選択中は完了待ち（カードを再掲）
    if Q[step]["multi"] and text != "完了":
        line_bot_api.reply_message(event.reply_token, _render_question(step, state))
        return

    # 次のステップへ
    step += 1
    state["step"] = step

    if step < len(Q):
        line_bot_api.reply_message(event.reply_token, _render_question(step, state))
        return

    # === 全質問終了 → 送信 ===
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

