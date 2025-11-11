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
    FlexSendMessage,  # ← 大きいボタン用
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
    {"key": "region", "title": "地域を教えてください（複数選択→最後に［完了］）", "choices": REGIONS, "multi": True},
    {"key": "date", "title": "出発日を選んでください（例: 2025-03-20 を直接入力）", "choices": {}, "multi": False},
    {"key": "stay", "title": "日程を選択してください。", "choices": {1: "日帰り", 2: "1泊2日", 3: "2泊3日", 4: "3泊以上"}, "multi": False},
    {"key": "theme", "title": "テーマを選んでください（複数選択→最後に［完了］）", "choices": THEMES, "multi": True},
    {"key": "budget", "title": "予算（1人）を選んでください。", "choices": BUDGETS, "multi": False},
    {"key": "hotel", "title": "ホテルタイプを選んでください。", "choices": HOTELS, "multi": False},
    {"key": "transport", "title": "交通手段を選んでください（複数選択→最後に［完了］）", "choices": TRANSPORT, "multi": True},
    {"key": "companion", "title": "同行者を選んでください。", "choices": COMPANION, "multi": False},
    {"key": "dept", "title": "出発時間帯を選んでください。", "choices": DEPT, "multi": False},
    {"key": "arrv", "title": "帰着時間帯を選んでください。", "choices": ARRV, "multi": False},
]

WELCOME = "どちらの言語でご案内しますか？"

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

# ====================== 正規表現・抽出 ======================
FW_TO_HW = str.maketrans({"０":"0","１":"1","２":"2","３":"3","４":"4","５":"5","６":"6","７":"7","８":"8","９":"9","．":".","，":",","、":",","・":",","　":" "})
TIME_RANGE_RE = re.compile(r"\b(\d{1,2}[:：]\d{2})\s*[–\-~〜]\s*(\d{1,2}[:：]\d{2})\b")
# 「00–」「30–」などのゴミを徹底排除
TRAIL_BAD_TIME_RE = re.compile(r"\b(?:00|30)\s*[–\-~〜]\s*\d{1,2}:\d{2}\b")
LEADING_BAD_TIME_RE = re.compile(r"\b(?:00|30)\s*[–\-~〜]")  # タイトル先頭に出るケース
OFFICIAL_URL_RE = re.compile(r"^(?:🔗\s*)?(?:公式|Official)\s*[:：]\s*(https?://[^\s)]+)", re.M)
MAP_URL_RE = re.compile(r"^(?:📍\s*)?(?:Google ?マップ|Google ?Maps)\s*[:：]\s*(https?://[^\s)]+)", re.M | re.I)
PRICE_RE = re.compile(r"(?:💰|料金|価格帯|目安)\s*[:：]\s*([^\n／]+)")
HOURS_RE = re.compile(r"(?:🕰|営業時間|営業)\s*[:：]\s*([^\n]+)")
DAY_HEAD_RE   = re.compile(r"^Day\s*\d+", re.M | re.I)
BLOCK_SPLIT_RE= re.compile(r"\n\s*↓\s*\n", re.M)
ACT_TITLE_RE  = re.compile(r"^[^\n：:]*[：:]\s*(?P<title>[^\n（(]+)", re.M)
SECTION_SPLIT_RE = re.compile(r"\n[-─]{6,}\n")
FOOD_HEAD_RE  = re.compile(r"^\s*🍽\s*(?P<title>[^（\(\n]+)", re.M)
EXPER_HEAD_RE = re.compile(r"^\s*🎯\s*(?P<title>[^（\(\n]+)", re.M)

def _clean_time_noise(s: str) -> str:
    # 余計な「00–」「30–」片方だけの時間を削除
    s = TRAIL_BAD_TIME_RE.sub("", s)
    s = LEADING_BAD_TIME_RE.sub("", s)
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s

def _parse_numbers(s: str) -> Optional[List[int]]:
    if not s: return None
    s = s.translate(FW_TO_HW)
    for sep in [".","･","・","、","　","，"," ", "/", "／"]:
        s = s.replace(sep, ",")
    s = re.sub(r",+", ",", s).strip(",")
    if not re.fullmatch(r"[0-9,]+", s): return None
    try:
        ns = [int(x) for x in s.split(",") if x != ""]
        return ns if ns else None
    except Exception:
        return None

