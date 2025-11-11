# -*- coding: utf-8 -*-
import os, re, sys, json, logging
from collections import defaultdict, deque
from datetime import datetime
from typing import Optional, Dict, Any, List

from flask import Flask, request, abort

# LINE SDK
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, ImageSendMessage,
    TemplateSendMessage, ButtonsTemplate, URITemplateAction,
    QuickReply, QuickReplyButton, MessageAction, FlexSendMessage
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
    {"key": "region", "title": "地域を選んでください（複数選択可）", "choices": REGIONS, "multi": True},
    {"key": "date", "title": "出発日を入力してください（例: 2025-03-20）", "choices": {}, "multi": False},
    {"key": "stay", "title": "日程を選択してください。", "choices": {1: "日帰り", 2: "1泊2日", 3: "2泊3日", 4: "3泊以上"}, "multi": False},
    {"key": "theme", "title": "テーマを選んでください（複数選択可）", "choices": THEMES, "multi": True},
    {"key": "budget", "title": "予算（1人）を選んでください。", "choices": BUDGETS, "multi": False},
    {"key": "hotel", "title": "ホテルタイプを選んでください。", "choices": HOTELS, "multi": False},
    {"key": "transport", "title": "交通手段を選んでください（複数選択可）", "choices": TRANSPORT, "multi": True},
    {"key": "companion", "title": "同行者を選んでください。", "choices": COMPANION, "multi": False},
    {"key": "dept", "title": "出発時間帯を選んでください。", "choices": DEPT, "multi": False},
    {"key": "arrv", "title": "帰着時間帯を選んでください。", "choices": ARRV, "multi": False},
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

# ====================== 質問レンダリング（Flex版：スクショの見た目） ======================
def _flex_choice_button(label: str, out_text: str) -> dict:
    return {
        "type": "box",
        "layout": "vertical",
        "cornerRadius": "16px",
        "backgroundColor": "#EEF2F7",
        "height": "76px",
        "paddingAll": "0px",
        "justifyContent": "center",
        "action": {"type": "message", "label": label, "text": out_text},
        "contents": [{
            "type": "text",
            "text": label,
            "weight": "bold",
            "size": "20px",
            "align": "center",
            "color": "#111111",
            "wrap": False
        }]
    }

def _flex_question_bubble(title: str, selected_line: str, pairs: List[List[dict]], show_done: bool) -> dict:
    rows = []
    for row in pairs:
        # 奇数個なら右側フィラーで左右幅を揃える
        if len(row) == 1:
            row.append({"type": "filler"})
        rows.append({"type": "box", "layout": "horizontal", "spacing": "14px", "contents": row})

    footer_contents = []
    if show_done:
        footer_contents.append({
            "type": "box",
            "layout": "vertical",
            "cornerRadius": "12px",
            "backgroundColor": "#22C55E",
            "paddingAll": "14px",
            "action": {"type": "message", "label": "✅ 完了", "text": "完了"},
            "contents": [{
                "type": "text", "text": "✅ 完了", "weight": "bold",
                "size": "20px", "align": "center", "color": "#FFFFFF"
            }]
        })
    footer_contents.append({
        "type": "text", "text": "↪ 最初から", "size": "14px", "color": "#4F46E5",
        "align": "center", "margin": "8px",
        "action": {"type": "message", "label": "最初から", "text": "最初から"}
    })

    return {
        "type": "bubble", "size": "mega",
        "body": {
            "type": "box", "layout": "vertical", "spacing": "12px", "paddingAll": "16px",
            "contents": [
                {"type": "text", "text": title, "wrap": True, "size": "24px", "weight": "bold"},
                ({"type": "text", "text": selected_line, "size": "14px", "color": "#6B7280", "wrap": True}
                 if selected_line else {"type": "filler"}),
                {"type": "separator"},
                *rows
            ]
        },
        "footer": {"type": "box", "layout": "vertical", "spacing": "6px",
                   "paddingAll": "12px", "contents": footer_contents}
    }

