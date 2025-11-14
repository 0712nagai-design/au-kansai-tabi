# -*- coding: utf-8 -*-
import os, re, sys, json, logging
from collections import defaultdict, deque
from datetime import datetime
from typing import Optional, Dict, Any, List
from urllib.parse import quote, urlsplit, urlunsplit

from flask import Flask, request, abort

# LINE SDK
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, LocationMessage,
    TextSendMessage,
    QuickReply, QuickReplyButton, LocationAction, FlexSendMessage
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

LAST_LANG: Dict[str, str] = {}
RESTART = {"start", "restart", "reset", "スタート", "最初から", "やり直す"}

FW_TO_HW = str.maketrans({
    "０":"0","１":"1","２":"2","３":"3","４":"4",
    "５":"5","６":"6","７":"7","８":"8","９":"9",
    "．":".","　":" "
})

# ======================
# ★ 画像URL：あなたの提供した4つを完全反映
# ======================
BUTTON_IMAGE_URLS = {
    ("request", "ホテル"):
    "https://chatgpt.com/backend-api/estuary/content?id=file_00000000757071faa8e3af6b9483df34&ts=489745&p=fs&cid=1&sig=a304932dd611a8db27b2798bcb522bd0a4b0b11e7ddcaccc5b5c48fd1f455372&v=0",

    ("request", "飲食店"):
    "https://chatgpt.com/backend-api/estuary/content?id=file_00000000806872079901498c978a67fd&ts=489745&p=fs&cid=1&sig=e515e49f1b2939acbb05866e4e93b2b4a3882f4afef7410ba30b5f03d9fb211a&v=0",

    ("request", "体験スポット"):
    "https://chatgpt.com/backend-api/estuary/content?id=file_0000000030687207a40f66a58e8395ba&ts=489745&p=fs&cid=1&sig=33966094d1a21074cd9ada0a7d330d399553125ce1dfdbc12b5f1107092faa17&v=0",

    ("request", "観光地"):
    "https://chatgpt.com/backend-api/estuary/content?id=file_0000000067f071fabb39b5d172f0412e&ts=489745&p=fs&cid=1&sig=169256d3a8655fea5342c3dcce80acdfbd6ee9a8daba441648e26990af63d7cb&v=0",

    # ★ 日程表はこのあと生成したらここに入れる
    ("request", "日程表"): "",
}

BUTTON_ICON_TEXT = {
    "ホテル":      "🏨",
    "飲食店":      "🍽",
    "体験スポット": "🎯",
    "観光地":      "🏯",
    "日程表":      "📅",
    "最初から":    "🔁",
}
# ====================== Flex ボタン生成 ======================

def _flex_choice_button(label: str, out_text: str, qkey: str) -> dict:
    """
    写真付き or シンプル版の質問ボタンを生成
    - request 質問（ホテル/飲食店/体験スポット/観光地/日程表）は写真付き
    - それ以外（都道府県/人数など）は文字のみのシンプルカード
    """
    img = BUTTON_IMAGE_URLS.get((qkey, label), "")
    icon = BUTTON_ICON_TEXT.get(label, "")

    # ================== 写真付きカード ==================
    if img:
        return {
            "type": "box",
            "layout": "vertical",
            "cornerRadius": "18px",
            "backgroundColor": "#F5EBDD",
            "paddingAll": "0px",
            "action": {"type": "message", "label": label, "text": out_text},
            "contents": [
                {
                    "type": "image",
                    "url": img,
                    "size": "full",
                    "aspectMode": "cover",
                    "aspectRatio": "1:1",
                    "cornerRadius": "18px 18px 0px 0px"
                },
                {
                    "type": "box",
                    "layout": "vertical",
                    "paddingAll": "14px",
                    "contents": [
                        {
                            "type": "text",
                            "text": f"{label}",
                            "weight": "bold",
                            "size": "lg",
                            "color": "#4A3728",
                            "align": "center"
                        }
                    ]
                }
            ]
        }

    # ================== シンプル文字だけカード ==================
    return {
        "type": "box",
        "layout": "vertical",
        "cornerRadius": "16px",
        "backgroundColor": "#F5EBDD",
        "paddingAll": "14px",
        "action": {"type": "message", "label": label, "text": out_text},
        "contents": [
            {
                "type": "text",
                "text": f"{label}",
                "weight": "bold",
                "size": "lg",
                "color": "#4A3728",
                "align": "center"
            }
        ]
    }