def _label_to_num(choices: Dict[int, str], text: str) -> Optional[int]:
    text = text.strip()
    for n, label in choices.items():
        if text == str(n) or text == label:
            return n
    return None

# ====================== 大きい選択パネル（質問用） ======================
def _choice_panel_flex(title: str, choices: Dict[int, str], multi: bool, selected: List[str]) -> FlexSendMessage:
    # 2列グリッドの大きいボタン
    buttons = []
    for n, label in choices.items():
        cap = f"{n} {label}"
        buttons.append({
            "type":"button","style":"secondary","height":"md","gravity":"center",
            "action":{"type":"message","label":cap,"text":str(n)}
        })
    if multi:
        buttons.append({"type":"button","style":"primary","height":"md",
                        "action":{"type":"message","label":"✅ 完了","text":"完了"}})

    body_contents = [
        {"type":"text","text":title, "wrap":True,"weight":"bold","size":"xl"},
    ]
    if multi:
        body_contents.append({"type":"text",
            "text":f"選択中：{('、'.join(selected) if selected else 'なし')}（最後に［完了］）",
            "size":"sm","color":"#666666","wrap":True})

    # 2列レイアウト
    grid = []
    row = []
    for i, btn in enumerate(buttons, 1):
        row.append(btn)
        if i % 2 == 0:
            grid.append({"type":"box","layout":"horizontal","spacing":"md","contents":row})
            row = []
    if row:
        grid.append({"type":"box","layout":"horizontal","spacing":"md","contents":row})

    bubble = {
        "type":"bubble",
        "body":{"type":"box","layout":"vertical","spacing":"lg","contents": body_contents + grid}
    }
    return FlexSendMessage(alt_text=title, contents=bubble)

def _reply_question(reply_token: str, idx: int, state: State):
    q = Q[idx]
    selected = state.get("multi_temp", {}).get(q["key"], []) if q["multi"] else []
    panel = _choice_panel_flex(q["title"], q.get("choices", {}), q["multi"], selected)
    # テキスト＋大ボタン（Flex）を同時返信
    line_bot_api.reply_message(reply_token, [TextSendMessage(text=q["title"]), panel])

# ====================== 入力検証 ======================
def _validate_and_store(uid: str, step: int, text: str) -> bool:
    state = users[uid]; q = Q[step]; key = q["key"]
    state.setdefault("answers", {}); state.setdefault("multi_temp", {})

    if q["choices"]:
        n = _label_to_num(q["choices"], text)
        if n is not None:
            if q["multi"]:
                sel = state["multi_temp"].setdefault(key, [])
                label = q["choices"][n]
                if label not in sel: sel.append(label)
                return True
            else:
                state["answers"][key] = q["choices"][n] if key != "lang" else ("ja" if n == 1 else "en")
                return True

    if key == "date":
        try:
            datetime.strptime(text.strip(), "%Y-%m-%d")
            state["answers"][key] = text.strip(); return True
        except Exception: return False

    if q["multi"] and text.strip() == "完了":
        picked = state["multi_temp"].get(key, [])
        if not picked: return False
        state["answers"][key] = picked; return True

    nums = _parse_numbers(text)
    if nums:
        if q["multi"]:
            bad = [n for n in nums if n not in q["choices"]]
            if bad: return False
            labels = [q["choices"][n] for n in nums]
            state["multi_temp"][key] = sorted(set(state["multi_temp"].get(key, []) + labels), key=labels.index)
            return True
        else:
            if len(nums) != 1 or nums[0] not in q["choices"]: return False
            state["answers"][key] = q["choices"][nums[0]] if key != "lang" else ("ja" if nums[0]==1 else "en")
            return True
    return False

