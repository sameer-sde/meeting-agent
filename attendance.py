import os, json, time, requests
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()
BASE = "https://api.cloud.vexa.ai"
TX_H = {"X-API-Key": os.environ["VEXA_TX_KEY"]}
TG = f"https://api.telegram.org/bot{os.environ.get('TELEGRAM_BOT_TOKEN', '')}"
CHAT = os.environ.get("TELEGRAM_CHAT_ID")
ASK_MIN = float(os.environ.get("ASK_MINUTES_BEFORE", "5"))
STATE = "asked.json"

def load():
    return json.load(open(STATE)) if os.path.exists(STATE) else {}

def save(s):
    json.dump(s, open(STATE, "w"))

def tg(method, **kw):
    return requests.post(f"{TG}/{method}", json=kw, timeout=20).json()

def scheduled():
    r = requests.get(f"{BASE}/meetings", headers=TX_H, params={"status": "scheduled", "limit": 50}, timeout=15)
    d = r.json() if r.ok else []
    rows = d if isinstance(d, list) else d.get("meetings", [])
    out = []
    for m in rows:
        if m.get("platform") in (None, "unknown"):
            continue
        at = (m.get("data") or {}).get("scheduled_at") or m.get("scheduled_at")
        try:
            start = datetime.fromisoformat(at.replace("Z", "+00:00"))
        except Exception:
            continue
        out.append((m["id"], m.get("title") or m.get("native_meeting_id"), start))
    return out

def ask(mid, title, start):
    when = start.astimezone().strftime("%I:%M %p")
    tg("sendMessage", chat_id=CHAT,
       text=f"📅 Meeting '{title}' starts at {when}.\nAre you joining it yourself?",
       reply_markup={"inline_keyboard": [[
           {"text": "✅ Yes, I'll join", "callback_data": f"yes:{mid}"},
           {"text": "❌ No, send the bot", "callback_data": f"no:{mid}"}]]})

def set_auto_join(mid, on):
    r = requests.patch(f"{BASE}/meetings/{mid}", headers=TX_H, json={"auto_join": on}, timeout=15)
    return r.ok

def handle_answers(state):
    d = requests.get(f"{TG}/getUpdates", params={"offset": state.get("_offset", 0), "timeout": 0}, timeout=20).json()
    for u in d.get("result", []):
        state["_offset"] = u["update_id"] + 1
        cq = u.get("callback_query")
        if not cq or ":" not in cq.get("data", ""):
            continue
        answer, mid = cq["data"].split(":", 1)
        tg("answerCallbackQuery", callback_query_id=cq["id"])
        if answer == "yes":
            ok = set_auto_join(int(mid), False)
            note = "👍 Got it, you're joining. The bot will stay out." if ok else \
                   "⚠️ Couldn't cancel the bot (the meeting may have already started)."
        else:
            set_auto_join(int(mid), True)
            note = "🤖 Okay, the bot will join and send the report card."
        state.setdefault(mid, {})["answer"] = answer
        msg = cq["message"]
        tg("editMessageText", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
           text=msg["text"] + "\n\n" + note)
        print(f"Meeting {mid}: answer = {answer}")

def loop():
    if not CHAT or not os.environ.get("TELEGRAM_BOT_TOKEN"):
        print("Attendance check is off (Telegram not set up yet).")
        return
    print(f"Attendance check on: asking {ASK_MIN:g} min before each meeting.")
    state = load()
    while True:
        try:
            now = datetime.now(timezone.utc)
            for mid, title, start in scheduled():
                mins = (start - now).total_seconds() / 60
                if 1 < mins <= ASK_MIN and str(mid) not in state:
                    ask(mid, title, start)
                    state[str(mid)] = {"asked": now.isoformat()}
                    print(f"Asked about meeting {mid} ({title})")
            handle_answers(state)
            save(state)
        except Exception as e:
            print("Attendance check error, will retry:", e)
        time.sleep(15)

if __name__ == "__main__":
    loop()