def _render_question(idx: int, state: State):
    q = Q[idx]
    title = q["title"]
    selected = state.get("multi_temp", {}).get(q["key"], []) if q["multi"] else []
    selected_line = f"(選択中：{'、'.join(selected) if selected else 'なし'})" if q["multi"] else ""

    pairs, row = [], []
    for n, label in q.get("choices", {}).items():
        btn = _flex_choice_button(f"{n} {label}", str(n))
        row.append(btn)
        if len(row) == 2:
            pairs.append(row); row = []
    if row:
        pairs.append(row)

    bubble = _flex_question_bubble(title, selected_line, pairs, q["multi"])
    return FlexSendMessage(alt_text=title, contents=bubble)

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

    if key == "date":
        try:
            datetime.strptime(text.strip(), "%Y-%m-%d")
            state["answers"][key] = text.strip()
            return True
        except Exception:
            return False

    if q["multi"] and text.strip() == "完了":
        picked = state["multi_temp"].get(key, [])
        if not picked: return False
        state["answers"][key] = picked
        return True

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
# -*- coding: utf-8 -*-
import os, re, sys, json, logging
from collections import defaultdict, deque
from datetime import datetime
from typing import Optional, Dict, Any, List

from flask import Flask, request, abort

# LINE SDK
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, ImageSendMessage,
    TemplateSendMessage, ButtonsTemplate, URITemplateAction,
    QuickReply, QuickReplyButton, MessageAction, FlexSendMessage
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
    {"key": "region", "title": "地域を選んでください（複数選択可）", "choices": REGIONS, "multi": True},
    {"key": "date", "title": "出発日を入力してください（例: 2025-03-20）", "choices": {}, "multi": False},
    {"key": "stay", "title": "日程を選択してください。", "choices": {1: "日帰り", 2: "1泊2日", 3: "2泊3日", 4: "3泊以上"}, "multi": False},
    {"key": "theme", "title": "テーマを選んでください（複数選択可）", "choices": THEMES, "multi": True},
    {"key": "budget", "title": "予算（1人）を選んでください。", "choices": BUDGETS, "multi": False},
    {"key": "hotel", "title": "ホテルタイプを選んでください。", "choices": HOTELS, "multi": False},
    {"key": "transport", "title": "交通手段を選んでください（複数選択可）", "choices": TRANSPORT, "multi": True},
    {"key": "companion", "title": "同行者を選んでください。", "choices": COMPANION, "multi": False},
    {"key": "dept", "title": "出発時間帯を選んでください。", "choices": DEPT, "multi": False},
    {"key": "arrv", "title": "帰着時間帯を選んでください。", "choices": ARRV, "multi": False},
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

# ====================== 質問レンダリング（Flex版：スクショの見た目） ======================
def _flex_choice_button(label: str, out_text: str) -> dict:
    return {
        "type": "box",
        "layout": "vertical",
        "cornerRadius": "16px",
        "backgroundColor": "#EEF2F7",
        "height": "76px",
        "paddingAll": "0px",
        "justifyContent": "center",
        "action": {"type": "message", "label": label, "text": out_text},
        "contents": [{
            "type": "text",
            "text": label,
            "weight": "bold",
            "size": "20px",
            "align": "center",
            "color": "#111111",
            "wrap": False
        }]
    }

