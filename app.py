# -*- coding: utf-8 -*-
import os, sys, time, re, logging, random
from typing import Dict, List, Tuple
from collections import defaultdict

from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, ImageSendMessage
)
from openai import OpenAI
from openai._exceptions import RateLimitError, APIError, APITimeoutError

# ====== 環境変数 ======
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
if not LINE_CHANNEL_SECRET or not LINE_CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("LINE env missing")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY missing")

# ====== 基本 ======
app = Flask(__name__)
app.logger.setLevel(logging.INFO)
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
oai = OpenAI(api_key=OPENAI_API_KEY)

# ====== プロンプト（ホットリロード） ======
PROMPT_PATH = os.path.join(os.path.dirname(__file__), "prompt.txt")
_prompt_text, _prompt_mtime = None, 0.0
DEFAULT_PROMPT = (
    "あなたは「AI旅ナビ関西（AI Travel Navi Kansai）」です。"
    "関西（京都・大阪・奈良・神戸・滋賀・和歌山）に精通した旅行コンシェルジュ。"
    "【重要】サーバ側で集めた回答（JSON）が与えられるので、"
    "それを唯一の真実として用い、質問は一切行わず、"
    "ホテル3件→日程表→実用ガイド→総評/注意/代替案→操作メニュー を"
    "1回の出力で完成させてください。"
    "禁止：途中の中間メッセージ・分割出力・Markdownリンク画像。"
    "画像は各ブロック1枚・許可ドメイン（japan-guide.com / upload.wikimedia.org / images.unsplash.com / placehold.co）のみ。"
)
def load_system_prompt() -> str:
    global _prompt_text, _prompt_mtime
    try:
        st = os.stat(PROMPT_PATH)
        if st.st_mtime != _prompt_mtime:
            with open(PROMPT_PATH, "r", encoding="utf-8") as f:
                _prompt_text = f.read().strip() or DEFAULT_PROMPT
            _prompt_mtime = st.st_mtime
            app.logger.info("[PROMPT] reloaded")
    except FileNotFoundError:
        _prompt_text = DEFAULT_PROMPT
        _prompt_mtime = 0.0
    return _prompt_text

# ====== 画像抽出（本文→Imageメッセージに分離） ======
IMG_ALLOW = (
    r"https://(?:www\.)?japan-guide\.com/[^)\s]+",
    r"https://upload\.wikimedia\.org/[^)\s]+",
    r"https://images\.unsplash\.com/[^)\s]+",
    r"https://placehold\.co/[^)\s]+",
)
IMG_MD = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
PLAIN_URL = re.compile(r"(https?://[^\s)]+)")

def extract_image_urls(text: str) -> List[str]:
    urls: List[str] = [m.group(1) for m in IMG_MD.finditer(text)]
    for m in PLAIN_URL.finditer(text):
        u = m.group(1)
        if any(re.match(p, u) for p in IMG_ALLOW):
            if u not in urls:
                urls.append(u)
    return urls[:5]

def strip_md_images(text: str) -> str:
    return IMG_MD.sub("", text)

def reply_text(reply_token: str, text: str) -> None:
    try:
        MAX = 4900
        chunks = [text[i:i+MAX] for i in range(0, len(text), MAX)] or [""]
        line_bot_api.reply_message(reply_token, [TextSendMessage(text=c) for c in chunks])
    except LineBotApiError:
        app.logger.exception("reply error")

def push_images(uid: str, urls: List[str]) -> None:
    try:
        batch = []
        for u in urls:
            batch.append(ImageSendMessage(original_content_url=u, preview_image_url=u))
            if len(batch) == 5:
                line_bot_api.push_message(uid, batch); batch=[]
        if batch: line_bot_api.push_message(uid, batch)
    except LineBotApiError:
        app.logger.exception("push images error")

