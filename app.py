# -*- coding: utf-8 -*-
import os, re, sys, json, logging
from collections import defaultdict, deque
from datetime import datetime
from typing import Optional, Dict, Any, List

from flask import Flask, request, abort

# LINE SDK
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageSendMessage

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
    {"key": "region", "title": "地域を教えてください。（複数選択可）", "choices": REGIONS, "multi": True},
    {"key": "date", "title": "出発日を YYYY-MM-DD で入力してください（例：2025-03-20）", "choices": {}, "multi": False},
    {"key": "stay", "title": "日程を選択してください。", "choices": {1: "日帰り", 2: "1泊2日", 3: "2泊3日", 4: "3泊以上"}, "multi": False},
    {"key": "theme", "title": "テーマを選んでください。（複数選択可）", "choices": THEMES, "multi": True},
    {"key": "budget", "title": "予算（1人）を選んでください。", "choices": BUDGETS, "multi": False},
    {"key": "hotel", "title": "ホテルタイプを選んでください。", "choices": HOTELS, "multi": False},
    {"key": "transport", "title": "交通手段を選んでください。（複数選択可）", "choices": TRANSPORT, "multi": True},
    {"key": "companion", "title": "同行者を選んでください。", "choices": COMPANION, "multi": False},
    {"key": "dept", "title": "出発時間帯を選んでください。", "choices": DEPT, "multi": False},
    {"key": "arrv", "title": "帰着時間帯はどのくらいを予定されていますか？", "choices": ARRV, "multi": False},
]