# ====================== Flex質問テンプレート生成 ======================

def _flex_question_bubble(title: str, selected_line: str, pairs: List[List[dict]], show_done: bool) -> dict:
    rows = []
    for row in pairs:
        if len(row) == 1:
            row.append({"type": "filler"})
        rows.append({
            "type": "box",
            "layout": "horizontal",
            "spacing": "14px",
            "contents": row
        })

    footer_contents = []
    if show_done:
        footer_contents.append({
            "type": "box",
            "layout": "vertical",
            "cornerRadius": "12px",
            "backgroundColor": "#22C55E",
            "paddingAll": "14px",
            "action": {"type": "message", "label": "完了", "text": "完了"},
            "contents": [{
                "type": "text",
                "text": "✅ 完了",
                "weight": "bold",
                "size": "lg",
                "align": "center",
                "color": "#FFFFFF"
            }]
        })

    footer_contents.append({
        "type": "text",
        "text": "↪ 最初から",
        "size": "14px",
        "color": "#4F46E5",
        "align": "center",
        "margin": "8px",
        "action": {"type": "message", "label": "最初から", "text": "最初から"}
    })

    return {
        "type": "bubble",
        "size": "mega",
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "14px",
            "paddingAll": "16px",
            "contents": [
                {"type": "text", "text": title, "wrap": True, "size": "xl", "weight": "bold"},
                {"type": "text", "text": selected_line, "size": "14px", "color": "#6B7280", "wrap": True} if selected_line else {"type": "filler"},
                {"type": "separator"},
                *rows
            ]
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "12px",
            "spacing": "8px",
            "contents": footer_contents
        }
    }

def _render_question(idx: int, state: State):
    seq = _get_question_sequence(state.get("answers", {}))
    q = seq[idx]

    title = q["title"]
    selected = state.get("multi_temp", {}).get(q["key"], []) if q.get("multi") else []
    selected_line = f"(選択中：{'、'.join(selected) if selected else 'なし'})" if q.get("multi") else ""

    pairs = []
    row = []

    for n, label in q.get("choices", {}).items():
        btn = _flex_choice_button(label, str(n), q["key"])
        row.append(btn)
        if len(row) == 2:
            pairs.append(row)
            row = []

    if row:
        pairs.append(row)

    bubble = _flex_question_bubble(title, selected_line, pairs, q.get("multi", False))
    return FlexSendMessage(alt_text=title, contents=bubble)

# ====================== 質問定義 ======================

LANG = {1: "日本語", 2: "English"}

REQUESTS = {
    1: "ホテル",
    2: "飲食店",
    3: "体験スポット",
    4: "観光地",
    5: "日程表"
}

PREFS_KANSAI = {1: "京都", 2: "大阪", 3: "奈良", 4: "兵庫", 5: "滋賀", 6: "和歌山"}

STAY_PLAN_HOTEL = {1: "日帰り", 2: "1泊2日", 3: "2泊3日", 4: "3泊4日", 5: "4泊5日", 6: "5泊6日"}
PEOPLE_HOTEL = {1: "1人", 2: "2人", 3: "3人", 4: "4人", 5: "5人", 6: "6人以上"}
HOTELS = {1: "高級", 2: "中価格", 3: "コスパ", 4: "和風旅館", 5: "こだわらない"}

MEAL_TIMES = {1: "朝", 2: "昼", 3: "夜"}
AREAS_FOOD = {1: "現在地から近く", 2: "京都", 3: "大阪", 4: "奈良", 5: "兵庫", 6: "滋賀", 7: "和歌山"}
PEOPLE_FOOD = {1: "1人", 2: "2人", 3: "3人", 4: "4人", 5: "5人", 6: "6人以上"}
COMPANION_FOOD = {1: "一人", 2: "カップル", 3: "友達", 4: "家族"}

