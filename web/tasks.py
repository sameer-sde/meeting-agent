"""Background work, run every minute: write report cards for finished meetings,
ask Telegram 'are you joining?' before meetings, and handle the answers.

Online, an external cron service calls /cron/tick every minute.
Locally, web/app.py runs tick() in a background thread.
"""
import json
import os
import threading
import traceback
from datetime import datetime, timezone

import requests

from . import mailer, reports, vexa
from .models import KV, Asked, Meeting, Session, User

_lock = threading.Lock()


# ---------- Telegram ----------

def tg_token():
    return os.environ.get("TELEGRAM_BOT_TOKEN", "")


def tg(method, **kw):
    if not tg_token():
        return {}
    try:
        return requests.post(f"https://api.telegram.org/bot{tg_token()}/{method}", json=kw, timeout=20).json()
    except requests.RequestException:
        return {}


def _kv_get(db, key, default=""):
    row = db.get(KV, key)
    return row.value if row else default


def _kv_set(db, key, value):
    row = db.get(KV, key) or KV(key=key)
    row.value = str(value)
    db.add(row)


def handle_telegram(db):
    """Link chats (/start <token>) and process Yes/No answers."""
    if not tg_token():
        return 0
    offset = int(_kv_get(db, "tg_offset", "0") or 0)
    try:
        d = requests.get(f"https://api.telegram.org/bot{tg_token()}/getUpdates",
                         params={"offset": offset, "timeout": 0}, timeout=20).json()
    except requests.RequestException:
        return 0
    handled = 0
    for u in d.get("result", []):
        offset = u["update_id"] + 1
        msg = u.get("message") or {}
        text = (msg.get("text") or "").strip()
        if text.startswith("/start"):
            token = text.split(" ", 1)[1].strip() if " " in text else ""
            user = db.query(User).filter_by(tg_token=token).first() if token else None
            chat_id = str(msg["chat"]["id"])
            if user:
                user.telegram_chat_id = chat_id
                tg("sendMessage", chat_id=chat_id,
                   text="✅ Connected to Meeting Agent. I'll ask you before each meeting whether you're joining.")
            else:
                tg("sendMessage", chat_id=chat_id,
                   text="Open Meeting Agent → Settings and tap 'Connect Telegram' to link this chat.")
            handled += 1
            continue
        cq = u.get("callback_query")
        if not cq or cq.get("data", "").count(":") != 2:
            continue
        answer, uid, mid = cq["data"].split(":")
        tg("answerCallbackQuery", callback_query_id=cq["id"])
        user = db.get(User, int(uid))
        chat_id = str(cq["message"]["chat"]["id"])
        if not user or user.telegram_chat_id != chat_id:
            continue
        if answer == "yes":
            ok = vexa.set_auto_join(user.tx_key, int(mid), False)
            note = ("👍 Got it, you're joining. The bot will stay out." if ok else
                    "⚠️ Couldn't cancel the bot (the meeting may have already started).")
        else:
            vexa.set_auto_join(user.tx_key, int(mid), True)
            note = "🤖 Okay, the bot will join and send the report card."
        row = db.query(Asked).filter_by(user_id=user.id, vexa_id=int(mid)).first()
        if row:
            row.answer = answer
        tg("editMessageText", chat_id=chat_id, message_id=cq["message"]["message_id"],
           text=cq["message"].get("text", "") + "\n\n" + note)
        handled += 1
    _kv_set(db, "tg_offset", offset)
    return handled


def ask_attendance(db, user):
    if not (tg_token() and user.telegram_chat_id):
        return 0
    asked = 0
    now = datetime.now(timezone.utc)
    for m in vexa.upcoming(user.tx_key):
        mins = (m["start"] - now).total_seconds() / 60
        if not (1 < mins <= user.ask_minutes):
            continue
        if db.query(Asked).filter_by(user_id=user.id, vexa_id=m["id"]).first():
            continue
        when = m["start"].astimezone(_user_tz()).strftime("%I:%M %p")
        tg("sendMessage", chat_id=user.telegram_chat_id,
           text=f"📅 Meeting '{m['title']}' starts at {when}.\nAre you joining it yourself?",
           reply_markup={"inline_keyboard": [[
               {"text": "✅ Yes, I'll join", "callback_data": f"yes:{user.id}:{m['id']}"},
               {"text": "❌ No, send the bot", "callback_data": f"no:{user.id}:{m['id']}"}]]})
        db.add(Asked(user_id=user.id, vexa_id=m["id"]))
        asked += 1
    return asked


def _user_tz():
    from zoneinfo import ZoneInfo
    return ZoneInfo(os.environ.get("DISPLAY_TZ", "Asia/Kolkata"))


# ---------- Reports ----------

def process_finished(db, user):
    done_ids = {v for (v,) in db.query(Meeting.vexa_id).filter(Meeting.user_id == user.id)}
    finished = vexa.meetings(user.tx_key, "completed", 50)
    if not user.baseline_done:
        # First run for this user: don't flood them with reports for old meetings.
        for m in finished:
            if m["id"] not in done_ids:
                db.add(Meeting(user_id=user.id, vexa_id=m["id"], platform=m.get("platform") or "",
                               native_id=m.get("native_meeting_id") or "", title=vexa.title_of(m), status="skipped"))
        user.baseline_done = True
        return 0
    made = 0
    for m in finished:
        if m["id"] in done_ids:
            continue
        row = Meeting(user_id=user.id, vexa_id=m["id"], platform=m.get("platform") or "",
                      native_id=m.get("native_meeting_id") or "", title=vexa.title_of(m))
        try:
            data = vexa.transcript(user.tx_key, m["id"])
            row.transcript_json = json.dumps(data.get("segments", []), ensure_ascii=False, indent=1)
            lines = reports.clean_lines(data)
            if not lines:
                row.status = "empty"
            else:
                row.report_md = reports.make_report(lines)
                row.status = "done"
        except Exception as e:  # keep going for other meetings
            row.status = "error"
            row.report_md = f"Couldn't write this report: {e}"
        db.add(row)
        db.flush()
        if row.status == "done":
            base = os.environ.get("PUBLIC_URL", "").rstrip("/")
            link = f"{base}/report/{row.id}" if base else None
            try:
                mailer.send_report(user.recipients, f"Meeting Report Card - {row.title}",
                                   row.report_md, row.transcript_json, link)
            except Exception:
                traceback.print_exc()
        made += 1
    return made


def tick():
    """One pass over every user. Safe to call often; overlapping calls are skipped."""
    if not _lock.acquire(blocking=False):
        return {"skipped": True}
    stats = {"users": 0, "reports": 0, "asked": 0, "telegram": 0, "errors": 0}
    db = Session()
    try:
        stats["telegram"] = handle_telegram(db)
        db.commit()
        for user in db.query(User).all():
            if not user.ready:
                continue
            stats["users"] += 1
            try:
                stats["reports"] += process_finished(db, user)
                stats["asked"] += ask_attendance(db, user)
                db.commit()
            except Exception:
                db.rollback()
                stats["errors"] += 1
                traceback.print_exc()
    finally:
        Session.remove()
        _lock.release()
    return stats