WELCOME = (
    "🔄 最初から\n"
    "こんにちは！私はAI旅ナビ関西です🧭\n"
    "どちらの言語でご案内しますか？\n"
    "1️⃣ 日本語（Japanese）\n"
    "2️⃣ English（英語）"
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

# ====================== ユーティリティ ======================
def _render_question(idx: int) -> str:
    q = Q[idx]
    lines = [q["title"]]
    if q["choices"]:
        for n, label in q["choices"].items():
            lines.append(f"{n}\u20E3 {label}")  # 1⃣ の見た目
    lines.append("🔁 最初から")
    return "\n".join(lines)

# 数値選択のパースを強化（カンマ/ドット/中黒/全角など全て区切りに）
FW_TO_HW = str.maketrans({
    "０":"0","１":"1","２":"2","３":"3","４":"4","５":"5","６":"6","７":"7","８":"8","９":"9",
    "．":".","，":",","、":",","・":",","　":" "
})

def _parse_numbers(s: str) -> Optional[List[int]]:
    if not s:
        return None
    # 全角→半角に寄せる
    s = s.translate(FW_TO_HW)
    # さまざまな区切り記号をカンマに統一
    for sep in [".", "･", "・", "、", "，", " ", "　", "/", "／"]:
        s = s.replace(sep, ",")
    # 余分なカンマを整理
    s = re.sub(r",+", ",", s).strip(",")
    # 数字とカンマだけになっているか
    if not re.fullmatch(r"[0-9,]+", s):
        return None
    try:
        nums = [int(x) for x in s.split(",") if x != ""]
        return nums if nums else None
    except Exception:
        return None


def _validate_and_store(uid: str, step: int, text: str) -> bool:
    """有効なら users[uid]['answers'] に保存して True を返す。無効なら False。"""
    state = users[uid]
    q = Q[step]
    key = q["key"]
    if "answers" not in state: state["answers"] = {}

    # 言語
    if key == "lang":
        nums = _parse_numbers(text)
        if nums and len(nums) == 1 and nums[0] in (1, 2):
            state["answers"][key] = "ja" if nums[0] == 1 else "en"
            return True
        return False

    # 地域（複数）
    if key == "region":
        nums = _parse_numbers(text)
        if not nums: return False
        bad = [n for n in nums if n not in REGIONS]
        if bad: return False
        state["answers"][key] = [REGIONS[n] for n in sorted(set(nums))]
        return True

    # 日付
    if key == "date":
        try:
            datetime.strptime(text.strip(), "%Y-%m-%d")
            state["answers"][key] = text.strip()
            return True
        except Exception:
            return False

    # 共通（単一/複数）
    nums = _parse_numbers(text)
    if not nums: return False

    if q["multi"]:
        bad = [n for n in nums if n not in q["choices"]]
        if bad: return False
        state["answers"][key] = [q["choices"][n] for n in sorted(set(nums))]
        return True
    else:
        if len(nums) != 1 or nums[0] not in q["choices"]:
            return False
        state["answers"][key] = q["choices"][nums[0]]
        return True

def answers_brief(a: Dict[str, Any]) -> str:
    """回答の短い要約（キー名はこの実装のものに合わせる）"""
    def pick(v, default="未選択"):
        if v is None or v == "": return default
        if isinstance(v, list): return "、".join(map(str, v)) if v else default
        return str(v)

    return (
        f"- 地域：{pick(a.get('region'))}\n"
        f"- 出発日：{pick(a.get('date'))}\n"
        f"- 日程：{pick(a.get('stay'))}\n"
        f"- テーマ：{pick(a.get('theme'))}\n"
        f"- 予算：{pick(a.get('budget'))}\n"
        f"- ホテルタイプ：{pick(a.get('hotel'))}\n"
        f"- 交通手段：{pick(a.get('transport'))}\n"
        f"- 同行者：{pick(a.get('companion'))}\n"
        f"- 出発時間帯：{pick(a.get('dept'))}\n"
        f"- 帰着時間帯：{pick(a.get('arrv'))}\n"
    )

def _count_days_in_text(text: str) -> int:
    a = len(re.findall(r"\*\*\s*\d+日目", text))
    b = len(re.findall(r"Day\s*\d+", text, flags=re.I))
    return max(a, b)

def _required_days(answers: dict) -> int:
    """stay から必要日数を返す（最低2日）"""
    stay = str(answers.get("stay", "2"))
    table = {"日帰り": 1, "1泊2日": 2, "2泊3日": 3, "3泊以上": 3}
    d = table.get(stay, 2)
    return max(d, 2)

# ---------- 生成プロンプト ----------
def build_final_prompt(answers: Dict[str, Any]) -> str:
    lang = answers.get("lang", "ja")
    locale = "Japanese output." if lang == "ja" else "English output."
    answers_json = json.dumps(answers, ensure_ascii=False, indent=2)

    return f"""
{locale}
あなたは「AI旅ナビ関西」です。以下のユーザー回答に**厳密**に従い、読みやすい“完成版”旅プランを**1回で**出力してください。
（JSONやコードブロックやキー:値の羅列は出さない）

【ユーザー回答(JSON 参照用)】
{answers_json}

【出力順（厳守）】
1️⃣ ホテル候補（3件）※各ホテルに①②③…と番号棒線（──────────────────────────────）で区切る**。
　書式：
　🏨 ホテル名
　特徴：要約1行
　🔗 公式：生URL
　📍 Googleマップ：生URL
　💰 価格目安：〜円／泊
──────────────────────────────

2️⃣ 日程表（Day1〜帰着まで。**各日6ブロック以上**）その日の初めにDay〇をつける。
　※**日程表では画像URLは一切出さない（📸の行も出さない）**  
　各ブロックの厳密フォーマット（**これに従う**）：
　🕘 9:00–10:30　🏯 観光：施設名（エリア）
　短評：見どころ/体験/小さなコツを2–3行
　🕒 所要：60–90分　🚶アクセス：公共交通/徒歩・所要
　🔗 公式：生URL
　📍 Googleマップ：生URL
　🕰 営業：時間／休：定休
　──────────────────────────────  ←**各ブロックの最後に必ず入れる**

　※「Day1」「Day2」などは**各日の最初のブロックのみに表示**し、同じ日のブロック内では省略する。
　※「昼食」「夕食」は**店名を必須**（例：○○食堂）＋短評＋価格帯＋営業時間＋GoogleマップURL。
　※体験は**固有施設名で最低1ブロック**（例：ならまち着物レンタル△△店、茶道体験□□亭）。
　※各日1件、雨天時代替（屋内）も書く。
　※9:00開始〜17:30前後で主要観光／移動は30分刻みで自然に。
──────────────────────────────


3️⃣ 実用ガイド（この順で）
　各施設・店舗・体験は**必ず番号（①②③…）と棒線（──────────────────────────────）で区切る**。  
　1) 🚆 交通（主要3行／運賃目安。必要に応じて駅・路線ごとに区切る）
　──────────────────────────────
　2) 🍱 食事おすすめ：
　　**昼3件／夜3件**（店名必須・短評・価格帯・🕰時間/休・公式URL・GoogleマップURL）
　　※各店ごとに以下の形式で書き、棒線で区切る：
　　　🍽 店名（エリア）
　　　短評：料理内容や雰囲気・おすすめメニュー
　　　💰 価格帯：〜円程度　🕰 営業：時間／休：定休
　　　🔗 公式：URL  
　　　📍 Googleマップ：URL
　──────────────────────────────
　3) 🎟️ 体験予約：
　　**3件**（施設名必須・公式URL・料金目安・所要・予約要否・GoogleマップURL）
　　※各体験ごとに以下の形式で書き、棒線で区切る：
　　　🎯 施設名（エリア）
　　　短評：どんな体験ができるか、ポイントを2〜3行で
　　　💰 料金：〜円　⌛ 所要：〜分／予約：要・不要
　　　🔗 公式：URL  
　　　📍 Googleマップ：URL
　──────────────────────────────
　4) 💰 合計予算（宿/交通/食事/体験の小計＋合計）
　──────────────────────────────
　5) ✅ チェックリスト


4️⃣ 総評・注意点・代替案（2–4行）

5️⃣ 次の操作メニュー（**この1行のみを必ず出力**）
🔄 最初から

【リンクルール】URLは**生URL**のみ（Markdownリンク禁止）
【言語】lang=jaなら日本語、enなら英語で一貫
"""

# ---------- OpenAI 呼び出し ----------
def _call_openai_plan(answers: dict) -> str:
    user_prompt = build_final_prompt(answers)

    res = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.6,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    )
    text = res.choices[0].message.content or ""

    # 必要日数チェック（足りなければ追記指示）
    need = _required_days(answers)
    got = _count_days_in_text(text)
    if got < need:
        text += f"\n\n（補足）現在 {got} 日分です。{need} 日分になるよう続きも含めて出力してください。"
    return text