CUISINES = {1: "和食", 2: "洋食", 3: "中華", 4: "ラーメン", 5: "カフェ・スイーツ", 6: "こだわらない"}
BUDGET_FOOD = {1: "～1000円", 2: "1000～2000円", 3: "2000～5000円", 4: "5000円以上"}

AREAS_EXP = PREFS_KANSAI.copy()
PEOPLE_EXP = {1: "1人", 2: "2人", 3: "3人", 4: "4人", 5: "5人", 6: "6人以上"}
COMPANION_EXP = COMPANION_FOOD.copy()

EXP_GENRES = {1: "温泉", 2: "自然体験", 3: "文化体験", 4: "モノづくり体験", 5: "グルメ・食体験"}

AREAS_SIGHT = PREFS_KANSAI.copy()

PREFS_MULTI = PREFS_KANSAI.copy()
STAY_PLAN_ITI = {1: "日帰り", 2: "1泊2日", 3: "2泊3日", 4: "3泊4日", 5: "4泊5日", 6: "5泊6日"}
THEMES_MULTI = {1: "グルメ", 2: "歴史文化", 3: "自然癒し", 4: "夜景", 5: "温泉", 6: "家族", 7: "ショッピング", 8: "体験メイン", 9: "その他"}
COMPANION_ITI = {1: "ひとり", 2: "カップル", 3: "友人", 4: "家族", 5: "外国人友人", 6: "その他"}
DEPT_CHOICES = {1: "6–8時", 2: "9–11時", 3: "12–14時", 4: "15–17時", 5: "18時以降"}
ARRV_CHOICES = {1: "14–17時", 2: "17–19時", 3: "19–21時", 4: "21時以降", 5: "未定"}
TRANSPORT_ITI = {1: "公共交通", 2: "車", 3: "徒歩中心"}
# ====================== OpenAI 呼び出し ======================

SYSTEM_PROMPT = (
    "You are AI Travel Navi Kansai.\n"
    "URLは生URL（Markdownリンク禁止）。画像URLは出さない。\n"
)

def _call_openai_text(user_prompt: str) -> str:
    res = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.6,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt}
        ],
    )
    return (res.choices[0].message.content or "").strip()

# ====================== 共通抽出用 ======================

OFFICIAL_URL_RE = re.compile(r"^(?:🔗\s*)?(?:公式|Official)\s*[:：]\s*(https?://[^\s)]+)", re.M)
MAP_URL_RE      = re.compile(r"^(?:📍\s*)?(?:Google ?マップ|Google ?Maps)\s*[:：]\s*(https?://[^\s)]+)", re.M | re.I)
PRICE_RE        = re.compile(r"(?:💰|価格帯|料金|料金目安|価格目安)[:：]\s*([^\n／]+)")
HOURS_RE        = re.compile(r"(?:🕰|営業時間|営業)[:：]\s*([^\n]+)")
DURA_RE         = re.compile(r"(?:⌛|所要|体験時間)[:：]\s*([^\n／]+)")
TIME_RANGE_RE   = re.compile(r"\b(\d{1,2}[:：]\d{2})\s*[–\-~〜]\s*(\d{1,2}[:：]\d{2})\b")
ACT_TITLE_RE    = re.compile(r"^[^\n：:]*[：:]\s*(?P<title>[^\n（(]+)", re.M)

# ====================== Soft URL Cleaner ======================

def _clean_url(u: str) -> str:
    if not u:
        return ""
    u = (u.replace("\u200b","")
           .replace("\u200c","")
           .replace("\u200d","")
           .replace("\ufeff","")
           .strip().strip("。．、 ，)）]］>＞」』"))
    if u.startswith("http://"):
        u = "https://" + u[len("http://"):]
    try:
        scheme, netloc, path, query, frag = urlsplit(u)
        path = quote(path, safe="/:-_.~")
        if query:
            parts = []
            for p in query.split("&"):
                if "=" in p:
                    k, v = p.split("=", 1)
                    parts.append(f"{quote(k, safe='')}={quote(v, safe='')}")
                else:
                    parts.append(quote(p, safe=""))
            query = "&".join(parts)
        u = urlunsplit((scheme or "https", netloc, path, query, ""))
    except:
        u = u.replace(" ", "%20")
    return u

