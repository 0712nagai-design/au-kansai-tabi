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
    TemplateSendMessage, ButtonsTemplate, URITemplateAction,
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

# 直近言語の保持（他プランメニュー→再分岐で再利用）
LAST_LANG: Dict[str, str] = {}

RESTART = {"start", "restart", "reset", "スタート", "最初から", "やり直す"}

# ====================== 共通ユーティリティ ======================
FW_TO_HW = str.maketrans({
    "０":"0","１":"1","２":"2","３":"3","４":"4",
    "５":"5","６":"6","７":"7","８":"8","９":"9",
    "．":".","　":" "
})

def _push_messages_in_chunks(uid: str, msgs, size: int = 5):
    for i in range(0, len(msgs), size):
        chunk = msgs[i:i+size]
        line_bot_api.push_message(uid, chunk if len(chunk) > 1 else chunk[0])

def _clean_url(u: str) -> str:
    """
    https化・不可視/全角句読点除去・エンコード整形。
    明らかにURLでない文字列（-, なし 等）の場合は空文字を返す。
    """
    if not u:
        return ""

    u = (u.replace("\u200b","").replace("\u200c","").replace("\u200d","")
           .replace("\ufeff","").strip().strip("。．、 ，)）]］>＞」』"))

    # URLではない典型パターンを除外
    if u in {"-", "ー", "なし", "無し", "不明", "公式サイトなし", "公式サイト無し"}:
        return ""

    # scheme が付いてない www.xxx.com などを https 付きに
    if not u.startswith(("http://", "https://")):
        # 単純なドメイン or www から始まるものっぽければ https を付与
        if u.startswith("www.") or ("." in u and " " not in u):
            u = "https://" + u
        else:
            # URLというより検索キーワードの可能性が高いので、そのまま返す（後段で検索URLに使う）
            return u

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
    except Exception:
        u = u.replace(" ", "%20")
    return u

def _normalize_map_url(u: str, fallback_query: str = "") -> str:
    """
    GoogleマップURLを端末互換の高い形式へ正規化。
    ・すでにGoogleマップURLならそのまま
    ・(lat,lng) なら検索API形式に
    ・それ以外は検索キーワードとして扱う
    """
    if not u:
        # fallback_query から検索URLを作る
        q = fallback_query.strip()
        if not q:
            return ""
        return f"https://www.google.com/maps/search/?api=1&query={quote(q)}"

    u = _clean_url(u)
    if not u:
        q = fallback_query.strip()
        if not q:
            return ""
        return f"https://www.google.com/maps/search/?api=1&query={quote(q)}"

    # すでにGoogleマップURLっぽい
    if ("maps.app.goo.gl" in u) or ("google." in u and "/maps" in u):
        return u

    # (lat,lng) 形式なら検索APIへ
    if re.fullmatch(r"\(?-?\d+(\.\d+)?\s*,\s*-?\d+(\.\d+)?\)?", u):
        coords = u.strip("()").replace(" ", "")
        return f"https://www.google.com/maps/search/?api=1&query={quote(coords)}"

    # それ以外はキーワード検索
    q = fallback_query or u
    return f"https://www.google.com/maps/search/?api=1&query={quote(q)}"

def _uri_action(label: str, url: str) -> dict:
    """
    Flexのuriアクション（スマホでも確実に開けるシンプル形式）
    url が非URLの場合は Google検索にフォールバック
    """
    clean = _clean_url(url)
    if not clean or clean.startswith("http") is False:
        # タイトルで検索にフォールバック
        clean = f"https://www.google.com/search?q={quote(url or label)}"
    return {
        "type": "uri",
        "label": label,
        "uri": clean
    }

def _parse_numbers(s: str) -> Optional[List[int]]:
    if not s:
        return None
    s = s.translate(FW_TO_HW)
    for sep in [".", "･", "・", "、", "，", " ", "/", "／"]:
        s = s.replace(sep, ",")
    s = re.sub(r",+", ",", s).strip(",")
    if not re.fullmatch(r"[0-9,]+", s):
        return None
    try:
        nums = [int(x) for x in s.split(",") if x != ""]
        return nums if nums else None
    except Exception:
        return None

def _chunk(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i+n]

# 共通抽出用
OFFICIAL_URL_RE = re.compile(r"^(?:🔗\s*)?(?:公式|Official)\s*[:：]\s*(https?://[^\s)]+)", re.M)
MAP_URL_RE      = re.compile(r"^(?:📍\s*)?(?:Google ?マップ|Google ?Maps)\s*[:：]\s*(https?://[^\s)]+)", re.M | re.I)
PRICE_RE        = re.compile(r"(?:💰|価格帯|料金|料金目安|価格目安)[:：]\s*([^\n／]+)")
HOURS_RE        = re.compile(r"(?:🕰|営業時間|営業)[:：]\s*([^\n]+)")
DURA_RE         = re.compile(r"(?:⌛|所要|体験時間)[:：]\s*([^\n／]+)")
TIME_RANGE_RE   = re.compile(r"\b(\d{1,2}[:：]\d{2})\s*[–\-~〜]\s*(\d{1,2}[:：]\d{2})\b")
ACT_TITLE_RE    = re.compile(r"^[^\n：:]*[：:]\s*(?P<title>[^\n（(]+)", re.M)

# ====================== 分岐質問 定義 ======================
REQUESTS = {1: "ホテル", 2: "日程表", 3: "飲食店", 4: "体験スポット", 5: "観光地"}

PREFS_KANSAI = {1: "京都", 2: "大阪", 3: "奈良", 4: "兵庫", 5: "滋賀", 6: "和歌山"}

# --- ホテル ---
STAY_PLAN_HOTEL = {1: "日帰り", 2: "1泊2日", 3: "2泊3日", 4: "3泊4日", 5: "4泊5日", 6: "5泊6日"}
PEOPLE_HOTEL = {1: "1人", 2: "2人", 3: "3人", 4: "4人", 5: "5人", 6: "6人以上"}
HOTELS  = {1: "高級", 2: "中価格", 3: "コスパ", 4: "和風旅館", 5: "こだわらない"}

# --- 飲食店 ---
MEAL_TIMES   = {1: "朝", 2: "昼", 3: "夜"}
AREAS_FOOD   = {1: "現在地から近く", 2: "京都", 3: "大阪", 4: "奈良", 5: "兵庫", 6: "滋賀", 7: "和歌山"}
PEOPLE_FOOD  = {1: "1人", 2: "2人", 3: "3人", 4: "4人", 5: "5人", 6: "6人以上"}
COMPANION_FOOD = {1: "一人", 2: "カップル", 3: "友達", 4: "家族"}
CUISINES     = {1: "和食", 2: "洋食", 3: "中華", 4: "ラーメン", 5: "カフェ・スイーツ", 6: "こだわらない"}
BUDGET_FOOD  = {1: "～1000円", 2: "1000～2000円", 3: "2000～5000円", 4: "5000円以上"}