SYSTEM_PROMPT = (
    "You are AI Travel Navi Kansai.\n"
    "以下の利用者回答（JSON）に厳密に従って、選択されていない地域は一切含めず、"
    "最終プランを**一度だけ**返します。中間メッセージ・分割出力は禁止。\n"
    "出力順：1)ホテル候補3件 2)日程表 3)実用ガイド 4)総評・注意点・代替案 5)次の操作メニュー。\n"
    "画像は各ブロック1枚。許可ドメイン：https://www.japan-guide.com / "
    "日程表と実用ガイドでは**画像URLを一切出さない**（📸行も出さない）。\n"
    "https://upload.wikimedia.org / https://images.unsplash.com 。無い場合は "
    "https://placehold.co/800x500.png?text={施設名} を使用。URLは生URL（Markdownリンク禁止）。\n"
    "日本語モード（ja）は日本語、英語モード（en）は英語で一貫出力。\n"
    # ★ ここから追加の強制条件
    "食事と体験は**固有の店名・施設名**を必ず記載し、各項目に Google マップ検索URL と営業時間・定休の情報を付けること。\n"
    "体験は**最低3つ**提示すること（候補として3件、各々に料金目安・所要時間・予約要否を明記）。\n"


)

# ---------- 画像検出・送信 ----------
IMG_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:japan-guide\.com|upload\.wikimedia\.org|images\.unsplash\.com|placehold\.co)/[^\s)]+",
    re.I,
)

def _detect_image_urls(text: str, limit=5) -> List[str]:
    urls = []
    for m in IMG_URL_RE.finditer(text):
        urls.append(m.group(0))
        if len(urls) >= limit:
            break
    return urls


# 画像・地図のドメインはプレビュー除外
NON_PREVIEW_DOMAINS = re.compile(
    r"(?:japan-guide\.com|upload\.wikimedia\.org|images\.unsplash\.com|placehold\.co|google\.com/maps|goo\.gl/maps)",
    re.I,
)
URL_RE = re.compile(r"https?://[^\s)]+", re.I)