def _normalize_map_url(u: str, fallback_query: str = "") -> str:
    u = _clean_url(u)
    if not u:
        return ""
    if ("maps.app.goo.gl" in u) or ("google." in u and "/maps" in u):
        return u
    if re.fullmatch(r"\(?-?\d+(\.\d+)?\s*,\s*-?\d+(\.\d+)?\)?", u):
        coords = u.strip("()").replace(" ", "")
        return f"https://www.google.com/maps/search/?api=1&query={quote(coords)}"
    q = fallback_query or u
    return f"https://www.google.com/maps/search/?api=1&query={quote(q)}"

# ====================== ホテル生成 ======================

def build_hotel3_prompt(answers: Dict[str, Any]) -> str:
    answers_json = json.dumps(answers, ensure_ascii=False, indent=2)
    return f"""
あなたは関西旅行のホテルコンシェルジュです。
以下のユーザー条件に合うホテル候補を**ちょうど3件**出してください。
必ず「公式：URL」「Googleマップ：URL」を含めること。
存在しないお店・ホテルは生成しないでください（実在確認を厳密に）。

【条件】
{answers_json}

【フォーマット】
🏨 ホテル正式名称（最寄エリア）
特徴：1行
💰 価格目安：
🔗 公式：https://...
📍 Googleマップ：https://...
"""

def _parse_hotel_block(block: str):
    lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
    name = ""
    if lines:
        name = re.sub(r"^\s*[🏨\d\.\)\）\s]*", "", lines[0])
    short = ""
    mshort = re.search(r"^特徴[:：]\s*(.+)$", block, re.M)
    if mshort:
        short = mshort.group(1).strip()
    mprice = PRICE_RE.search(block)
    price = mprice.group(1).strip() if mprice else ""
    moff = OFFICIAL_URL_RE.search(block)
    mmap = MAP_URL_RE.search(block)
    return {
        "name": name or "ホテル",
        "desc": short,
        "price": price,
        "official": _clean_url(moff.group(1)) if moff else "",
        "map": _clean_url(mmap.group(1)) if mmap else ""
    }

def _send_hotels_three(uid: str, reply_token: str, hotels_text: str):
    blocks = [b.strip() for b in re.split(r"\n\s*\n", hotels_text) if b.strip()][:3]
    if not blocks:
        line_bot_api.reply_message(reply_token, TextSendMessage(text="ホテル候補が見つかりませんでした。"))
        return

    # テキスト
    line_bot_api.reply_message(reply_token, TextSendMessage(text="🏨 条件に合うホテル3件です👇"))

    items = []
    for block in blocks:
        info = _parse_hotel_block(block)
        txt = f"🏨 {info['name']}\n{info['desc']}\n💰 {info['price']}"
        line_bot_api.push_message(uid, TextSendMessage(text=txt))

        items.append({
            "title": info["name"],
            "subtitle": info["desc"][:60],
            "official": info["official"],
            "map": info["map"]
        })

    # Flex
    line_bot_api.push_message(uid, _flex_list_bubble("🏨 ホテル候補（3件）", items))

# ====================== 飲食店 ======================

def build_food3_prompt(answers: Dict[str, Any]) -> str:
    geo = answers.get("geo")
    near_hint = ""
    if geo:
        near_hint = f"現在地({geo})から半径700m以内の実在店のみを出すこと。"
    answers_json = json.dumps(answers, ensure_ascii=False, indent=2)
    return f"""
あなたは関西のグルメ専門AIです。
以下の条件に合う飲食店を**ちょうど3件**、すべて“実在する店舗のみ”出してください。
虚偽の店名は禁止。
必ず「公式URL」「GoogleマップURL」を含めること。

{near_hint}

【条件】
{answers_json}

【フォーマット】
🍽 店名（最寄駅）
短評：
💰 価格帯：
🕰 営業：
🔗 公式：https://...
📍 Googleマップ：https://...
"""