def _flex_question_bubble(title: str, selected_line: str, pairs: List[List[dict]], show_done: bool) -> dict:
    rows = []
    for row in pairs:
        # 奇数個なら右側フィラーで左右幅を揃える
        if len(row) == 1:
            row.append({"type": "filler"})
        rows.append({"type": "box", "layout": "horizontal", "spacing": "14px", "contents": row})

    footer_contents = []
    if show_done:
        footer_contents.append({
            "type": "box",
            "layout": "vertical",
            "cornerRadius": "12px",
            "backgroundColor": "#22C55E",
            "paddingAll": "14px",
            "action": {"type": "message", "label": "✅ 完了", "text": "完了"},
            "contents": [{
                "type": "text", "text": "✅ 完了", "weight": "bold",
                "size": "20px", "align": "center", "color": "#FFFFFF"
            }]
        })
    footer_contents.append({
        "type": "text", "text": "↪ 最初から", "size": "14px", "color": "#4F46E5",
        "align": "center", "margin": "8px",
        "action": {"type": "message", "label": "最初から", "text": "最初から"}
    })

    return {
        "type": "bubble", "size": "mega",
        "body": {
            "type": "box", "layout": "vertical", "spacing": "12px", "paddingAll": "16px",
            "contents": [
                {"type": "text", "text": title, "wrap": True, "size": "24px", "weight": "bold"},
                ({"type": "text", "text": selected_line, "size": "14px", "color": "#6B7280", "wrap": True}
                 if selected_line else {"type": "filler"}),
                {"type": "separator"},
                *rows
            ]
        },
        "footer": {"type": "box", "layout": "vertical", "spacing": "6px",
                   "paddingAll": "12px", "contents": footer_contents}
    }

def _render_question(idx: int, state: State):
    q = Q[idx]
    title = q["title"]
    selected = state.get("multi_temp", {}).get(q["key"], []) if q["multi"] else []
    selected_line = f"(選択中：{'、'.join(selected) if selected else 'なし'})" if q["multi"] else ""

    pairs, row = [], []
    for n, label in q.get("choices", {}).items():
        btn = _flex_choice_button(f"{n} {label}", str(n))
        row.append(btn)
        if len(row) == 2:
            pairs.append(row); row = []
    if row:
        pairs.append(row)

    bubble = _flex_question_bubble(title, selected_line, pairs, q["multi"])
    return FlexSendMessage(alt_text=title, contents=bubble)

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

    if key == "date":
        try:
            datetime.strptime(text.strip(), "%Y-%m-%d")
            state["answers"][key] = text.strip()
            return True
        except Exception:
            return False

    if q["multi"] and text.strip() == "完了":
        picked = state["multi_temp"].get(key, [])
        if not picked: return False
        state["answers"][key] = picked
        return True

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
# ----------- ホテル出力（1件のみ、導入＋説明＋ボタン） -----------
    def build_hotel_prompt(answers: Dict[str, Any]) -> str:
    # ホテルは 1件だけ出力させる（_send_hotel_single で1件だけ使うため）
    answers_json = json.dumps(answers, ensure_ascii=False, indent=2)
    return f"""
以下は「ホテル候補」セクションの出力指示です。
ユーザー回答に基づいて最適なホテルを**1件のみ**出力してください。
後段でボタン化するため、必ず「公式」と「Googleマップ」のURL行を含めてください。

【ユーザー回答(JSON参照用)】
{answers_json}

出力形式（この1件のみ）：
🏨 ホテル正式名称
特徴：1行要約
💰 価格目安：〜円／泊
🔗 公式：URL
📍 Googleマップ：URL
"""

def _parse_hotel_block(block: str):
    """ホテル情報を1件分パース"""
    name = ""
    desc = ""
    price = ""
    off = None
    mp = None

    lines = [ln.strip() for ln in block.strip().splitlines() if ln.strip()]
    if lines:
        name = re.sub(r"^\s*[①-⑳]?\s*[🏨\d\.\)\）\s]*", "", lines[0])

    mdesc  = re.search(r"^特徴[:：]\s*(.+)$", block, re.M)
    mprice = re.search(r"(?:💰|価格目安|料金目安)[:：]\s*([^\n]+)", block)
    moff   = OFFICIAL_URL_RE.search(block)
    mmap   = MAP_URL_RE.search(block)

    if mdesc:  desc = mdesc.group(1).strip()
    if mprice: price = mprice.group(1).strip()
    if moff:   off   = _clean_url(moff.group(1))
    if mmap:   mp    = _clean_url(mmap.group(1))

    return {
        "name": name or "ホテル",
        "desc": desc,
        "price": price,
        "official": off,
        "map": mp
    }


