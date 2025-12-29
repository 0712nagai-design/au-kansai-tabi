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
from linebot.models import (
    MessageEvent, TextMessage, LocationMessage,
    TextSendMessage,
    TemplateSendMessage, ButtonsTemplate, URITemplateAction,
    QuickReply, QuickReplyButton, LocationAction, FlexSendMessage,
    CarouselTemplate, CarouselColumn,   # ★ 追加
)
from linebot.models import FollowEvent

# OpenAI v1
from openai import OpenAI
import math


# ====================== 環境変数 ======================
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

if not LINE_CHANNEL_SECRET or not LINE_CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("LINEの環境変数が未設定です（LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN）")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY が未設定です")

client = OpenAI(api_key=OPENAI_API_KEY)
EXPERIENCE_MASTER = {}

try:
    with open("data/experience_master.json", "r", encoding="utf-8") as f:
        raw = json.load(f)
        if isinstance(raw, list):
            conv = {}
            for row in raw:
                if isinstance(row, dict) and row.get("id"):
                    conv[row["id"]] = row
            EXPERIENCE_MASTER = conv
        elif isinstance(raw, dict):
            EXPERIENCE_MASTER = raw
        else:
            EXPERIENCE_MASTER = {}
except FileNotFoundError:
    EXPERIENCE_MASTER = {}

# ====================== マスターデータ（観光地）読込 ======================


EXPERIENCE_MASTER = {}

try:
    with open("data/experience_master.json", "r", encoding="utf-8") as f:
        raw = json.load(f)
        if isinstance(raw, list):
            conv = {}
            for row in raw:
                if isinstance(row, dict) and row.get("id"):
                    conv[row["id"]] = row
            EXPERIENCE_MASTER = conv
        elif isinstance(raw, dict):
            EXPERIENCE_MASTER = raw
        else:
            EXPERIENCE_MASTER = {}
except FileNotFoundError:
    EXPERIENCE_MASTER = {}
FOOD_MASTER: Dict[str, Any] = {}

def _load_master_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, list):
            conv = {}
            for row in raw:
                if isinstance(row, dict) and row.get("id"):
                    conv[row["id"]] = row
            return conv
        if isinstance(raw, dict):
            return raw
        return {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
HOTEL_MASTER: Dict[str, Any] = {}
HOTEL_MASTER = _load_master_json("data/hotel_master.json")
FOOD_MASTER = _load_master_json("data/food_master.json")

SIGHTSEEING_MASTER = {}

def _normalize_pref_name(name: str) -> str:
    """京都府 / 大阪府 / 奈良県 みたいなのを 京都 / 大阪 / 奈良 にそろえる"""
    if not isinstance(name, str):
        return ""
    name = name.strip()
    for suf in ("府", "県"):
        if name.endswith(suf):
            name = name[:-1]
    return name

try:
    with open("data/sightseeing_master (3).json", "r", encoding="utf-8") as f:
        raw = json.load(f)

        # もし [ {...}, {...} ] みたいなリスト形式なら id をキーにして dict に変換
        if isinstance(raw, list):
            conv = {}
            for row in raw:
                if not isinstance(row, dict):
                    continue
                sid = row.get("id")
                if not sid:
                    continue
                conv[sid] = row
            SIGHTSEEING_MASTER = conv

        # すでに {"kifune_jinja": {...}} 形式ならそのまま
        elif isinstance(raw, dict):
            SIGHTSEEING_MASTER = raw

        else:
            SIGHTSEEING_MASTER = {}

except FileNotFoundError:
    SIGHTSEEING_MASTER = {
        "kifune_jinja": {
            "name": "貴船神社",
            "name_en": "Kifune Shrine",
            "pref": "京都",
            "pref_en": "Kyoto",
            "area": "京都市左京区・貴船",
            "description": "京都の山あいにある水の神様を祀る神社。四季の景色と川沿いの参道が人気。",
            "description_en": "Shrine in the mountains of Kyoto, famous for water deity and scenic seasons.",
            "official_url": "https://kifunejinja.jp/",
            "map_url": "https://www.google.com/maps/place/貴船神社/",
            "address": "京都府京都市左京区鞍馬貴船町180",
            "images": []
        }
    }


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
def get_geo(item: dict):
    # 正式推奨: item["geo"]["lat"], item["geo"]["lng"]
    if "geo" in item and item["geo"]:
        lat = item["geo"].get("lat")
        lng = item["geo"].get("lng") or item["geo"].get("lon")
        if lat is not None and lng is not None:
            return float(lat), float(lng)

    if "k" in item and item["k"]:
        lat = item["k"].get("lat")
        lng = item["k"].get("lng") or item["k"].get("lon")
        if lat is not None and lng is not None:
            return float(lat), float(lng)

    lat = item.get("lat")
    lng = item.get("lng") or item.get("lon")
    if lat is not None and lng is not None:
        return float(lat), float(lng)

    return None
    
def _get_sp_lat(sp: Dict[str, Any]) -> Optional[float]:
    # 1) geo 優先
    g = sp.get("geo")
    if isinstance(g, dict):
        v = g.get("lat") or g.get("latitude")
        if v is not None and v != "":
            try:
                return float(v)
            except Exception:
                return None

    # 2) 既存互換
    v = sp.get("lat") or sp.get("latitude")
    if v is None or v == "":
        return None
    try:
        return float(v)
    except Exception:
        return None


def _get_sp_lon(sp: Dict[str, Any]) -> Optional[float]:
    # 1) geo 優先
    g = sp.get("geo")
    if isinstance(g, dict):
        v = g.get("lng") or g.get("lon") or g.get("longitude")
        if v is not None and v != "":
            try:
                return float(v)
            except Exception:
                return None

    # 2) 既存互換
    for k in ("lon", "lng", "longitude"):
        v = sp.get(k)
        if v is not None and v != "":
            try:
                return float(v)
            except Exception:
                return None
    return None

def _get_near_food_from_master(geo: Dict[str, Any], max_km: float = 1.0, limit: int = 3):
    lat0, lon0 = float(geo["lat"]), float(geo["lng"])
    scored = []

    for sp in FOOD_MASTER.values():
        if not isinstance(sp, dict):
            continue
        g = sp.get("geo") or {}
        lat = g.get("lat")
        lon = g.get("lng") or g.get("lon")
        if lat is None or lon is None:
            continue
        try:
            d = _distance_km(lat0, lon0, float(lat), float(lon))
        except Exception:
            continue
        if d <= max_km:
            scored.append((d, sp))

    scored.sort(key=lambda x: x[0])
    return [sp for _, sp in scored[:limit]]


def _get_near_sightseeing_from_master(geo: Dict[str, Any], max_km: float = 15.0, limit: int = 3):
    lat0, lon0 = float(geo["lat"]), float(geo["lng"])
    scored = []

    for sp in SIGHTSEEING_MASTER.values():
        if not isinstance(sp, dict):
            continue

        lat = _get_sp_lat(sp)
        lon = _get_sp_lon(sp)
        if lat is None or lon is None:
            continue

        d = _distance_km(lat0, lon0, lat, lon)
        if d <= max_km:
            scored.append((d, sp))

    scored.sort(key=lambda x: x[0])
    return [sp for _, sp in scored[:limit]]
def _get_near_experience_from_master(geo: Dict[str, Any], max_km: float = 10.0, limit: int = 3):
    lat0, lon0 = float(geo["lat"]), float(geo["lng"])
    scored = []

    for sp in EXPERIENCE_MASTER.values():
        if not isinstance(sp, dict):
            continue

        g = sp.get("geo") or {}
        lat = g.get("lat")
        lon = g.get("lng") or g.get("lon")
        if lat is None or lon is None:
            continue

        try:
            d = _distance_km(lat0, lon0, float(lat), float(lon))
        except Exception:
            continue

        if d <= max_km:
            scored.append((d, sp))

    scored.sort(key=lambda x: x[0])
    return [sp for _, sp in scored[:limit]]

def normalize_state(state: dict) -> dict:
    # 人数は聞かないが、下流が参照しても落ちないようにする
    state.setdefault("people", 2)
    return state

    

def _distance_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1-a))
def _normalize_hotel_type(label: str) -> str:
    """
    UIのHOTELS（日本語）→ マスターの正規化hotel_typeへ寄せる
    """
    m = {
        "高級": "luxury",
        "中価格": "mid",
        "コスパ": "value",
        "和風旅館": "ryokan",   # ←あなたの正規化が "ryokan" ならこれでOK
        "こだわらない": "",
    }
    return m.get(label, "")
import re
from typing import Any, List

import re
from typing import Any, Dict, List

import re
from typing import Any, Dict, List, Tuple