def _parse_food_block(block: str):
    lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
    name = re.sub(r"^\s*[🍽\d\.\)\）\s]*", "", lines[0]) if lines else "飲食店"
    mshort = re.search(r"^短評[:：]\s*(.+)$", block, re.M)
    short = mshort.group(1).strip() if mshort else ""
    mprice = PRICE_RE.search(block)
    price = mprice.group(1).strip() if mprice else ""
    mhours = HOURS_RE.search(block)
    hours = mhours.group(1).strip() if mhours else ""
    moff = OFFICIAL_URL_RE.search(block)
    mmap = MAP_URL_RE.search(block)
    return {
        "name": name,
        "short": short,
        "price": price,
        "hours": hours,
        "official": _clean_url(moff.group(1)) if moff else "",
        "map": _normalize_map_url(mmap.group(1), fallback_query=name) if mmap else ""
    }

def _send_food_three(uid, reply_token, text):
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()][:3]
    if not blocks:
        line_bot_api.reply_message(reply_token, TextSendMessage(text="飲食店が見つかりませんでした。"))
        return

    line_bot_api.reply_message(reply_token, TextSendMessage(text="🍽 条件に合う飲食店3件です👇"))

    items = []
    for block in blocks:
        info = _parse_food_block(block)
        txt = f"🍽 {info['name']}\n{info['short']}\n💰 {info['price']}\n🕰 {info['hours']}"
        line_bot_api.push_message(uid, TextSendMessage(text=txt))

        items.append({
            "title": info["name"],
            "subtitle": info["short"][:60],
            "official": info["official"],
            "map": info["map"]
        })

    line_bot_api.push_message(uid, _flex_list_bubble("🍽 飲食店（3件）", items))

# ====================== 体験スポット ======================

def build_experience3_prompt(answers):
    answers_json = json.dumps(answers, ensure_ascii=False, indent=2)
    return f"""
あなたは関西観光の体験アクティビティ専門AIです。
以下の条件に合う体験スポット（陶芸・着物・和菓子作りなど）を3件出してください。
必ず実在施設のみ。公式とGoogleマップを含めること。

【条件】
{answers_json}

【フォーマット】
🎯 施設名
短評：
💰 料金：
⌛ 所要：
🕰 営業：
🔗 公式：https://...
📍 Googleマップ：https://...
"""

def _parse_experience_block(block):
    lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
    name = re.sub(r"^\s*[🎯\d\.\)\）\s]*", "", lines[0]) if lines else "体験スポット"
    mshort = re.search(r"^短評[:：]\s*(.+)$", block, re.M)
    short = mshort.group(1).strip() if mshort else ""
    mprice = PRICE_RE.search(block)
    price = mprice.group(1).strip() if mprice else ""
    mhours = HOURS_RE.search(block)
    hours = mhours.group(1).strip() if mhours else ""
    mdura = DURA_RE.search(block)
    dura = mdura.group(1).strip() if mdura else ""
    moff = OFFICIAL_URL_RE.search(block)
    mmap = MAP_URL_RE.search(block)
    return {
        "name": name,
        "short": short,
        "price": price,
        "hours": hours,
        "dura": dura,
        "official": _clean_url(moff.group(1)) if moff else "",
        "map": _normalize_map_url(mmap.group(1), fallback_query=name) if mmap else ""
    }

def _send_experiences_three(uid, reply_token, text):
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()][:3]
    if not blocks:
        line_bot_api.reply_message(reply_token, TextSendMessage(text="体験スポットが見つかりませんでした。"))
        return

    line_bot_api.reply_message(reply_token, TextSendMessage(text="🎯 条件に合う体験スポット3件です👇"))

    items = []
    for block in blocks:
        info = _parse_experience_block(block)
        txt = f"🎯 {info['name']}\n{info['short']}\n💰 {info['price']}\n⌛ {info['dura']}\n🕰 {info['hours']}"
        line_bot_api.push_message(uid, TextSendMessage(text=txt))

        items.append({
            "title": info["name"],
            "subtitle": info["short"][:60],
            "official": info["official"],
            "map": info["map"]
        })

    line_bot_api.push_message(uid, _flex_list_bubble("🎯 体験スポット（3件）", items))

# ====================== 観光地 ======================