# ====================== OpenAI プロンプト ======================
def build_hotel_prompt(a: Dict[str, Any]) -> str:
    j = json.dumps(a, ensure_ascii=False, indent=2)
    return f"""
以下は「ホテル候補」セクションの出力指示です。
必ず3件、公式URLとGoogleマップURLを出力。
【ユーザー回答】
{j}
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

def build_schedule_prompt(a: Dict[str, Any]) -> str:
    j = json.dumps(a, ensure_ascii=False, indent=2)
    return f"""
以下は「日程表」セクション。旅程のみ生成。
URLは生のまま（公式/Googleマップ）。各日6ブロック目安。最終日に宿泊を入れない。
【ユーザー回答】
{j}
出力例：
Day1
🕘 9:00–10:30　🏯 観光：施設名（エリア）
短評：2〜3行
⌛ 所要：60〜90分　🚶アクセス：交通手段・所要
🔗 公式：URL
📍 Googleマップ：URL
🕰 営業：時間／休：定休
↓
"""

def build_guide_prompt(a: Dict[str, Any]) -> str:
    j = json.dumps(a, ensure_ascii=False, indent=2)
    return f"""
以下は「実用ガイド」。食事6件（昼3/夜3）・体験3件は各々に営業時間・定休を付す。
【ユーザー回答】
{j}
"""

def build_review_prompt(_: Dict[str, Any]) -> str:
    return "旅全体の総評・注意点・代替案を2〜4行で。"

def build_next_prompt(_: Dict[str, Any]) -> str:
    return "🔄 最初から"

SYSTEM_PROMPT = (
    "You are AI Travel Navi Kansai.\n"
    "ユーザー回答に厳密に従い、選択されていない地域は含めない。\n"
    "出力順：1)ホテル 2)日程表 3)実用ガイド 4)総評 5)次の操作。\n"
    "食事/体験は固有名と営業時間・定休・料金/所要を付す。画像URLは出さない。\n"
)

def _call_openai_text(user_prompt: str) -> str:
    res = client.chat.completions.create(
        model="gpt-4o-mini", temperature=0.6,
        messages=[{"role":"system","content":SYSTEM_PROMPT},{"role":"user","content":user_prompt}]
    )
    return (res.choices[0].message.content or "").strip()

# ====================== 表示用ユーティリティ ======================
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
    # 時間レンジ（最初のフル表記のみ採用）
    mtime = TIME_RANGE_RE.search(block)
    time_range = (mtime.group(0) if mtime else "").replace("：", ":")
    # タイトル（店名/施設名を抽出）
    mtitle = ACT_TITLE_RE.search(block)
    name = (mtitle.group("title").strip() if mtitle else "スポット")
    # サブ（営業時間＋料金）
    mh = HOURS_RE.search(block)
    hours = mh.group(1).strip() if mh else ""
    mp = PRICE_RE.search(block)
    price = mp.group(1).strip() if mp else ""
    subtitle = " ／ ".join([s for s in [f"営業時間：{hours}" if hours else "", f"目安：{price}" if price else ""] if s]) or "リンクを選択してください"
    return time_range, name, subtitle

def _clean_url(u: str) -> str:
    if not u: return ""
    u = u.replace("\u200b","").replace("\u200c","").replace("\u200d","").replace("\ufeff","")
    u = u.strip().strip("。．、，)）]］>＞")
    if u.startswith("http://"): u = "https://" + u[len("http://"):]
    return u

def _flex_card(title: str, subtitle: str, actions: List[Dict[str,str]]) -> FlexSendMessage:
    title = _clean_time_noise(title)[:60]
    subtitle = (subtitle or "リンクを選択してください")[:120]
    bubble = {
        "type":"bubble",
        "body":{"type":"box","layout":"vertical","spacing":"md","contents":[
            {"type":"text","text":title,"weight":"bold","size":"xl","wrap":True},
            {"type":"text","text":subtitle,"size":"sm","color":"#555555","wrap":True}
        ]},
        "footer":{"type":"box","layout":"horizontal","spacing":"md","contents":[
            {"type":"button","style":"primary","height":"sm",
             "action":{"type":"uri","label":a["label"],"uri":a["uri"]}}
            for a in actions[:2]
        ]}
    }
    return FlexSendMessage(alt_text=title, contents=bubble)

def _spot_actions(title_or_name: str, block: str) -> List[Dict[str,str]]:
    off = OFFICIAL_URL_RE.search(block); mp = MAP_URL_RE.search(block)
    acts = []
    if off: acts.append({"label":"公式サイトを見る","uri":_clean_url(off.group(1))})
    if mp:  acts.append({"label":"Googleマップ","uri":_clean_url(mp.group(1))})
    if not acts:
        q = title_or_name.replace(" ", "+")
        acts = [
            {"label":"Google検索","uri":f"https://www.google.com/search?q={q}"},
            {"label":"Googleマップ","uri":f"https://www.google.com/maps/search/?api=1&query={q}"}
        ]
    return acts

def _push_chunks(uid: str, msgs, size: int = 5):
    for i in range(0, len(msgs), size):
        chunk = msgs[i:i+size]
        line_bot_api.push_message(uid, chunk if len(chunk) > 1 else chunk[0])

# ---- ホテル（Flex）
def _send_hotels(uid: str, hotels_text: str):
    blocks = re.split(r"\n[- ─]{6,}\n|\n{2,}", hotels_text.strip())
    msgs = []
    for b in blocks:
        b = b.strip()
        if not b: continue
        first_line = next((ln.strip() for ln in b.splitlines() if ln.strip()), "")
        title = re.sub(r"^\s*[①-⑳]?\s*[🏨\d\.\)\）\s]*", "", first_line) or "ホテル"
        mprice = PRICE_RE.search(b); price = mprice.group(1).strip() if mprice else ""
        sub = f"価格目安：{price}" if price else "リンクを選択してください"
        msgs.append(_flex_card(title, sub, _spot_actions(title, b)))
    if msgs: _push_chunks(uid, msgs, size=5)

# ---- 日程：スクショ風（短文）＋各スポットFlex
def _render_compact_day(day_title: str, day_body: str) -> str:
    lines = [f"{day_title} スケジュール"]
    for b in _blocks_in_day(day_body):
        t, name, _ = _info_from_block(b)
        kind = "観光"
        if "グルメ" in b or "🍽" in b: kind = "食"
        elif "温泉" in b or "♨" in b or "体験" in b: kind = "体験"
        icon = {"観光":"🗺","食":"🍽","体験":"🎯"}.get(kind,"🗺")
        hhmm = (t.split("–")[0] if t else "").replace("：", ":")
        lines.append(f"{hhmm}  {icon} {kind}：{name}")
    return "\n".join(lines)

def _send_day_cards(uid: str, day_title: str, day_body: str):
    msgs = []
    for block in _blocks_in_day(day_body):
        time_range, name, subtitle = _info_from_block(block)
        title = _clean_time_noise(f"{time_range} {name}".strip())
        msgs.append(_flex_card(title, subtitle, _spot_actions(name, block)))
    if msgs: _push_chunks(uid, msgs, size=5)

# ---- 実用ガイド（食事/体験は店名タイトルでカード化）
def _blocks_by_head(section_text: str, head_re: re.Pattern):
    lines = section_text.splitlines()
    idxs = [i for i, ln in enumerate(lines) if head_re.search(ln)]
    blocks = []
    for j, start in enumerate(idxs):
        end = idxs[j+1] if j+1 < len(idxs) else len(lines)
        blocks.append("\n".join(lines[start:end]).strip())
    return blocks

# ====================== フロー ======================
def _required_days(answers: dict) -> int:
    stay = str(answers.get("stay", "2"))
    table = {"日帰り": 1, "1泊2日": 2, "2泊3日": 3, "3泊以上": 3}
    return max(table.get(stay, 2), 2)

def _generate_full_schedule(answers: Dict[str, Any]) -> str:
    schedule = _call_openai_text(build_schedule_prompt(answers))
    need = _required_days(answers)
    got  = len(_split_days(schedule))
    guard = 0
    while got < need and guard < 4:
        cont = build_schedule_prompt(answers) + f"\n補足：すでに Day1〜Day{got} まで作成済み。続きの Day{got+1} 以降のみ。"
        extra = _call_openai_text(cont)
        schedule = (schedule.rstrip() + "\n" + extra.lstrip()).strip()
        got = len(_split_days(schedule)); guard += 1
    return schedule

def send_plan_parts(reply_token: str, uid: str, answers: Dict[str, Any]):
    # ① ホテル
    hotels = _call_openai_text(build_hotel_prompt(answers))
    line_bot_api.reply_message(reply_token, TextSendMessage(text=hotels.strip() + "\n" + ("─"*30)))
    _send_hotels(uid, hotels)  # 3件Push

    # ② 日程（短文→Flex）
    schedule = _generate_full_schedule(answers)
    for day_title, day_body in _split_days(schedule):
        line_bot_api.push_message(uid, TextSendMessage(text=_render_compact_day(day_title, day_body)))
        _send_day_cards(uid, day_title, day_body)

    # ③ 実用ガイド（食事/体験は店名タイトル）
    guide = _call_openai_text(build_guide_prompt(answers))
    parts = SECTION_SPLIT_RE.split(guide)

    food_idx = next((i for i, s in enumerate(parts) if "食事おすすめ" in s), None)
    if food_idx is not None:
        food_blocks = _blocks_by_head(parts[food_idx], FOOD_HEAD_RE)[:3]
        cards = []
        for b in food_blocks:
            _, name, subtitle = _info_from_block(b)
            cards.append(_flex_card(name, subtitle, _spot_actions(name, b)))
        if cards: _push_chunks(uid, cards, size=5)

    exp_idx = next((i for i, s in enumerate(parts) if "体験" in s), None)
    if exp_idx is not None:
        exp_blocks = _blocks_by_head(parts[exp_idx], EXPER_HEAD_RE)[:3]
        cards = []
        for b in exp_blocks:
            _, name, subtitle = _info_from_block(b)
            cards.append(_flex_card(name, subtitle, _spot_actions(name, b)))
        if cards: _push_chunks(uid, cards, size=5)

    # ④ 総評
    review = _call_openai_text(build_review_prompt(answers))
    line_bot_api.push_message(uid, TextSendMessage(text=review))
    # ⑤ メニュー
    nextmsg = _call_openai_text(build_next_prompt(answers))
    line_bot_api.push_message(uid, TextSendMessage(text=nextmsg))

# ====================== メインハンドラ ======================
@handler.add(MessageEvent, message=TextMessage)
def on_message(event: MessageEvent):
    uid = event.source.user_id
    text = (event.message.text or "").strip()

    if text in RESTART or text.lower() in RESTART:
        users[uid] = {"step": 0, "answers": {}, "hist": deque(maxlen=MAX_TURNS), "multi_temp": {}}
        _reply_question(event.reply_token, 0, users[uid])  # 言語は一度だけ
        return

    if uid not in users or not users[uid]:
        users[uid] = {"step": 0, "answers": {}, "hist": deque(maxlen=MAX_TURNS), "multi_temp": {}}
        _reply_question(event.reply_token, 0, users[uid])
        return

    state = users[uid]; step = state["step"]

    if not _validate_and_store(uid, step, text):
        _reply_question(event.reply_token, step, state)
        return

    # 複数選択中は［完了］まで同じ質問をFlexで再表示
    if Q[step]["multi"] and text != "完了":
        _reply_question(event.reply_token, step, state)
        return

    # 次の質問へ
    state["step"] = step + 1
    if state["step"] < len(Q):
        _reply_question(event.reply_token, state["step"], state)
        return

    # すべて回答 → 送信
    answers = state["answers"].copy()
    try:
        send_plan_parts(event.reply_token, uid, answers)
    except Exception as e:
        app.logger.exception("OpenAI API error")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"サーバ側で一時的なエラーが発生しました。\n(debug: {type(e).__name__})"))
        return
    users.pop(uid, None)

# ====================== ローカル実行 ======================
if __name__ == "__main__":
    logging.getLogger().setLevel(logging.INFO)
    logging.info(f"Running Python: {sys.version}")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
