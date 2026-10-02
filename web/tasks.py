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
from .models import KV, Asked, Greeted, Meeting, Session, User

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

def meeting_people(db, user, meeting):
    """Attendance rows for a meeting: invitees + speakers + the user's directory."""
    from . import analytics
    from .models import Person
    segs = analytics.segments(meeting)
    try:
        parts = json.loads(meeting.participants_json or "[]")
    except ValueError:
        parts = []
    invitees = [p for p in parts if p.get("source") == "invite"]
    directory = [{"name": p.name, "email": p.email, "designation": p.designation}
                 for p in db.query(Person).filter_by(user_id=user.id)]
    part_rows = analytics.participation(segs)
    return segs, analytics.attendance(segs, invitees, directory, part_rows), part_rows, directory


def build_mom(db, user, meeting, lines=None):
    """Write the Minutes of Meeting text for a meeting with Gemini (or Ollama locally)."""
    _, people, _, _ = meeting_people(db, user, meeting)
    if lines is None:
        from . import analytics
        lines = reports.clean_lines({"segments": analytics.segments(meeting)})
    attendees = [f"{p['name']} ({p['designation']})" if p["designation"] else p["name"]
                 for p in people if p["status"].startswith("Attended")]
    when = (meeting.created_at or datetime.now(timezone.utc)).astimezone(_user_tz()).strftime("%d %B %Y")
    return reports.make_mom(lines, meeting.title, when, attendees)


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
            row.participants_json = json.dumps(
                vexa.participants(user.tx_key, row.platform, row.native_id), ensure_ascii=False)
            if not lines:
                row.status = "empty"
            else:
                row.report_md = reports.make_report(lines)
                row.status = "done"
                try:
                    row.created_at = row.created_at or datetime.now(timezone.utc)
                    row.mom_md = build_mom(db, user, row, lines)
                except Exception:
                    traceback.print_exc()  # the report card still goes out; MOM can be made later
        except Exception as e:  # keep going for other meetings
            row.status = "error"
            row.report_md = f"Couldn't write this report: {e}"
        db.add(row)
        db.flush()
        if row.status == "done":
            base = os.environ.get("PUBLIC_URL", "").rstrip("/")
            link = f"{base}/report/{row.id}" if base else None
            try:
                email_report(user, row, link)
            except Exception:
                traceback.print_exc()
        made += 1
    return made


def _meeting_facts(row):
    """A short line like "2 Oct, 7:26 AM · 4 min · 5 people". Never raises."""
    try:
        from . import analytics
        segs = analytics.segments(row)
        bits = []
        base = analytics.clock_base(segs)
        start = base or row.created_at
        if start:
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            t = start.astimezone(_user_tz())
            bits.append(f"{t.day} {t.strftime('%b')}, {t.strftime('%I:%M %p').lstrip('0')}")
        ends = [float(s.get("end") or s.get("end_time") or 0) for s in segs]
        mins = round(max(ends) / 60) if ends and max(ends) > 0 else 0
        if mins:
            bits.append(f"{mins} min")
        people = len({analytics.speaker_of(s) for s in segs} - {"Unknown speaker"})
        if people:
            bits.append(f"{people} {'person' if people == 1 else 'people'}")
        return " · ".join(bits)
    except Exception:
        return ""


def email_report(user, row, link):
    """The owner gets "Hi <name>"; teammates on the list get a plain "Hi"."""
    facts = _meeting_facts(row)
    tail = f" ({facts})" if facts else ""
    owner = (user.email or "").lower()
    mine = [e for e in user.recipients if e.lower() == owner]
    others = [e for e in user.recipients if e.lower() != owner]
    if mine:
        mailer.send_report(mine, f"{user.first_name}, your report card is ready: {row.title}",
                           row.report_md, row.transcript_json, link,
                           hello=f"Hi {user.first_name},",
                           intro=f"Here's what happened in your meeting \"{row.title}\" today{tail}.")
    if others:
        mailer.send_report(others, f"Meeting report card: {row.title}",
                           row.report_md, row.transcript_json, link,
                           hello="Hi,",
                           intro=f"Here's what happened in the meeting \"{row.title}\" today{tail}. "
                                 f"These notes were taken by {user.agent_name}.")


def greet_meetings(db, user):
    """Once the bot is inside a meeting, say hello in the chat (one time per meeting)."""
    if not user.greets:
        return 0
    sent = 0
    for b in vexa.running(user.bot_key):
        if (b.get("status") or "").lower() != "active":
            continue
        platform, native = b.get("platform"), b.get("native_meeting_id")
        if not platform or not native:
            continue
        mid = b.get("meeting_id") or b.get("id")
        key = f"{platform}:{native}:{mid or datetime.now(timezone.utc).date().isoformat()}"[:400]
        if db.query(Greeted).filter_by(user_id=user.id, key=key).first():
            continue
        absent = False
        if isinstance(mid, int):
            asked = db.query(Asked).filter_by(user_id=user.id, vexa_id=mid).first()
            absent = bool(asked and asked.answer == "no")
        if vexa.chat(user.bot_key, platform, native, user.greeting(absent)):
            db.add(Greeted(user_id=user.id, key=key))
            sent += 1
    return sent


def tick():
    """One pass over every user. Safe to call often; overlapping calls are skipped."""
    if not _lock.acquire(blocking=False):
        return {"skipped": True}
    stats = {"users": 0, "reports": 0, "asked": 0, "greeted": 0, "telegram": 0, "errors": 0}
    db = Session()
    try:
        stats["telegram"] = handle_telegram(db)
        db.commit()
        for user in db.query(User).all():
            if not user.ready:
                continue
            stats["users"] += 1
            try:
                stats["greeted"] += greet_meetings(db, user)
                db.commit()
            except Exception:
                db.rollback()
                traceback.print_exc()
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
