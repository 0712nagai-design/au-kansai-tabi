# app.py
import os
import json
import logging
from datetime import datetime
from flask import Flask, request, abort, jsonify
import requests

# LINE SDK
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    LocationMessage, QuickReply, QuickReplyButton, MessageAction,
    FlexSendMessage, TemplateSendMessage, CarouselTemplate, CarouselColumn
)

# Optional: OpenAI
# We'll call the OpenAI Chat Completions API via requests; replace with openai package if preferred.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# LINE credentials (set as environment variables)
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    raise RuntimeError("Set LINE_CHANNEL_ACCESS_TOKEN and LINE_CHANNEL_SECRET environment variables")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# ---- In-memory user session store (for demo). Replace with Redis or DB in production. ----
# structure: user_sessions[user_id] = {
#   "step": "start" / "area" / "time" / "meal" / "cuisine" / "scene" / "budget" / "confirm",
#   "data": {...}
# }
user_sessions = {}

# ---- Utility: Quick Reply builders ----
def make_quick_reply_buttons(items):
    # items: list of (label, text)
    buttons = []
    for label, text in items:
        buttons.append(
            QuickReplyButton(
                action=MessageAction(label=label, text=text)
            )
        )
    return QuickReply(quick_reply_buttons=buttons)

# ---- Flow steps ----
def start_flow(user_id):
    user_sessions[user_id] = {"step": "ask_area", "data": {}}
    text = "こんにちは！😋 どのエリアでお店を探しますか？\n（現在地を送りたい場合は位置情報ボタンを送ってください）"
    quick = make_quick_reply_buttons([
        ("現在地の近く", "現在地"),
        ("京都", "京都"),
        ("東京", "東京"),
        ("大阪", "大阪"),
        ("その他（入力）", "その他")
    ])
    return TextSendMessage(text=text, quick_reply=quick)

def ask_meal_time(user_id):
    user_sessions[user_id]["step"] = "ask_meal_time"
    text = "今はどんな食事のタイミングですか？"
    quick = make_quick_reply_buttons([
        ("朝ごはん", "朝ごはん"),
        ("ランチ", "ランチ"),
        ("カフェ・スイーツ", "カフェ・スイーツ"),
        ("夜ごはん", "夜ごはん"),
        ("飲み会・バー", "飲み会・バー"),
    ])
    return TextSendMessage(text=text, quick_reply=quick)

def ask_cuisine(user_id):
    user_sessions[user_id]["step"] = "ask_cuisine"
    text = "どんなジャンルの料理が食べたいですか？（複数選択でもOKです）"
    quick = make_quick_reply_buttons([
        ("和食", "和食"),
        ("洋食", "洋食"),
        ("中華", "中華"),
        ("焼肉", "焼肉"),
        ("居酒屋", "居酒屋"),
        ("カフェ・スイーツ", "カフェ・スイーツ"),
        ("ラーメン", "ラーメン"),
        ("こだわらない", "こだわらない"),
    ])
    return TextSendMessage(text=text, quick_reply=quick)

def ask_scene(user_id):
    user_sessions[user_id]["step"] = "ask_scene"
    text = "どんなシーンで利用しますか？"
    quick = make_quick_reply_buttons([
        ("一人で", "一人で"),
        ("カップル・デート", "カップル・デート"),
        ("家族で", "家族で"),
        ("友達グループ", "友達グループ"),
        ("仕事・接待", "仕事・接待"),
    ])
    return TextSendMessage(text=text, quick_reply=quick)

def ask_budget(user_id):
    user_sessions[user_id]["step"] = "ask_budget"
    text = "お一人あたりの予算を教えてください"
    quick = make_quick_reply_buttons([
        ("〜1,000円", "〜1,000円"),
        ("1,000〜3,000円", "1,000〜3,000円"),
        ("3,000〜6,000円", "3,000〜6,000円"),
        ("6,000円以上", "6,000円以上"),
    ])
    return TextSendMessage(text=text, quick_reply=quick)

def ask_atmosphere(user_id):
    user_sessions[user_id]["step"] = "ask_atmosphere"
    text = "お店の雰囲気や条件で希望はありますか？（複数OK）"
    quick = make_quick_reply_buttons([
        ("おしゃれ・落ち着いた", "おしゃれ・落ち着いた"),
        ("にぎやか・カジュアル", "にぎやか・カジュアル"),
        ("個室あり", "個室あり"),
        ("夜景・雰囲気重視", "夜景・雰囲気重視"),
        ("駐車場あり", "駐車場あり"),
        ("禁煙", "禁煙"),
        ("特になし", "特になし"),
    ])
    return TextSendMessage(text=text, quick_reply=quick)

