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
    TemplateSendMessage, ButtonsTemplate, URITemplateAction
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

# ====================== URL抽出用 正規表現群 ======================
OFFICIAL_URL_RE = re.compile(r"^(?:🔗\s*)?(?:公式|Official)\s*[:：]\s*(https?://[^\s)]+)", re.M)
MAP_URL_RE = re.compile(r"^(?:📍\s*)?(?:Google ?マップ|Google ?Maps)\s*[:：]\s*(https?://[^\s)]+)", re.M | re.I)
SECTION_SPLIT_RE = re.compile(r"\n[-─]{6,}\n")
FOOD_HEAD_RE = re.compile(r"^\s*🍽\s*(?P<title>[^（\(\n]+)", re.M)
EXPER_HEAD_RE = re.compile(r"^\s*🎯\s*(?P<title>[^（\(\n]+)", re.M)
DAY_HEAD_RE = re.compile(r"^Day\s*\d+", re.M | re.I)
BLOCK_SPLIT_RE = re.compile(r"\n\s*↓\s*\n", re.M)
ACT_TITLE_RE = re.compile(r"^[^\n：:]*[：:]\s*(?P<title>[^\n（(]+)", re.M)

# ====================== 共通ヘルパー ======================
def _push_messages_in_chunks(uid: str, msgs, size: int = 5):
    for i in range(0, len(msgs), size):
        chunk = msgs[i:i+size]
        line_bot_api.push_message(uid, chunk if len(chunk) > 1 else chunk[0])

# ====================== ホテル ======================
def _send_hotels_as_buttons(reply_token: str, hotels_text: str):
    blocks = re.split(r"\n[-─]{6,}\n|\n{2,}", hotels_text.strip())
    msgs = []
    for b in blocks:
        if not b.strip(): continue
        first_line = next((ln.strip() for ln in b.splitlines() if ln.strip()), "")
        title = re.sub(r"^\s*[①-⑳]?\s*[🏨\d\.\)\）\s]*", "", first_line) or "ホテル"
        off, mp = OFFICIAL_URL_RE.search(b), MAP_URL_RE.search(b)
        actions = []
        if off: actions.append(URITemplateAction(label="公式サイトを見る", uri=off.group(1)))
        if mp: actions.append(URITemplateAction(label="Googleマップ", uri=mp.group(1)))
        if not actions: continue
        msgs.append(TemplateSendMessage(alt_text=title,
            template=ButtonsTemplate(title=title[:40], text="リンクを選択してください", actions=actions[:4])))
    if msgs: line_bot_api.reply_message(reply_token, msgs[:5])
    else: line_bot_api.reply_message(reply_token, TextSendMessage(text=hotels_text))

# ====================== 実用ガイド（食事・体験） ======================
def _extract_blocks_by_head(section_text: str, head_re: re.Pattern):
    lines = section_text.splitlines()
    idxs = [i for i, ln in enumerate(lines) if head_re.search(ln)]
    return ["\n".join(lines[idxs[j]:idxs[j+1]] if j+1 < len(idxs) else lines[idxs[j]:]).strip() for j in range(len(idxs))]

def _build_buttons_from_blocks(blocks, head_re):
    msgs = []
    for b in blocks:
        m = head_re.search(b or "")
        title = (m.group("title").strip() if m else "リスト")
        off, mp = OFFICIAL_URL_RE.search(b), MAP_URL_RE.search(b)
        actions = []
        if off: actions.append(URITemplateAction(label="公式サイトを見る", uri=off.group(1)))
        if mp: actions.append(URITemplateAction(label="Googleマップ", uri=mp.group(1)))
        if not actions: continue
        msgs.append(TemplateSendMessage(
            alt_text=title,
            template=ButtonsTemplate(title=title[:40], text="リンクを選択してください", actions=actions[:4])
        ))
    return msgs

def _send_guide_as_buttons(uid: str, guide_text: str):
    sections = SECTION_SPLIT_RE.split(guide_text)
    food_idx = next((i for i, s in enumerate(sections) if "食事おすすめ" in s), None)
    exp_idx = next((i for i, s in enumerate(sections) if "体験予約" in s), None)
    msgs = []
    if food_idx is not None:
        msgs += _build_buttons_from_blocks(_extract_blocks_by_head(sections[food_idx], FOOD_HEAD_RE), FOOD_HEAD_RE)
    if exp_idx is not None:
        msgs += _build_buttons_from_blocks(_extract_blocks_by_head(sections[exp_idx], EXPER_HEAD_RE), EXPER_HEAD_RE)
    if msgs: _push_messages_in_chunks(uid, msgs)

# ====================== 日程表 ======================
def _split_days(schedule_text: str):
    parts, positions = [], [(m.group(0).strip(), m.start()) for m in DAY_HEAD_RE.finditer(schedule_text)]
    for i, (title, start) in enumerate(positions):
        end = positions[i+1][1] if i+1 < len(positions) else len(schedule_text)
        parts.append((title, schedule_text[start:end]))
    return parts

def _blocks_in_day(day_text: str): return [b.strip() for b in BLOCK_SPLIT_RE.split(day_text.strip()) if b.strip()]

def _title_from_block(block: str):
    m = ACT_TITLE_RE.search(block)
    return m.group("title").strip() if m else (block.splitlines()[0][:40] if block.strip() else "スポット")

def _send_schedule_as_buttons(uid: str, schedule_text: str):
    msgs = []
    for day_title, day_body in _split_days(schedule_text):
        for block in _blocks_in_day(day_body):
            off, mp = OFFICIAL_URL_RE.search(block), MAP_URL_RE.search(block)
            if not (off or mp): continue
            title = _title_from_block(block)
            actions = []
            if off: actions.append(URITemplateAction(label="公式サイトを見る", uri=off.group(1)))
            if mp: actions.append(URITemplateAction(label="Googleマップ", uri=mp.group(1)))
            desc = f"{day_title}｜{block.splitlines()[0][:40]}" if block.strip() else day_title
            msgs.append(TemplateSendMessage(
                alt_text=f"{day_title}-{title}",
                template=ButtonsTemplate(title=title[:40], text=desc[:60], actions=actions[:4])
            ))
    if msgs: _push_messages_in_chunks(uid, msgs)

# ====================== send_plan_parts ======================
def send_plan_parts(reply_token: str, uid: str, answers: Dict[str, Any]):
    # ① ホテル（ボタン）
    hotels = _call_openai_text(build_hotel_prompt(answers))
    _send_hotels_as_buttons(reply_token, hotels)
    # ② 日程表（テキスト＋ボタン）
    schedule = _call_openai_text(build_schedule_prompt(answers))
    need = _required_days(answers); got = _count_days_in_text(schedule)
    if got < need: schedule += f"\n\n（補足）現在 {got} 日分です。{need} 日分になるよう続きも含めて出力してください。"
    line_bot_api.push_message(uid, TextSendMessage(text=schedule))
    _send_schedule_as_buttons(uid, schedule)
    # ③ ガイド（テキスト＋🍽/🎯ボタン）
    guide = _call_openai_text(build_guide_prompt(answers))
    line_bot_api.push_message(uid, TextSendMessage(text=guide))
    _send_guide_as_buttons(uid, guide)
    # ④ 総評
    review = _call_openai_text(build_review_prompt(answers))
    line_bot_api.push_message(uid, TextSendMessage(text=review))
    # ⑤ 次の操作メニュー
    nxt = _call_openai_text(build_next_prompt(answers))
    line_bot_api.push_message(uid, TextSendMessage(text=nxt))




