def _extract_preview_urls(text: str, limit=6) -> List[str]:
    """LINE のリンクプレビューを出したいURLだけを抽出"""
    urls: List[str] = []
    for m in URL_RE.finditer(text):
        u = m.group(0)
        if NON_PREVIEW_DOMAINS.search(u):  # 画像や地図リンクは除外
            continue
        if u not in urls:
            urls.append(u)
        if len(urls) >= limit:
            break
    return urls

    urls = []
    for m in IMG_URL_RE.finditer(text):
        urls.append(m.group(0))
        if len(urls) >= limit:
            break
    return urls
# 🔗 公式サイトなどのURLを単体で Push（リンクプレビューを出す）
preview_urls = _extract_preview_urls(plan, limit=6)
for u in preview_urls:
    try:
        line_bot_api.push_message(uid, TextSendMessage(text=u))
    except LineBotApiError:
        app.logger.exception("Preview URL push failed: %s", u)

def _split_long_text(text: str, maxlen=4900) -> List[str]:
    if len(text) <= maxlen:
        return [text]
    parts, buf, count = [], [], 0
    for line in text.splitlines(True):
        if count + len(line) > maxlen:
            parts.append("".join(buf))
            buf, count = [line], len(line)
        else:
            buf.append(line); count += len(line)
    if buf:
        parts.append("".join(buf))
    return parts

def _reply_text(reply_token: str, text: str):
    chunks = _split_long_text(text)
    msgs = [TextSendMessage(text=c) for c in chunks]
    line_bot_api.reply_message(reply_token, msgs)

def _push_images(uid: str, urls: List[str]):
    for u in urls:
        try:
            line_bot_api.push_message(uid, ImageSendMessage(original_content_url=u, preview_image_url=u))
        except LineBotApiError:
            app.logger.exception("Image push failed: %s", u)

# ====================== メインハンドラ ======================
@handler.add(MessageEvent, message=TextMessage)
def on_message(event: MessageEvent):
    uid = event.source.user_id
    text = (event.message.text or "").strip()

    # リスタート：状態を初期化して WELCOME だけ返す（質問は二度出さない）
    if text in RESTART or text.lower() in RESTART:
        users[uid] = {"step": 0, "answers": {}, "hist": deque(maxlen=MAX_TURNS)}
        _reply_text(event.reply_token, WELCOME)
        return

    # セッション初期化：最初も WELCOME だけ返す（言語質問を重複表示しない）
    if uid not in users or not users[uid]:
        users[uid] = {"step": 0, "answers": {}, "hist": deque(maxlen=MAX_TURNS)}
        _reply_text(event.reply_token, WELCOME)
        return

    state = users[uid]
    step = state["step"]

    # 現在ステップに対する入力を検証・保存
    if not _validate_and_store(uid, step, text):
        _reply_text(event.reply_token, _render_question(step))
        return

    # 次のステップへ
    step += 1
    state["step"] = step

    # まだ質問が残っていれば次の質問を提示
    if step < len(Q):
        _reply_text(event.reply_token, _render_question(step))
        return

    # === 全質問終了 → プラン生成 ===
    answers = state["answers"].copy()
    try:
        plan = _call_openai_plan(answers)
    except Exception as e:
        app.logger.exception("OpenAI API error")
        _reply_text(event.reply_token, f"サーバ側で一時的なエラーが発生しました。\n(debug: {type(e).__name__})")
        return

    # 本文（旅程）を返信
    _reply_text(event.reply_token, plan)

    # 画像URLを push（既存仕様）
    imgs = _detect_image_urls(plan, limit=5)
    if imgs:
        _push_images(uid, imgs)

    # 公式サイトなどのURLを push（リンクプレビュー狙い）
    preview_urls = _extract_preview_urls(plan, limit=6)
    for u in preview_urls:
        try:
            line_bot_api.push_message(uid, TextSendMessage(text=u))
        except LineBotApiError:
            app.logger.exception("Preview URL push failed: %s", u)

    # セッション終了
    users.pop(uid, None)


    

# ====================== ローカル実行 ======================
if __name__ == "__main__":
    logging.getLogger().setLevel(logging.INFO)
    logging.info(f"Running Python: {sys.version}")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)