# --- 体験スポット ---
AREAS_EXP    = PREFS_KANSAI.copy()
PEOPLE_EXP   = {1: "1人", 2: "2人", 3: "3人", 4: "4人", 5: "5人", 6: "6人以上"}
COMPANION_EXP= COMPANION_FOOD.copy()
EXP_GENRES   = {1: "温泉", 2: "自然体験", 3: "文化体験", 4: "モノづくり体験", 5: "グルメ・食体験"}

# --- 観光地 ---
AREAS_SIGHT  = PREFS_KANSAI.copy()

# --- 日程表 ---
PREFS_MULTI  = PREFS_KANSAI.copy()   # 複数選択
STAY_PLAN_ITI= {1: "日帰り", 2: "1泊2日", 3: "2泊3日", 4: "3泊4日", 5: "4泊5日", 6: "5泊6日"}
THEMES_MULTI = {1:"グルメ",2:"歴史文化",3:"自然癒し",4:"夜景",5:"温泉",6:"家族",7:"ショッピング",8:"体験メイン",9:"その他"}  # 複数選択
COMPANION_ITI= {1:"ひとり",2:"カップル",3:"友人",4:"家族",5:"外国人友人",6:"その他"}
DEPT_CHOICES = {1:"6–8時",2:"9–11時",3:"12–14時",4:"15–17時",5:"18時以降"}
ARRV_CHOICES = {1:"14–17時",2:"17–19時",3:"19–21時",4:"21時以降",5:"未定"}
TRANSPORT_ITI= {1:"公共交通",2:"車",3:"徒歩中心"}

def _get_question_sequence(answers: Dict[str, Any]) -> List[Dict[str, Any]]:
    # 最初の質問は「何を提案しますか？」のみ（言語選択は削除）
    seq: List[Dict[str, Any]] = [
        {"key": "request", "title": "何を提案しますか？", "choices": REQUESTS, "multi": False},
    ]
    req = answers.get("request")

    if req == "ホテル":
        seq += [
            {"key": "pref",      "title": "関西の都道府県を1つ選んでください。", "choices": PREFS_KANSAI, "multi": False},
            {"key": "stay_plan", "title": "何泊何日ですか？",                   "choices": STAY_PLAN_HOTEL, "multi": False},
            {"key": "people",    "title": "人数を選んでください。",             "choices": PEOPLE_HOTEL, "multi": False},
            {"key": "hotel",     "title": "ホテルタイプを選んでください。",     "choices": HOTELS, "multi": False},
        ]
        return seq

    if req == "飲食店":
        seq += [
            {"key": "meal_time", "title": "食事のタイミングを選んでください。", "choices": MEAL_TIMES, "multi": False},
            {"key": "area",      "title": "エリアを選んでください。",         "choices": AREAS_FOOD, "multi": False},
            {"key": "people",    "title": "人数を選んでください。",           "choices": PEOPLE_FOOD, "multi": False},
            {"key": "companion", "title": "同行者を選んでください。",         "choices": COMPANION_FOOD, "multi": False},
            {"key": "cuisine",   "title": "食べたいジャンルを選んでください。", "choices": CUISINES, "multi": False},
            {"key": "budget",    "title": "ご予算を選んでください。",         "choices": BUDGET_FOOD, "multi": False},
        ]
        return seq

    if req == "体験スポット":
        seq += [
            {"key": "pref",      "title": "関西の都道府県を1つ選んでください。", "choices": AREAS_EXP, "multi": False},
            {"key": "exp_genre", "title": "体験ジャンルを選んでください。",     "choices": EXP_GENRES, "multi": False},
            {"key": "people",    "title": "人数を選んでください。",             "choices": PEOPLE_EXP, "multi": False},
            {"key": "companion", "title": "同行者を選んでください。",           "choices": COMPANION_EXP, "multi": False},
        ]
        return seq

    if req == "観光地":
        seq += [
            {"key": "pref", "title": "関西の都道府県を1つ選んでください。", "choices": AREAS_SIGHT, "multi": False}
        ]
        return seq

    if req == "日程表":
        seq += [
            {"key": "prefs",   "title": "訪問する都道府県を選んでください（複数選択可：例 1,3,6 で同時選択。タップの場合は『完了』）", "choices": PREFS_MULTI, "multi": True},
            {"key": "date",    "title": "出発日を入力してください（例: 2025-03-20）", "choices": {}, "multi": False},
            {"key": "stay",    "title": "何泊何日ですか？", "choices": STAY_PLAN_ITI, "multi": False},
            {"key": "themes",  "title": "旅行のテーマを選んでください（複数選択可：例 1,4,5。タップの場合は『完了』）", "choices": THEMES_MULTI, "multi": True},
            {"key": "transport","title":"主な交通手段を選んでください。", "choices": TRANSPORT_ITI, "multi": False},
            {"key": "companion","title":"同行者を選んでください。", "choices": COMPANION_ITI, "multi": False},
            {"key": "dept",    "title": "出発時間帯を選んでください。", "choices": DEPT_CHOICES, "multi": False},
            {"key": "arrv",    "title": "帰着時間帯を選んでください。", "choices": ARRV_CHOICES, "multi": False},
        ]
        return seq

    return seq

# ========= Flex Question（見切れ対策・✅完了対応） =========
def _flex_choice_button(label: str, out_text: str) -> dict:
    """
    ボタンに表示するのは label（例: ラーメン）だけ。
    押したときに送るテキストは out_text（例: "4"）。
    → 表示上の「4 ラーメン」をなくす。
    """
    return {
        "type": "box",
        "layout": "vertical",
        "cornerRadius": "16px",
        "backgroundColor": "#EEF2F7",
        "height": "92px",
        "paddingAll": "0px",
        "justifyContent": "center",
        "action": {"type": "message", "label": label, "text": out_text},
        "contents": [{
            "type": "text",
            "text": label,
            "weight": "bold",
            "size": "18px",
            "align": "center",
            "color": "#111111",
            "wrap": True,
            "maxLines": 2
        }]
    }