# ====== OpenAI（リトライ） ======
def call_openai(messages: List[dict], temperature=0.6, retries=3) -> str:
    delay = 1.2
    for i in range(retries):
        try:
            res = oai.chat.completions.create(
                model="gpt-4o-mini",
                temperature=temperature,
                messages=messages,
            )
            return res.choices[0].message.content
        except (RateLimitError, APITimeoutError, APIError):
            time.sleep(delay + random.random()); delay *= 1.8
        except Exception:
            app.logger.exception("OpenAI fatal"); break
    raise RuntimeError("OpenAI failed")

# ====== 質問スロット定義（FSM） ======
Q = [
    # 0 言語
    {"key":"lang","title":"どちらの言語でご案内しますか？","multi":False,
     "options":["日本語（Japanese）","English（英語）"]},
    # 1 地域
    {"key":"regions","title":"地域を教えてください。（複数選択可）","multi":True,
     "options":["京都","大阪","奈良","神戸","滋賀","和歌山"]},
    # 2 出発日
    {"key":"date","title":"出発日を YYYY-MM-DD で入力してください（例：2025-03-20）","multi":"free"},
    # 3 日程
    {"key":"duration","title":"日程を選んでください。","multi":False,
     "options":["日帰り","1泊2日","2泊3日","3泊以上"]},
    # 4 テーマ
    {"key":"themes","title":"テーマを選んでください。（複数選択可）","multi":True,
     "options":["グルメ","歴史文化","自然癒し","夜景","温泉","家族","ショッピング","体験メイン","その他"]},
    # 5 予算
    {"key":"budget","title":"1人あたりの予算を選んでください。","multi":False,
     "options":["~¥5,000","~¥10,000","~¥20,000","¥30,000以上"]},
    # 6 ホテルタイプ
    {"key":"hotel","title":"ホテルタイプを選んでください。","multi":False,
     "options":["高級","中価格","コスパ","和風旅館","こだわらない"]},
    # 7 交通手段
    {"key":"transport","title":"交通手段を選んでください。（複数選択可）","multi":True,
     "options":["公共交通","車","徒歩中心","指定なし"]},
    # 8 同行者
    {"key":"companions","title":"同行者を選んでください。","multi":False,
     "options":["ひとり","カップル","友人","家族","外国人友人","その他"]},
    # 9 出発時間帯
    {"key":"depart","title":"出発時間帯は？","multi":False,
     "options":["6–8時","9–11時","12–14時","15–17時","18時以降"]},
    # 10 帰着時間帯
    {"key":"return","title":"帰着時間帯はどのくらいを予定？","multi":False,
     "options":["14–17時","17–19時","19–21時","21時以降","未定"]},
]

RESTART_WORDS = {"start","restart","reset","スタート","最初から","やり直す"}

START_MSG = (
    "🔄 最初から\n"
    "こんにちは！私はAI旅ナビ関西です🧭\n"
    "どちらの言語でご案内しますか？\n"
    "1️⃣ 日本語（Japanese）\n"
    "2️⃣ English（英語）"
)

# ユーザー状態： step(いまの質問index), answers(辞書)
State = Dict[str, object]
users: Dict[str, State] = defaultdict(lambda: {"step":0, "answers":{}})

# ====== 質問表示 ======
def question_text(step:int) -> str:
    q = Q[step]
    if q["multi"] == "free":
        opt = ""
    else:
        nums = "\n".join([f"{i+1} {o}" for i,o in enumerate(q["options"])])
        opt = "\n" + nums
    return f"{q['title']}{opt}\n\n🔄 最初から"