def build_sightseeing3_prompt(answers):
    answers_json = json.dumps(answers, ensure_ascii=False, indent=2)
    return f"""
あなたは関西旅行の観光案内専門AIです。
以下の条件に合う観光地（寺社仏閣・城・展望台等）を3件出してください。
必ず実在スポットのみ。公式とGoogleマップ必須。

【条件】
{answers_json}
"""

def _parse_sightseeing_block(block):
    lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
    name = re.sub(r"^\s*[🏯\d\.\)\）\s]*", "", lines[0]) if lines else "観光地"
    mshort = re.search(r"^短評[:：]\s*(.+)$", block, re.M)
    short = mshort.group(1).strip() if mshort else ""
    mprice = PRICE_RE.search(block)
    price = mprice.group(1).strip() if mprice else "無料"
    mhours = HOURS_RE.search(block)
    hours = mhours.group(1).strip() if mhours else ""
    moff = OFFICIAL_URL_RE.search(block)
    mmap = MAP_URL_RE.search(block)
    return {
        "name": name,
        "short": short,
        "price": price,
        "hours": hours,
        "official": _clean_url(moff.group(1)) if moff else "",
        "map": _normalize_map_url(mmap.group(1), fallback_query=name) if mmap else ""
    }

def _send_sightseeing_three(uid, reply_token, text):
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()][:3]
    if not blocks:
        line_bot_api.reply_message(reply_token, TextSendMessage(text="観光地が見つかりませんでした。"))
        return

    line_bot_api.reply_message(reply_token, TextSendMessage(text="🏯 条件に合う観光地3件です👇"))

    items = []
    for block in blocks:
        info = _parse_sightseeing_block(block)
        txt = f"🏯 {info['name']}\n{info['short']}\n💰 {info['price']}\n🕰 {info['hours']}"
        line_bot_api.push_message(uid, TextSendMessage(text=txt))

        items.append({
            "title": info["name"],
            "subtitle": info["short"][:60],
            "official": info["official"],
            "map": info["map"]
        })

    line_bot_api.push_message(uid, _flex_list_bubble("🏯 観光地（3件）", items))

# ====================== 日程表 ======================

DAY_HEAD_RE = re.compile(r"^Day\s*\d+", re.M | re.I)
BLOCK_SPLIT_RE = re.compile(r"\n\s*↓\s*\n", re.M)

def build_itinerary_prompt(answers):
    answers_json = json.dumps(answers, ensure_ascii=False, indent=2)
    prefs = "、".join(answers.get("prefs", [])) if isinstance(answers.get("prefs"), list) else answers.get("prefs","")
    themes = "、".join(answers.get("themes", [])) if isinstance(answers.get("themes"), list) else answers.get("themes","")

    return f"""
あなたは関西旅行の旅行プランナーです。
以下の条件に基づいて**実在スポットのみ**を使った濃い日程表を作成してください。
各日3〜5スポット以上、短評1行、公式URL・GoogleマップURL必須。

【条件】
{answers_json}
"""

def _blocks_in_day(day_text: str):
    return [b.strip() for b in BLOCK_SPLIT_RE.split(day_text) if b.strip()]

def _send_itinerary(uid, reply_token, schedule_text):
    parts = []
    positions = [(m.group(0).strip(), m.start()) for m in DAY_HEAD_RE.finditer(schedule_text)]
    for i, (title, start) in enumerate(positions):
        end = positions[i+1][1] if i+1 < len(positions) else len(schedule_text)
        parts.append((title, schedule_text[start:end]))

    if not parts:
        line_bot_api.reply_message(reply_token, TextSendMessage(text="日程表の生成に失敗しました。"))
        return

    # 1日ずつ送る
    for day_title, day_body in parts:
        line_bot_api.push_message(uid, TextSendMessage(text=f"📅 {day_title}"))

        items = []
        for block in _blocks_in_day(day_body):
            off = OFFICIAL_URL_RE.search(block)
            mp  = MAP_URL_RE.search(block)

            mtitle = ACT_TITLE_RE.search(block)
            name = mtitle.group("title") if mtitle else "スポット"

            mshort = re.search(r"^短評[:：]\s*(.+)$", block, re.M)
            short = mshort.group(1) if mshort else ""

            item = {
                "title": name[:60],
                "subtitle": (short or " ")[:60],
                "official": _clean_url(off.group(1)) if off else "",
                "map": _clean_url(mp.group(1)) if mp else ""
            }
            items.append(item)

        for trio in _chunk(items, 3):
            line_bot_api.push_message(uid, _flex_list_bubble(f"{day_title} の予定", trio))

