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
    {"key": "region", "title": "地域を番号でまとめて入力（例: 1,3,5）※複数OK", "choices": REGIONS, "multi": True},
    {"key": "date", "title": "出発日を YYYY-MM-DD で入力（例: 2025-03-20）", "choices": {}, "multi": False},
    {"key": "stay", "title": "日程を選択", "choices": {1: "日帰り", 2: "1泊2日", 3: "2泊3日", 4: "3泊以上"}, "multi": False},
    {"key": "theme", "title": "テーマを番号でまとめて入力（例: 1 4 6）※複数OK", "choices": THEMES, "multi": True},
    {"key": "budget", "title": "予算（1人）を選択", "choices": BUDGETS, "multi": False},
    {"key": "hotel", "title": "ホテルタイプを選択", "choices": HOTELS, "multi": False},
    {"key": "transport", "title": "交通手段を番号でまとめて入力（例: 1,2）", "choices": TRANSPORT, "multi": True},
    {"key": "companion", "title": "同行者を選択", "choices": COMPANION, "multi": False},
    {"key": "dept", "title": "出発時間帯を選択", "choices": DEPT, "multi": False},
    {"key": "arrv", "title": "帰着時間帯を選択", "choices": ARRV, "multi": False},
]

WELCOME = "🔄 最初から\nこんにちは！私はAI旅ナビ関西です🧭\nどちらの言語でご案内しますか？"

# ========= 質問レンダリング（一覧を本文に縦並び表示＋補助クイックリプライ） =========
def _quick_buttons(choices: Dict[int, str], multi: bool) -> QuickReply:
    btns = [QuickReplyButton(action=MessageAction(label="↪ 最初から", text="最初から"))]
    if multi:
        btns += [
            QuickReplyButton(action=MessageAction(label="例) 1,3,5 を送信", text="1,3,5")),
            QuickReplyButton(action=MessageAction(label="例) 2 4 6 を送信", text="2 4 6")),
        ]
    else:
        for n, label in choices.items():
            btns.append(QuickReplyButton(action=MessageAction(label=f"{n} {label}", text=str(n))))
    return QuickReply(items=btns[:13])  # LINEの上限を考慮

def _choices_block(choices: Dict[int, str]) -> str:
    if not choices: return ""
    lines = [f"{n} {label}" for n, label in choices.items()]
    return "\n".join(lines)

def _render_question(idx: int, state: State) -> TextSendMessage:
    q = Q[idx]
    title = q["title"]
    list_block = _choices_block(q.get("choices", {}))
    hint = "\n（複数は 1,3,5 や 1 3 5 のように一度に送信）" if q["multi"] else ""
    text = f"{title}\n{list_block}{hint}" if list_block else f"{title}{hint}"
    return TextSendMessage(text=text, quick_reply=_quick_buttons(q.get("choices", {}), q["multi"]))

# ====================== ユーティリティ ======================
FW_TO_HW = str.maketrans({"０":"0","１":"1","２":"2","３":"3","４":"4","５":"5","６":"6","７":"7","８":"8","９":"9","．":".","，":",","、":",","・":",","　":" "})

def _parse_numbers(s: str) -> Optional[List[int]]:
    if not s: return None
    s = s.translate(FW_TO_HW)
    for sep in [".","･","・","、","　","，"," ","/","／","|","｜"]:
        s = s.replace(sep, ",")
    s = re.sub(r",+", ",", s).strip(",")
    if not re.fullmatch(r"[0-9,]+", s): return None
    try:
        nums = [int(x) for x in s.split(",") if x!=""]
        return nums if nums else None
    except Exception:
        return None

def _label_to_num(choices: Dict[int, str], text: str) -> Optional[int]:
    t = text.strip()
    for n, label in choices.items():
        if t == str(n) or t == label: return n
    return None