def _flex_question_bubble(title: str, selected_line: str, pairs: List[List[dict]], show_done: bool) -> dict:
    rows = []
    for row in pairs:
        if len(row) == 1:
            row.append({"type":"filler"})
        rows.append({"type":"box","layout":"horizontal","spacing":"14px","contents":row})

    footer_contents = []
    if show_done:
        footer_contents.append({
            "type":"box","layout":"vertical","cornerRadius":"12px","backgroundColor":"#22C55E","paddingAll":"14px",
            "action":{"type":"message","label":"✅ 完了","text":"完了"},
            "contents":[{"type":"text","text":"✅ 完了","weight":"bold","size":"20px","align":"center","color":"#FFFFFF"}]
        })
    footer_contents.append({
        "type":"text","text":"↪ 最初から","size":"14px","color":"#4F46E5","align":"center","margin":"8px",
        "action":{"type":"message","label":"最初から","text":"最初から"}
    })

    return {
        "type":"bubble","size":"mega",
        "body":{"type":"box","layout":"vertical","spacing":"12px","paddingAll":"16px",
            "contents":[
                {"type":"text","text":title,"wrap":True,"size":"24px","weight":"bold"},
                ({"type":"text","text":selected_line,"size":"14px","color":"#6B7280","wrap":True} if selected_line else {"type":"filler"}),
                {"type":"separator"},
                *rows
            ]},
        "footer":{"type":"box","layout":"vertical","spacing":"6px","paddingAll":"12px","contents":footer_contents}
    }
REQUEST_IMAGE_URLS = {
    "ホテル":     "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/%E3%81%BB%E3%81%A6%E3%82%8B.png",
    "飲食店":     "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/%E9%A3%B2%E9%A3%9F%E5%BA%97.png",
    "体験スポット": "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/%E4%BD%93%E9%A8%93.png",
    "観光地":     "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/kannku.png",
}

def _render_question(idx: int, state: State):
    seq = _get_question_sequence(state.get("answers", {}))
    q = seq[idx]
    title = q["title"]

    # --- Q1「何を提案しますか？」だけ、画像付きの特別レイアウト ---
    if q["key"] == "request":

        def img_btn(label: str, url: str) -> dict:
            return {
                "type": "box",
                "layout": "vertical",
                "flex": 1,
                "cornerRadius": "16px",
                "backgroundColor": "#FFFFFF",
                "height": "120px",
                "action": {"type": "message", "label": label, "text": label},
                "contents": [
                    {
                        "type": "image",
                        "url": url,
                        "size": "full",
                        "aspectRatio": "16:9",   # 小さめ横長
                        "aspectMode": "fit"      # 見切れ防止
                    },
                    {
                        "type": "box",
                        "layout": "vertical",
                        "paddingAll": "4px",
                        "contents": [
                            {
                                "type": "text",
                                "text": label,
                                "weight": "bold",
                                "size": "14px",
                                "align": "center",
                                "color": "#111111",
                                "wrap": True
                            }
                        ]
                    }
                ]
            }

        def txt_btn(label: str) -> dict:
            return {
                "type": "box",
                "layout": "vertical",
                "flex": 1,
                "cornerRadius": "16px",
                "backgroundColor": "#EEF2F7",
                "height": "120px",
                "justifyContent": "center",
                "action": {"type": "message", "label": label, "text": label},
                "contents": [
                    {
                        "type": "text",
                        "text": label,
                        "weight": "bold",
                        "size": "16px",
                        "align": "center",
                        "color": "#111111",
                        "wrap": True
                    }
                ]
            }

        # 1行目: ホテル / 飲食店
        row1 = {
            "type": "box",
            "layout": "horizontal",
            "spacing": "12px",
            "contents": [
                img_btn("ホテル", REQUEST_IMAGE_URLS["ホテル"]),
                img_btn("飲食店", REQUEST_IMAGE_URLS["飲食店"])
            ]
        }

        # 2行目: 体験スポット / 観光地
        row2 = {
            "type": "box",
            "layout": "horizontal",
            "spacing": "12px",
            "contents": [
                img_btn("体験スポット", REQUEST_IMAGE_URLS["体験スポット"]),
                img_btn("観光地", REQUEST_IMAGE_URLS["観光地"])
            ]
        }

        # 3行目: 日程表（テキスト）＋空白
        row3 = {
            "type": "box",
            "layout": "horizontal",
            "spacing": "12px",
            "contents": [
                txt_btn("日程表"),
                {"type": "filler"}
            ]
        }

        bubble = {
            "type": "bubble",
            "size": "mega",
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "16px",
                "paddingAll": "16px",
                "contents": [
                    {
                        "type": "text",
                        "text": title,  # 「何を提案しますか？」
                        "size": "24px",
                        "weight": "bold",
                        "wrap": True
                    },
                    {"type": "separator"},
                    row1,
                    row2,
                    row3
                ]
            }
        }

        return FlexSendMessage(alt_text=title, contents=bubble)

    # =========================
    # 2問目以降はボタンUI
    # =========================
    selected = state.get("multi_temp", {}).get(q["key"], []) if q.get("multi") else []
    selected_line = (
        f"(選択中：{'、'.join(selected) if selected else 'なし'})"
        if q.get("multi") else ""
    )

    pairs, row = [], []
    for n, label in q.get("choices", {}).items():
        btn = _flex_choice_button(label, str(n))
        row.append(btn)
        if len(row) == 2:
            pairs.append(row)
            row = []
    if row:
        pairs.append(row)

    bubble = _flex_question_bubble(title, selected_line, pairs, q.get("multi", False))
    return FlexSendMessage(alt_text=title, contents=bubble)

    
def _label_to_num(choices: Dict[int, str], text: str) -> Optional[int]:
    t = text.strip().translate(FW_TO_HW)
    for n, label in choices.items():
        if t == str(n) or text.strip() == label:
            return n
    return None