# ====================== メイン送信フロー ======================

def send_plan_parts(reply_token, uid, answers):
    req = answers.get("request")

    if req == "ホテル":
        text = _call_openai_text(build_hotel3_prompt(answers))
        _send_hotels_three(uid, reply_token, text)
        _send_finish_menu(uid)
        return

    if req == "飲食店":
        text = _call_openai_text(build_food3_prompt(answers))
        _send_food_three(uid, reply_token, text)
        _send_finish_menu(uid)
        return

    if req == "体験スポット":
        text = _call_openai_text(build_experience3_prompt(answers))
        _send_experiences_three(uid, reply_token, text)
        _send_finish_menu(uid)
        return

    if req == "観光地":
        text = _call_openai_text(build_sightseeing3_prompt(answers))
        _send_sightseeing_three(uid, reply_token, text)
        _send_finish_menu(uid)
        return

    if req == "日程表":
        text = _call_openai_text(build_itinerary_prompt(answers))
        _send_itinerary(uid, reply_token, text)
        _send_finish_menu(uid)
        return


# ====================== on_message ======================

@handler.add(MessageEvent, message=TextMessage)
def on_message(event: MessageEvent):
    uid = event.source.user_id
    text = (event.message.text or "").strip()

    # 直接ジャンプ
    if text in {"ホテル","飲食店","体験スポット","観光地","日程表"}:
        users[uid] = {
            "step": 1,
            "answers": {"request": text},
            "multi_temp": {}
        }
        line_bot_api.reply_message(event.reply_token, _render_question(1, users[uid]))
        return

    if text in RESTART:
        users[uid] = {"step": 0, "answers": {}, "multi_temp": {}}
        line_bot_api.reply_message(event.reply_token, _render_question(0, users[uid]))
        return

    if uid not in users:
        users[uid] = {"step": 0, "answers": {}, "multi_temp": {}}
        line_bot_api.reply_message(event.reply_token, _render_question(0, users[uid]))
        return

    # 質問処理
    state = users[uid]
    step = state.get("step", 0)

    ok = _validate_and_store(uid, step, text)
    if not ok:
        line_bot_api.reply_message(event.reply_token, _render_question(step, state))
        return

    seq = _get_question_sequence(state["answers"])
    q_now = seq[step]

    if q_now.get("multi") and text != "完了" and not state.pop("_autodone", False):
        line_bot_api.reply_message(event.reply_token, _render_question(step, state))
        return

    # 飲食店で現在地選択 → 位置情報要求
    if state["answers"].get("request") == "飲食店" and q_now["key"] == "area":
        if state.get("need_location") and not state.get("geo"):
            _ask_location(event.reply_token)
            return

    # 次へ
    state["step"] = step + 1

    if state["step"] < len(seq):
        line_bot_api.reply_message(event.reply_token, _render_question(state["step"], state))
        return

    # 最後 → 提案
    answers = state["answers"].copy()
    send_plan_parts(event.reply_token, uid, answers)

    users.pop(uid, None)

# ====================== on_location ======================

@handler.add(MessageEvent, message=LocationMessage)
def on_location(event: MessageEvent):
    uid = event.source.user_id
    lat = event.message.latitude
    lng = event.message.longitude

    if uid not in users:
        users[uid] = {"step": 0, "answers": {}, "multi_temp": {}}

    state = users[uid]
    state["answers"]["geo"] = f"{lat},{lng}"
    state["geo"] = (lat, lng)
    state["need_location"] = False

    seq = _get_question_sequence(state["answers"])
    state["step"] += 1

    if state["step"] < len(seq):
        line_bot_api.reply_message(event.reply_token, _render_question(state["step"], state))
        return

    send_plan_parts(event.reply_token, uid, state["answers"])
    users.pop(uid, None)


# ====================== 起動 ======================

if __name__ == "__main__":
    logging.getLogger().setLevel(logging.INFO)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)


