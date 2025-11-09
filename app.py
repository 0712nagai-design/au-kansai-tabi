# -*- coding: utf-8 -*-
import os
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, FlexSendMessage

LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]

app = Flask(__name__)
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

@app.get("/")
def health(): return "ok", 200

@app.post("/callback")
def callback():
    sig = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, sig)
    except InvalidSignatureError:
        abort(400)
    return "OK", 200

# -------- Flex: ホテルカード生成 --------
def hotel_bubble(name, features, price_range, official_url, gmaps_url, image_url):
    return {
      "type": "bubble",
      "hero": {
        "type": "image",
        "url": image_url,              # 例: https://images.unsplash.com/photo-1542314831-068cd1dbfeeb
        "size": "full",
        "aspectRatio": "16:9",
        "aspectMode": "cover",
        "action": {"type":"uri","label":"Open","uri": official_url or gmaps_url}
      },
      "body": {
        "type": "box",
        "layout": "vertical",
        "spacing": "md",
        "contents": [
          {"type":"text","text": name,"weight":"bold","size":"lg","wrap": True},
          {"type":"box","layout":"baseline","contents":[
            {"type":"icon","url":"https://scdn.line-apps.com/n/channel_devcenter/img/fx/restaurant_regular_32.png"},
            {"type":"text","text": features, "size":"sm","wrap": True,"color":"#555555"}
          ]},
          {"type":"box","layout":"baseline","contents":[
            {"type":"icon","url":"https://scdn.line-apps.com/n/channel_devcenter/img/fx/coin_32.png"},
            {"type":"text","text": price_range, "size":"sm","color":"#333333","wrap": True}
          ]}
        ]
      },
      "footer": {
        "type": "box",
        "layout": "vertical",
        "spacing": "sm",
        "contents": [
          {"type":"button","style":"primary","height":"sm",
           "action":{"type":"uri","label":"公式サイト","uri": official_url}},
          {"type":"button","style":"link","height":"sm",
           "action":{"type":"uri","label":"Googleマップ","uri": gmaps_url}}
        ],
        "flex": 0
      }
    }

def hotels_carousel(items):
    # items: list[dict] ← hotel_bubble(...) を並べる
    return {"type": "carousel", "contents": items[:10]}  # 最大10件

# -------- 使い方（例）：ユーザーが「ホテル」と送ったら3件表示 --------
@handler.add(MessageEvent, message=TextMessage)
def on_text(event):
    text = (event.message.text or "").strip()

    if text in {"ホテル","hotel","1"}:
        bubbles = [
            hotel_bubble(
              "琵琶湖グランドホテル",
              "湖畔の大型リゾート／多彩な温泉と料理",
              "約¥12,000〜¥20,000／泊",
              "https://www.biwako-gh.co.jp/",
              "https://www.google.com/maps/search/琵琶湖グランドホテル",
              "https://images.unsplash.com/photo-1542314831-068cd1dbfeeb"
            ),
            hotel_bubble(
              "THE BLOSSOM KYOTO",
              "四条駅徒歩3分／上質モダン／外国人対応◎",
              "約¥15,000〜¥25,000／泊",
              "https://theblossomhotel.jp/kyoto/",
              "https://www.google.com/maps/search/THE+BLOSSOM+KYOTO",
              "https://images.unsplash.com/photo-1566073771259-6a8506099945"
            ),
            hotel_bubble(
              "旅館 ひやま",
              "琵琶湖湖畔の和風旅館／静かな環境でリラックス",
              "約¥8,000〜¥15,000／泊",
              "https://www.hiyama.com/",
              "https://www.google.com/maps/search/旅館+ひやま",
              "https://images.unsplash.com/photo-1551776235-dde6d4829808"
            ),
        ]
        car = hotels_carousel(bubbles)
        flex = FlexSendMessage(alt_text="ホテル候補", contents=car)
        line_bot_api.reply_message(event.reply_token, flex)
        return

    # それ以外は通常テキスト
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="「ホテル」と送ると写真付きカードを表示します！"))

if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT",5000)), debug=True)