def _validate_and_store(uid: str, step: int, text: str) -> bool:
    state = users[uid]
    seq = _get_question_sequence(state.get("answers", {}))
    q = seq[step]; key = q["key"]
    state.setdefault("answers", {}); state.setdefault("multi_temp", {})

    # choicesあり
    if q.get("choices"):
        n = _label_to_num(q["choices"], text)
        if n is not None:
            val = q["choices"][n]
            if q.get("multi"):
                sel = state["multi_temp"].setdefault(key, [])
                if val not in sel:
                    sel.append(val)
                return True
            else:
                state["answers"][key] = val
                if state["answers"].get("request") == "飲食店" and key == "area":
                    if val == "現在地から近く" and not state.get("geo"):
                        state["need_location"] = True
                return True

    # マルチ選択の確定
    if q.get("multi") and text.strip() == "完了":
        picked = state["multi_temp"].get(key, [])
        if not picked:
            return False
        state["answers"][key] = picked
        return True

    # 日付
    if key == "date":
        try:
            datetime.strptime(text.strip(), "%Y-%m-%d")
            state["answers"][key] = text.strip()
            return True
        except Exception:
            return False

    # 数字列（例: 1,3,5 一括指定 → 自動確定で次へ）
    nums = _parse_numbers(text)
    if nums and q.get("choices"):
        bad = [n for n in nums if n not in q["choices"]]
        if bad:
            return False
        labels = [q["choices"][n] for n in nums]
        if q.get("multi"):
            state["answers"][key] = labels
            state["_autodone"] = True
            return True
        else:
            if len(nums) != 1:
                return False
            state["answers"][key] = q["choices"][nums[0]]
            return True

    return False

# ====================== OpenAI呼び出し ======================
SYSTEM_PROMPT = (
    "You are AI Travel Navi Kansai.\n"
    "URLは生URL（Markdownリンク禁止）。画像URLは出さない。\n"
    "架空の施設名・店舗名・ホテル名などを新たに作らないこと。\n"
    "必ず実在し、Googleマップ等で検索できる施設のみを提案してください。\n"
    "条件に合う実在の候補が3件見つからない場合は、無理に埋めず、見つからない旨をはっきり書いてください。\n"
)

def _call_openai_text(user_prompt: str) -> str:
    res = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.6,
        messages=[{"role":"system","content":SYSTEM_PROMPT},{"role":"user","content":user_prompt}],
    )
    return (res.choices[0].message.content or "").strip()

# ====================== Flexリストカード（1通に3件） ======================
def _flex_list_bubble(header_title: str, items: List[Dict[str, str]]) -> FlexSendMessage:
    def _one_card(it):
        title_text = {
            "type": "text",
            "text": it.get("title",""),
            "weight": "bold",
            "size": "md",
            "wrap": True
        }
        # タイトル全体を公式へリンク可
        if it.get("official"):
            title_text["action"] = _uri_action("open", it["official"])

        subtitle_text = {
            "type": "text",
            "text": it.get("subtitle"," "),
            "size": "sm",
            "color": "#6B7280",
            "wrap": True
        }

        buttons = []
        if it.get("official"):
            buttons.append({
                "type":"button","style":"secondary","height":"sm","margin":"sm",
                "action": _uri_action("公式サイト", it["official"])
            })
        if it.get("map"):
            map_url = _normalize_map_url(it["map"], fallback_query=it.get("title",""))
            buttons.append({
                "type":"button","style":"secondary","height":"sm","margin":"sm",
                "action": _uri_action("Googleマップ", map_url)
            })
        if not buttons:
            buttons = [{
                "type":"button","style":"secondary","height":"sm","margin":"sm",
                "action": _uri_action("検索", f"https://www.google.com/search?q={quote(it.get('title',''))}")
            }]

        return {
            "type": "box",
            "layout": "horizontal",
            "cornerRadius": "16px",
            "backgroundColor": "#FFFFFF",
            "paddingAll": "14px",
            "spacing": "10px",
            "contents": [
                {"type": "box","layout":"vertical","flex":7,"spacing":"6px","contents":[title_text, subtitle_text]},
                {"type": "box","layout":"vertical","flex":5,"spacing":"6px","contents": buttons}
            ]
        }

    rows = [_one_card(it) for it in items[:3]]
    bubble = {
        "type": "bubble",
        "size": "mega",
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "14px",
            "paddingAll": "16px",
            "contents": [
                {"type":"text","text":header_title,"weight":"bold","size":"lg"},
                {"type":"separator"},
                *rows
            ]
        }
    }
    return FlexSendMessage(alt_text=header_title, contents=bubble)

# ====================== ホテル：3件提案 ======================
def build_hotel3_prompt(answers: Dict[str, Any]) -> str:
    answers_json = json.dumps(answers, ensure_ascii=False, indent=2)
    return f"""
あなたは関西旅行のホテルコンシェルジュです。
以下のユーザー条件に合うホテル候補を**ちょうど3件**、同一フォーマットで出力してください。

重要:
- 架空のホテル名を作らないこと。
- 必ず実在し、Googleマップで検索できるホテルのみを提案すること。
- 公式サイトURLまたは予約サイトURLが分からない場合は、その候補は出さず、別の候補を選ぶこと。
- 3件そろわない場合は、足りない件数分について「条件に合う実在のホテルが見つかりませんでした」とだけ書いてください。

各候補は必ず「公式：URL」「Googleマップ：URL」を含め、画像URLは出力しないこと。
各候補の間は空行で区切る（罫線禁止）。

【ユーザー条件(JSON)】
{answers_json}

【出力フォーマット（3件ぶん）】
🏨 ホテル正式名称（最寄エリア）
特徴：立地/客室/温浴/朝食/子連れなど1行要約
💰 価格目安：{answers.get('people','2人')}・{answers.get('stay_plan','1泊2日')}の概算 or 1名/泊目安
🔗 公式：https://...
📍 Googleマップ：https://...
""".strip()

def _parse_hotel_block(block: str):
    name = desc = price = ""
    area = ""
    off = mp = None
    lines = [ln.strip() for ln in block.strip().splitlines() if ln.strip()]
    if lines:
        raw = re.sub(r"^\s*[①-⑳]?\s*[🏨\d\.\)\）\s]*", "", lines[0])
        m_area = re.match(r"(.+?)（(.+?)）", raw)
        if m_area:
            name = m_area.group(1).strip()
            area = m_area.group(2).strip()
        else:
            name = raw
    mdesc  = re.search(r"^特徴[:：]\s*(.+)$", block, re.M)
    mprice = PRICE_RE.search(block)
    moff   = OFFICIAL_URL_RE.search(block)
    mmap   = MAP_URL_RE.search(block)
    if mdesc:  desc = mdesc.group(1).strip()
    if mprice: price = mprice.group(1).strip()
    if moff:   off   = moff.group(1)
    if mmap:   mp    = mmap.group(1)
    return {
        "name": name or "ホテル",
        "area": area,
        "desc": desc,
        "price": price,
        "official": off,
        "map": mp
    }

