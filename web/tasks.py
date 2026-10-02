"""Background work, run every minute: write report cards for finished meetings,
ask Telegram 'are you joining?' before meetings, and handle the answers.

Online, an external cron service calls /cron/tick every minute.
Locally, web/app.py runs tick() in a background thread.
"""
import json
import os
import threading
import traceback
from datetime import datetime, timedelta, timezone

import requests

from . import mailer, reports, vexa
from .models import KV, Asked, ChatMessage, Greeted, Meeting, Session, Task, User

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
    return segs, analytics.attendance(segs, invitees, directory, part_rows, _user_tz()), part_rows, directory


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


def build_extras(db, user, meeting):
    """Split a meeting into chapters and pick out its action items (one AI call). Needs meeting.id."""
    from . import analytics
    rows = analytics.transcript_rows(analytics.segments(meeting), _user_tz(), reports.JUNK)
    if not rows:
        meeting.chapters_json = "[]"
        return 0
    data = reports.make_extras([f"[{i}] {r['speaker']}: {r['text']}" for i, r in enumerate(rows)])
    chapters, last = [], -1
    for c in data["chapters"][:12]:
        try:
            i = min(max(int(c.get("line", 0)), 0), len(rows) - 1)
        except (TypeError, ValueError):
            continue
        title = str(c.get("title") or "").strip()
        if not title or i <= last:
            continue
        last = i
        chapters.append({"title": title[:120], "summary": str(c.get("summary") or "").strip()[:400],
                         "sec": rows[i]["sec"], "time": rows[i]["time"]})
    meeting.chapters_json = json.dumps(chapters, ensure_ascii=False)
    db.query(Task).filter_by(meeting_id=meeting.id, done=False).delete()
    kept = {t.text.lower() for t in db.query(Task).filter_by(meeting_id=meeting.id)}
    made = 0
    for t in data["tasks"][:40]:
        text = str(t.get("task") or "").strip()
        if not text or text.lower() in kept:
            continue
        kept.add(text.lower())
        db.add(Task(user_id=user.id, meeting_id=meeting.id, text=text[:500],
                    owner=str(t.get("owner") or "").strip()[:120], due=str(t.get("due") or "").strip()[:120]))
        made += 1
    return made


def share_report(user, row, emails, link):
    """Email a meeting's report card to other people (the Share button)."""
    facts = _meeting_facts(row)
    tail = f" ({facts})" if facts else ""
    return mailer.send_report(emails, f"Meeting notes: {row.title}", row.report_md, None, None,
                              hello="Hi,",
                              intro=f"{user.name or user.first_name} shared the notes from the meeting "
                                    f"\"{row.title}\"{tail}. These notes were taken by {user.agent_name}.")


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
            fill_times(row, *vexa.times_of(m))
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
        if row.platform and row.native_id:  # a chat started while the meeting was running stays with it
            db.query(ChatMessage).filter_by(user_id=user.id, meeting_id=None,
                                            live_key=f"{row.platform}:{row.native_id}").update({"meeting_id": row.id})
        if row.status == "done":
            try:
                build_extras(db, user, row)
            except Exception:
                traceback.print_exc()  # chapters and tasks can be made later from the report page
            base = os.environ.get("PUBLIC_URL", "").rstrip("/")
            link = f"{base}/report/{row.id}" if base else None
            try:
                email_report(user, row, link)
            except Exception:
                traceback.print_exc()
        made += 1
    return made


def _aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt and dt.tzinfo is None else dt


def fill_times(row, start=None, end=None):
    """Work out when a meeting started and ended, and for how long. Done once per meeting.

    start/end: when the bot joined and left (from Vexa). Without them, the first and last
    spoken line are used. Returns True when something was filled in.
    """
    if row.duration_min is not None:
        return False
    from . import analytics
    tl = analytics.timeline(analytics.segments(row), _user_tz())
    start, end = _aware(start), _aware(end)
    if not (start and end and timedelta(0) < end - start < timedelta(hours=24)):
        start, end = (tl["start_dt"], tl["end_dt"]) if tl else (None, None)
    row.started_at, row.ended_at = start, end
    if start and end:
        row.duration_min = max(round((end - start).total_seconds() / 60), 1)
    else:
        row.duration_min = tl["minutes"] if tl else 0
    return True


def _span(mins):
    return f"{mins} min" if mins < 60 else f"{mins // 60} h {mins % 60:02d} min"


def meeting_times(row):
    """Ready-to-show start, end and duration, e.g. {"start": "2 Oct, 7:30 PM", "end": "7:58 PM",
    "duration": "28 min"}. A value is "" when it isn't known."""
    out = {"start": "", "end": "", "duration": "", "clock": ""}
    try:
        tz = _user_tz()
        a, b = _aware(row.started_at), _aware(row.ended_at)

        def clock(t):
            return t.strftime("%I:%M %p").lstrip("0")
        if a:
            a = a.astimezone(tz)
            out["clock"] = clock(a)
            out["start"] = f"{a.day} {a.strftime('%b')}, {clock(a)}"
        if b:
            b = b.astimezone(tz)
            out["end"] = clock(b) if a and a.date() == b.date() else f"{b.day} {b.strftime('%b')}, {clock(b)}"
        if row.duration_min:
            out["duration"] = _span(row.duration_min)
    except Exception:
        traceback.print_exc()
    return out


def _meeting_facts(row):
    """A short line like "2 Oct, 7:26 AM to 7:30 AM · 4 min · 5 people". Never raises."""
    try:
        from . import analytics
        segs = analytics.segments(row)
        fill_times(row)
        t = meeting_times(row)
        bits = []
        if t["start"]:
            bits.append(t["start"] + (f" to {t['end']}" if t["end"] else ""))
        if t["duration"]:
            bits.append(t["duration"])
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