def confirm_and_search(user_id):
    user_sessions[user_id]["step"] = "confirm"
    d = user_sessions[user_id]["data"]
    summary_lines = [
        "検索条件はこちらです：",
        f"📍 場所: {d.get('area','（未設定）')}",
        f"🕓 タイミング: {d.get('meal_time','（未設定）')}",
        f"🍽 ジャンル: {', '.join(d.get('cuisine',[])) if isinstance(d.get('cuisine',[]), list) else d.get('cuisine','（未設定）')}",
        f"👥 シーン: {d.get('scene','（未設定）')}",
        f"💴 予算: {d.get('budget','（未設定）')}",
        f"✨ 雰囲気: {', '.join(d.get('atmosphere',[])) if isinstance(d.get('atmosphere',[]), list) else d.get('atmosphere','（未設定）')}"
    ]
    text = "\n".join(summary_lines) + "\n\nこの条件でおすすめのお店を探しますか？"
    quick = make_quick_reply_buttons([
        ("はい、探して", "探して"),
        ("条件を変える", "条件を変える"),
        ("最初からやり直す", "最初から")
    ])
    return TextSendMessage(text=text, quick_reply=quick)

# ---- Recommendation: call OpenAI or mock ----
def get_restaurant_recommendations(session_data, limit=3):
    """
    session_data: dict with keys like area, meal_time, cuisine (list), scene, budget, atmosphere, lat/lon optional
    Returns: list of dict {name, description, price, address, google_map_url}
    """
    # If OPENAI_API_KEY set, query model to generate recommendations in JSON
    if OPENAI_API_KEY:
        prompt = build_openai_prompt(session_data, limit)
        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json"
        }
        # Using Chat Completions (gpt-4o / gpt-4 / gpt-3.5) - adjust model name as you have access
        body = {
            "model": "gpt-4o-mini",  # change to actual model you have access to
            "messages": [
                {"role": "system", "content": "You are an assistant that returns a JSON list of restaurant recommendations based on user preferences. Respond with valid JSON only."},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.7,
            "max_tokens": 800,
        }
        resp = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, data=json.dumps(body), timeout=15)
        if resp.status_code == 200:
            j = resp.json()
            try:
                content = j["choices"][0]["message"]["content"]
                # Expect the model to return a JSON array. Try to parse.
                recs = json.loads(content)
                # Validate minimal fields
                filtered = []
                for r in recs[:limit]:
                    filtered.append({
                        "name": r.get("name", "不明なお店"),
                        "description": r.get("description", ""),
                        "price": r.get("price", ""),
                        "address": r.get("address", ""),
                        "google_map_url": r.get("google_map_url", "")
                    })
                return filtered
            except Exception as e:
                app.logger.error("OpenAI JSON parse error: %s / content: %s", e, j)
                # fallthrough to mock
        else:
            app.logger.error("OpenAI API error %s: %s", resp.status_code, resp.text)

    # Mock recommendations (fallback)
    area = session_data.get("area", "近く")
    meal_time = session_data.get("meal_time", "")
    cuisine = session_data.get("cuisine", ["和食"])
    return [
        {
            "name": f"{area} の人気店A",
            "description": f"{cuisine[0]}の名店。{meal_time}におすすめのコースあり。落ち着いた雰囲気。",
            "price": "¥3,500〜",
            "address": f"{area}の中心地",
            "google_map_url": ""
        },
        {
            "name": f"{area} の隠れ家B",
            "description": f"デートに人気の和食店。個室あり、雰囲気◎",
            "price": "¥4,200〜",
            "address": f"{area}の路地裏",
            "google_map_url": ""
        },
        {
            "name": f"{area} の居酒屋C",
            "description": "地元食材を使った家庭的な料理。コスパが良い。",
            "price": "¥2,800〜",
            "address": f"{area}駅近く",
            "google_map_url": ""
        }
    ][:limit]

def build_openai_prompt(session_data, limit=3):
    # Build a clear instruction so model returns a JSON array of recommendations.
    prompt_lines = [
        "ユーザーの条件に合う飲食店を日本語でJSON配列として最大 {} 件返してください。".format(limit),
        "各要素は name, description, price, address, google_map_url をキーに含めてください。",
        "以下がユーザーの条件です：",
        json.dumps(session_data, ensure_ascii=False, indent=2)
    ]
    return "\n".join(prompt_lines)

# ---- Flex message builder for restaurants ----
def build_restaurant_flex_messages(recs):
    # recs: list of dict {name, description, price, address, google_map_url}
    # Build a simple Flex message. For brevity we return multiple TextSendMessage and one FlexSendMessage containing basic cards.
    bubbles = []
    for r in recs:
        bubble = {
            "type": "bubble",
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "contents": [
                    {"type": "text", "text": r["name"], "weight": "bold", "size": "md"},
                    {"type": "text", "text": r["description"], "size": "sm", "wrap": True},
                    {"type": "text", "text": f"予算: {r.get('price','-')}", "size": "sm", "color": "#666666"},
                    {"type": "text", "text": f"住所: {r.get('address','-')}", "size": "xxs", "color": "#999999", "wrap": True}
                ]
            },
            "footer": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {
                        "type": "button",
                        "action": {
                            "type": "uri",
                            "label": "地図で見る",
                            "uri": r.get("google_map_url") or "https://www.google.com/maps/search/?api=1&query=" + requests.utils.quote(r.get("n