def _send_hotels_three(uid: str, reply_token: str, hotels_text: str):
    blocks = [b.strip() for b in re.split(r"\n\s*\n", hotels_text.strip()) if b.strip()][:3]
    if not blocks:
        line_bot_api.reply_message(reply_token, TextSendMessage(text="ホテル候補が見つかりませんでした。"))
        return
    line_bot_api.reply_message(reply_token, TextSendMessage(text="🏨 条件に合うホテル候補を3件ご提案します👇"))
    items = []
    for block in blocks:
        info = _parse_hotel_block(block)
        # 1件ずつのシンプル説明
        lines = [f"🏨 {info['name']}"]
        if info["desc"]:
            lines.append(info["desc"])
        if info["price"]:
            lines.append(f"💰 価格目安：{info['price']}")
        line_bot_api.push_message(uid, TextSendMessage(text="\n".join(lines)))
        # Flexリスト用
        items.append({
            "title": info["name"],
            "subtitle": (info.get("desc") or info.get("price") or " ")[:60],
            "official": info.get("official") or "",
            "map": info.get("map") or ""
        })
    if items:
        line_bot_api.push_message(uid, _flex_list_bubble("🏨 ホテル候補（3件）", items))

# ====================== 飲食店：3件提案 ======================
def build_food3_prompt(answers: Dict[str, Any]) -> str:
    answers_json = json.dumps(answers, ensure_ascii=False, indent=2)
    near_hint = ""
    if answers.get("geo"):
        near_hint = (
            f"現在地の緯度経度: {answers.get('geo')} から**半径1km以内**にある飲食店のみを候補にしてください。\n"
            "- 1kmを超える店舗は候補に含めないでください。\n"
            "- 距離が近い順に3件までを提案してください。\n"
            "- 1km以内に条件に合う店が3件見つからない場合、無理に店名を作らず、「条件に合う実在の飲食店が見つかりませんでした」と書いてください。\n"
        )
    return f"""
あなたは関西のグルメコンシェルジュです。
以下の条件に合う飲食店を**ちょうど3件**、同一フォーマットで出力してください。

重要:
- 架空の店名を作らないこと。
- 必ず実在し、Googleマップで検索できる飲食店のみを提案してください。
- 公式サイトまたは食べログ等のページURL、GoogleマップのURLが分からない店は候補から外してください。
- 3件そろわない場合は、足りない件数分について「条件に合う実在の飲食店が見つかりませんでした」とだけ書いてください。
{near_hint}

【条件(JSON)】
{answers_json}

【出力フォーマット（3件ぶん）】
🍽 店名（最寄駅/エリア）
短評：名物/雰囲気/席数/予約可など1行
💰 価格帯：〜円程度
🕰 営業：例) 11:00-22:00／休：水
🔗 公式：https://...
📍 Googleマップ：https://...
""".strip()

def _parse_food_block(block: str) -> Dict[str, Optional[str]]:
    lines = [ln.strip() for ln in block.strip().splitlines() if ln.strip()]
    name = short = price = hours = ""
    off = mp = None
    if lines:
        name = re.sub(r"^\s*[🍽\d\.\)\）\s]*", "", lines[0])
    mshort = re.search(r"^(?:短評|特徴)[:：]\s*(.+)$", block, re.M)
    mprice = PRICE_RE.search(block); mhours = HOURS_RE.search(block)
    moff   = OFFICIAL_URL_RE.search(block); mmap = MAP_URL_RE.search(block)
    if mshort: short = mshort.group(1).strip()
    if mprice: price = mprice.group(1).strip()
    if mhours: hours = mhours.group(1).strip()
    if moff:   off   = moff.group(1)
    if mmap:   mp    = mmap.group(1)
    return {"name": name or "飲食店", "short": short, "price": price, "hours": hours, "official": off, "map": mp}

def _send_food_three(uid: str, reply_token: str, text: str):
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text.strip()) if b.strip()][:3]
    if not blocks:
        line_bot_api.reply_message(reply_token, TextSendMessage(text="条件に合う飲食店が見つかりませんでした。"))
        return
    line_bot_api.reply_message(reply_token, TextSendMessage(text="🍽 条件に合うお店を3件ご提案します👇"))
    items = []
    for block in blocks:
        info = _parse_food_block(block)
        # テキスト（従来）
        lines = [f"🍽 {info['name']}"]
        if info["short"]:
            lines.append(info["short"])
        if info["price"]:
            lines.append(f"💰 価格帯：{info['price']}")
        if info["hours"]:
            lines.append(f"🕰 営業：{info['hours']}")
        line_bot_api.push_message(uid, TextSendMessage(text="\n".join(lines)))
        # Flexリスト用
        items.append({
            "title": info["name"],
            "subtitle": (info.get("short") or info.get("hours") or info.get("price") or " ")[:60],
            "official": info.get("official") or "",
            "map": info.get("map") or ""
        })
    if items:
        line_bot_api.push_message(uid, _flex_list_bubble("🍽 お店候補（3件）", items))

# ====================== 体験スポット：3件提案 ======================
def build_experience3_prompt(answers: Dict[str, Any]) -> str:
    answers_json = json.dumps(answers, ensure_ascii=False, indent=2)
    return f"""
あなたは関西観光の体験コンシェルジュです。
以下の条件に合う**体験スポット/アクティビティ（陶芸体験、着物体験、和菓子作り、ラフティング、温泉入浴など）をちょうど3件**、同一フォーマットで出力してください。

重要:
- 寺社・城・展望台・名所などの「観光地（ランドマーク）」は含めないでください（別カテゴリ）。
- 架空の施設名を作らないこと。
- 必ず実在し、Googleマップで検索できる体験施設のみを提案してください。
- 公式サイトURLや予約ページURL、GoogleマップURLがない施設は候補から外してください。
- 3件そろわない場合は、足りない分について「条件に合う実在の体験スポットが見つかりませんでした」とだけ書いてください。

各候補は必ず「公式：URL」「Googleマップ：URL」を含め、画像URLは出力しないこと。
各候補は空行で区切る（罫線禁止）。誇張なしの短評を1行入れること。

【条件(JSON)】
{answers_json}

【出力フォーマット（3件ぶん）】
🎯 施設名（エリア/最寄駅）
短評：内容・誰向け・注意点など1行
💰 料金：〜円/人
⌛ 所要：〜分／予約：要 or 不要
🕰 営業：例) 10:00-18:00／休：火
🔗 公式：https://...
📍 Googleマップ：https://...
""".strip()