def _extract_tags(v: Any) -> List[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    s = str(v).strip()
    if not s:
        return []
    parts = re.split(r"[,\u3001/|\s\u3000\u30fb]+", s)  # , 、 / | 空白 全角空白 ・
    return [p.strip() for p in parts if p.strip()]

def _infer_from_tags(tags: Any) -> Tuple[str, bool]:
    """
    return: (tier, is_ryokan)
      tier: 'luxury'|'mid'|'value'|''
      is_ryokan: True/False
    """
    ts = _extract_tags(tags)
    joined = " ".join(ts)

    # 形式（旅館）判定
    is_ryokan = bool(re.search(
        r"(和風旅館|温泉旅館|旅館|ryokan|japanese\s*inn|japanese[- ]style)",
        joined, flags=re.I
    ))

    # 価格帯（tier）判定
    # ※「高級,和風旅館」なら tier='luxury' かつ is_ryokan=True になる
    if re.search(r"(高級|ラグジュアリー|luxury|premium)", joined, flags=re.I):
        tier = "luxury"
    elif re.search(r"(中価格|ミドル|standard|mid|middle)", joined, flags=re.I):
        tier = "mid"
    elif re.search(r"(コスパ|お得|budget|value|リーズナブル)", joined, flags=re.I):
        tier = "value"
    else:
        tier = ""

    return tier, is_ryokan

def _normalize_hotel_label(label: str) -> Tuple[str, bool]:
    """
    UI入力を2軸に正規化して返す
    - tier: luxury/mid/value/''
    - want_ryokan: True/False
    """
    s = (label or "").strip()

    # UIが単一選択しかない前提：和風旅館なら want_ryokan=True
    if s in ("和風旅館", "旅館"):
        return "", True

    m = {
        "高級": "luxury",
        "中価格": "mid",
        "コスパ": "value",
        "こだわらない": "",
    }
    return m.get(s, ""), False

def _search_hotel_master(pref: str = "", hotel_label: str = "", limit: int = 3) -> List[Dict[str, Any]]:
    pref = _normalize_pref_name(pref)

    want_tier, want_ryokan = _normalize_hotel_label(hotel_label)

    results: List[Dict[str, Any]] = []
    for sp in HOTEL_MASTER.values():
        if not isinstance(sp, dict):
            continue

        sp_pref = _normalize_pref_name(sp.get("pref", ""))
        if pref and sp_pref and sp_pref != pref:
            continue

        tier, is_ryokan = _infer_from_tags(sp.get("tags"))

        # UIが旅館指定なら旅館のみ
        if want_ryokan and not is_ryokan:
            continue

        # UIが価格帯指定なら価格帯一致（旅館でもホテルでもOK）
        if want_tier and tier != want_tier:
            continue

        if not sp.get("official_url"):
            continue
        if sp.get("price_num") in (None, "", 0):
            continue

        results.append(sp)

    results.sort(key=lambda x: int(x.get("price_num", 10**12) or 10**12))
    return results[:limit]




def _hotel_type_from_tags(tags: Any) -> str:
    ts = set(_extract_tags(tags))

    # まずは和風旅館系
    if "和風旅館" in ts or "旅館" in ts or "ryokan" in {t.lower() for t in ts}:
        return "ryokan"

    # 次に価格帯系
    if "高級" in ts:
        return "luxury"
    if "中価格" in ts or "ミドル" in ts:
        return "mid"
    if "コスパ" in ts or "格安" in ts or "安い" in ts:
        return "value"

    return ""
    
def _normalize_hotel_type_value(v: Any) -> str:
    """
    master 側の hotel_type/type が
    '高級' / 'luxury' / 'Luxury' / 'LUX' みたいに揺れても
    'luxury'/'mid'/'value'/'ryokan' に寄せる
    """
    s = ("" if v is None else str(v)).strip().lower()

    # 日本語 → 正規化
    ja_map = {
        "高級": "luxury",
        "中価格": "mid",
        "コスパ": "value",
        "和風旅館": "ryokan",
        "旅館": "ryokan",
    }
    if s in [k.lower() for k in ja_map.keys()]:
        # 元のキーが日本語なので再検索
        for k, vv in ja_map.items():
            if s == k.lower():
                return vv

    # 英語っぽい揺れ → 正規化
    en_map = {
        "luxury": "luxury",
        "high": "luxury",
        "premium": "luxury",
        "mid": "mid",
        "middle": "mid",
        "standard": "mid",
        "value": "value",
        "budget": "value",
        "cospa": "value",
        "ryokan": "ryokan",
        "japanese inn": "ryokan",
        "inn": "ryokan",
    }
    return en_map.get(s, s)




def _normalize_food_genre(label: str) -> str:
    # CUISINES のラベル → tags検索用キーワード寄せ
    m = {
        "和食": "和食,定食,おばんざい,寿司,うどん,蕎麦",
        "洋食": "洋食,イタリアン,フレンチ,ビストロ",
        "中華": "中華,餃子,点心",
        "ラーメン": "ラーメン,つけ麺",
        "カフェ・スイーツ": "カフェ,喫茶,スイーツ,パン",
        "こだわらない": "",
    }
    return m.get(label, label)
def _is_food_complete(sp: Dict[str, Any]) -> bool:
    g = sp.get("geo") or {}
    must = [
        g.get("lat"), g.get("lng"),
        sp.get("official_url"),
        sp.get("open_hours"),
        sp.get("price"),
    ]
    return all(x is not None and str(x).strip() != "" for x in must)

def _search_food_master(pref: str = "", area: str = "", cuisine: str = "", limit: int = 3) -> List[Dict[str, Any]]:
    pref = _normalize_pref_name(pref)
    area = (area or "").strip()
    keys = []
    if cuisine:
        keys = [x.strip() for x in _normalize_food_genre(cuisine).split(",") if x.strip()]

    results = []
    for sp in FOOD_MASTER.values():
        if not isinstance(sp, dict):
            continue

        sp_pref = _normalize_pref_name(sp.get("pref", ""))
        if pref and pref != "現在地から近く" and sp_pref and sp_pref != pref:
            continue

        # area が「京都」「大阪」等の県名になっている運用なら、prefで足りるので area は任意
        if area and area not in {"現在地から近く"}:
            # sp["area"] 内に含まれるか（駅名でもOK）
            if area not in (sp.get("area") or ""):
                # area でヒットしなければスキップ（厳しめ）
                continue

        if keys:
            hay = (sp.get("name","") + " " + sp.get("area","") + " " + (sp.get("tags") or "")).lower()
            if not any(k.lower() in hay for k in keys):
                continue

        results.append(sp)

    return results[:limit]


def _normalize_exp_genre_label(label: str) -> str:
    # EXP_GENRESのラベル → tags検索用キーワードに寄せる（ざっくり）
    m = {
        "温泉": "温泉",
        "自然体験": "自然",
        "文化体験": "文化",
        "モノづくり体験": "陶芸,工房,クラフト,ものづくり",
        "グルメ・食体験": "グルメ,食,和菓子,酒蔵,漬物",
    }
    return m.get(label, label)

def _search_experience_master(pref: str = "", exp_genre: str = "", limit: int = 3) -> List[Dict[str, Any]]:
    pref = _normalize_pref_name(pref)

    genre_keys = []
    if exp_genre:
        genre_keys = [x.strip() for x in _normalize_exp_genre_label(exp_genre).split(",") if x.strip()]

    results = []
    for sp in EXPERIENCE_MASTER.values():
        if not isinstance(sp, dict):
            continue

        sp_pref = _normalize_pref_name(sp.get("pref", ""))
        if pref and pref != "現在地から近く" and sp_pref and sp_pref != pref:
            continue

        if genre_keys:
            tags = (sp.get("tags") or "")
            hay = (sp.get("name","") + " " + sp.get("area","") + " " + tags).lower()
            ok = any(k.lower() in hay for k in genre_keys)
            if not ok:
                continue

        results.append(sp)

    return results[:limit]



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

def _get_lang_code(answers: Dict[str, Any]) -> str:
    """
    answers["lang"] から 'ja' / 'en' を返す。
    未設定のときは 'ja'。
    """
    lang = answers.get("lang", "")
    if str(lang).lower().startswith("e"):
        return "en"
    return "ja"
PREFS_KANSAI = {1: "京都", 2: "大阪", 3: "奈良", 4: "兵庫", 5: "滋賀", 6: "和歌山"}

# --- ホテル ---
STAY_PLAN_HOTEL = {1: "日帰り", 2: "1泊2日", 3: "2泊3日", 4: "3泊4日", 5: "4泊5日", 6: "5泊6日"}
HOTELS  = {1: "高級", 2: "中価格", 3: "コスパ", 4: "和風旅館", 5: "こだわらない"}

# --- 飲食店 ---
MEAL_TIMES   = {1: "朝", 2: "昼", 3: "夜"}
AREAS_FOOD   = {1: "現在地から近く", 2: "京都", 3: "大阪", 4: "奈良", 5: "兵庫", 6: "滋賀", 7: "和歌山"}

COMPANION_FOOD = {1: "一人", 2: "カップル", 3: "友達", 4: "家族"}
CUISINES     = {1: "和食", 2: "洋食", 3: "中華", 4: "ラーメン", 5: "カフェ・スイーツ", 6: "こだわらない"}
BUDGET_FOOD  = {1: "～1000円", 2: "1000～2000円", 3: "2000～5000円", 4: "5000円以上"}

# --- 体験スポット ---
AREAS_EXP    = AREAS_FOOD.copy()
COMPANION_EXP= COMPANION_FOOD.copy()
EXP_GENRES   = {1: "温泉", 2: "自然体験", 3: "文化体験", 4: "モノづくり体験", 5: "グルメ・食体験"}

# --- 観光地 ---
AREAS_SIGHT = {
    0: "現在地から近く",
    **PREFS_KANSAI
}


# --- 日程表 ---
PREFS_MULTI  = PREFS_KANSAI.copy()   # 複数選択
STAY_PLAN_ITI= {1: "日帰り", 2: "1泊2日", 3: "2泊3日", 4: "3泊4日", 5: "4泊5日", 6: "5泊6日"}
THEMES_MULTI = {1:"グルメ",2:"歴史文化",3:"自然癒し",4:"夜景",5:"温泉",6:"家族",7:"ショッピング",8:"体験メイン",9:"その他"}  # 複数選択
COMPANION_ITI= COMPANION_FOOD.copy()
DEPT_CHOICES = {1:"6–8時",2:"9–11時",3:"12–14時",4:"15–17時",5:"18時以降"}
ARRV_CHOICES = {1:"14–17時",2:"17–19時",3:"19–21時",4:"21時以降",5:"未定"}
TRANSPORT_ITI= {1:"公共交通",2:"車",3:"徒歩中心"}


def _get_question_sequence(answers: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    ★ 言語選択を廃止して、日本語固定にしたバージョン
    - いきなり「何を提案しますか？」から始まる
    - 英語用の分岐は一旦なくして、日本語だけ
    """

    # ここで日本語固定
    lang = "ja"

    seq: List[Dict[str, Any]] = []

    # 0問目：何を提案しますか？
    req_title = "何を提案しますか？"
    seq.append({
        "key": "request",
        "title": req_title,
        "choices": REQUESTS,   # {1: "ホテル", 2: "日程表", 3: "飲食店", 4: "体験スポット", 5: "観光地"}
        "multi": False,
    })

    # 以降は request に応じて分岐
    req = answers.get("request")

    # ===================== ホテル =====================
    if req == "ホテル":
        prefs_choices   = PREFS_KANSAI
        stay_choices    = STAY_PLAN_HOTEL
        hotel_choices   = HOTELS

        title_pref   = "関西の都道府県を1つ選んでください。"
        title_stay   = "何泊何日ですか？"
        title_hotel  = "ホテルタイプを選んでください。"

        seq += [
            {"key": "pref",      "title": title_pref,   "choices": prefs_choices,   "multi": False},
            {"key": "stay_plan", "title": title_stay,   "choices": stay_choices,    "multi": False},
            {"key": "hotel",     "title": title_hotel,  "choices": hotel_choices,   "multi": False},
        ]
        return seq

    # ===================== 飲食店 =====================
    if req == "飲食店":
        meal_choices   = MEAL_TIMES
        area_choices   = AREAS_FOOD
        comp_choices   = COMPANION_FOOD
        cui_choices    = CUISINES
        budget_choices = BUDGET_FOOD

        title_meal   = "食事のタイミングを選んでください。"
        title_area   = "エリアを選んでください。"
        title_comp   = "同行者を選んでください。"
        title_cui    = "食べたいジャンルを選んでください。"
        title_budget = "ご予算を選んでください。"

        seq += [
            {"key": "meal_time", "title": title_meal,   "choices": meal_choices,   "multi": False},
            {"key": "area",      "title": title_area,   "choices": area_choices,   "multi": False},
            {"key": "companion", "title": title_comp,   "choices": comp_choices,   "multi": False},
            {"key": "cuisine",   "title": title_cui,    "choices": cui_choices,    "multi": False},
            {"key": "budget",    "title": title_budget, "choices": budget_choices, "multi": False},
        ]
        return seq

    # ===================== 体験スポット =====================
    if req == "体験スポット":
        pref_choices   = AREAS_EXP
        comp_choices   = COMPANION_EXP
        genre_choices  = EXP_GENRES

        title_pref   = "関西の都道府県を1つ選んでください。"
        title_genre  = "体験ジャンルを選んでください。"
        title_comp   = "同行者を選んでください。"

        seq += [
            {"key": "pref",      "title": title_pref,   "choices": pref_choices,   "multi": False},
            {"key": "exp_genre", "title": title_genre,  "choices": genre_choices,  "multi": False},
            {"key": "companion", "title": title_comp,   "choices": comp_choices,   "multi": False},
        ]
        return seq

    # ===================== 観光地 =====================
    if req == "観光地":
        pref_choices = AREAS_SIGHT
        title_pref   = "関西の都道府県を1つ選んでください。"

        seq += [
            {"key": "pref", "title": title_pref, "choices": pref_choices, "multi": False},
        ]
        return seq

    # ===================== 日程表 =====================
    if req == "日程表":
        prefs_choices   = PREFS_MULTI
        stay_choices    = STAY_PLAN_ITI
        themes_choices  = THEMES_MULTI
        trans_choices   = TRANSPORT_ITI
        comp_choices    = COMPANION_ITI
        dept_choices    = DEPT_CHOICES
        arrv_choices    = ARRV_CHOICES

        t_prefs = "訪問する都道府県を選んでください（複数選択可：例 1,3,6。タップの場合は『完了』）"
        t_date  = "出発日を入力してください（例: 2025-03-20）"
        t_stay  = "何泊何日ですか？"
        t_themes= "旅行のテーマを選んでください（複数選択可：例 1,4,5。タップの場合は『完了』）"
        t_trans = "主な交通手段を選んでください。"
        t_comp  = "同行者を選んでください。"
        t_dept  = "出発時間帯を選んでください。"
        t_arrv  = "帰着時間帯を選んでください。"

        seq += [
            {"key": "prefs",    "title": t_prefs,  "choices": prefs_choices,  "multi": True},
            {"key": "date",     "title": t_date,   "choices": {},             "multi": False},
            {"key": "stay",     "title": t_stay,   "choices": stay_choices,   "multi": False},
            {"key": "themes",   "title": t_themes, "choices": themes_choices, "multi": True},
            {"key": "transport","title": t_trans,  "choices": trans_choices,  "multi": False},
            {"key": "companion","title": t_comp,   "choices": comp_choices,   "multi": False},
            {"key": "dept",     "title": t_dept,   "choices": dept_choices,   "multi": False},
            {"key": "arrv",     "title": t_arrv,   "choices": arrv_choices,   "multi": False},
        ]
        return seq

    # request 未選択時は request だけ返す
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

def _flex_question_bubble(title: str, selected_line: str, pairs: List[List[dict]], show_done: bool, lang: str) -> dict:
    rows = []
    for row in pairs:
        if len(row) == 1:
            row.append({"type": "filler"})
        rows.append({"type": "box", "layout": "horizontal", "spacing": "14px", "contents": row})

    footer_contents = []
    if show_done:
        done_label = "✅ 完了" if lang == "ja" else "✅ Done"
        footer_contents.append({
            "type": "box",
            "layout": "vertical",
            "cornerRadius": "12px",
            "backgroundColor": "#22C55E",
            "paddingAll": "14px",
            "action": {"type": "message", "label": done_label, "text": "完了"},
            "contents": [{
                "type": "text",
                "text": done_label,
                "weight": "bold",
                "size": "20px",
                "align": "center",
                "color": "#FFFFFF"
            }]
        })

    restart_label = "↪ 最初から" if lang == "ja" else "↪ Back to start"
    footer_contents.append({
        "type": "text",
        "text": restart_label,
        "size": "14px",
        "color": "#4F46E5",
        "align": "center",
        "margin": "8px",
        "action": {"type": "message", "label": restart_label, "text": "最初から"}
    })

    return {
        "type": "bubble",
        "size": "mega",
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "12px",
            "paddingAll": "16px",
            "contents": [
                {"type": "text", "text": title, "wrap": True, "size": "24px", "weight": "bold"},
                ({"type": "text", "text": selected_line, "size": "14px", "color": "#6B7280", "wrap": True}
                 if selected_line else {"type": "filler"}),
                {"type": "separator"},
                *rows
            ]
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "spacing": "6px",
            "paddingAll": "12px",
            "contents": footer_contents
        }
    }

REQUEST_IMAGE_URLS = {
    "ホテル":     "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/%E3%81%BB%E3%81%A6%E3%82%8B.png",
    "飲食店":     "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/%E9%A3%B2%E9%A3%9F%E5%BA%97.png",
    "体験スポット": "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/%E4%BD%93%E9%A8%93.png",
    "観光地":     "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/kannku.png",
}

REQUEST_LABELS_EN = {
    "ホテル": "Hotels",
    "飲食店": "Restaurants",
    "体験スポット": "Experiences",
    "観光地": "Sightseeing",
    "日程表": "Itinerary",
}
HOTEL_TYPE_IMAGE_URLS = {
    "高級": "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/koukyu.png",
    "中価格": "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/tyuukakaku.png",
    "コスパ": "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/kosupa.png",
    "和風旅館": "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/wahuu.png",
    # こだわらない → 汎用ホテル画像でOK（とりあえず全体用のホテル画像を使う）
    "こだわらない": "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/kodawaranai.png",
}

# ★ 食事タイミング別の画像（番号で紐づける）
# MEAL_TIMES   = {1: "朝", 2: "昼", 3: "夜"}
# MEAL_TIMES_EN= {1: "Breakfast", 2: "Lunch", 3: "Dinner"}
MEAL_TIME_IMAGE_URLS = {
    1: "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/asa%20(2).png",   # 朝
    2: "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/hiruu.png",       # 昼
    3: "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/yoruu.png",       # 夜
}
COMPANION_IMAGE_URLS = {
    1: "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/hitori%20(1).png",   # ひとり
    2: "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/couple.png",         # カップル
    3: "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/tomodachi.png",      # 友だち
    4: "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/kazoku.png",         # 家族
}
FOOD_GENRE_IMAGE_URLS = {
    1: "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/wa.png",          # 和食
    2: "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/you.png",         # 洋食
    3: "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/tyu.png",         # 中華
    4: "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/ramen.png",       # ラーメン
    5: "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/kahue.png",       # カフェ・スイーツ
    6: "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/kodawaranai.png", # こだわらない
}
EXP_GENRE_IMAGE_URLS = {
    1: "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/onsen.png",      # 温泉
    2: "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/shizen.png",     # 自然体験
    3: "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/bunka%20(2).png",# 文化体験
    4: "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/monoo.png",      # ものづくり体験
    5: "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/gurume.png",     # グルメ・食体験
}
PREF_IMAGE_URLS = {
    "京都": "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/kyoto.png",
    "奈良": "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/nara.png",
    "兵庫": "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/hyogo.png",
    "大阪": "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/osaka.png",
    "和歌山": "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/wakayama.png",
    "滋賀": "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/siga.png",
}

FOOD_AREA_IMAGE_URLS = {
    "現在地から近く": "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/gennzaiti.png",
}

CAROUSEL_IMAGES = {
    "hotel": [
        "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/hotel1.png",
        "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/hotel2.png",
        "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/hotel3.png",
    ],
    "food": [
        "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/food1.png",
        "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/food2.png",
        "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/food3.png",
    ],
    "experience": [
        "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/exp1.png",
        "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/exp2.png",
        "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/exp3.png",
    ],
}
CURRENT_LOCATION_IMAGE_URL = "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/gennzaiti.png"

def _pick_carousel_image(kind: str, idx: int, fallback: str = "") -> str:
    """
    kind: "hotel" / "food" / "experience" など
    idx: 0,1,2...
    """
    try:
        arr = CAROUSEL_IMAGES.get(kind, [])
        if arr and 0 <= idx < len(arr):
            return arr[idx]
        if arr:
            return arr[idx % len(arr)]
    except Exception:
        pass
    return fallback or "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/kannku.png"


def _render_question(idx: int, state: State):
    answers = state.get("answers", {})
    lang = _get_lang_code(answers)  # 'ja' or 'en'

    seq = _get_question_sequence(answers)
    q = seq[idx]
    title = q["title"]

    # ===================== Q1: request =====================
    if q["key"] == "request":

        def img_btn(display_label: str, send_text: str, url: str) -> dict:
            return {
                "type": "box",
                "layout": "vertical",
                "flex": 1,
                "cornerRadius": "16px",
                "backgroundColor": "#FFFFFF",
                "paddingAll": "0px",
                "height": "160px",
                "action": {"type": "message", "label": display_label, "text": send_text},
                "contents": [
                    {
                        "type": "image",
                        "url": url,
                        "size": "full",
                        "aspectMode": "cover",
                        "aspectRatio": "4:3"
                    },
                    {
                        "type": "box",
                        "layout": "vertical",
                        "paddingAll": "4px",
                        "contents": [{
                            "type": "text",
                            "text": display_label,
                            "weight": "bold",
                            "size": "14px",
                            "align": "center",
                            "wrap": True,
                        }],
                    },
                ],
            }

        def txt_btn(display_label: str, send_text: str) -> dict:
            return {
                "type": "box",
                "layout": "vertical",
                "flex": 1,
                "cornerRadius": "16px",
                "backgroundColor": "#EEF2F7",
                "height": "110px",
                "justifyContent": "center",
                "action": {"type": "message", "label": display_label, "text": send_text},
                "contents": [{
                    "type": "text",
                    "text": display_label,
                    "weight": "bold",
                    "size": "16px",
                    "align": "center",
                    "wrap": True,
                }],
            }

        def label_req(v: str) -> str:
            if lang == "en":
                return REQUEST_LABELS_EN.get(v, v)
            return v

        bubble = {
            "type": "bubble",
            "size": "mega",
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "12px",
                "paddingAll": "14px",
                "contents": [
                    {"type": "text", "text": title, "size": "22px", "weight": "bold", "wrap": True},
                    {"type": "separator"},
                    {
                        "type": "box",
                        "layout": "horizontal",
                        "spacing": "10px",
                        "contents": [
                            img_btn(label_req("ホテル"), "ホテル", REQUEST_IMAGE_URLS["ホテル"]),
                            img_btn(label_req("飲食店"), "飲食店", REQUEST_IMAGE_URLS["飲食店"]),
                        ],
                    },
                    {
                        "type": "box",
                        "layout": "horizontal",
                        "spacing": "10px",
                        "contents": [
                            img_btn(label_req("体験スポット"), "体験スポット", REQUEST_IMAGE_URLS["体験スポット"]),
                            img_btn(label_req("観光地"), "観光地", REQUEST_IMAGE_URLS["観光地"]),
                        ],
                    },
                    {
                        "type": "box",
                        "layout": "horizontal",
                        "spacing": "10px",
                        "contents": [
                            txt_btn(label_req("日程表"), "日程表"),
                            {"type": "filler"}
                        ]
                    }
                ],
            },
        }
        return FlexSendMessage(alt_text=title, contents=bubble)

    # ===================== 共通：画像ボタン生成（160pxに統一） =====================
    def vbtn(img_url: str, label: str, num: int) -> dict:
        return {
            "type": "box",
            "layout": "vertical",
            "flex": 1,
            "cornerRadius": "16px",
            "backgroundColor": "#FFFFFF",
            "paddingAll": "0px",
            "height": "160px",
            "action": {"type": "message", "label": label, "text": str(num)},
            "contents": [
                {
                    "type": "image",
                    "url": img_url,
                    "size": "full",
                    "aspectMode": "cover",
                    "aspectRatio": "4:3",
                },
                {
                    "type": "box",
                    "layout": "vertical",
                    "paddingAll": "4px",
                    "contents": [{
                        "type": "text",
                        "text": label,
                        "weight": "bold",
                        "size": "14px",
                        "align": "center",
                        "wrap": True,
                    }],
                },
            ],
        }

    def make_2col_rows(btns: List[dict]) -> List[dict]:
        rows, row = [], []
        for b in btns:
            row.append(b)
            if len(row) == 2:
                rows.append({"type": "box", "layout": "horizontal", "spacing": "10px", "contents": row})
                row = []
        if row:
            row.append({"type": "filler"})
            rows.append({"type": "box", "layout": "horizontal", "spacing": "10px", "contents": row})
        return rows

    # ===================== 画像付き2列ボタン対象を一本化（重複分岐を廃止） =====================
    # NOTE:
    # - numで引ける: meal_time / companion / cuisine / exp_genre / hotel
    # - labelで引ける: pref / area（京都など）
    # - 「現在地から近く」は CURRENT_LOCATION_IMAGE_URL に固定
    image_maps = {
        "meal_time": MEAL_TIME_IMAGE_URLS,
        "companion": COMPANION_IMAGE_URLS,
        "cuisine": FOOD_GENRE_IMAGE_URLS,
        "exp_genre": EXP_GENRE_IMAGE_URLS,
        "hotel": HOTEL_TYPE_IMAGE_URLS,
        "area": (PREF_IMAGE_URLS | FOOD_AREA_IMAGE_URLS) if "FOOD_AREA_IMAGE_URLS" in globals() else PREF_IMAGE_URLS,
        "pref": (PREF_IMAGE_URLS | FOOD_AREA_IMAGE_URLS) if "FOOD_AREA_IMAGE_URLS" in globals() else PREF_IMAGE_URLS,
    }

    if q["key"] in image_maps:
        image_map = image_maps[q["key"]]
        btns = []

        for num, label in q.get("choices", {}).items():
            if label in {"現在地から近く", "Near current location"}:
                img = CURRENT_LOCATION_IMAGE_URL
            else:
                img = (image_map.get(num)          # 例: 1 → 朝/昼/夜 etc
                       or image_map.get(label)     # 例: "京都" → kyoto.png
                       or REQUEST_IMAGE_URLS.get("観光地"))

            btns.append(vbtn(img, label, num))

        rows = make_2col_rows(btns)

        bubble = {
            "type": "bubble",
            "size": "mega",
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "14px",
                "paddingAll": "14px",
                "contents": [
                    {"type": "text", "text": title, "size": "22px", "weight": "bold", "wrap": True},
                    {"type": "separator"},
                    *rows
                ],
            },
        }
        return FlexSendMessage(alt_text=title, contents=bubble)

    # ===================== 最後：通常のテキスト2列ボタン =====================
    selected = state.get("multi_temp", {}).get(q["key"], []) if q.get("multi") else []
    selected_line = f"(選択中：{'、'.join(selected) if selected else 'なし'})" if q.get("multi") else ""

    pairs, row = [], []
    for n, label in q.get("choices", {}).items():
        btn = _flex_choice_button(label, str(n))
        row.append(btn)
        if len(row) == 2:
            pairs.append(row)
            row = []
    if row:
        pairs.append(row)

    bubble = _flex_question_bubble(title, selected_line, pairs, q.get("multi", False), lang)
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
    if step < 0 or step >= len(seq):
        return False

    q = seq[step]
    key = q["key"]

    state.setdefault("answers", {})
    state.setdefault("multi_temp", {})

    t_raw = (text or "").strip()
    t_norm = t_raw.translate(FW_TO_HW)

    # -------------------------------------------------
    # 0) 特別処理：言語選択（※いま seq に lang を入れてないなら実質通らないが安全のため残す）
    # -------------------------------------------------
    if key == "lang":
        if t_raw in {"English", "english", "EN", "en", "2"}:
            state["answers"]["lang"] = "English"
            return True
        if t_raw in {"日本語", "にほんご", "JP", "jp", "1"}:
            state["answers"]["lang"] = "日本語"
            return True
        # 取れなければ通常処理へ（choices があればそっちで拾う）

    # -------------------------------------------------
    # 1) choices がある質問（単一/複数）
    # -------------------------------------------------
    if q.get("choices"):
        n = _label_to_num(q["choices"], t_raw)
        if n is not None:
            val = q["choices"][n]

            # --- 複数選択（タップで溜める） ---
            if q.get("multi"):
                sel = state["multi_temp"].setdefault(key, [])
                if val not in sel:
                    sel.append(val)
                return True

            # --- 単一選択 ---
            state["answers"][key] = val

            # 現在地が必要か（geo がまだ無い & 「現在地から近く」選択）
            need_loc = (not state["answers"].get("geo")) and (val in {"現在地から近く", "Near current location"})

            req = state["answers"].get("request")

            # 飲食店：area=現在地
            if req == "飲食店" and key == "area" and need_loc:
                state["need_location"] = True

            # 体験：pref=現在地
            if req == "体験スポット" and key == "pref" and need_loc:
                state["need_location"] = True

            # 観光地：pref=現在地
            if req == "観光地" and key == "pref" and need_loc:
                state["need_location"] = True

            return True

        # ここに来た＝choices質問なのに番号/ラベル一致しなかった
        # → 下の「数字列入力」判定に回す（例: "1,3"）
        # それもダメなら False

    # -------------------------------------------------
    # 2) マルチ選択の確定（「完了」/「Done」）
    # -------------------------------------------------
    if q.get("multi") and t_raw in {"完了", "Done"}:
        picked = state["multi_temp"].get(key, [])
        if not picked:
            return False
        state["answers"][key] = picked
        return True

    # -------------------------------------------------
    # 3) 日付入力（date）
    # -------------------------------------------------
    if key == "date":
        try:
            datetime.strptime(t_raw, "%Y-%m-%d")
            state["answers"][key] = t_raw
            return True
        except Exception:
            return False

    # -------------------------------------------------
    # 4) 数字列入力（例: "1,3,5"）→ choices がある質問のみ
    # -------------------------------------------------
    nums = _parse_numbers(t_raw)
    if nums and q.get("choices"):
        bad = [n for n in nums if n not in q["choices"]]
        if bad:
            return False

        labels = [q["choices"][n] for n in nums]

        if q.get("multi"):
            # マルチは数字列が来たら即確定（自動で次へ）
            state["answers"][key] = labels
            state["_autodone"] = True
            return True

        # 単一質問に複数来たらNG
        if len(nums) != 1:
            return False

        val = q["choices"][nums[0]]
        state["answers"][key] = val

        # 単一でも「現在地から近く」なら need_location 判定
        need_loc = (not state["answers"].get("geo")) and (val in {"現在地から近く", "Near current location"})
        req = state["answers"].get("request")

        if req == "飲食店" and key == "area" and need_loc:
            state["need_location"] = True
        if req == "体験スポット" and key == "pref" and need_loc:
            state["need_location"] = True
        if req == "観光地" and key == "pref" and need_loc:
            state["need_location"] = True

        return True

    # -------------------------------------------------
    # 5) ここまで全部ダメなら不正入力
    # -------------------------------------------------
    return False


# ====================== OpenAI呼び出し ======================
SYSTEM_PROMPT_BASE = (
    "You are AI Travel Navi Kansai.\n"
    "URLは生URL（Markdownリンク禁止）。画像URLは出さない。\n"
    "架空の施設名・店舗名・ホテル名などを新たに作らないこと。\n"
    "必ず実在し、Googleマップ等で検索できる施設のみを提案してください。\n"
    "条件に合う実在の候補が3件見つからない場合は、無理に埋めず、見つからない旨をはっきり書いてください。\n"
)


def _call_openai_text(user_prompt: str, lang: str = "日本語") -> str:
    # lang: "日本語" or "English"
    if str(lang).lower().startswith("e"):
        sys_prompt = SYSTEM_PROMPT_BASE + "All responses must be written in natural English.\n"
    else:
        sys_prompt = SYSTEM_PROMPT_BASE + "すべての回答は自然な日本語で出力してください。\n"

    res = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.6,
        messages=[
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt},
        ],
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
def _carousel_from_items(header_title: str, items: List[Dict[str, str]]) -> TemplateSendMessage:
    """
    items: {
      "title": str,
      "subtitle": str,
      "official": str,      # 公式サイトURL or 予約サイトURL
      "map": str,           # GoogleマップURL or 緯度経度 or 検索ワード
      "image": str,         # サムネイル画像URL（任意）
      "spot_type": str,     # "hotel" / "food" / "experience" / "sightseeing" など
      "affiliate_url": str, # アフィリエイト(予約)リンク ※今は空欄でもOK
    } の配列を想定
    """
    columns = []

    for it in items[:10]:  # カルーセルは最大10列
        title = (it.get("title") or "")[:40] or "No title"
        text = (it.get("subtitle") or " ")[:60]

        # ---------- 画像 ----------
        img = it.get("image") or ""
        if not img or not img.startswith("http"):
            img = "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/kannku.png"

        # ---------- ボタン ----------
        actions = []

        # 公式サイト
        if it.get("official"):
            official_url = _clean_url(it["official"])
            if official_url and official_url.startswith("http"):
                actions.append(
                    URITemplateAction(
                        label="公式サイト",
                        uri=official_url,
                    )
                )

        # Googleマップ
        if it.get("map"):
            map_url = _normalize_map_url(it["map"], fallback_query=it.get("title", ""))
            if map_url and map_url.startswith("http"):
                actions.append(
                    URITemplateAction(
                        label="Googleマップ",
                        uri=map_url,
                    )
                )

        # ★ 予約(アフィリエイト)ボタン：ホテル/飲食店/体験スポットだけ付ける
        spot_type = it.get("spot_type", "")
        aff_url = it.get("affiliate_url", "")
        if spot_type in ("hotel", "food", "experience") and aff_url:
            aff_url = _clean_url(aff_url)
            if aff_url.startswith("http"):
                actions.append(
                    URITemplateAction(
                        label="予約・詳細はこちら",
                        uri=aff_url,
                    )
                )

        # どのURLも無いときは検索にフォールバック
        if not actions:
            actions.append(
                URITemplateAction(
                    label="検索",
                    uri=f"https://www.google.com/search?q={quote(it.get('title',''))}"
                )
            )

        columns.append(
            CarouselColumn(
                thumbnail_image_url=img,
                title=title,
                text=text,
                actions=actions[:3],  # 3個まで
            )
        )

    return TemplateSendMessage(
        alt_text=header_title,
        template=CarouselTemplate(columns=columns)
    )
def _push_hotels_three_from_master(uid: str, spots: List[Dict[str, Any]], lang: str):
    is_en = str(lang).lower().startswith("e")
    header_text = "🏨 条件に合うホテルを3件ご提案します👇" if not is_en else "🏨 Here are 3 hotels 👇"

    messages = [TextSendMessage(text=header_text)]
    items_for_carousel = []

    for i, sp in enumerate(spots[:3]):
        title = sp.get("name", "")[:40] or "Hotel"
        # price 表示用 + タイプ + 一言
        price = sp.get("price", "")
        ht = sp.get("hotel_type", "")
        desc = (sp.get("description") or "")[:40]
        subtitle = f"{price} / {ht} / {desc}".strip()[:60] or " "

        # map_url 無い場合は geo から生成
        map_url = sp.get("map_url") or ""
        latlng = get_geo(sp)
        if not map_url and latlng:
            map_url = f"https://www.google.com/maps/search/?api=1&query={latlng[0]},{latlng[1]}"

        items_for_carousel.append({
            "title": title,
            "subtitle": subtitle,
            "official": sp.get("official_url") or "",
            "map": map_url,
            "image": _pick_carousel_image("hotel", i, REQUEST_IMAGE_URLS.get("ホテル")),
            "spot_type": "hotel",
            "affiliate_url": "",
        })

        messages.append(TextSendMessage(text=f"🏨 {title}\n{subtitle}"))

    messages.append(_carousel_from_items("🏨 ホテル（マスターデータ）", items_for_carousel))
    _push_messages_in_chunks(uid, messages, size=5)
def _reply_experiences_three_from_master(reply_token: str, spots: List[Dict[str, Any]], lang: str):
    is_en = str(lang).lower().startswith("e")

    def _fmt(v: Any, fallback_ja="情報なし", fallback_en="N/A") -> str:
        s = ("" if v is None else str(v)).strip()
        if s:
            return s
        return fallback_en if is_en else fallback_ja

    header_text = "🎯 条件に合う体験スポットを3件ご提案します👇" if not is_en else "🎯 Here are 3 experience spots 👇"

    items_for_carousel = []
    texts = []

    for i, sp in enumerate(spots[:3]):
        title = (sp.get("name") or "").strip() or ("Experience" if is_en else "体験スポット")
        desc  = (sp.get("description") or "").strip() or ("No description." if is_en else "説明なし")

        hours = _fmt(sp.get("open_hours"))
        price = _fmt(sp.get("price"))

        latlng = get_geo(sp)
        map_url = (sp.get("map_url") or "").strip()
        if not map_url and latlng:
            map_url = f"https://www.google.com/maps/search/?api=1&query={latlng[0]},{latlng[1]}"

        # 1件説明（replyに入れる）
        if not is_en:
            body = f"🎯 {title}\n{desc}\n\n🕒 営業時間：{hours}\n💴 料金：{price}"
        else:
            body = f"🎯 {title}\n{desc}\n\n🕒 Hours: {hours}\n💴 Price: {price}"
        texts.append(TextSendMessage(text=body))

        subtitle = f"{desc} / 🕒{hours} / 💴{price}"
        subtitle = subtitle[:60] if subtitle else " "

        items_for_carousel.append({
            "title": title[:40],
            "subtitle": subtitle,
            "official": (sp.get("official_url") or "").strip(),
            "map": map_url,
            "image": _pick_carousel_image("experience", i, REQUEST_IMAGE_URLS.get("体験スポット")),
            "spot_type": "experience",
            "affiliate_url": "",
        })

    carousel = _carousel_from_items("🎯 体験スポット（マスターデータ）", items_for_carousel)

    # replyは最大5件：ヘッダー(1)+説明3(3)+カルーセル(1)=5
    line_bot_api.reply_message(reply_token, [TextSendMessage(text=header_text)] + texts + [carousel])
    
def _push_experiences_three_from_master(uid: str, spots: List[Dict[str, Any]], lang: str):
    is_en = str(lang).lower().startswith("e")

    header_text = "🎯 条件に合う体験スポットを3件ご提案します👇" if not is_en else "🎯 Here are 3 experience spots 👇"
    line_bot_api.push_message(uid, TextSendMessage(text=header_text))

    items_for_carousel = []

    def _fmt(v: Any, fallback_ja="情報なし", fallback_en="N/A") -> str:
        s = ("" if v is None else str(v)).strip()
        if s:
            return s
        return fallback_en if is_en else fallback_ja

    for i, sp in enumerate(spots[:3]):
        title = (sp.get("name") or "").strip() or ("Experience" if is_en else "体験スポット")
        desc  = (sp.get("description") or "").strip()
        if not desc:
            desc = "説明なし" if not is_en else "No description."

        # ★ 追加：営業時間・料金
        hours = _fmt(sp.get("open_hours"))
        price = _fmt(sp.get("price"))

        # map_url 無い場合は geo から生成
        latlng = get_geo(sp)
        map_url = (sp.get("map_url") or "").strip()
        if not map_url and latlng:
            map_url = f"https://www.google.com/maps/search/?api=1&query={latlng[0]},{latlng[1]}"

        # --- 1件ずつの説明テキスト（必ず出す） ---
        if not is_en:
            body = (
                f"🎯 {title}\n"
                f"{desc}\n\n"
                f"🕒 営業時間：{hours}\n"
                f"💴 料金：{price}"
            )
        else:
            body = (
                f"🎯 {title}\n"
                f"{desc}\n\n"
                f"🕒 Hours: {hours}\n"
                f"💴 Price: {price}"
            )
        line_bot_api.push_message(uid, TextSendMessage(text=body))

        # --- カルーセル subtitle（60文字制限） ---
        subtitle = f"{desc} / 🕒{hours} / 💴{price}"
        subtitle = subtitle[:60] if subtitle else " "

        items_for_carousel.append({
            "title": title[:40],
            "subtitle": subtitle,
            "official": (sp.get("official_url") or "").strip(),
            "map": map_url,
            "image": _pick_carousel_image("experience", i, REQUEST_IMAGE_URLS.get("体験スポット")),
            "spot_type": "experience",
            "affiliate_url": "",
        })

    # --- 最後にカルーセル（必ず出す） ---
    line_bot_api.push_message(uid, _carousel_from_items("🎯 体験スポット（マスターデータ）", items_for_carousel))


def _push_foods_three_from_master(uid: str, spots: List[Dict[str, Any]], lang: str):
    is_en = str(lang).lower().startswith("e")
    header_text = "🍽 条件に合う飲食店を3件ご提案します👇" if not is_en else "🍽 Here are 3 restaurants 👇"

    messages = [TextSendMessage(text=header_text)]
    items_for_carousel = []

    for i, sp in enumerate(spots[:3]):
        title = (sp.get("name_en") if is_en else sp.get("name")) or "Restaurant"
        desc  = (sp.get("description_en") if is_en else sp.get("description")) or ""
        subtitle = (desc[:60] if desc else " ")

        # map_url が無い場合は geo から生成
        map_url = sp.get("map_url") or ""
        latlng = get_geo(sp)
        if not map_url and latlng:
            map_url = f"https://www.google.com/maps/search/?api=1&query={latlng[0]},{latlng[1]}"

        # 1件ずつテキスト（任意）
        if not is_en:
            body = f"🍽 {sp.get('name','')}\n{subtitle}\n\n💰 価格帯：{sp.get('price','情報なし')}\n🕰 営業：{sp.get('open_hours','情報なし')}"
        else:
            body = f"🍽 {title}\n{subtitle}\n\n💰 Price: {sp.get('price','N/A')}\n🕰 Hours: {sp.get('open_hours','N/A')}"
        messages.append(TextSendMessage(text=body))

        items_for_carousel.append({
            "title": (sp.get("name_en") if is_en else sp.get("name") or "Restaurant")[:40],
            "subtitle": subtitle[:60],
            "official": sp.get("official_url") or "",
            "map": map_url,
            "image": _pick_carousel_image("food", i, REQUEST_IMAGE_URLS.get("飲食店")),
            "spot_type": "food",
            "affiliate_url": "",
        })

    messages.append(_carousel_from_items("🍽 飲食店（マスターデータ）", items_for_carousel))
    _push_messages_in_chunks(uid, messages, size=5)

# ====================== AI観光モード 用ヘルパー ======================

def build_ai_kanko_prompt(user_query: str, lang: str, geo: Optional[Dict[str, Any]] = None) -> str:
    """
    ユーザーが自由入力した「行きたいところ・やりたいこと」をもとに、
    ホテル / 飲食店 / 体験 / 観光地 をミックスして最大3件提案させるためのプロンプト。
    画像URLは出させない & URLは必ず生URLで。
    geo があれば現在地付近優先の指示を追加。
    """
    is_en = str(lang).lower().startswith("e")

    # 現在地ヒント（あれば）
    near_hint_ja = ""
    near_hint_en = ""
    if geo:
        lat = geo.get("lat")
        lng = geo.get("lng")
        near_hint_ja = f"""
なお、ユーザーは現在位置（lat={lat}, lng={lng}）の近くを希望しています。
可能な範囲で、この座標からおおよそ半径2km圏内のスポットを優先して選んでください。
座標が直接使えない場合は、「現在地周辺」「最寄り駅周辺」など、現在地付近のエリアで探してください。
""".strip()

        near_hint_en = f"""
The user prefers places **near their current location** (lat={lat}, lng={lng}).
As much as possible, prioritize spots within roughly a 2 km radius of these coordinates.
If you cannot use coordinates directly, search around the nearest station / area to this location.
""".strip()

    if is_en:
        return f"""
You are a Kansai travel concierge.

User's request:
"{user_query}"

{near_hint_en}

Based on this request, suggest up to **3 places** in Kansai
(hotels, restaurants, experience spots, or sightseeing spots).

Important rules:
- Do NOT invent place names.
- Only suggest real places that actually exist and can be found on Google Maps.
- For every place, ALWAYS output:
  - "Category:" ...   (one of: Hotel / Restaurant / Experience / Sightseeing)
  - "Official:" URL   (official site or major info page)
  - "Google Maps:" URL
- Do NOT output any image URLs.
- If you cannot find 3 suitable places, it's OK to output only 1 or 2.

Output exactly in the following format, separated by blank lines (no extra text):

① [Category] Place name (area)
Short comment: 1-line summary (who it's for / what's good)
💰 Price guide: ...
Category: Hotel / Restaurant / Experience / Sightseeing
🔗 Official: https://...
📍 Google Maps: https://...

② [Category] Place name (area)
Short comment: ...
💰 Price guide: ...
Category: ...
🔗 Official: https://...
📍 Google Maps: https://...

③ [Category] Place name (area)
Short comment: ...
💰 Price guide: ...
Category: ...
🔗 Official: https://...
📍 Google Maps: https://...
""".strip()
    else:
        return f"""
あなたは関西旅行のコンシェルジュです。

ユーザーの要望：
「{user_query}」

{near_hint_ja}

この内容に合う **最大3件** の候補を、
ホテル / 飲食店 / 体験スポット / 観光地 の区別なくミックスで提案してください。

ルール：
- 架空の施設名・店舗名を作らないこと。
- 必ず実在し、Googleマップで検索できる場所のみを出すこと。
- 各候補ごとに必ず
  - 「Category: 」… Hotel / Restaurant / Experience / Sightseeing のどれか
  - 「Official: 」公式サイト or 信頼できる紹介ページのURL
  - 「Google Maps: 」GoogleマップURL
  を含めること。
- 画像URLは出さないこと。
- 条件に合う場所が1〜2件しかなくても、その件数だけ出してOK。

出力は **以下のフォーマットだけ** にしてください（解説文・前後の文章は不要）。
候補同士は「空行」で区切ること（罫線は使わない）：

① [カテゴリ] 名称（エリア）
短評：誰向けか／何が良いか など1行
💰 目安：...
Category: Hotel / Restaurant / Experience / Sightseeing
🔗 Official: https://...
📍 Google Maps: https://...

② [カテゴリ] 名称（エリア）
短評：...
💰 目安：...
Category: ...
🔗 Official: https://...
📍 Google Maps: https://...

③ [カテゴリ] 名称（エリア）
短評：...
💰 目安：...
Category: ...
🔗 Official: https://...
📍 Google Maps: https://...
""".strip()



def parse_ai_kanko_result(text: str) -> List[Dict[str, str]]:
    """
    build_ai_kanko_prompt の出力テキストをパースして、
    カテゴリ情報付きの items リストに変換する。
    戻り値の各 dict は:
      - title
      - subtitle
      - official
      - map
      - image
      - category  (hotel / restaurant / experience / sightseeing / other)
    """
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text.strip()) if b.strip()]
    results = []

    for block in blocks[:10]:  # 念のため多めに見ておく（あとでカテゴリごとに3件に絞る）
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue

        # 1行目: ① [カテゴリ] 名称（エリア）
        raw_title = re.sub(r"^[①-⑳\d\.\)\s🏨🍽🎯🏯]*", "", lines[0])
        title = raw_title or "スポット"

        # 短評
        mshort = re.search(r"^(?:短評|Short comment)[:：]\s*(.+)$", block, re.M)
        short = mshort.group(1).strip() if mshort else ""

        # URL類
        moff = OFFICIAL_URL_RE.search(block)
        mmap = MAP_URL_RE.search(block)

        official = moff.group(1).strip() if moff else ""
        map_url  = mmap.group(1).strip() if mmap else ""

        # カテゴリ判定
        mcat = re.search(r"^Category[:：]\s*(.+)$", block, re.M | re.I)
        category_raw = (mcat.group(1).strip().lower() if mcat else "")

        if "hotel" in category_raw:
            cat = "hotel"
            img = REQUEST_IMAGE_URLS.get("ホテル")
        elif "restaurant" in category_raw:
            cat = "restaurant"
            img = REQUEST_IMAGE_URLS.get("飲食店")
        elif "experience" in category_raw:
            cat = "experience"
            img = REQUEST_IMAGE_URLS.get("体験スポット")
        elif "sightseeing" in category_raw:
            cat = "sightseeing"
            img = REQUEST_IMAGE_URLS.get("観光地")
        else:
            cat = "other"
            img = REQUEST_IMAGE_URLS.get("観光地")

        results.append({
            "title": title[:40],
            "subtitle": (short or " ")[:60],
            "official": official,
            "map": map_url,
            "image": img,
            "category": cat,
        })

    return results

# ====================== ホテル：3件提案 ======================
def build_hotel3_prompt(answers: Dict[str, Any], lang: str) -> str:
    answers_json = json.dumps(answers, ensure_ascii=False, indent=2)
    is_en = str(lang).lower().startswith("e")

    if is_en:
        return f"""
You are a Kansai travel hotel concierge.
Based on the following user conditions, output **exactly 3 hotel options** in the same format.

Important:
- Do NOT invent hotel names.
- Only suggest real hotels that actually exist and can be found on Google Maps.
- If you cannot find the official site or a booking-site URL for a hotel, do not use that hotel.
- If you cannot find 3 valid hotels, for the missing ones just write:
  "No real hotel matching the conditions was found."

Each option MUST contain: "Official: URL" and "Google Maps: URL".
Do NOT output any image URLs.
Separate each option with a blank line (no lines with separators/--- etc.).

[User conditions (JSON)]
{answers_json}

[Output format (for 3 options)]
🏨 Official hotel name (nearest area)
Short description: 1-line summary (location / rooms / bath / breakfast / family-friendly etc.)
💰 Price guide: rough total for {answers.get('people','2 people')} & {answers.get('stay_plan','1 night 2 days')} OR price per person per night
🔗 Official: https://...
📍 Google Maps: https://...
""".strip()
    else:
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


def _send_hotels_three(uid: str, reply_token: str, hotels_text: str, lang: str):
    is_en = str(lang).lower().startswith("e")

    header = (
        "🏨 条件に合うホテル候補を3件ご提案します👇"
        if not is_en else "🏨 Here are 3 hotel options for you 👇"
    )

    # まずヘッダーを reply（reply_token は1回だけ）
    line_bot_api.reply_message(reply_token, TextSendMessage(text=header))

    # OpenAI出力をブロック分割して3件まで
    blocks = [b.strip() for b in re.split(r"\n\s*\n", (hotels_text or "").strip()) if b.strip()][:3]

    # 3ブロック未満でも「見つかりませんでした」を埋めて3件にする（UIを崩さない）
    while len(blocks) < 3:
        blocks.append("条件に合う実在のホテルが見つかりませんでした")

    items = []

    for i, block in enumerate(blocks):
        # 「見つかりませんでした」系
        if "見つかりません" in block or "No real hotel" in block:
            title = "条件に合うホテルが見つかりませんでした" if not is_en else "No matching hotel found"
            line_bot_api.push_message(uid, TextSendMessage(text=f"🏨 {title}"))
            items.append({
                "title": title[:40],
                "subtitle": "条件を変えて再検索してみてください。" if not is_en else "Try changing conditions.",
                "official": "",
                "map": "",
                "image": _pick_carousel_image("hotel", i, REQUEST_IMAGE_URLS.get("ホテル")),
                "spot_type": "hotel",
                "affiliate_url": "",
            })
            continue

        info = _parse_hotel_block(block)

        # 1件ずつテキストも push
        if not is_en:
            lines = [f"🏨 {info['name']}"]
            if info.get("desc"):  lines.append(info["desc"])
            if info.get("price"): lines.append(f"💰 価格目安：{info['price']}")
        else:
            lines = [f"🏨 {info['name']}"]
            if info.get("desc"):  lines.append(info["desc"])
            if info.get("price"): lines.append(f"💰 Price guide: {info['price']}")

        line_bot_api.push_message(uid, TextSendMessage(text="\n".join(lines)))

        # カルーセル用
        items.append({
            "title": (info.get("name") or "Hotel")[:40],
            "subtitle": (info.get("desc") or info.get("price") or " ")[:60],
            "official": info.get("official") or "",
            "map": info.get("map") or "",
            "image": _pick_carousel_image("hotel", i, REQUEST_IMAGE_URLS.get("ホテル")),
            "spot_type": "hotel",
            "affiliate_url": "",
        })

    # 最後にカルーセル
    list_title = "🏨 ホテル候補（3件）" if not is_en else "🏨 Hotel options (3)"
    line_bot_api.push_message(uid, _carousel_from_items(list_title, items))





# ====================== 飲食店：3件提案 ======================
def build_food3_prompt(answers: Dict[str, Any], lang: str) -> str:
    answers_json = json.dumps(answers, ensure_ascii=False, indent=2)
    is_en = str(lang).lower().startswith("e")

    near_hint_ja = ""
    near_hint_en = ""
    if answers.get("geo"):
        lat = answers["geo"].get("lat")
        lng = answers["geo"].get("lng")
        near_hint_ja = f"""
現在地の緯度経度（lat={lat}, lng={lng}）から**半径1km以内**にある飲食店のみを候補にしてください。
- 1kmを超える店舗は候補に含めないでください。
- 距離が近い順に3件までを提案してください。
- 1km以内に条件に合う店が3件見つからない場合、無理に店名を作らず、「条件に合う実在の飲食店が見つかりませんでした」と書いてください。
""".strip()
        near_hint_en = f"""
Only suggest restaurants **within 1 km radius** of the current coordinates (lat={lat}, lng={lng}).
- Do not include restaurants farther than 1 km.
- Suggest up to 3 places ordered by distance.
- If there are not 3 places within 1 km, do NOT invent names; simply say
  "No real restaurant matching the conditions was found."
""".strip()

    if is_en:
        return f"""
You are a Kansai gourmet concierge.
Based on the following conditions, output **exactly 3 restaurants** in the same format.

Important:
- Do NOT invent restaurant names.
- Only suggest real restaurants that can be found on Google Maps.
- If you cannot find the official site or a major listing (e.g. Tabelog) and Google Maps URL, do not use that restaurant.
- If you cannot find 3 valid restaurants, for the missing ones just write:
  "No real restaurant matching the conditions was found."

{near_hint_en}

[Conditions (JSON)]
{answers_json}

[Output format (3 restaurants)]
🍽 Restaurant name (nearest station / area)
Short comment: 1-line summary (signature dishes / atmosphere / seating / reservation, etc.)
💰 Price range: approx. total amount
🕰 Hours: e.g. 11:00–22:00 / Closed: Wed
🔗 Official: https://...
📍 Google Maps: https://...
""".strip()
    else:
        return f"""
あなたは関西のグルメコンシェルジュです。
以下の条件に合う飲食店を**ちょうど3件**、同一フォーマットで出力してください。

重要:
- 架空の店名を作らないこと。
- 必ず実在し、Googleマップで検索できる飲食店のみを提案してください。
- 公式サイトまたは食べログ等のページURL、GoogleマップのURLが分からない店は候補から外してください。
- 3件そろわない場合は、足りない件数分について「条件に合う実在の飲食店が見つかりませんでした」とだけ書いてください。
{near_hint_ja}

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
    mprice = PRICE_RE.search(block)
    mhours = HOURS_RE.search(block)
    moff   = OFFICIAL_URL_RE.search(block)
    mmap   = MAP_URL_RE.search(block)
    if mshort: short = mshort.group(1).strip()
    if mprice: price = mprice.group(1).strip()
    if mhours: hours = mhours.group(1).strip()
    if moff:   off   = moff.group(1)
    if mmap:   mp    = mmap.group(1)
    return {"name": name or "飲食店", "short": short, "price": price, "hours": hours, "official": off, "map": mp}


def _send_food_three(uid: str, reply_token: str, foods_text: str, lang: str):
    is_en = str(lang).lower().startswith("e")

    header = (
        "🍽 条件に合うお店を3件ご提案します👇"
        if not is_en else "🍽 Here are 3 restaurant suggestions 👇"
    )

    # まずヘッダーを reply（reply_token は1回だけ）
    line_bot_api.reply_message(reply_token, TextSendMessage(text=header))

    # OpenAI出力をブロック分割して3件まで
    blocks = [b.strip() for b in re.split(r"\n\s*\n", (foods_text or "").strip()) if b.strip()][:3]

    # 3ブロック未満でも埋めて3件にする（UI崩れ防止）
    while len(blocks) < 3:
        blocks.append("条件に合う実在の飲食店が見つかりませんでした")

    items = []

    for i, block in enumerate(blocks):
        # 「見つかりませんでした」系
        if ("見つかりません" in block) or ("No real restaurant" in block) or ("No matching" in block):
            title = "条件に合う飲食店が見つかりませんでした" if not is_en else "No matching restaurant found"
            line_bot_api.push_message(uid, TextSendMessage(text=f"🍽 {title}"))

            items.append({
                "title": title[:40],
                "subtitle": "条件を変えて再検索してみてください。" if not is_en else "Try changing conditions.",
                "official": "",
                "map": "",
                "image": _pick_carousel_image("food", i, REQUEST_IMAGE_URLS.get("飲食店")),
                "spot_type": "food",
                "affiliate_url": "",
            })
            continue

        info = _parse_food_block(block)  # 既にあなたのコードにある想定

        # 1件ずつテキストも push
        if not is_en:
            lines = [f"🍽 {info.get('name','飲食店')}"]
            if info.get("short"): lines.append(info["short"])
            if info.get("price"): lines.append(f"💰 価格帯：{info['price']}")
            if info.get("hours"): lines.append(f"🕰 営業：{info['hours']}")
        else:
            lines = [f"🍽 {info.get('name','Restaurant')}"]
            if info.get("short"): lines.append(info["short"])
            if info.get("price"): lines.append(f"💰 Price range: {info['price']}")
            if info.get("hours"): lines.append(f"🕰 Hours: {info['hours']}")

        line_bot_api.push_message(uid, TextSendMessage(text="\n".join(lines)))

        # カルーセル用
        subtitle = (info.get("short") or info.get("hours") or info.get("price") or " ")[:60]

        items.append({
            "title": (info.get("name") or "Restaurant")[:40],
            "subtitle": subtitle,
            "official": info.get("official") or "",
            "map": info.get("map") or "",
            "image": _pick_carousel_image("food", i, REQUEST_IMAGE_URLS.get("飲食店")),
            "spot_type": "food",
            "affiliate_url": "",
        })

    # 最後にカルーセル
    list_title = "🍽 お店候補（3件）" if not is_en else "🍽 Restaurant options (3)"
    line_bot_api.push_message(uid, _carousel_from_items(list_title, items))




# ====================== 体験スポット：3件提案 ======================
def build_experience3_prompt(answers: Dict[str, Any], lang: str) -> str:
    answers_json = json.dumps(answers, ensure_ascii=False, indent=2)
    is_en = str(lang).lower().startswith("e")

    near_hint_ja = ""
    near_hint_en = ""
    geo = answers.get("geo")
    if geo:
        lat = geo.get("lat")
        lng = geo.get("lng")
        near_hint_ja = f"""
現在地の緯度経度（lat={lat}, lng={lng}）から**半径2km以内**にある体験スポットを優先して候補にしてください。
- 2kmを大きく超える施設は、よほど条件に合う場合を除き避けてください。
- 距離が近い順 or 現実的に移動しやすい順に最大3件までを提案してください。
- 条件に合う実在の施設が見つからない場合は、無理に名前を作らず、
  「条件に合う実在の体験スポットが見つかりませんでした」と書いてください。
""".strip()

        near_hint_en = f"""
Prioritize experience spots **within about 2 km** of the current coordinates (lat={lat}, lng={lng}).
- Avoid facilities far beyond 2 km unless they are exceptionally suitable.
- Suggest up to 3 places ordered by distance / realistic travel order.
- If no real facilities match the conditions, do NOT invent names; say
  "No real experience spot matching the conditions was found."
""".strip()

    if is_en:
        return f"""
You are a Kansai travel experience concierge.
Based on the following conditions, suggest **exactly 3 experience spots / activities**
(e.g. pottery class, kimono dressing, wagashi making, rafting, hot spring day-use)
in the same format.

Important:
- Do NOT include sightseeing landmarks such as temples, castles, towers, or observatories
  (those are handled in a separate category).
- Do NOT invent facility names.
- Only suggest real facilities that actually exist and can be found on Google Maps.
- If you cannot find the official site / booking page URL and a Google Maps URL,
  do not use that facility as a candidate.
- If you cannot find 3 valid experience spots, for the missing ones just write:
  "No real experience spot matching the conditions was found."

{near_hint_en}

Each option MUST contain: "Official: URL" and "Google Maps: URL".
Do NOT output any image URLs.
Separate each option with a blank line (no separator lines like ---).

[Conditions (JSON)]
{answers_json}

[Output format (for 3 options)]
🎯 Facility name (area / nearest station)
Short comment: 1-line summary (contents / for whom / any cautions)
💰 Price: 〜 JPY per person
⌛ Duration: 〜 minutes / Reservation: required or not required
🕰 Hours: e.g. 10:00–18:00 / Closed: Tue
🔗 Official: https://...
📍 Google Maps: https://...
""".strip()
    else:
        return f"""
あなたは関西観光の体験コンシェルジュです。
以下の条件に合う**体験スポット/アクティビティ（陶芸体験、着物体験、和菓子作り、ラフティング、温泉入浴など）をちょうど3件**、同一フォーマットで出力してください。

重要:
- 寺社・城・展望台・名所などの「観光地（ランドマーク）」は含めないでください（別カテゴリ）。
- 架空の施設名を作らないこと。
- 必ず実在し、Googleマップで検索できる体験施設のみを提案してください。
- 公式サイトURLや予約ページURL、GoogleマップURLがない施設は候補から外してください。
- 3件そろわない場合は、足りない分について「条件に合う実在の体験スポットが見つかりませんでした」とだけ書いてください。

{near_hint_ja}

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
    mprice = PRICE_RE.search(block)
    mhours = HOURS_RE.search(block)
    mdura = DURA_RE.search(block)
    moff   = OFFICIAL_URL_RE.search(block)
    mmap   = MAP_URL_RE.search(block)
    if mshort: short = mshort.group(1).strip()
    if mprice: price = mprice.group(1).strip()
    if mhours: hours = mhours.group(1).strip()
    if mdura:  dura  = mdura.group(1).strip()
    if moff:   off   = moff.group(1)
    if mmap:   mp    = mmap.group(1)
    return {
        "name": name or "体験スポット",
        "short": short,
        "price": price,
        "hours": hours,
        "dura": dura,
        "official": off,
        "map": mp
    }


def _send_experiences_three(uid: str, reply_token: str, exp_text: str, lang: str):
    is_en = str(lang).lower().startswith("e")

    header = (
        "🎯 条件に合う体験スポットを3件ご提案します👇"
        if not is_en else "🎯 Here are 3 experience spots 👇"
    )

    # まずヘッダーを reply（reply_token は1回だけ）
    line_bot_api.reply_message(reply_token, TextSendMessage(text=header))

    # OpenAI出力をブロック分割して3件まで
    blocks = [b.strip() for b in re.split(r"\n\s*\n", (exp_text or "").strip()) if b.strip()][:3]

    # 3ブロック未満でも埋めて3件にする（UI崩れ防止）
    while len(blocks) < 3:
        blocks.append("条件に合う実在の体験スポットが見つかりませんでした")

    items = []

    for i, block in enumerate(blocks):
        # 「見つかりませんでした」系
        if ("見つかりません" in block) or ("No real experience" in block) or ("No matching" in block):
            title = "条件に合う体験スポットが見つかりませんでした" if not is_en else "No matching experience found"
            line_bot_api.push_message(uid, TextSendMessage(text=f"🎯 {title}"))

            items.append({
                "title": title[:40],
                "subtitle": "条件を変えて再検索してみてください。" if not is_en else "Try changing conditions.",
                "official": "",
                "map": "",
                "image": _pick_carousel_image("experience", i, REQUEST_IMAGE_URLS.get("体験スポット")),
                "spot_type": "experience",
                "affiliate_url": "",
            })
            continue

        info = _parse_experience_block(block)  # 既にあなたのコードにある想定

        # 1件ずつテキストも push
        if not is_en:
            lines = [f"🎯 {info.get('name','体験スポット')}"]
            if info.get("short"): lines.append(info["short"])
            if info.get("price"): lines.append(f"💰 料金：{info['price']}")
            if info.get("dura"):  lines.append(f"⌛ 所要：{info['dura']}")
            if info.get("hours"): lines.append(f"🕰 営業：{info['hours']}")
        else:
            lines = [f"🎯 {info.get('name','Experience')}"]
            if info.get("short"): lines.append(info["short"])
            if info.get("price"): lines.append(f"💰 Price: {info['price']}")
            if info.get("dura"):  lines.append(f"⌛ Duration: {info['dura']}")
            if info.get("hours"): lines.append(f"🕰 Hours: {info['hours']}")

        line_bot_api.push_message(uid, TextSendMessage(text="\n".join(lines)))

        # カルーセル用サブタイトル
        subtitle = (info.get("short") or info.get("dura") or info.get("hours") or info.get("price") or " ")[:60]

        items.append({
            "title": (info.get("name") or "Experience")[:40],
            "subtitle": subtitle,
            "official": info.get("official") or "",
            "map": info.get("map") or "",
            "image": _pick_carousel_image("experience", i, REQUEST_IMAGE_URLS.get("体験スポット")),
            "spot_type": "experience",
            "affiliate_url": "",
        })

    # 最後にカルーセル
    list_title = "🎯 体験スポット（3件）" if not is_en else "🎯 Experiences (3)"
    line_bot_api.push_message(uid, _carousel_from_items(list_title, items))



# ====================== 観光地：3件提案 ======================
def build_sightseeing3_prompt(answers: Dict[str, Any], lang: str) -> str:
    answers_json = json.dumps(answers, ensure_ascii=False, indent=2)
    is_en = str(lang).lower().startswith("e")

    if is_en:
        return f"""
You are a Kansai sightseeing concierge.
Based on the following conditions, suggest **exactly 3 sightseeing spots**
(temples, shrines, castles, gardens, scenic spots, museums, observatories, etc.)
in the same format.

Important:
- Do NOT include hands-on activity facilities (pottery class, workshops etc.).
  Those are handled as experience spots.
- Do NOT invent place names.
- Only suggest real places that actually exist and can be found on Google Maps.
- If you cannot find the official site or a reliable information page AND Google Maps URL,
  do not use that place as a candidate.
- If you cannot find 3 valid sightseeing spots, for the missing ones just write:
  "No real sightseeing spot matching the conditions was found."

Each option MUST contain: "Official: URL" and "Google Maps: URL".
Do NOT output any image URLs.
Separate each option with a blank line (no separator lines like ---).

[Conditions (JSON)]
{answers_json}

[Output format (for 3 spots)]
🏯 Spot name (area / nearest station)
Short comment: 1-line summary (highlights / history / typical required time)
💰 Admission: 〜 JPY (entrance fee etc.) *write "free" if it is free
🕰 Hours: e.g. 9:00–17:00 / Closed: none
🔗 Official: https://...
📍 Google Maps: https://...
""".strip()
    else:
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
    mprice = PRICE_RE.search(block)
    mhours = HOURS_RE.search(block)
    moff   = OFFICIAL_URL_RE.search(block)
    mmap   = MAP_URL_RE.search(block)
    if mshort: short = mshort.group(1).strip()
    if mprice: price = mprice.group(1).strip()
    if mhours: hours = mhours.group(1).strip()
    if moff:   off   = moff.group(1)
    if mmap:   mp    = mmap.group(1)
    return {
        "name": name or "観光地",
        "short": short,
        "price": price,
        "hours": hours,
        "official": off,
        "map": mp
    }


def _send_sightseeing_three(uid: str, reply_token: str, text: str, lang: str):
    is_en = str(lang).lower().startswith("e")

    blocks = [b.strip() for b in re.split(r"\n\s*\n", text.strip()) if b.strip()][:3]
    if not blocks:
        msg = "条件に合う観光地が見つかりませんでした。" if not is_en else "No matching sightseeing spots were found."
        line_bot_api.reply_message(reply_token, TextSendMessage(text=msg))
        return

    header = "🏯 条件に合う観光地を3件ご提案します👇" if not is_en else "🏯 Here are 3 sightseeing spots 👇"
    line_bot_api.reply_message(reply_token, TextSendMessage(text=header))

    items = []

    for block in blocks:
        info = _parse_sightseeing_block(block)

        # ---- テキストメッセージ ----
        if not is_en:
            lines = [f"🏯 {info['name']}"]
            if info["short"]:
                lines.append(info["short"])
            if info["price"]:
                lines.append(f"💰 料金目安：{info['price']}")
            if info["hours"]:
                lines.append(f"🕰 営業：{info['hours']}")
        else:
            lines = [f"🏯 {info['name']}"]
            if info["short"]:
                lines.append(info["short"])
            if info["price"]:
                lines.append(f"💰 Admission: {info['price']}")
            if info["hours"]:
                lines.append(f"🕰 Hours: {info['hours']}")

        line_bot_api.push_message(uid, TextSendMessage(text="\n".join(lines)))

        # ---- サブタイトル候補 ----
        sub = (
            info.get("short")
            or (f"Hours: {info.get('hours','')}" if info.get("hours") else info.get("price"))
            or " "
        )

        # ---- カルーセル用データ ----
        items.append({
            "title": info["name"],
            "subtitle": sub[:60],
            "official": info.get("official") or "",
            "map": info.get("map") or "",
            "image": REQUEST_IMAGE_URLS.get("観光地"),
            "spot_type": "sightseeing",  # ★ 観光地
            "affiliate_url": "",         # ★ あっても _carousel_from_items 側で予約ボタンを出さない
        })

    list_title = "🏯 観光地（3件）" if not is_en else "🏯 Sightseeing spots (3)"
    if items:
        line_bot_api.push_message(uid, _carousel_from_items(list_title, items))
def _send_sightseeing_three_from_master(uid: str, reply_token: str, spots: List[Dict[str, Any]], lang: str):
    is_en = str(lang).lower().startswith("e")

    header_text = "🏯 条件に合う観光地を3件ご提案します👇" if not is_en else "🏯 Here are 3 sightseeing spots 👇"
    messages = [TextSendMessage(text=header_text)]

    def _get_spot_image(sp):
        imgs = sp.get("images") or []
        if isinstance(imgs, list) and imgs:
            return imgs[0]
        if isinstance(imgs, str) and imgs.strip():
            return imgs.strip()
        return REQUEST_IMAGE_URLS.get("観光地")

    items_for_carousel = []

    for sp in spots[:3]:
        if not is_en:
            title = sp.get("name","")
            subtitle = (sp.get("description","") or "")[:60] or " "
        else:
            title = sp.get("name_en") or sp.get("name","")
            subtitle = (sp.get("description_en") or sp.get("description","") or "")[:60] or " "

        # テキスト（任意：欲しければ）
        messages.append(TextSendMessage(text=f"🏯 {title}\n{subtitle}"))

        items_for_carousel.append({
            "title": title[:40],
            "subtitle": subtitle[:60],
            "official": sp.get("official_url",""),
            "map": sp.get("map_url",""),
            "image": _get_spot_image(sp),
            "spot_type": "sightseeing",
            "affiliate_url": "",
        })

    carousel_title = "🏯 観光地（3件）" if not is_en else "🏯 Sightseeing (3)"
    messages.append(_carousel_from_items(carousel_title, items_for_carousel))

    # replyは最大5件なので、収める
    line_bot_api.reply_message(reply_token, messages[:5])

    


# ====================== 日程表 生成＆送信 ======================
DAY_HEAD_RE   = re.compile(r"^Day\s*\d+", re.M | re.I)
BLOCK_SPLIT_RE= re.compile(r"\n\s*↓\s*\n", re.M)

def build_itinerary_prompt(answers: Dict[str, Any], lang: str) -> str:
    answers_json = json.dumps(answers, ensure_ascii=False, indent=2)
    prefs = "、".join(answers.get("prefs", [])) if isinstance(answers.get("prefs"), list) else answers.get("prefs","")
    themes = "、".join(answers.get("themes", [])) if isinstance(answers.get("themes"), list) else answers.get("themes","")
    is_en = str(lang).lower().startswith("e")

    if is_en:
        return f"""
You are a Kansai travel planner. Create a **dense, detailed itinerary** based on
the following conditions.

Requirements:
- Each day must include **3–5 or more spots**, in a realistic order considering travel time.
- Each spot must have a **1-line short comment**.
- For every spot, ALWAYS output both an "Official: URL" and "Google Maps: URL".
- Do NOT output image URLs.
- The last day must NOT include another overnight block.
- Output only the itinerary (no extra explanation before or after).

Important:
- Every spot must be an actual, real facility or area.
- Do not invent any names.
- Avoid spots where you cannot find an official / reliable page AND a Google Maps URL
  (you may omit such spots instead of forcing them).

[Conditions (JSON)]
{answers_json}

[Summary]
- Area(s): {prefs}
- Departure date: {answers.get('date','-')}
- Trip length: {answers.get('stay','-')}
- Themes: {themes}
- Main transportation: {answers.get('transport','-')}
- Companion(s): {answers.get('companion','-')}
- Departure time band: {answers.get('dept','-')} / Return time band: {answers.get('arrv','-')}

[Output example format]
Day1
🕘 09:00–10:00  🏯 Sightseeing: Spot name (area)
Short comment: 1-line highlight
💰 Price: 〜 JPY
🔗 Official: https://...
📍 Google Maps: https://...
🕰 Hours: open-close / Closed: days
↓
(3–5 or more spots in total for this day)
Day2
(continue in the same style)
""".strip()
    else:
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

def _send_itinerary(uid: str, reply_token: str, schedule_text: str, lang: str):
    is_en = str(lang).lower().startswith("e")

    parts = []
    positions = [(m.group(0).strip(), m.start()) for m in DAY_HEAD_RE.finditer(schedule_text)]
    for i, (title, start) in enumerate(positions):
        end = positions[i+1][1] if i+1 < len(positions) else len(schedule_text)
        parts.append((title, schedule_text[start:end]))

    if not parts:
        msg = (
            "日程表の生成に失敗しました。条件を変えて再試行してください。"
            if not is_en else
            "Failed to generate the itinerary. Please change the conditions and try again."
        )
        line_bot_api.reply_message(reply_token, TextSendMessage(text=msg))
        return

    for day_title, day_body in parts:
        head = f"📅 {day_title}" if not is_en else f"📅 {day_title}"
        line_bot_api.push_message(uid, TextSendMessage(text=head))

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

        list_title = f"{day_title} の予定" if not is_en else f"{day_title} schedule"
        for trio in _chunk(items, 3):
            line_bot_api.push_message(uid, _flex_list_bubble(list_title, trio))


# ====================== “他のプランを提案” メニュー ======================
def _send_finish_menu(uid: str, lang: str):
    is_en = str(lang).lower().startswith("e")

    def _img_btn(label_display: str, text_send: str, url: str) -> dict:
        return {
            "type": "box",
            "layout": "vertical",
            "flex": 1,
            "cornerRadius": "16px",
            "backgroundColor": "#FFFFFF",
            "height": "160px",
            "action": {"type": "message", "label": label_display, "text": text_send},
            "contents": [
                {
                    "type": "image",
                    "url": url,
                    "size": "full",
                    "aspectRatio": "16:9",
                    "aspectMode": "fit"
                },
                {
                    "type": "box",
                    "layout": "vertical",
                    "paddingAll": "4px",
                    "contents": [
                        {
                            "type": "text",
                            "text": label_display,
                            "weight": "bold",
                            "size": "14px",
                            "align": "center",
                            "color": "#111111"
                        }
                    ]
                }
            ]
        }

    def _txt_btn(label_display: str, text_send: str) -> dict:
        return {
            "type": "box",
            "layout": "vertical",
            "flex": 1,
            "cornerRadius": "16px",
            "backgroundColor": "#EEF2F7",
            "height": "120px",
            "justifyContent": "center",
            "action": {"type": "message", "label": label_display, "text": text_send},
            "contents": [
                {
                    "type": "text",
                    "text": label_display,
                    "weight": "bold",
                    "size": "16px",
                    "align": "center",
                    "color": "#111111",
                    "wrap": True
                }
            ]
        }

    def t_req(v: str) -> str:
        if is_en:
            return REQUEST_LABELS_EN.get(v, v)
        return v

    title_text = "他のプランを提案" if not is_en else "See other suggestions"

    row1 = {
        "type": "box",
        "layout": "horizontal",
        "spacing": "12px",
        "contents": [
            _img_btn(t_req("ホテル"), "ホテル",
                     "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/%E3%81%BB%E3%81%A6%E3%82%8B.png"),
            _txt_btn(t_req("日程表"), "日程表"),
        ]
    }

    row2 = {
        "type": "box",
        "layout": "horizontal",
        "spacing": "12px",
        "contents": [
            _img_btn(t_req("飲食店"), "飲食店",
                     "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/%E9%A3%B2%E9%A3%9F%E5%BA%97.png"),
            _img_btn(t_req("体験スポット"), "体験スポット",
                     "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/%E4%BD%93%E9%A8%93.png"),
        ]
    }

    start_label = "最初から" if not is_en else "Back to start"

    row3 = {
        "type": "box",
        "layout": "horizontal",
        "spacing": "12px",
        "contents": [
            _img_btn(t_req("観光地"), "観光地",
                     "https://raw.githubusercontent.com/0712nagai-design/au-kansai-tabi/main/images/kannku.png"),
            _txt_btn(start_label, "最初から"),
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
                {"type": "text", "text": title_text, "size": "24px", "weight": "bold"},
                {"type": "separator"},
                {"type": "box", "layout": "vertical", "spacing": "12px", "contents": [row1, row2, row3]},
            ]
        }
    }

    line_bot_api.push_message(uid, FlexSendMessage(alt_text=title_text, contents=bubble))


def send_plan_parts(reply_token: str, uid: str, answers: Dict[str, Any]):
    """
    ✅ send_plan_parts() 最新版（人数質問を外しても落ちない）
    - ホテル：hotel_master.json から検索（※現在地は使わない）
    - 飲食店：food_master.json から検索（現在地 or 県×ジャンル）
    - 体験：experience_master.json から検索（現在地 or 県×ジャンル）
      ★体験は reply 5件に収めて「説明もカルーセルも確実に出る」方式
    - 観光地：sightseeing_master から県 or 現在地近傍
    - 日程表：OpenAI生成（people が無くても動く）
    """

    # --- 言語を取得＆記録（日本語固定運用でも壊れない） ---
    lang = answers.get("lang", LAST_LANG.get(uid, "日本語"))
    LAST_LANG[uid] = lang
    is_en = str(lang).lower().startswith("e")

    req = answers.get("request")

    # =========================
    # ユーティリティ（必須条件チェック）
    # =========================
    def _is_food_complete_local(sp: Dict[str, Any]) -> bool:
        g = sp.get("geo") or {}
        must = [
            g.get("lat"), g.get("lng"),
            sp.get("official_url"),
            sp.get("open_hours"),
            sp.get("price"),
        ]
        return all(x is not None and str(x).strip() != "" for x in must)

    def _is_hotel_complete_local(sp: Dict[str, Any]) -> bool:
        g = sp.get("geo") or {}
        must = [
            sp.get("official_url"),
            sp.get("price"),
            sp.get("price_num"),
            # hotel_type は tags 推論だけでも動く可能性あるが、表示のため基本必須扱い
            sp.get("hotel_type") or _hotel_type_from_tags(sp.get("tags")),
            g.get("lat"), g.get("lng"),
        ]
        return all(x is not None and str(x).strip() != "" for x in must)

    def _is_exp_complete_local(sp: Dict[str, Any]) -> bool:
        g = sp.get("geo") or {}
        # 体験は最低限 geo があれば出す（URLはあれば嬉しいが足切りすると候補が減りすぎる）
        must = [g.get("lat"), g.get("lng")]
        return all(x is not None and str(x).strip() != "" for x in must)

    def _safe_reply_text(msg_ja: str, msg_en: str):
        line_bot_api.reply_message(
            reply_token,
            TextSendMessage(text=(msg_en if is_en else msg_ja))
        )

    # =========================
    # ① ホテル（マスターデータ駆動）※現在地は使わない
    # =========================
    if req in {"ホテル", "Hotels"}:
        pref = answers.get("pref", "")          # "大阪" / "京都" / "奈良" ...
        hotel_label = answers.get("hotel", "")  # "高級" / "中価格" / "コスパ" / "和風旅館" / "こだわらない"

        spots = _search_hotel_master(pref=pref, hotel_label=hotel_label, limit=50)
        # hotel_type 欄が無いデータでも tags 推論で補う
        fixed = []
        for sp in spots:
            if not isinstance(sp, dict):
                continue
            if not sp.get("hotel_type"):
                sp = dict(sp)
                sp["hotel_type"] = _hotel_type_from_tags(sp.get("tags"))
            if _is_hotel_complete_local(sp):
                fixed.append(sp)

        picked = fixed[:3]
        if not picked:
            _safe_reply_text(
                "条件に合うホテル（マスター）が見つかりませんでした。",
                "No matching hotels found in master."
            )
            return

        # reply は1回だけ（ヘッダー）
        _safe_reply_text("🏨 ホテル候補を送ります👇", "🏨 Sending hotel options 👇")
        # 中身は push
        _push_hotels_three_from_master(uid, picked, lang=lang)
        return

    # =========================
    # ② 飲食店（マスターデータ駆動）
    # =========================
    if req in {"飲食店", "Restaurants"}:
        area = answers.get("area", "")      # "現在地から近く" or "京都"など
        cuisine = answers.get("cuisine", "")
        geo = answers.get("geo")

        spots: List[Dict[str, Any]] = []

        # 2-1) 現在地から近く（3km）
        if area == "現在地から近く":
            if not geo:
                _safe_reply_text(
                    "現在地の近くで探すには、先に位置情報を送ってください。",
                    "To search near you, please send your location first."
                )
                return
            tmp = _get_near_food_from_master(geo, max_km=3.0, limit=80)
            spots = [sp for sp in tmp if _is_food_complete_local(sp)][:3]

        # 2-2) 県×ジャンル
        else:
            pref = area
            tmp = _search_food_master(pref=pref, area="", cuisine=cuisine, limit=80)
            spots = [sp for sp in tmp if _is_food_complete_local(sp)][:3]

        if not spots:
            _safe_reply_text(
                "条件に合う飲食店（マスター）が見つかりませんでした。",
                "No matching restaurants found in master."
            )
            return

        _safe_reply_text("🍽 飲食店候補を送ります👇", "🍽 Sending restaurant options 👇")
        _push_foods_three_from_master(uid, spots, lang=lang)
        return

    # =========================
    # ③ 体験スポット（マスターデータ駆動）
    # =========================
    if req in {"体験スポット", "Experiences"}:
        pref = answers.get("pref", "")
        genre = answers.get("exp_genre", "")
        geo = answers.get("geo")

        if pref == "現在地から近く":
            if not geo:
                _safe_reply_text(
                    "現在地の近くで探すには、先に位置情報を送ってください。",
                    "To search near you, please send your location first."
                )
                return
            candidates = _get_near_experience_from_master(geo, max_km=10.0, limit=60)
        else:
            candidates = _search_experience_master(pref=pref, exp_genre=genre, limit=60)

        # 体験は geo 必須で足切り
        spots = [sp for sp in candidates if _is_exp_complete_local(sp)][:3]

        if not spots:
            _safe_reply_text(
                "条件に合う体験スポット（マスター）が見つかりませんでした。",
                "No matching experiences found in master."
            )
            return

        # ★体験は reply 5件で完結（説明3 + カルーセル）
        _reply_experiences_three_from_master(reply_token, spots, lang=lang)
        return

    # =========================
    # ④ 観光地（マスターデータ）
    # =========================
    if req in {"観光地", "Sightseeing"}:
        pref_answer = answers.get("pref", "")
        geo = answers.get("geo")

        def _norm_pref(s: str) -> str:
            if not isinstance(s, str):
                return ""
            s = s.strip()
            for suf in ("府", "県"):
                if s.endswith(suf):
                    s = s[:-1]
            return s

        # 4-1) 現在地から近く → 近傍
        if pref_answer == "現在地から近く":
            if not geo:
                _safe_reply_text(
                    "現在地の近くで探すには、先に位置情報を送ってください。",
                    "To search near you, please send your location first."
                )
                return

            spots = _get_near_sightseeing_from_master(geo, max_km=15.0, limit=3)
            if not spots:
                _safe_reply_text(
                    "現在地付近（15km以内）に登録済みの観光地が見つかりませんでした。",
                    "No registered sightseeing spots were found within 15km."
                )
                return

            _send_sightseeing_three_from_master(uid, reply_token, spots, lang=lang)
            return

        # 4-2) 県指定 → 県で抽出して3件
        import random
        candidates: List[Dict[str, Any]] = []
        pref_norm = _norm_pref(pref_answer)

        for sp in SIGHTSEEING_MASTER.values():
            if not isinstance(sp, dict):
                continue
            if _norm_pref(sp.get("pref", "")) != pref_norm:
                continue
            candidates.append(sp)

        if not candidates:
            _safe_reply_text(
                f"{pref_answer}の観光地マスターデータがまだ登録されていません。",
                f"No sightseeing master data registered for {pref_answer} yet."
            )
            return

        random.shuffle(candidates)
        picked = candidates[:3]

        header_text = "🏯 観光地を3件ご提案します👇" if not is_en else "🏯 Here are 3 sightseeing spots 👇"

        def _get_spot_image(spot: Dict[str, Any]) -> str:
            imgs = spot.get("images") or []
            if isinstance(imgs, list) and imgs:
                return imgs[0]
            if isinstance(imgs, str) and imgs.strip():
                return imgs.strip()
            return REQUEST_IMAGE_URLS.get("観光地")

        items_for_carousel = []
        for sp in picked:
            title = sp.get("name", "") or ("Spot" if is_en else "観光地")
            subtitle = (sp.get("description", "") or "")[:60] or " "
            items_for_carousel.append({
                "title": title[:40],
                "subtitle": subtitle[:60],
                "official": sp.get("official_url", ""),
                "map": sp.get("map_url", ""),
                "image": _get_spot_image(sp),
                "spot_type": "sightseeing",
                "affiliate_url": "",
            })

        carousel_title = "🏯 観光地（マスターデータ）" if not is_en else "🏯 Sightseeing (from master)"
        line_bot_api.reply_message(reply_token, [
            TextSendMessage(text=header_text),
            _carousel_from_items(carousel_title, items_for_carousel),
        ])
        return

    # =========================
    # ⑤ 日程表（OpenAI）
    # =========================
    if req in {"日程表", "Itinerary"}:
        # people が無い運用でも、プロンプト側が参照してても .get なので落ちない想定
        try:
            schedule = _call_openai_text(build_itinerary_prompt(answers, lang), lang)
            _send_itinerary(uid, reply_token, schedule, lang)
        except Exception:
            _safe_reply_text(
                "日程表の生成に失敗しました。条件を変えて再試行してください。",
                "Failed to generate itinerary. Please change conditions and try again."
            )
        return

    # =========================
    # 想定外
    # =========================
    _safe_reply_text("未対応のリクエストです。", "This request is not supported yet.")






# ====================== 位置情報（飲食店の現在地用） ======================
def _ask_location(reply_token: str, lang: str):
    is_en = str(lang).lower().startswith("e")
    if not is_en:
        text = "📍 現在地の近くで探します。『位置情報を送信』を押して、現在地を共有してください。"
        label = "位置情報を送る"
    else:
        text = "📍 I'll search near your current location. Tap 'Send location' to share it."
        label = "Send location"

    msg = TextSendMessage(
        text=text,
        quick_reply=QuickReply(items=[
            QuickReplyButton(action=LocationAction(label=label))
        ])
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
@handler.add(FollowEvent)
def send_welcome_message(event):

    greeting = TextSendMessage(
        text=(
            "ようこそ！関西旅プランAIへお越しやす✨\n"
            "これからあんさんの旅、しっかりサポートしていきますえ。\n"
            "おすすめスポットやお店の“お得なクーポン”も、見つかったらその都度お知らせしますわ〜！\n"
            "ほなまず、どんなジャンルで旅したいか教えてな。"
        )
    )

    initial_state = {
        "answers": {},
        "step": 0,
        "hist": deque(maxlen=MAX_TURNS),
        "multi_temp": {}
    }

    question = _render_question(0, initial_state)

    line_bot_api.reply_message(
        event.reply_token,
        [greeting, question]
    )


# ====================== メインハンドラ ======================
@handler.add(MessageEvent, message=TextMessage)
def on_message(event: MessageEvent):
    uid  = event.source.user_id
    text = (event.message.text or "").strip()

    # =====================================================
    # 🔁 リスタート（最優先）
    # =====================================================
    if text in RESTART or text.lower() in RESTART:
        users[uid] = {
            "step": 0,
            "answers": {},
            "hist": deque(maxlen=MAX_TURNS),
            "multi_temp": {},
            "mode": "wizard",
        }
        line_bot_api.reply_message(event.reply_token, _render_question(0, users[uid]))
        return

    # =====================================================
    # 🤖 AI観光モード起動（キーワード）
    # =====================================================
    if text in {"AI観光モード", "AI観光", "AIプラン"}:
        lang = LAST_LANG.get(uid, "日本語")

        # セッション初期化（AI観光モード用）
        users[uid] = {
            "mode": "ai_travel",
            "ai_stage": "waiting_query",
            "lang": lang,
            "step": 0,
            "answers": {},
            "hist": deque(maxlen=MAX_TURNS),
            "multi_temp": {},
            "geo": None,   # 位置情報をここに保存
        }

        # 説明メッセージ + 現在地送信用 QuickReply
        if lang == "日本語":
            m1_text = (
                "🧠 AI観光モードを開始します！\n\n"
                "エリア・目的・雰囲気などを自由に入力してください。\n"
                "ホテル / 飲食店 / 体験 / 観光地を横断検索してご提案します。"
            )
            m2_text = (
                "まず現在地を送ると、その周辺スポットを優先して探します。\n"
                "そのあとで「京都で夜景がきれいなデート向き」など、行きたいイメージを送ってください👇"
            )
            loc_label = "現在地を送る"
        else:
            m1_text = (
                "🧠 Starting AI Travel Mode!\n\n"
                "Tell me any conditions you like.\n"
                "I will suggest hotels, restaurants, experiences and sightseeing spots."
            )
            m2_text = (
                "If you send your current location first, I'll prioritize places around you.\n"
                "Then send a request like 'Romantic night-view spots in Kyoto'."
            )
            loc_label = "Send location"

        m1 = TextSendMessage(text=m1_text)
        m2 = TextSendMessage(
            text=m2_text,
            quick_reply=QuickReply(
                items=[
                    QuickReplyButton(
                        action=LocationAction(label=loc_label)
                    )
                ]
            )
        )

        # 説明 + 位置情報ボタンをまとめて返信
        line_bot_api.reply_message(event.reply_token, [m1, m2])
        return

    # =====================================================
    # 🟦 ショートカットボタン（ホテル / 観光地 / 飲食店 / 体験 / 日程表）
    # =====================================================
    if text in {"ホテル", "日程表", "飲食店", "体験スポット", "観光地"}:
        lang = LAST_LANG.get(uid, "日本語")

        users[uid] = {
            "step": 1,   # 「何を提案しますか？」をスキップして2問目から
            "answers": {"lang": lang, "request": text},
            "hist": deque(maxlen=MAX_TURNS),
            "multi_temp": {},
            "mode": "wizard",
        }

        # 2問目（エリアなど）から開始
        line_bot_api.reply_message(
            event.reply_token,
            _render_question(1, users[uid])
        )
        return

    # =====================================================
    # 🟦 セッション未作成なら通常フロー(step=0)開始
    # =====================================================
    if uid not in users or not users[uid]:
        users[uid] = {
            "step": 0,
            "answers": {},
            "hist": deque(maxlen=MAX_TURNS),
            "multi_temp": {},
            "mode": "wizard",
        }
        line_bot_api.reply_message(event.reply_token, _render_question(0, users[uid]))
        return

    # ここから既存セッション前提
    state = users[uid]

    # =====================================================
    # 🤖 AI観光モード（フリーテキスト → カテゴリ別カルーセル）
    # =====================================================
    if state.get("mode") == "ai_travel":
        lang = state.get("lang", "日本語")
        user_query = text

        try:
            # 現在地（あれば）をプロンプトに渡す
            geo = state.get("geo")

            prompt = build_ai_kanko_prompt(user_query, lang, geo=geo)
            ai_text = _call_openai_text(prompt, lang)

            items = parse_ai_kanko_result(ai_text)

            if not items:
                msg = (
                    "条件に合いそうなスポットが見つかりませんでした。"
                    if lang == "日本語"
                    else "No matching spots found."
                )
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
                users.pop(uid, None)
                return

            from collections import defaultdict
            by_cat = defaultdict(list)
            for it in items:
                # official / map がどちらも無いものはカルーセルで扱いにくいのでスキップ
                if not it.get("official") and not it.get("map"):
                    continue
                by_cat[it["category"]].append(it)

            order = [
                ("hotel",       "ホテル",      "Hotels",       "🏨"),
                ("restaurant",  "飲食店",      "Restaurants",  "🍽"),
                ("experience",  "体験スポット", "Experiences",  "🎯"),
                ("sightseeing", "観光地",      "Sightseeing",  "🏯"),
                ("other",       "その他",      "Other",        "📍"),
            ]

            messages = []

            for key, label_ja, label_en, icon in order:
                spots = by_cat.get(key)
                if not spots:
                    continue
                spots = spots[:3]   # カテゴリごとに最大3件

                if lang == "日本語":
                    header = f"{icon} {label_ja}（{len(spots)}件）"
                    alt    = f"{label_ja}候補"
                else:
                    header = f"{icon} {label_en} ({len(spots)} spots)"
                    alt    = header

                messages.append(TextSendMessage(text=header))
                messages.append(_carousel_from_items(alt, spots))

            if not messages:
                msg = (
                    "URL付きで提案できるスポットが見つかりませんでした。"
                    if lang == "日本語"
                    else "No spots with valid URLs were found."
                )
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
                users.pop(uid, None)
                return

            # reply 最大5件 → それ以降は push
            first = messages[:5]
            rest  = messages[5:]

            line_bot_api.reply_message(event.reply_token, first)
            if rest:
                _push_messages_in_chunks(uid, rest, size=5)

            users.pop(uid, None)
            return

        except Exception:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="AI観光モードでエラーが発生しました。")
            )
            users.pop(uid, None)
            return

    # =====================================================
    # 🧩 通常の質問フロー（wizard モード）
    # =====================================================
    step = state.get("step", 0)

    ok = _validate_and_store(uid, step, text)
    if not ok:
        # 入力不正 → 同じ質問を再表示
        line_bot_api.reply_message(event.reply_token, _render_question(step, state))
        return

    # ★ 現在地フロー（飲食店 × 「現在地から近く」）
    # _validate_and_store 内で、該当ケースなら state["need_location"] = True になっている前提。
    if state.get("need_location"):
        # 言語を取得（未設定なら最後に使った言語 or 日本語）
        lang = state.get("answers", {}).get("lang", LAST_LANG.get(uid, "日本語"))
        # 位置情報送信を促す QuickReply（共通ヘルパー）
        _ask_location(event.reply_token, lang)
        # LocationMessage が来るまで step は進めない
        return

    seq = _get_question_sequence(state["answers"])

    # multi 質問で「完了」待ち
    qnow = seq[step]
    if qnow.get("multi") and text not in {"完了", "Done"} and not state.pop("_autodone", False):
        line_bot_api.reply_message(event.reply_token, _render_question(step, state))
        return

    # 次のステップへ
    state["step"] = step + 1

    # まだ質問が残っている場合 → 次の質問を表示
    if state["step"] < len(seq):
        line_bot_api.reply_message(event.reply_token, _render_question(state["step"], state))
        return

    # ---- 全回答完了 → プラン生成 ----
    answers = state["answers"]
    try:
        send_plan_parts(event.reply_token, uid, answers)
    except Exception:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="プラン生成でエラーが発生しました。")
        )
        return

    # セッション終了
    users.pop(uid, None)



@handler.add(MessageEvent, message=LocationMessage)
def on_location(event: MessageEvent):
    """ユーザーが位置情報を送ってきたときの処理"""
    uid = event.source.user_id
    state = users.get(uid)

    if not state:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="位置情報を受け取りました。もう一度メニューからやり直してください。")
        )
        return

    mode = state.get("mode", "wizard")
    loc = event.message

    # 共通の geo データ
    geo_data = {
        "lat": loc.latitude,
        "lng": loc.longitude,
        "address": loc.address,
        "title": loc.title,
    }

    # =====================================================
    # ① AI観光モード中の現在地
    # =====================================================
    if mode == "ai_travel":
        state["geo"] = geo_data
        lang = state.get("lang", "日本語")
        if lang == "日本語":
            msg = (
                "📍 現在地を受け取りました！\n"
                "この周辺で行きたいイメージや条件を、自由に入力してください。\n"
                "例）「夜景がきれいなデート向き」「ひとりで入れるラーメン」など"
            )
        else:
            msg = (
                "📍 Got your location!\n"
                "Now tell me what you are looking for around here.\n"
                "e.g. 'Romantic night view', 'Solo-friendly ramen shop', etc."
            )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        return

    # =====================================================
    # ② 通常の wizard モード（現在地が必要な分岐）
    # =====================================================
    if mode == "wizard":
        state.setdefault("answers", {})
        state["answers"]["geo"] = geo_data

        # need_location フラグを落とす
        need_loc = bool(state.get("need_location"))
        state["need_location"] = False

        # 「たまたま位置情報送っただけ」の場合
        if not need_loc:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="位置情報を受け取りました。")
            )
            return

        req  = state["answers"].get("request")
        pref = state["answers"].get("pref")
        lang = state["answers"].get("lang", LAST_LANG.get(uid, "日本語"))

        # ★ 観光地 × 現在地から近く → master から近傍3件を出して終了
        if req == "観光地" and pref == "現在地から近く":
            spots = _get_near_sightseeing_from_master(geo_data, max_km=15.0, limit=3)

            if spots:
                _send_sightseeing_three_from_master(uid, event.reply_token, spots, lang=lang)
            else:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(
                        text=(
                            "現在地付近（15km以内）に登録済みの観光地が見つかりませんでした。\n"
                            "距離を広げるか、マスターデータに緯度経度（lat/lon）を追加してください。"
                        )
                    )
                )

            users.pop(uid, None)
            return

        # ★ 体験スポット × 現在地から近く → experience master から近傍3件を出して終了
        if req == "体験スポット" and pref == "現在地から近く":
            spots = _get_near_experience_from_master(geo_data, max_km=10.0, limit=3)

            if spots:
                _push_experiences_three_from_master(uid, spots, lang=lang)
            else:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(
                        text="現在地付近（10km以内）に登録済みの体験スポットが見つかりませんでした。"
                    )
                )

            users.pop(uid, None)
            return

        # ---- それ以外（飲食店/体験スポットの県選択など）は従来通り質問を進める ----
        step = state.get("step", 0)
        step += 1
        state["step"] = step

        seq = _get_question_sequence(state["answers"])

        if step < len(seq):
            next_question = _render_question(step, state)
            line_bot_api.reply_message(event.reply_token, next_question)
        else:
            answers = state["answers"]
            try:
                send_plan_parts(event.reply_token, uid, answers)
            except Exception:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text="プラン生成でエラーが発生しました。")
                )
            users.pop(uid, None)
        return

    # =====================================================
    # ③ それ以外のモード
    # =====================================================
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text="位置情報を受け取りました。")
    )




# ====================== ローカル実行 ======================
if __name__ == "__main__":
    logging.getLogger().setLevel(logging.INFO)
    logging.info(f"Running Python: {sys.version}")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)




