def _send_hotel_single(uid: str, hotels_text: str, reply_token: str):
    """ホテルは1件のみ出力：導入→説明→ボタン"""
    block = re.split(r"\n[- ─]{6,}\n|\n{2,}", hotels_text.strip())
    block = next((b for b in block if b.strip()), "")
    if not block:
        line_bot_api.reply_message(reply_token, TextSendMessage(text="ホテル候補が見つかりませんでした。"))
        return

    info = _parse_hotel_block(block)

    # ① 導入メッセージ
    line_bot_api.reply_message(reply_token, TextSendMessage(text="🏨 あなたにおすすめのホテルはこちらです👇"))

    # ② 説明テキスト（URL行なし）
    text_lines = [f"🏨 {info['name']}"]
    if info["desc"]:
        text_lines.append(info["desc"])
    if info["price"]:
        text_lines.append(f"💰 価格目安：{info['price']}")
    text = "\n".join(text_lines)
    line_bot_api.push_message(uid, TextSendMessage(text=text))

    # ③ ボタン（タイトル＝ホテル名／サブタイトル＝価格目安）
    actions = []
    if info["official"]:
        actions.append(URITemplateAction(label="公式サイト", uri=info["official"]))
    if info["map"]:
        actions.append(URITemplateAction(label="Googleマップ", uri=info["map"]))

    if actions:
        btn = TemplateSendMessage(
            alt_text=info["name"],
            template=ButtonsTemplate(
                title=info["name"][:40],
                text=(f"価格目安：{info['price']}"[:60] if info["price"] else " "),
                actions=actions[:4]
            )
        )
        line_bot_api.push_message(uid, btn)