def _parse_experience_block(block: str) -> Dict[str, Optional[str]]:
    lines = [ln.strip() for ln in block.strip().splitlines() if ln.strip()]
    name = short = price = hours = dura = ""
    off = mp = None
    if lines:
        name = re.sub(r"^\s*[🎯\d\.\)\）\s]*", "", lines[0])
    mshort = re.search(r"^(?:短評|特徴)[:：]\s*(.+)$", block, re.M)
    mprice = PRICE_RE.search(block); mhours = HOURS_RE.search(block); mdura = DURA_RE.search(block)
    moff   = OFFICIAL_URL_RE.search(block); mmap = MAP_URL_RE.search(block)
    if mshort: short = mshort.group(1).strip()
    if mprice: price = mprice.group(1).strip()
    if mhours: hours = mhours.group(1).strip()
    if mdura:  dura  = mdura.group(1).strip()
    if moff:   off   = moff.group(1)
    if mmap:   mp    = mmap.group(1)
    return {"name": name or "体験スポット", "short": short, "price": price, "hours": hours, "dura": dura, "official": off, "map": mp}

def _send_experiences_three(uid: str, reply_token: str, text: str):
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text.strip()) if b.strip()][:3]
    if not blocks:
        line_bot_api.reply_message(reply_token, TextSendMessage(text="条件に合う体験スポットが見つかりませんでした。"))
        return
    line_bot_api.reply_message(reply_token, TextSendMessage(text="🎯 条件に合う体験スポットを3件ご提案します👇"))
    items = []
    for block in blocks:
        info = _parse_experience_block(block)
        # テキスト
        lines = [f"🎯 {info['name']}"]
        if info["short"]:
            lines.append(info["short"])
        if info["price"]:
            lines.append(f"💰 料金：{info['price']}")
        if info["dura"]:
            lines.append(f"⌛ 所要：{info['dura']}")
        if info["hours"]:
            lines.append(f"🕰 営業：{info['hours']}")
        line_bot_api.push_message(uid, TextSendMessage(text="\n".join(lines)))
        # Flexリスト用
        sub = info.get("short") or (f"所要:{info.get('dura','')}" if info.get("dura") else info.get("hours")) or " "
        items.append({
            "title": info["name"],
            "subtitle": sub[:60],
            "official": info.get("official") or "",
            "map": info.get("map") or ""
        })
    if items:
        line_bot_api.push_message(uid, _flex_list_bubble("🎯 体験スポット（3件）", items))

# ====================== 観光地：3件提案 ======================
def build_sightseeing3_prompt(answers: Dict[str, Any]) -> str:
    answers_json = json.dumps(answers, ensure_ascii=False, indent=2)
    return f"""
あなたは関西旅行の観光案内コンシェルジュです。
以下の条件に合う**観光地（寺社仏閣・城・庭園・名所・景勝地・ミュージアム・展望台など）をちょうど3件**、同一フォーマットで出力してください。

重要:
- 体験型アクティビティ（陶芸体験など）は含めないでください（それらは体験スポット）。
- 架空の施設名を作らないこと。
- 必ず実在し、Googleマップで検索できる観光地のみを提案してください。
- 公式サイトURLまたは紹介ページURL、GoogleマップURLが不明な観光地は候補として出さないでください。
- 3件そろわない場合は、足りない分について「条件に合う実在の観光地が見つかりませんでした」とだけ書いてください。

必ず「公式：URL」「Googleマップ：URL」を含め、画像URLは出力しないこと。
各候補は空行で区切る（罫線禁止）。短評を1行入れること。

【条件(JSON)】
{answers_json}

【出力フォーマット（3件ぶん）】
🏯 観光地名（エリア/最寄駅）
短評：見どころ/歴史/所要目安 など1行
💰 料金目安：〜円（拝観料/入館料など）※無料なら「無料」
🕰 営業：例) 9:00-17:00／休：無休
🔗 公式：https://...
📍 Googleマップ：https://...
""".strip()

def _parse_sightseeing_block(block: str) -> Dict[str, Optional[str]]:
    lines = [ln.strip() for ln in block.strip().splitlines() if ln.strip()]
    name = short = price = hours = ""
    off = mp = None
    if lines:
        name = re.sub(r"^\s*[🏯\d\.\)\）\s]*", "", lines[0])
    mshort = re.search(r"^(?:短評|特徴)[:：]\s*(.+)$", block, re.M)
    mprice = PRICE_RE.search(block); mhours = HOURS_RE.search(block)
    moff   = OFFICIAL_URL_RE.search(block); mmap = MAP_URL_RE.search(block)
    if mshort: short = mshort.group(1).strip()
    if mprice: price = mprice.group(1).strip()
    if mhours: hours = mhours.group(1).strip()
    if moff:   off   = moff.group(1)
    if mmap:   mp    = mmap.group(1)
    return {"name": name or "観光地", "short": short, "price": price, "hours": hours, "official": off, "map": mp}

def _send_sightseeing_three(uid: str, reply_token: str, text: str):
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text.strip()) if b.strip()][:3]
    if not blocks:
        line_bot_api.reply_message(reply_token, TextSendMessage(text="条件に合う観光地が見つかりませんでした。"))
        return
    line_bot_api.reply_message(reply_token, TextSendMessage(text="🏯 条件に合う観光地を3件ご提案します👇"))
    items = []
    for block in blocks:
        info = _parse_sightseeing_block(block)
        # テキスト
        lines = [f"🏯 {info['name']}"]
        if info["short"]:
            lines.append(info["short"])
        if info["price"]:
            lines.append(f"💰 料金目安：{info['price']}")
        if info["hours"]:
            lines.append(f"🕰 営業：{info['hours']}")
        line_bot_api.push_message(uid, TextSendMessage(text="\n".join(lines)))
        # Flexリスト用
        sub = info.get("short") or (f"営業時間:{info.get('hours','')}" if info.get("hours") else info.get("price")) or " "
        items.append({
            "title": info["name"],
            "subtitle": sub[:60],
            "official": info.get("official") or "",
            "map": info.get("map") or ""
        })
    if items:
        line_bot_api.push_message(uid, _flex_list_bubble("🏯 観光地（3件）", items))

# ====================== 日程表 生成＆送信 ======================
DAY_HEAD_RE   = re.compile(r"^Day\s*\d+", re.M | re.I)
BLOCK_SPLIT_RE= re.compile(r"\n\s*↓\s*\n", re.M)