def _validate_and_store(uid: str, step: int, text: str) -> bool:
    state = users[uid]
    q = Q[step]; key = q["key"]
    state.setdefault("answers", {})

    # ラベル/番号の単一選択
    if q["choices"]:
        n = _label_to_num(q["choices"], text)
        if n is not None and not q["multi"]:
            state["answers"][key] = q["choices"][n] if key != "lang" else ("ja" if n == 1 else "en")
            return True

    # 日付
    if key == "date":
        try:
            datetime.strptime(text.strip(), "%Y-%m-%d")
            state["answers"][key] = text.strip(); return True
        except Exception:
            return False

    # まとめて複数選択（1回で確定）
    if q["multi"]:
        nums = _parse_numbers(text)
        if nums:
            bad = [n for n in nums if n not in q["choices"]]
            if bad: return False
            labels = [q["choices"][n] for n in nums]
            state["answers"][key] = labels
            return True

    # 単一選択の数字入力
    nums = _parse_numbers(text)
    if nums and not q["multi"]:
        if len(nums) == 1 and nums[0] in q["choices"]:
            state["answers"][key] = q["choices"][nums[0]] if key != "lang" else ("ja" if nums[0]==1 else "en")
            return True
        return False

    return False

def _count_days_in_text(text: str) -> int:
    # "Day2" / "Day 2" / "DAY2" どれでも
    return len(re.findall(r"^Day\s*\d+", text, flags=re.M|re.I))

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
    need = _required_days(answers)
    return f"""
以下は「日程表」セクションの出力指示です。
他の項目は出さず、旅程のみを生成してください。
極めて重要：「公式：URL」および「Googleマップ：URL」の行に、実際のURLを出力してください。
【ユーザー回答(JSON参照用)】
{answers_json}

厳守事項：
- Day1 から Day{need} まで**必ず**作成する
- 最終日には「宿泊」を入れない（宿泊は前日まで）
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
        messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_prompt}],
    )
    return (res.choices[0].message.content or "").strip()

# ---------- 解析・整形 ----------
OFFICIAL_URL_RE = re.compile(r"^(?:🔗\s*)?(?:公式|Official)\s*[:：]\s*(https?://[^\s)]+)", re.M)
MAP_URL_RE = re.compile(r"^(?:📍\s*)?(?:Google ?マップ|Google ?Maps)\s*[:：]\s*(https?://[^\s)]+)", re.M|re.I)
DAY_HEAD_RE   = re.compile(r"^Day\s*\d+", re.M|re.I)
BLOCK_SPLIT_RE= re.compile(r"\n\s*↓\s*\n", re.M)
ACT_TITLE_RE  = re.compile(r"^[^\n：:]*[：:]\s*(?P<title>[^\n（(]+)", re.M)
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
    u = u.replace("\u200b","").replace("\u200c","").replace("\u200d","").replace("\ufeff","")
    u = u.strip().strip("。．、，)）]］>＞")
    if u.startswith("http://"): u = "https://" + u[len("http://"):]
    return u

# ---------- ボタン生成 ----------
def _push_messages_in_chunks(uid: str, msgs, size: int = 5):
    for i in range(0, len(msgs), size):
        chunk = msgs[i:i+size]
        line_bot_api.push_message(uid, chunk if len(chunk)>1 else chunk[0])

def _send_hotels_as_buttons(uid: str, hotels_text: str):
    blocks = re.split(r"\n[- ─]{6,}\n|\n{2,}", hotels_text.strip())
    msgs = []
    for b in blocks:
        b = b.strip()
        if not b: continue
        first_line = next((ln.strip() for ln in b.splitlines() if ln.strip()), "")
        title = re.sub(r"^\s*[①-⑳]?\s*[🏨\d\.\)\）\s]*", "", first_line) or "ホテル"
        off = OFFICIAL_URL_RE.search(b); mp = MAP_URL_RE.search(b)
        actions = []
        if off: actions.append(URITemplateAction(label="公式サイトを見る", uri=_clean_url(off.group(1))))
        if mp:  actions.append(URITemplateAction(label="Googleマップ", uri=_clean_url(mp.group(1))))
        if actions:
            msgs.append(TemplateSendMessage(
                alt_text=title,
                template=ButtonsTemplate(title=title[:40], text="リンクを選択してください", actions=actions[:4])
            ))
    if msgs: _push_messages_in_chunks(uid, msgs, size=5)

def _split_days(schedule_text: str):
    parts = []
    positions = [(m.group(0).strip(), m.start()) for m in DAY_HEAD_RE.finditer(schedule_text)]
    for i, (title, start) in enumerate(positions):
        end = positions[i+1][1] if i+1 < len(positions) else len(schedule_text)
        parts.append((title, schedule_text[start:end]))
    return parts

def _blocks_in_day(day_text: str):
    return [b.strip() for b in BLOCK_SPLIT_RE.split(day_text.strip()) if b.strip()]

def _info_from_block(block: str):
    mtime = TIME_RANGE_RE.search(block)
    time_range = mtime.group(0).replace("：", ":") if mtime else ""
    mtitle = ACT_TITLE_RE.search(block)
    name = (mtitle.group("title").strip() if mtitle else "スポット")
    mh = HOURS_RE.search(block)
    hp = mh.group(1).strip() if mh else ""
    mp = PRICE_RE.search(block)
    price = mp.group(1).strip() if mp else ""
    subtitle = " ／ ".join([s for s in [f"営業時間：{hp}" if hp else "", f"目安：{price}" if price else ""] if s]) or "リンクを選択してください"
    return time_range, name, subtitle

def _send_schedule_buttons_for_day(uid: str, day_title: str, day_body: str):
    msgs = []; first_of_day = True
    for block in _blocks_in_day(day_body):
        off = OFFICIAL_URL_RE.search(block); mp = MAP_URL_RE.search(block)
        if not (off or mp): continue
        time_range, name, subtitle = _info_from_block(block)
        title = f"{day_title}｜{time_range} {name}" if first_of_day else f"{time_range} {name}".strip()
        actions = []
        if off: actions.append(URITemplateAction(label="公式サイトを見る", uri=_clean_url(off.group(1))))
        if mp:  actions.append(URITemplateAction(label="Googleマップ", uri=_clean_url(mp.group(1))))
        msgs.append(TemplateSendMessage(
            alt_text=f"{day_title}-{name}",
            template=ButtonsTemplate(title=title[:40], text=subtitle[:60], actions=actions[:4])
        ))
        first_of_day = False
    if msgs: _push_messages_in_chunks(uid, msgs, size=5)

# ======== 旅程の追生成（Day2以降を強化） ========
def _generate_full_schedule(answers: Dict[str, Any]) -> str:
    schedule = _call_openai_text(build_schedule_prompt(answers))
    need = _required_days(answers)
    got  = _count_days_in_text(schedule)
    guard = 0
    # 最大6回まで続きだけを要求（Day2以降の欠落を補完）
    while got < need and guard < 6:
        cont_prompt = (
            build_schedule_prompt(answers)
            + f"\n補足：現在 Day1〜Day{got} まで作成済み。"
              f"**続きの Day{got+1}〜Day{need} のみ**を同じ書式で出力。"
              f"既出は繰り返さないこと。"
        )
        extra = _call_openai_text(cont_prompt)
        schedule = (schedule.rstrip() + "\n" + extra.lstrip()).strip()
        got = _count_days_in_text(schedule); guard += 1
    return schedule

# ---------- 指定順で送信 ----------
def send_plan_parts(reply_token: str, uid: str, answers: Dict[str, Any]):
    # ① ホテル 説明 → ボタン
    hotels = _call_openai_text(build_hotel_prompt(answers))
    line_bot_api.reply_message(reply_token, TextSendMessage(text=hotels.strip() + "\n" + ("─"*30)))
    _send_hotels_as_buttons(uid, hotels)

    # ② 日程表（各Day：テキスト→ボタン）
    schedule = _generate_full_schedule(answers)
    for day_title, day_body in _split_days(schedule):
        body_clean = _strip_links(day_body.strip())
        line_bot_api.push_message(uid, TextSendMessage(text=body_clean))
        _send_schedule_buttons_for_day(uid, day_title, day_body)

    # ③ 実用ガイド（食事→ボタン→体験→ボタン→合計予算→チェックリスト）
    guide = _call_openai_text(build_guide_prompt(answers))
    sections = re.split(r"\n[-─]{6,}\n", guide)

    def _extract_blocks(section_text: str, head_re: re.Pattern):
        lines = section_text.splitlines()
        idxs = [i for i, ln in enumerate(lines) if head_re.search(ln)]
        blocks = []
        for j, start in enumerate(idxs):
            end = idxs[j+1] if j+1 < len(idxs) else len(lines)
            blocks.append("\n".join(lines[start:end]).strip())
        return blocks

    # 食事
    food_idx = next((i for i, s in enumerate(sections) if "食事おすすめ" in s), None)
    if food_idx is not None:
        food_blocks = _extract_blocks(sections[food_idx], re.compile(r"^\s*🍽\s*(?P<title>[^（\(\n]+)", re.M))[:3]
        if food_blocks:
            line_bot_api.push_message(uid, TextSendMessage(text=_strip_links("\n\n".join(food_blocks))))
            msgs = []
            for b in food_blocks:
                _, name, subtitle = _info_from_block(b)
                off = OFFICIAL_URL_RE.search(b); mp = MAP_URL_RE.search(b)
                actions = []
                if off: actions.append(URITemplateAction(label="公式サイトを見る", uri=_clean_url(off.group(1))))
                if mp:  actions.append(URITemplateAction(label="Googleマップ", uri=_clean_url(mp.group(1))))
                msgs.append(TemplateSendMessage(alt_text=name, template=ButtonsTemplate(title=name[:40], text=subtitle[:60], actions=actions[:4])))
            if msgs: _push_messages_in_chunks(uid, msgs, size=5)

    # 体験
    exp_idx  = next((i for i, s in enumerate(sections) if "体験予約" in s), None)
    if exp_idx is not None:
        exp_blocks = _extract_blocks(sections[exp_idx], re.compile(r"^\s*🎯\s*(?P<title>[^（\(\n]+)", re.M))[:3]
        if exp_blocks:
            line_bot_api.push_message(uid, TextSendMessage(text=_strip_links("\n\n".join(exp_blocks))))
            msgs = []
            for b in exp_blocks:
                _, name, subtitle = _info_from_block(b)
                off = OFFICIAL_URL_RE.search(b); mp = MAP_URL_RE.search(b)
                actions = []
                if off: actions.append(URITemplateAction(label="公式サイトを見る", uri=_clean_url(off.group(1))))
                if mp:  actions.append(URITemplateAction(label="Googleマップ", uri=_clean_url(mp.group(1))))
                msgs.append(TemplateSendMessage(alt_text=name, template=ButtonsTemplate(title=name[:40], text=subtitle[:60], actions=actions[:4])))
            if msgs: _push_messages_in_chunks(uid, msgs, size=5)

    # 合計予算・チェックリスト
    budget_idx = next((i for i, s in enumerate(sections) if "合計予算" in s), None)
    checklist_idx = next((i for i, s in enumerate(sections) if "チェックリスト" in s), None)
    if budget_idx is not None: line_bot_api.push_message(uid, TextSendMessage(text=sections[budget_idx].strip()))
    if checklist_idx is not None: line_bot_api.push_message(uid, TextSendMessage(text=sections[checklist_idx].strip()))

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
    "画像は各ブロック1枚。許可ドメイン：https://www.japan-guide.com / https://upload.wikimedia.org / https://images.unsplash.com 。"
    "無い場合は https://placehold.co/800x500.png?text={施設名} を使用。URLは生URL（Markdownリンク禁止）。\n"
    "日程表と実用ガイドでは**画像URLを一切出さない**。\n"
    "食事と体験は**固有の店名・施設名**を必ず記載し、各項目に Google マップ検索URL と営業時間・定休の情報を付けること。\n"
    "体験は**最低3つ**提示（料金目安・所要・予約要否・営業時間を明記）。\n"
    "重要：**旅程の最終日には宿泊/チェックインのブロックを入れない**。\n"
)

# ---------- 共通 ----------
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
        users[uid] = {"step": 0, "answers": {}, "hist": deque(maxlen=MAX_TURNS)}
        line_bot_api.reply_message(event.reply_token, _render_question(0, users[uid]))
        return

    # 初回
    if uid not in users or not users[uid]:
        users[uid] = {"step": 0, "answers": {}, "hist": deque(maxlen=MAX_TURNS)}
        line_bot_api.reply_message(event.reply_token, _render_question(0, users[uid]))
        return

    state = users[uid]; step = state["step"]

    # 入力検証
    if not _validate_and_store(uid, step, text):
        line_bot_api.reply_message(event.reply_token, _render_question(step, state))
        return

    # 言語は一度で確定して次へ（重複質問防止）
    step += 1; state["step"] = step
    if step < len(Q):
        line_bot_api.reply_message(event.reply_token, _render_question(step, state))
        return

    # === 全質問終了 → 指定順 ===
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