def build_schedule_prompt(answers: Dict[str, Any]) -> str:
    answers_json = json.dumps(answers, ensure_ascii=False, indent=2)
    return f"""
以下は「日程表」セクションの出力指示です。
他の項目は出さず、旅程のみを生成してください。
極めて重要：「公式：URL」および「Googleマップ：URL」の行に、実際のURLを出力してください。
【ユーザー回答(JSON参照用)】
{answers_json}

厳守事項：
- 最終日には宿泊ブロックを入れない
- 各日6ブロック以上
- 各ブロックは「時間帯」「カテゴリ」「名称」「URL/営業時間」を含める
- ボタン用タイトルは「HH:MM–HH:MM 名称」にすると解釈可能な出力

出力例：
Day1
🕘 9:00–10:30　🏯 観光：施設名（エリア）
⌛ 所要：60〜90分
🔗 公式：URL
📍 Googleマップ：URL
🕰 営業：時間／休：定休
↓
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
💰 価格帯：〜円程度　🕰 営業：時間／休：定休
🔗 公式：URL
📍 Googleマップ：URL
──────────────────────────────
3) 🎟️ 体験予約（3件）
🎯 施設名（エリア）
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
    return "旅全体の特徴や注意事項を2〜4行で簡潔にまとめてください。"

def build_next_prompt(answers: Dict[str, Any]) -> str:
    return "🔄 最初から"

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

# ---------- URL検出 ----------
URL_RE = re.compile(r"https?://[^\s)]+", re.I)
OFFICIAL_URL_RE = re.compile(r"^(?:🔗\s*)?(?:公式|Official)\s*[:：]\s*(https?://[^\s)]+)", re.M)
MAP_URL_RE = re.compile(r"^(?:📍\s*)?(?:Google ?マップ|Google ?Maps)\s*[:：]\s*(https?://[^\s)]+)", re.M | re.I)

SECTION_SPLIT_RE = re.compile(r"\n[-─]{6,}\n")
FOOD_HEAD_RE  = re.compile(r"^\s*🍽\s*(?P<title>[^（\(\n]+)", re.M)
EXPER_HEAD_RE = re.compile(r"^\s*🎯\s*(?P<title>[^（\(\n]+)", re.M)
DAY_HEAD_RE   = re.compile(r"^Day\s*\d+", re.M | re.I)
BLOCK_SPLIT_RE= re.compile(r"\n\s*↓\s*\n", re.M)
ACT_TITLE_RE  = re.compile(r"^[^\n：:]*[：:]\s*(?P<title>[^\n（(]+)", re.M)

TIME_RANGE_RE = re.compile(r"\b(\d{1,2}[:：]\d{2})\s*[–\-~〜]\s*(\d{1,2}[:：]\d{2})\b")
PRICE_RE = re.compile(r"(?:💰|料金|価格帯)\s*[:：]\s*([^\n／]+)")
HOURS_RE = re.compile(r"(?:🕰|営業時間|営業)\s*[:：]\s*([^\n]+)")

def _clean_url(u: str) -> str:
    if not u: return ""
    u = u.replace("\u200b", "").replace("\u200c", "").replace("\u200d", "").replace("\ufeff", "")
    u = u.strip().strip("。．、，)）]］>＞")
    if u.startswith("http://"): u = "https://" + u[len("http://"):]
    return u

def _push_messages_in_chunks(uid: str, msgs, size: int = 5):
    for i in range(0, len(msgs), size):
        chunk = msgs[i:i+size]
        line_bot_api.push_message(uid, chunk if len(chunk) > 1 else chunk[0])

# -------- ホテル（カードのみ） --------
def _send_hotels_as_buttons(uid: str, hotels_text: str):
    blocks = re.split(r"\n[- ─]{6,}\n|\n{2,}", hotels_text.strip())
    msgs = []
    for b in blocks:
        b = b.strip()
        if not b:
            continue
        first_line = next((ln.strip() for ln in b.splitlines() if ln.strip()), "")
        title = re.sub(r"^\s*[①-⑳]?\s*[🏨\d\.\)\）\s]*", "", first_line) or "ホテル"
        off = OFFICIAL_URL_RE.search(b)
        mp  = MAP_URL_RE.search(b)
        mprice = re.search(r"価格目安[:：]\s*([^\n]+)", b)
        price_text = f"価格目安：{mprice.group(1).strip()}" if mprice else " "

        if not (off or mp):
            continue
        actions = []
        if off: actions.append(URITemplateAction(label="公式サイト", uri=_clean_url(off.group(1))))
        if mp:  actions.append(URITemplateAction(label="Googleマップ", uri=_clean_url(mp.group(1))))

        msgs.append(TemplateSendMessage(
            alt_text=title,
            template=ButtonsTemplate(
                title=title[:40],
                text=price_text[:60],
                actions=actions[:4]
            )
        ))
    if msgs: _push_messages_in_chunks(uid, msgs, size=5)

# -------- スケジュール分割 --------
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
    time_range = ""
    if mtime:
        t1 = mtime.group(1).replace("：", ":")
        t2 = mtime.group(2).replace("：", ":")
        time_range = f"{t1}–{t2}"
    mtitle = ACT_TITLE_RE.search(block)
    name = (mtitle.group("title").strip() if mtitle else "スポット")
    mh = HOURS_RE.search(block)
    hp = mh.group(1).strip() if mh else ""
    mp = PRICE_RE.search(block)
    price = mp.group(1).strip() if mp else ""
    subtitle_parts = []
    if hp: subtitle_parts.append(f"営業時間：{hp}")
    if price: subtitle_parts.append(f"目安：{price}")
    subtitle = " ／ ".join(subtitle_parts) if subtitle_parts else " "
    return time_range, name, subtitle

def _send_schedule_buttons_for_day(uid: str, day_title: str, day_body: str):
    msgs = []
    for block in _blocks_in_day(day_body):
        off = OFFICIAL_URL_RE.search(block)
        mp  = MAP_URL_RE.search(block)
        if not (off or mp):
            continue
        time_range, name, subtitle = _info_from_block(block)
        title = f"{time_range} {name}".strip()
        actions = []
        if off: actions.append(URITemplateAction(label="公式サイト", uri=_clean_url(off.group(1))))
        if mp:  actions.append(URITemplateAction(label="Googleマップ", uri=_clean_url(mp.group(1))))
        msgs.append(
            TemplateSendMessage(
                alt_text=title[:240],
                template=ButtonsTemplate(
                    title=title[:40],
                    text=(subtitle[:60] if subtitle else " "),
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

# ---------- 指定順で送信（カードのみ） ----------
def send_plan_parts(reply_token: str, uid: str, answers: Dict[str, Any]):
    # ① ホテル（1件のみ）
    hotels = _call_openai_text(build_hotel_prompt(answers))
    _send_hotel_single(uid, hotels, reply_token)

    # ② 日程表（Dayごとカード）
    schedule = _generate_full_schedule(answers)
    for day_title, day_body in _split_days(schedule):
        line_bot_api.push_message(uid, TextSendMessage(text=f"{day_title} の予定を表示します"))
        _send_schedule_buttons_for_day(uid, day_title, day_body)

    # ③ 実用ガイド：食事/体験はカードのみ（短評なし）
    guide = _call_openai_text(build_guide_prompt(answers))
    sections = SECTION_SPLIT_RE.split(guide)

    def _extract_blocks_by_head(section_text: str, head_re: re.Pattern):
        lines = section_text.splitlines()
        idxs = [i for i, ln in enumerate(lines) if head_re.search(ln)]
        blocks = []
        for j, start in enumerate(idxs):
            end = idxs[j+1] if j+1 < len(idxs) else len(lines)
            blocks.append("\n".join(lines[start:end]).strip())
        return blocks

    # 食事（昼3 / 夜3 最大6）
    food_idx = next((i for i, s in enumerate(sections) if "食事おすすめ" in s), None)
    if food_idx is not None:
        food_blocks_all = _extract_blocks_by_head(sections[food_idx], FOOD_HEAD_RE)[:6]
        msgs = []
        for b in food_blocks_all:
            _, name, subtitle = _info_from_block(b)
            off = OFFICIAL_URL_RE.search(b); mp = MAP_URL_RE.search(b)
            if not (off or mp): continue
            actions = []
            if off: actions.append(URITemplateAction(label="公式サイト", uri=_clean_url(off.group(1))))
            if mp:  actions.append(URITemplateAction(label="Googleマップ", uri=_clean_url(mp.group(1))))
            msgs.append(TemplateSendMessage(
                alt_text=name,
                template=ButtonsTemplate(title=name[:40], text=(subtitle[:60] if subtitle else " "), actions=actions[:4])
            ))
        if msgs: _push_messages_in_chunks(uid, msgs, size=5)

    # 体験（3）
    exp_idx = next((i for i, s in enumerate(sections) if "体験予約" in s), None)
    if exp_idx is not None:
        exp_blocks_all = _extract_blocks_by_head(sections[exp_idx], EXPER_HEAD_RE)[:3]
        msgs = []
        for b in exp_blocks_all:
            _, name, subtitle = _info_from_block(b)
            off = OFFICIAL_URL_RE.search(b); mp = MAP_URL_RE.search(b)
            if not (off or mp): continue
            actions = []
            if off: actions.append(URITemplateAction(label="公式サイト", uri=_clean_url(off.group(1))))
            if mp:  actions.append(URITemplateAction(label="Googleマップ", uri=_clean_url(mp.group(1))))
            msgs.append(TemplateSendMessage(
                alt_text=name,
                template=ButtonsTemplate(title=name[:40], text=(subtitle[:60] if subtitle else " "), actions=actions[:4])
            ))
        if msgs: _push_messages_in_chunks(uid, msgs, size=5)

    # ④ 総評
    review = _call_openai_text(build_review_prompt(answers))
    line_bot_api.push_message(uid, TextSendMessage(text=review))

    # ⑤ 次の操作
    nxt = _call_openai_text(build_next_prompt(answers))
    line_bot_api.push_message(uid, TextSendMessage(text=nxt))

SYSTEM_PROMPT = (
    "You are AI Travel Navi Kansai.\n"
    "以下の利用者回答（JSON）に厳密に従って、選択されていない地域は含めない。\n"
    "出力順：1)ホテル候補1件 2)日程表 3)実用ガイド 4)総評 5)次の操作。\n"
    "URLは生URL（Markdownリンク禁止）。実用ガイド/日程表では画像URLを出さない。\n"
    "食事・体験は固有名詞＆GoogleマップURL&営業時間必須。体験は最低3件。\n"
    "最終日に宿泊ブロックを入れない。"
)

# ---------- テキスト分割・送信 ----------
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
    msgs = [TextSendMessage(text=c) for c in chunks]
    line_bot_api.reply_message(reply_token, msgs)




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

# ---------- URL検出 ----------
URL_RE = re.compile(r"https?://[^\s)]+", re.I)
OFFICIAL_URL_RE = re.compile(r"^(?:🔗\s*)?(?:公式|Official)\s*[:：]\s*(https?://[^\s)]+)", re.M)
MAP_URL_RE = re.compile(r"^(?:📍\s*)?(?:Google ?マップ|Google ?Maps)\s*[:：]\s*(https?://[^\s)]+)", re.M | re.I)

SECTION_SPLIT_RE = re.compile(r"\n[-─]{6,}\n")
FOOD_HEAD_RE  = re.compile(r"^\s*🍽\s*(?P<title>[^（\(\n]+)", re.M)
EXPER_HEAD_RE = re.compile(r"^\s*🎯\s*(?P<title>[^（\(\n]+)", re.M)
DAY_HEAD_RE   = re.compile(r"^Day\s*\d+", re.M | re.I)
BLOCK_SPLIT_RE= re.compile(r"\n\s*↓\s*\n", re.M)
ACT_TITLE_RE  = re.compile(r"^[^\n：:]*[：:]\s*(?P<title>[^\n（(]+)", re.M)

TIME_RANGE_RE = re.compile(r"\b(\d{1,2}[:：]\d{2})\s*[–\-~〜]\s*(\d{1,2}[:：]\d{2})\b")
PRICE_RE = re.compile(r"(?:💰|料金|価格帯)\s*[:：]\s*([^\n／]+)")
HOURS_RE = re.compile(r"(?:🕰|営業時間|営業)\s*[:：]\s*([^\n]+)")

def _clean_url(u: str) -> str:
    if not u: return ""
    u = u.replace("\u200b", "").replace("\u200c", "").replace("\u200d", "").replace("\ufeff", "")
    u = u.strip().strip("。．、，)）]］>＞")
    if u.startswith("http://"): u = "https://" + u[len("http://"):]
    return u

def _push_messages_in_chunks(uid: str, msgs, size: int = 5):
    for i in range(0, len(msgs), size):
        chunk = msgs[i:i+size]
        line_bot_api.push_message(uid, chunk if len(chunk) > 1 else chunk[0])

# -------- ホテル（カードのみ） --------
def _send_hotels_as_buttons(uid: str, hotels_text: str):
    blocks = re.split(r"\n[- ─]{6,}\n|\n{2,}", hotels_text.strip())
    msgs = []
    for b in blocks:
        b = b.strip()
        if not b:
            continue
        first_line = next((ln.strip() for ln in b.splitlines() if ln.strip()), "")
        title = re.sub(r"^\s*[①-⑳]?\s*[🏨\d\.\)\）\s]*", "", first_line) or "ホテル"
        off = OFFICIAL_URL_RE.search(b)
        mp  = MAP_URL_RE.search(b)
        mprice = re.search(r"価格目安[:：]\s*([^\n]+)", b)
        price_text = f"価格目安：{mprice.group(1).strip()}" if mprice else " "

        if not (off or mp):
            continue
        actions = []
        if off: actions.append(URITemplateAction(label="公式サイト", uri=_clean_url(off.group(1))))
        if mp:  actions.append(URITemplateAction(label="Googleマップ", uri=_clean_url(mp.group(1))))

        msgs.append(TemplateSendMessage(
            alt_text=title,
            template=ButtonsTemplate(
                title=title[:40],
                text=price_text[:60],
                actions=actions[:4]
            )
        ))
    if msgs: _push_messages_in_chunks(uid, msgs, size=5)

# -------- スケジュール分割 --------
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
    time_range = ""
    if mtime:
        t1 = mtime.group(1).replace("：", ":")
        t2 = mtime.group(2).replace("：", ":")
        time_range = f"{t1}–{t2}"
    mtitle = ACT_TITLE_RE.search(block)
    name = (mtitle.group("title").strip() if mtitle else "スポット")
    mh = HOURS_RE.search(block)
    hp = mh.group(1).strip() if mh else ""
    mp = PRICE_RE.search(block)
    price = mp.group(1).strip() if mp else ""
    subtitle_parts = []
    if hp: subtitle_parts.append(f"営業時間：{hp}")
    if price: subtitle_parts.append(f"目安：{price}")
    subtitle = " ／ ".join(subtitle_parts) if subtitle_parts else " "
    return time_range, name, subtitle

def _send_schedule_buttons_for_day(uid: str, day_title: str, day_body: str):
    msgs = []
    for block in _blocks_in_day(day_body):
        off = OFFICIAL_URL_RE.search(block)
        mp  = MAP_URL_RE.search(block)
        if not (off or mp):
            continue
        time_range, name, subtitle = _info_from_block(block)
        title = f"{time_range} {name}".strip()
        actions = []
        if off: actions.append(URITemplateAction(label="公式サイト", uri=_clean_url(off.group(1))))
        if mp:  actions.append(URITemplateAction(label="Googleマップ", uri=_clean_url(mp.group(1))))
        msgs.append(
            TemplateSendMessage(
                alt_text=title[:240],
                template=ButtonsTemplate(
                    title=title[:40],
                    text=(subtitle[:60] if subtitle else " "),
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

)


)

# ---------- テキスト分割・送信 ----------
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
    msgs = [TextSendMessage(text=c) for c in chunks]
    line_bot_api.reply_message(reply_token, msgs)

# ====================== メインハンドラ ======================
@handler.add(MessageEvent, message=TextMessage)
def on_message(event: MessageEvent):
    uid = event.source.user_id
    text = (event.message.text or "").strip()

    if text in RESTART or text.lower() in RESTART:
        users[uid] = {"step": 0, "answers": {}, "hist": deque(maxlen=MAX_TURNS), "multi_temp": {}}
        line_bot_api.reply_message(event.reply_token, _render_question(0, users[uid]))
        return

    if uid not in users or not users[uid]:
        users[uid] = {"step": 0, "answers": {}, "hist": deque(maxlen=MAX_TURNS), "multi_temp": {}}
        line_bot_api.reply_message(event.reply_token, _render_question(0, users[uid]))
        return

    state = users[uid]
    step = state["step"]

    if not _validate_and_store(uid, step, text):
        line_bot_api.reply_message(event.reply_token, _render_question(step, state))
        return

    if Q[step]["multi"] and text != "完了":
        line_bot_api.reply_message(event.reply_token, _render_question(step, state))
        return

    step += 1
    state["step"] = step

    if step < len(Q):
        line_bot_api.reply_message(event.reply_token, _render_question(step, state))
        return

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