def build_itinerary_prompt(answers: Dict[str, Any]) -> str:
    answers_json = json.dumps(answers, ensure_ascii=False, indent=2)
    prefs = "、".join(answers.get("prefs", [])) if isinstance(answers.get("prefs"), list) else answers.get("prefs","")
    themes = "、".join(answers.get("themes", [])) if isinstance(answers.get("themes"), list) else answers.get("themes","")
    return f"""
あなたは関西旅行の旅行プランナーです。以下の条件に沿って**濃い日程表**を作ってください。
各日**3〜5スポット以上**、移動を考慮した流れ。各スポットは**短評1行**を含め、必ず「公式：URL」「Googleマップ：URL」を出力。
最終日は宿泊ブロックを入れない。画像URLは禁止。出力は**旅程のみ**。

重要:
- 各スポットは実在の施設・エリアのみを使用してください。
- 架空の名称は作らないでください。
- 公式サイトURLまたは紹介ページURL、GoogleマップURLがないスポットは極力避けてください（見つからない場合はそのスポットを省いても構いません）。

【条件(JSON)】
{answers_json}

【要約】
- エリア: {prefs}
- 出発日: {answers.get('date','-')}
- 旅程: {answers.get('stay','-')}
- テーマ: {themes}
- 主な交通手段: {answers.get('transport','-')}
- 同行者: {answers.get('companion','-')}
- 出発時間帯: {answers.get('dept','-')} / 帰着時間帯: {answers.get('arrv','-')}

【フォーマット例】
Day1
🕘 09:00–10:00　🏯 観光：施設名（エリア）
短評：見どころ1行
💰 料金目安：〜円
🔗 公式：https://...
📍 Googleマップ：https://...
🕰 営業：時間／休：定休
↓
（3〜5スポット以上）
Day2
（同様に続ける）
""".strip()

def _blocks_in_day(day_text: str):
    return [b.strip() for b in BLOCK_SPLIT_RE.split(day_text.strip()) if b.strip()]

def _info_from_block(block: str):
    mtime = TIME_RANGE_RE.search(block)
    if mtime:
        t1 = mtime.group(1).replace("：", ":")
        t2 = mtime.group(2).replace("：", ":")
        time_range = f"{t1}–{t2}"
    else:
        time_range = ""
    mtitle = ACT_TITLE_RE.search(block)
    name = (mtitle.group("title").strip() if mtitle else "スポット")
    mshort = re.search(r"^(?:短評|特徴)[:：]\s*(.+)$", block, re.M)
    short = mshort.group(1).strip() if mshort else ""
    return time_range, name, short

def _send_itinerary(uid: str, reply_token: str, schedule_text: str):
    # Dayごとに切り出し
    parts = []
    positions = [(m.group(0).strip(), m.start()) for m in DAY_HEAD_RE.finditer(schedule_text)]
    for i, (title, start) in enumerate(positions):
        end = positions[i+1][1] if i+1 < len(positions) else len(schedule_text)
        parts.append((title, schedule_text[start:end]))

    if not parts:
        line_bot_api.reply_message(reply_token, TextSendMessage(text="日程表の生成に失敗しました。条件を変えて再試行してください。"))
        return

    for day_title, day_body in parts:
        line_bot_api.push_message(uid, TextSendMessage(text=f"📅 {day_title}"))
        items: List[Dict[str,str]] = []
        for block in _blocks_in_day(day_body):
            off = OFFICIAL_URL_RE.search(block)
            mp  = MAP_URL_RE.search(block)
            time_range, name, short = _info_from_block(block)
            title = (f"{time_range} {name}".strip() or "スポット")[:60]
            items.append({
                "title": title,
                "subtitle": (short or " ")[:60],
                "official": off.group(1) if off else "",
                "map": mp.group(1) if mp else ""
            })
        # 3件ずつのFlexで送信
        for trio in _chunk(items, 3):
            line_bot_api.push_message(uid, _flex_list_bubble(f"{day_title} の予定", trio))

# ====================== “他のプランを提案” メニュー ======================
def _send_finish_menu(uid: str):
    # 画像付きボタン（横2個並べる用。画像小さめ）
    def _img_btn(label: str, text: str, url: str) -> dict:
        return {
            "type": "box",
            "layout": "vertical",
            "flex": 1,
            "cornerRadius": "16px",
            "backgroundColor": "#FFFFFF",
            "height": "120px",
            "action": {"type": "message", "label": label, "text": text},
            "contents": [
                {
                    "type": "image",
                    "url": url,
                    "size": "full",
                    "aspectRatio": "16:9",   # 横長＆小さめ
                    "aspectMode": "fit"      # 見切れ防止
                },
                {
                    "type": "box",
                    "layout": "vertical",
                    "paddingAll": "4px",
                    "contents": [
                        {
                            "type": "text",
                            "text": label,
                            "weight": "bold",
                            "size": "14px",
                            "align": "center",
                            "color": "#111111"
                        }
                    ]
                }
            ]
        }

    # テキストだけのボタン（高さを画像ボタンに合わせる）
    def _txt_btn(label: str, text: str) -> dict:
        return {
            "type": "box",
            "layout": "vertical",
            "flex": 1,
            "cornerRadius": "16px",
            "backgroundColor": "#EEF2F7",
            "height": "120px",
            "justifyContent": "center",
            "action": {"type": "message", "label": label, "text": text},
            "contents": [
                {
                    "type": "text",
                    "text": label,
                    "weight": "bold",
                    "size": "16px",
                    "align": "center",
                    "color": "#111111",
                    "wrap": True
                }
            ]
        }

    # 2列レイアウト（横並び2個ずつ）
    row1 = {
        "type": "box",
        "layout": "horizontal",
        "spacing": "12px",
        "contents": [
            _img_btn("ホテル", "ホテル",
                     "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/%E3%81%BB%E3%81%A6%E3%82%8B.png"),
            _txt_btn("日程表", "日程表"),
        ]
    }

    row2 = {
        "type": "box",
        "layout": "horizontal",
        "spacing": "12px",
        "contents": [
            _img_btn("飲食店", "飲食店",
                     "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/%E9%A3%B2%E9%A3%9F%E5%BA%97.png"),
            _img_btn("体験スポット", "体験スポット",
                     "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/%E4%BD%93%E9%A8%93.png"),
        ]
    }

    row3 = {
        "type": "box",
        "layout": "horizontal",
        "spacing": "12px",
        "contents": [
            _img_btn("観光地", "観光地",
                     "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/kannku.png"),
            _txt_btn("最初から", "最初から"),
        ]
    }

    bubble = {
        "type": "bubble",
        "size": "mega",
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "16px",
            "paddingAll": "16px",
            "contents": [
                {
                    "type": "text",
                    "text": "他のプランを提案",
                    "size": "24px",
                    "weight": "bold"
                },
                {"type": "separator"},
                {
                    "type": "box",
                    "layout": "vertical",
                    "spacing": "12px",
                    "contents": [row1, row2, row3]
                }
            ]
        }
    }

    line_bot_api.push_message(
        uid,
        FlexSendMessage(alt_text="他のプランを提案", contents=bubble)
    )