# ====== 入力の解釈 ======
def parse_answer(text:str, step:int):
    q = Q[step]
    text = text.strip()
    # free 書式（出発日）
    if q["multi"] == "free":
        # だいたいの検証だけ
        return text if re.match(r"^\d{4}-\d{2}-\d{2}$", text) else None

    # 数字 or カンマ区切り
    try:
        picks = [t.strip() for t in text.replace("，",",").split(",")]
        idxs = []
        for p in picks:
            if not p: continue
            n = int(p)
            if 1 <= n <= len(q["options"]):
                idxs.append(n-1)
            else:
                return None
        if not idxs: return None
        if not q["multi"] and len(idxs)>1: return None
        vals = [q["options"][i] for i in idxs]
        return vals if q["multi"] else vals[0]
    except Exception:
        return None

# ====== すべて埋まったら最終生成 ======
def make_final_json(a:dict) -> dict:
    return {
        "language": a.get("lang"),
        "regions": a.get("regions",[]),
        "depart_date": a.get("date"),
        "duration": a.get("duration"),
        "themes": a.get("themes",[]),
        "budget_per_person": a.get("budget"),
        "hotel_type": a.get("hotel"),
        "transport": a.get("transport",[]),
        "companions": a.get("companions"),
        "depart_time_band": a.get("depart"),
        "return_time_band": a.get("return"),
    }

def final_prompt(json_obj:dict) -> List[dict]:
    lang = json_obj.get("language","日本語")
    # 出力言語ヒント
    lang_hint = "日本語" if "日本" in lang else "English"
    system = load_system_prompt()
    user = (
        f"以下の固定JSONに基づき、{lang_hint}で、ホテル候補3件→日程表→実用ガイド→総評→操作メニューを"
        f"1回のメッセージで完成させてください。画像ルール・禁止事項を厳守。\n"
        f"JSON:\n{json_obj}"
    )
    return [{"role":"system","content":system},{"role":"user","content":user}]

# ====== ルーティング ======
@app.get("/")
def ok(): return "ok", 200

@app.get("/healthz")
def hz(): return "ok", 200

@app.get("/py")
def py(): return sys.version, 200

@app.post("/callback")
def callback():
    sig = request.headers.get("X-Line-Signature","")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, sig)
    except InvalidSignatureError:
        abort(400)
    except Exception:
        app.logger.exception("webhook error"); abort(500)
    return "OK", 200

# ====== メッセージ ======
@handler.add(MessageEvent, message=TextMessage)
def on_text(event:MessageEvent):
    uid = event.source.user_id
    text = (event.message.text or "").strip()

    # リセット
    if text in RESTART_WORDS or text.lower() in RESTART_WORDS:
        users.pop(uid, None)
        reply_text(event.reply_token, START_MSG)
        return

    st = users[uid]
    step = st["step"]
    ans: dict = st["answers"]

    # 初回誘導
    if step == 0 and not ans:
        reply_text(event.reply_token, question_text(0))
        return

    # 入力→検証→保存
    val = parse_answer(text, step)
    if val is None:
        reply_text(event.reply_token, "入力形式が正しくありません。番号（複数可はカンマ区切り）または例に従って入力してください。\n\n"+question_text(step))
        return

    ans[Q[step]["key"]] = val
    st["step"] = step + 1

    # まだ質問がある
    if st["step"] < len(Q):
        reply_text(event.reply_token, question_text(st["step"]))
        return

    # ここで全部埋まった → 最終プラン生成
    json_obj = make_final_json(ans)
    messages = final_prompt(json_obj)
    try:
        out = call_openai(messages, temperature=0.6)
    except Exception:
        reply_text(event.reply_token, "サーバ側で一時的なエラーが発生しました。少し時間をおいて再度お試しください。")
        return

    # 画像抽出→分離送信
    imgs = extract_image_urls(out)
    text_only = strip_md_images(out).strip()
    reply_text(event.reply_token, text_only or " ")
    if imgs: push_images(uid, imgs)

    # 完了後は状態をリセット（再利用しやすく）
    users.pop(uid, None)

# ====== ローカル ======
if __name__ == "__main__":
    logging.getLogger().setLevel(logging.INFO)
    logging.info(f"Running Python: {sys.version}")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT","5000")), debug=True)