def send_plan_parts(reply_token: str, uid: str, answers: Dict[str, Any]):
    # 直近言語を保存（他のプラン分岐で使う）※今は常にja運用だが形だけ保持
    LAST_LANG[uid] = answers.get("lang", LAST_LANG.get(uid, "ja"))

    req = answers.get("request")

    if req == "ホテル":
        hotels_text = _call_openai_text(build_hotel3_prompt(answers))
        _send_hotels_three(uid, reply_token, hotels_text)
        _send_finish_menu(uid)
        return

    if req == "飲食店":
        foods_text = _call_openai_text(build_food3_prompt(answers))
        _send_food_three(uid, reply_token, foods_text)
        _send_finish_menu(uid)
        return

    if req == "体験スポット":
        exp_text = _call_openai_text(build_experience3_prompt(answers))
        _send_experiences_three(uid, reply_token, exp_text)
        _send_finish_menu(uid)
        return

    if req == "観光地":
        sight_text = _call_openai_text(build_sightseeing3_prompt(answers))
        _send_sightseeing_three(uid, reply_token, sight_text)
        _send_finish_menu(uid)
        return

    if req == "日程表":
        schedule = _call_openai_text(build_itinerary_prompt(answers))
        _send_itinerary(uid, reply_token, schedule)
        _send_finish_menu(uid)
        return

    line_bot_api.reply_message(reply_token, TextSendMessage(text="未対応のリクエストです。"))

# ====================== 位置情報（飲食店の現在地用） ======================
def _ask_location(reply_token: str):
    msg = TextSendMessage(
        text="📍 現在地の近くで探します。『位置情報を送信』を押して、現在地を共有してください。",
        quick_reply=QuickReply(items=[QuickReplyButton(action=LocationAction(label="位置情報を送る"))])
    )
    line_bot_api.reply_message(reply_token, msg)

# ====================== ルーティング ======================
@app.get("/")
def root_ok():
    return "ok", 200

@app.get("/healthz")
def healthz():
    return "ok", 200

@app.get("/py")
def py():
    return sys.version, 200

@app.post("/callback")
def callback():
    sig = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, sig)
    except InvalidSignatureError:
        abort(400)
    return "OK", 200

# ====================== メインハンドラ ======================
@handler.add(MessageEvent, message=TextMessage)
def on_message(event: MessageEvent):
    uid = event.source.user_id
    text = (event.message.text or "").strip()

    # --- 他のプランメニューからのダイレクト分岐 ---
    if text in {"ホテル", "日程表", "飲食店", "体験スポット", "観光地"}:
        users[uid] = {
            # 言語質問を削除したので、request の次の質問はインデックス 1
            "step": 1,
            "answers": {"lang": LAST_LANG.get(uid, "ja"), "request": text},
            "hist": deque(maxlen=MAX_TURNS),
            "multi_temp": {}
        }
        line_bot_api.reply_message(event.reply_token, _render_question(1, users[uid]))
        return

    # 初期化
    if text in RESTART or text.lower() in RESTART:
        users[uid] = {"step": 0, "answers": {}, "hist": deque(maxlen=MAX_TURNS), "multi_temp": {}}
        line_bot_api.reply_message(event.reply_token, _render_question(0, users[uid]))
        return

    # セッション未作成 → 作成
    if uid not in users or not users[uid]:
        users[uid] = {"step": 0, "answers": {}, "hist": deque(maxlen=MAX_TURNS), "multi_temp": {}}
        line_bot_api.reply_message(event.reply_token, _render_question(0, users[uid]))
        return

    state = users[uid]
    step = state.get("step", 0)

    # 入力の検証＆保存
    ok = _validate_and_store(uid, step, text)
    if not ok:
        line_bot_api.reply_message(event.reply_token, _render_question(step, state))
        return

    # 複数選択：『完了』を待つ。ただし「1,3,5」の一括指定は自動確定で次へ
    seq_now = _get_question_sequence(state.get("answers", {}))
    q_now = seq_now[step]
    if q_now.get("multi") and text != "完了" and not state.pop("_autodone", False):
        line_bot_api.reply_message(event.reply_token, _render_question(step, state))
        return

    # 飲食店：エリア=現在地 → 位置情報が未取得なら要求
    if state["answers"].get("request") == "飲食店" and q_now["key"] == "area":
        if state.get("need_location") and not state.get("geo"):
            _ask_location(event.reply_token)
            return

    # 次の質問へ
    state["step"] = step + 1
    seq = _get_question_sequence(state.get("answers", {}))
    if state["step"] < len(seq):
        line_bot_api.reply_message(event.reply_token, _render_question(state["step"], state))
        return

    # すべて回答済み → 提案
    answers = state["answers"].copy()
    try:
        send_plan_parts(event.reply_token, uid, answers)
    except Exception as e:
        app.logger.exception("OpenAI API error")
        chunks = f"サーバ側で一時的なエラーが発生しました。\n(debug: {type(e).__name__})"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=chunks))
        return

    users.pop(uid, None)

# 位置情報メッセージの受信（飲食店で現在地指定時）
@handler.add(MessageEvent, message=LocationMessage)
def on_location(event: MessageEvent):
    uid = event.source.user_id
    if uid not in users or not users[uid]:
        users[uid] = {"step": 0, "answers": {}, "hist": deque(maxlen=MAX_TURNS), "multi_temp": {}}
        line_bot_api.reply_message(event.reply_token, _render_question(0, users[uid]))
        return

    state = users[uid]
    lat = event.message.latitude
    lng = event.message.longitude
    state["answers"]["geo"] = f"({lat:.6f},{lng:.6f})"
    state["geo"] = (lat, lng)
    state["need_location"] = False

    # 次の質問へ
    state["step"] = state.get("step", 0) + 1
    seq = _get_question_sequence(state.get("answers", {}))
    if state["step"] < len(seq):
        line_bot_api.reply_message(event.reply_token, _render_question(state["step"], state))
        return

    # 全回答完了 → 提案
    try:
        send_plan_parts(event.reply_token, uid, state["answers"].copy())
    except Exception as e:
        app.logger.exception("OpenAI API error")
        chunks = f"サーバ側で一時的なエラーが発生しました。\n(debug: {type(e).__name__})"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=chunks))
        return

    users.pop(uid, None)

# ====================== ローカル実行 ======================
if __name__ == "__main__":
    logging.getLogger().setLevel(logging.INFO)
    logging.info(f"Running Python: {sys.version}")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)









