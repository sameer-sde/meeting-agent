"""Meeting Agent web app.

Run locally:   python -m web.app        (opens on http://localhost:5050)
Run online:    gunicorn web.app:app     (see README for Render setup)
"""
import os
import threading
import time
from datetime import datetime, timezone
from functools import wraps
from zoneinfo import ZoneInfo

import markdown
from dotenv import load_dotenv

load_dotenv()

from flask import (Flask, Response, abort, flash, g, jsonify, redirect, render_template,  # noqa: E402
                   request, session, stream_with_context, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix  # noqa: E402

from . import chat, tasks, vexa  # noqa: E402
from .models import Asked, Briefed, ChatMessage, Greeted, Meeting, Session, Task, User, init_db  # noqa: E402

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.environ.get("PUBLIC_URL", "").startswith("https"))
init_db()

TZ = ZoneInfo(os.environ.get("DISPLAY_TZ", "Asia/Kolkata"))
GOOGLE_ON = bool(os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"))
LOCAL_LOGIN = os.environ.get("ALLOW_LOCAL_LOGIN", "1" if not GOOGLE_ON else "0") == "1"

oauth = None
if GOOGLE_ON:
    from authlib.integrations.flask_client import OAuth
    oauth = OAuth(app)
    oauth.register(
        "google",
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )


@app.teardown_appcontext
def _remove_session(exc=None):
    Session.remove()


@app.template_filter("local")
def local_time(dt, fmt="%d %b, %I:%M %p"):
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ).strftime(fmt)


@app.context_processor
def _helpers():
    return {"times": tasks.meeting_times}


def _fill_times(rows):
    """Older meetings were saved without start/end times; work them out the first time they're shown."""
    try:
        if sum(tasks.fill_times(r) for r in rows):
            Session.commit()
    except Exception as e:
        Session.rollback()
        print("Couldn't work out meeting times:", e)


@app.template_filter("platform_name")
def platform_name(p):
    return {"google_meet": "Google Meet", "teams": "Teams", "zoom": "Zoom"}.get(p, p or "")


def login_required(view):
    @wraps(view)
    def wrapper(*a, **kw):
        uid = session.get("uid")
        g.user = Session.get(User, uid) if uid else None
        if not g.user:
            return redirect(url_for("login"))
        return view(*a, **kw)
    return wrapper


def needs_keys(view):
    @wraps(view)
    def wrapper(*a, **kw):
        if not g.user.ready:
            return redirect(url_for("setup"))
        return view(*a, **kw)
    return wrapper


def _back(default):
    """Return to the setup page when a form was submitted from there."""
    return redirect(url_for("setup") if request.form.get("next") == "setup" else url_for(default))


def setup_state(user):
    """Which onboarding steps are done. Calendars are optional but counted."""
    real_email = any("@" in e and not e.endswith("@localhost") for e in user.recipients)
    cals = []
    if user.ready:
        try:
            cals = vexa.calendars(user.bot_key)
        except Exception:
            cals = []
    steps = {
        "vexa": user.ready,
        "email": real_email,
        "telegram": bool(user.telegram_chat_id),
        "calendar": bool(cals),
        "first": Session.query(Meeting).filter(Meeting.user_id == user.id,
                                               Meeting.status.in_(["done", "empty"])).count() > 0,
    }
    return steps, cals


def _sign_in(email, name="", picture="", sub=None, verified_by_google=False):
    db = Session()
    user = None
    if sub:
        user = db.query(User).filter_by(google_sub=sub).first()
    if not user:
        user = db.query(User).filter_by(email=email).first()
    if not user:
        user = User(email=email, name=name or email.split("@")[0],
                    report_to="" if email.endswith("@localhost") else email)
        db.add(user)
    if verified_by_google and not user.email_verified:
        # Someone may have started an email sign-up for this address without confirming it.
        # Google proves the real owner is here, so drop that unconfirmed password.
        user.password_hash = None
        user.email_verified = True
    user.google_sub = sub or user.google_sub
    user.name = name or user.name
    user.picture = picture or user.picture
    db.commit()
    session.clear()
    session["uid"] = user.id
    session.permanent = True
    return user


# ---------- Sign in ----------

CODE_MINUTES = 15
MAX_CODE_TRIES = 5


def _auth_page(mode, **kw):
    return render_template("login.html", mode=mode, google_on=GOOGLE_ON, local_login=LOCAL_LOGIN, **kw)


def _valid_email(e):
    import re
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e or ""))


def _send_code(user, purpose):
    """Make a fresh 6-digit code, store only its hash, and email it."""
    import hashlib
    import secrets
    from datetime import timedelta
    from . import mailer
    now = datetime.now(timezone.utc)
    sent = user.code_sent_at
    if sent and sent.tzinfo is None:
        sent = sent.replace(tzinfo=timezone.utc)
    if sent and (now - sent).total_seconds() < 45 and user.code_purpose == purpose:
        return "wait"
    code = f"{secrets.randbelow(1_000_000):06d}"
    user.code_hash = hashlib.sha256(f"{user.id}:{code}".encode()).hexdigest()
    user.code_purpose = purpose
    user.code_expires = now + timedelta(minutes=CODE_MINUTES)
    user.code_attempts = 0
    user.code_sent_at = now
    Session.commit()
    try:
        return "sent" if mailer.send_code(user.email, code, purpose) else "no-mail"
    except Exception as e:
        print("Couldn't send code:", e)
        return "error"


def _check_code(user, purpose, code):
    import hashlib
    import hmac
    if not user or user.code_purpose != purpose or not user.code_hash:
        return "Ask for a new code."
    exp = user.code_expires
    if exp and exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if not exp or exp < datetime.now(timezone.utc):
        return "That code has expired. Ask for a new one."
    if (user.code_attempts or 0) >= MAX_CODE_TRIES:
        return "Too many wrong tries. Ask for a new code."
    user.code_attempts = (user.code_attempts or 0) + 1
    ok = hmac.compare_digest(user.code_hash,
                             hashlib.sha256(f"{user.id}:{(code or '').strip()}".encode()).hexdigest())
    if ok:
        user.code_hash = user.code_purpose = None
    Session.commit()
    return None if ok else "That code isn't right. Check the email and try again."


def _code_message(result, email):
    return {
        "sent": f"We sent a 6-digit code to {email}.",
        "wait": "A code was sent a moment ago. Check your inbox (and spam) before asking again.",
        "no-mail": "Email isn't set up on this server, so codes can't be sent. Use Continue with Google.",
        "error": "We couldn't send the email just now. Try again in a minute.",
    }[result]


@app.route("/home")
def home():
    """The public front page: what Meeting Agent is. Signed-in users can open it here too."""
    return render_template("home.html", signed_in=bool(_signed_in_user()))


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("uid") and request.method == "GET":
        return redirect(url_for("index"))
    if request.method == "POST":
        from werkzeug.security import check_password_hash
        email = request.form.get("email", "").strip().lower()
        pw = request.form.get("password", "")
        user = Session.query(User).filter_by(email=email).first()
        if not user or not user.password_hash or not check_password_hash(user.password_hash, pw):
            if user and not user.password_hash and user.google_sub:
                return _auth_page("login", error="This email signs in with Google. Use Continue with Google, "
                                  "or set a password with Forgot password.", email=email)
            return _auth_page("login", error="Email or password is wrong.", email=email)
        if not user.email_verified:
            flash(_code_message(_send_code(user, "verify"), email))
            return redirect(url_for("verify", email=email))
        _sign_in(user.email)
        return redirect(url_for("index"))
    return _auth_page("login")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        from werkzeug.security import generate_password_hash
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        pw = request.form.get("password", "")
        form = {"name": name, "email": email}
        if not name or not _valid_email(email):
            return _auth_page("signup", error="Enter your name and a valid email.", **form)
        if len(pw) < 8:
            return _auth_page("signup", error="Use a password of at least 8 characters.", **form)
        user = Session.query(User).filter_by(email=email).first()
        if user and (user.email_verified or user.google_sub):
            return _auth_page("signup", error="An account with this email already exists. Sign in, "
                              "or use Forgot password.", **form)
        if not user:
            user = User(email=email, name=name, report_to=email, email_verified=False)
            Session.add(user)
        user.name = name
        user.password_hash = generate_password_hash(pw)
        Session.commit()
        flash(_code_message(_send_code(user, "verify"), email))
        return redirect(url_for("verify", email=email))
    return _auth_page("signup")


@app.route("/verify", methods=["GET", "POST"])
def verify():
    email = (request.values.get("email") or "").strip().lower()
    if request.method == "POST":
        user = Session.query(User).filter_by(email=email).first()
        err = _check_code(user, "verify", request.form.get("code"))
        if err:
            return _auth_page("verify", error=err, email=email)
        user.email_verified = True
        Session.commit()
        _sign_in(user.email)
        flash("Email confirmed. Welcome to Meeting Agent!")
        return redirect(url_for("index"))
    return _auth_page("verify", email=email)


@app.post("/verify/resend")
def verify_resend():
    email = request.form.get("email", "").strip().lower()
    user = Session.query(User).filter_by(email=email).first()
    if user and not user.email_verified:
        flash(_code_message(_send_code(user, "verify"), email))
    return redirect(url_for("verify", email=email))


@app.route("/forgot", methods=["GET", "POST"])
def forgot():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = Session.query(User).filter_by(email=email).first()
        if user:
            result = _send_code(user, "reset")
            if result in ("no-mail", "error"):
                return _auth_page("forgot", error=_code_message(result, email), email=email)
        flash(f"If an account exists for {email}, we sent it a 6-digit code.")
        return redirect(url_for("reset", email=email))
    return _auth_page("forgot")


@app.route("/reset", methods=["GET", "POST"])
def reset():
    email = (request.values.get("email") or "").strip().lower()
    if request.method == "POST":
        from werkzeug.security import generate_password_hash
        pw = request.form.get("password", "")
        if len(pw) < 8:
            return _auth_page("reset", error="Use a password of at least 8 characters.", email=email)
        user = Session.query(User).filter_by(email=email).first()
        err = _check_code(user, "reset", request.form.get("code"))
        if err:
            return _auth_page("reset", error=err, email=email)
        user.password_hash = generate_password_hash(pw)
        user.email_verified = True
        Session.commit()
        _sign_in(user.email)
        flash("Password updated. You're signed in.")
        return redirect(url_for("index"))
    return _auth_page("reset", email=email)


@app.route("/auth/google")
def auth_google():
    if not GOOGLE_ON:
        abort(404)
    return oauth.google.authorize_redirect(url_for("auth_callback", _external=True))


@app.route("/auth/callback")
def auth_callback():
    if not GOOGLE_ON:
        abort(404)
    token = oauth.google.authorize_access_token()
    info = token.get("userinfo") or oauth.google.userinfo()
    if not info.get("email_verified", True):
        flash("Please use a verified Google account.")
        return redirect(url_for("login"))
    _sign_in(info["email"].lower(), info.get("name", ""), info.get("picture", ""), info.get("sub"),
             verified_by_google=True)
    return redirect(url_for("index"))


@app.post("/auth/local")
def auth_local():
    if not LOCAL_LOGIN:
        abort(404)
    _sign_in(os.environ.get("LOCAL_EMAIL", "local@localhost"), "You")
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------- Dashboard ----------

def _safe(fn, default):
    try:
        return fn()
    except Exception as e:
        g.api_error = str(e)
        return default


def _signed_in_user():
    uid = session.get("uid")
    g.user = Session.get(User, uid) if uid else None
    return g.user


@app.route("/")
def index():
    """Visitors see the front page; signed-in users go straight to their dashboard."""
    u = _signed_in_user()
    if not u:
        return render_template("home.html", signed_in=False)
    if not u.ready:
        return redirect(url_for("setup"))
    return _dashboard()


def _dashboard():
    u = g.user
    g.api_error = None
    bots, upcoming, cals = [], [], []
    if u.ready:
        bots = _safe(lambda: vexa.running(u.bot_key), [])
        upcoming = _safe(lambda: vexa.upcoming(u.tx_key), [])[:10]
        cals = _safe(lambda: vexa.calendars(u.bot_key), [])
    q = request.args.get("q", "").strip()[:100]
    found = Session.query(Meeting).filter(Meeting.user_id == u.id, Meeting.status.in_(["done", "error"]))
    if q:
        # the title, the report card and every spoken line (which includes speaker names)
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        found = found.filter(Meeting.title.ilike(like, escape="\\") | Meeting.report_md.ilike(like, escape="\\")
                             | Meeting.transcript_json.ilike(like, escape="\\"))
    reports = found.order_by(Meeting.created_at.desc()).limit(50).all()
    _fill_times(reports)
    steps, _ = setup_state(u)
    return render_template("dashboard.html", bots=bots, upcoming=upcoming, cals=cals,
                           reports=reports, q=q, api_error=g.api_error, ready=u.ready,
                           setup_done=sum(steps.values()), setup_total=len(steps))


@app.post("/send")
@login_required
@needs_keys
def send():
    try:
        _, mid = vexa.send_bot(g.user.bot_key, request.form["link"].strip(), request.form.get("mode", "translate"),
                               g.user.agent_name)
        flash(f"Bot sent to {mid}. Admit '{g.user.agent_name}' when it asks to join.")
    except Exception as e:
        flash(f"Couldn't send the bot: {e}")
    return redirect(url_for("index"))


@app.post("/stop/<platform>/<mid>")
@login_required
@needs_keys
def stop(platform, mid):
    try:
        vexa.stop(g.user.bot_key, platform, mid)
        flash("Bot is leaving. The report card will follow in a few minutes.")
    except Exception as e:
        flash(f"Couldn't stop the bot: {e}")
    return redirect(url_for("index"))


@app.post("/schedule")
@login_required
@needs_keys
def schedule():
    try:
        local = datetime.strptime(request.form["when"], "%Y-%m-%dT%H:%M").replace(tzinfo=TZ)
        vexa.schedule(g.user.tx_key, request.form.get("title", "").strip(), request.form["link"].strip(),
                      local.astimezone(timezone.utc), g.user.agent_name)
        flash("Scheduled. The bot joins about a minute before the start.")
    except Exception as e:
        flash(f"Couldn't schedule the meeting: {e}")
    return redirect(url_for("index"))


@app.post("/calendar/connect")
@login_required
@needs_keys
def cal_connect():
    try:
        vexa.cal_connect(g.user.bot_key, request.form["name"].strip(), request.form["ics_url"].strip(),
                         g.user.agent_name)
        flash("Calendar connected. Meetings with a link will be joined automatically.")
    except Exception as e:
        flash(f"Couldn't connect the calendar: {e}")
    return _back("index")


@app.post("/calendar/<cid>/sync")
@login_required
@needs_keys
def cal_sync(cid):
    try:
        d = vexa.cal_sync(g.user.bot_key, cid)
        if d.get("last_error"):
            flash("Sync problem: " + d["last_error"])
        else:
            c = d.get("counts", {})
            flash(f"Synced: {c.get('created', 0)} new, {c.get('updated', 0)} updated, {c.get('cancelled', 0)} cancelled.")
    except Exception as e:
        flash(f"Couldn't sync: {e}")
    return redirect(url_for("index"))


@app.post("/calendar/<cid>/delete")
@login_required
@needs_keys
def cal_delete(cid):
    try:
        vexa.cal_delete(g.user.bot_key, cid)
        flash("Calendar removed.")
    except Exception as e:
        flash(f"Couldn't remove the calendar: {e}")
    return redirect(url_for("index"))


def _own_meeting(rid):
    m = Session.get(Meeting, rid)
    if not m or m.user_id != g.user.id or m.status == "deleted":
        abort(404)
    _fill_times([m])
    return m


def _people_names(segs, people, directory):
    names = [p["name"] for p in people] + [d["name"] for d in directory]
    names += [s.get("speaker") for s in segs if s.get("speaker")]
    out = []
    for n in names:
        if n and n not in out:
            out.append(n)
    return out


@app.route("/report/<int:rid>")
@login_required
def report(rid):
    from . import analytics
    m = _own_meeting(rid)
    html = markdown.markdown(m.report_md or "", extensions=["nl2br", "sane_lists"])
    segs, people, part_rows, directory = tasks.meeting_people(Session(), g.user, m)
    ments = analytics.mentions(segs, _people_names(segs, people, directory), TZ)
    from .reports import JUNK
    import json
    try:
        chapters = json.loads(m.chapters_json) if m.chapters_json is not None else None
    except ValueError:
        chapters = None
    try:
        followups = json.loads(m.followups_json) if m.followups_json is not None else None
    except ValueError:
        followups = None
    own_tasks = Session.query(Task).filter_by(meeting_id=m.id).all()
    from .reports import section_bullets
    score = analytics.score(part_rows, len(section_bullets(m.report_md, "Key decisions")),
                            [(t.owner, t.due) for t in own_tasks] if m.chapters_json is not None else None,
                            m.duration_min or 0) if segs else None
    mine = (g.user.email or "").lower()
    share_to = [p for p in people if p["email"] and p["email"].lower() != mine]
    from .reports import plain
    copies = {"summary": plain(m.report_md, "Summary"), "actions": plain(m.report_md, "Action items"),
              "all": f"{m.title}\n" + plain(m.report_md)}
    return render_template("report.html", m=m, report_html=html, part_rows=part_rows, copies=copies,
                           mentions=ments, people=people, chapters=chapters, share_to=share_to,
                           followups=followups, score=score,
                           widget=_widget("this meeting", url_for("ask", rid=m.id), url_for("chat_clear", rid=m.id),
                                          {"meeting_id": m.id},
                                          ["Give me a short summary", "What was decided?", "What do I need to do?",
                                           "Who said what?"]),
                           open_tasks=Session.query(Task).filter_by(meeting_id=m.id, done=False).count(),
                           lines=analytics.transcript_rows(segs, TZ, JUNK),
                           timeline=analytics.timeline(segs, TZ))


def _safe_name(m):
    return "".join(c if c.isalnum() else "-" for c in (m.title or "meeting"))[:40].strip("-") or "meeting"


@app.route("/report/<int:rid>/transcript.txt")
@login_required
def transcript_txt(rid):
    from flask import Response
    from . import analytics
    from .reports import JUNK
    m = _own_meeting(rid)
    rows = analytics.transcript_rows(analytics.segments(m), TZ, JUNK)
    head = f"{m.title}\n{local_time(m.created_at, '%d %b %Y, %I:%M %p')}\n\n"
    body = "\n".join(f"[{r['time']}] {r['speaker']}: {r['text']}" for r in rows)
    return Response(head + body + "\n", mimetype="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="transcript-{_safe_name(m)}.txt"'})


@app.post("/report/<int:rid>/delete")
@login_required
def delete_meeting(rid):
    """Remove a meeting's report, transcript, tasks and chat for good.

    The row itself stays as an empty marker, so the every-minute check doesn't see the meeting on
    Vexa again and write a fresh report for it."""
    m = _own_meeting(rid)
    title = m.title
    Session.query(Task).filter_by(meeting_id=m.id).delete()
    Session.query(ChatMessage).filter_by(meeting_id=m.id).delete()
    m.status, m.title = "deleted", ""
    m.report_md = m.transcript_json = m.mom_md = m.participants_json = ""
    m.chapters_json = m.followups_json = m.recording_json = None
    m.started_at = m.ended_at = None
    Session.commit()
    flash(f"“{title}” was deleted.")
    return redirect(url_for("index"))


@app.post("/report/<int:rid>/rename")
@login_required
def rename(rid):
    m = _own_meeting(rid)
    title = " ".join(request.form.get("title", "").split())[:200]
    if title:
        m.title = title
        Session.commit()
        flash("Meeting renamed.")
    return redirect(url_for("report", rid=rid))


@app.post("/report/<int:rid>/extras/generate")
@login_required
def extras_generate(rid):
    m = _own_meeting(rid)
    try:
        made = tasks.build_extras(Session(), g.user, m)
        Session.commit()
        flash(f"Chapters and the promise tracker are ready, and {made} task{'s' if made != 1 else ''} "
              "added to your Tasks page.")
    except Exception as e:
        Session.rollback()
        flash(f"Couldn't find the chapters and tasks: {e}")
    return redirect(url_for("report", rid=rid) + "#chapters")


# ---------- Chat with a meeting ----------



@app.template_filter("chat_html")
def chat_html(text):
    """A bot answer as safe HTML (the model writes light markdown: lists, bold)."""
    import html
    return markdown.markdown(html.escape(text or "", quote=False), extensions=["nl2br", "sane_lists"])


ALL_KEY = "all"          # the chat about every meeting (the bubble on the dashboard and other pages)

app.add_template_filter(chat.bot_html, "bot_html")


def _widget(scope, ask_url, clear_url, where, ideas, opened=False):
    """Everything the chat bubble needs on a page."""
    msgs = Session.query(ChatMessage).filter_by(user_id=g.user.id, **where).order_by(ChatMessage.id).all()
    return {"scope": scope, "ask_url": ask_url, "clear_url": clear_url, "messages": msgs, "ideas": ideas,
            "open": opened}


@app.context_processor
def _default_widget():
    """On pages that aren't about one meeting, the bubble chats about all of them."""
    if not getattr(g, "user", None):
        return {}
    try:
        return {"widget": _widget("your meetings", url_for("chat_all"), url_for("chat_all_clear"),
                                  {"meeting_id": None, "live_key": ALL_KEY},
                                  ["What did I miss this week?", "What tasks are still open?",
                                   "What was decided in my last meeting?"])}
    except Exception as e:
        print("Chat bubble unavailable:", e)
        return {}


def _chat_reply(kind, where, meeting=None, rows=None, material="", empty=None):
    """Answer the posted message, save both sides of the chat, and return JSON for the page."""
    from . import reports
    question = " ".join(((request.get_json(silent=True) or {}).get("question") or "").split())[:500]
    if not question:
        return jsonify(error="Type a question first."), 400
    if not (rows or material):
        return jsonify(error=empty or ("Nothing has been said in this meeting yet." if kind == "live" else
                                       "This meeting has no transcript to ask about.")), 400
    user = g.user

    def lines():
        """One JSON object per line: {"t": "words"} while writing, then {"html": ...} or {"error": ...}."""
        import json
        try:
            for what, value in chat.reply_stream(user, kind, question, where, meeting, rows, material):
                if what == "text":
                    yield json.dumps({"t": value}) + "\n"
                else:
                    yield json.dumps({"answer": value.text, "html": chat.bot_html(value)}) + "\n"
        except reports.Busy:
            yield json.dumps({"error": "The free AI limit is full right now. Wait a minute and ask again. "
                                       "If this keeps showing, today's free limit is used up and it "
                                       "opens again tomorrow."}) + "\n"
        except Exception as e:
            Session.rollback()
            print("Chat failed:", repr(e))
            yield json.dumps({"error": "Couldn't get an answer just now. Try again in a minute."}) + "\n"

    return Response(stream_with_context(lines()), mimetype="application/x-ndjson",
                    headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


@app.post("/chat/ask")
@login_required
def chat_all():
    return _chat_reply("all", {"meeting_id": None, "live_key": ALL_KEY}, material=chat.all_material(g.user),
                       empty="You have no report cards yet. Ask me again after your first meeting.")


@app.post("/chat/clear")
@login_required
def chat_all_clear():
    Session.query(ChatMessage).filter_by(user_id=g.user.id, meeting_id=None, live_key=ALL_KEY).delete()
    Session.commit()
    return redirect(request.referrer or url_for("index"))


@app.post("/chat/action/<int:mid>/<what>")
@login_required
def chat_action(mid, what):
    """Confirm or Cancel on something the chatbot offered to do."""
    msg = Session.get(ChatMessage, mid)
    if not msg or msg.user_id != g.user.id or msg.role != "bot" or what not in ("confirm", "cancel"):
        abort(404)
    reload = chat.decide(g.user, msg, what == "confirm")
    return jsonify(html=chat.bot_html(msg), reload=reload)


@app.post("/report/<int:rid>/ask")
@login_required
def ask(rid):
    from . import analytics, reports
    m = _own_meeting(rid)
    rows = analytics.transcript_rows(analytics.segments(m), TZ, reports.JUNK)
    return _chat_reply("meeting", {"meeting_id": m.id}, meeting=m, rows=rows)


@app.post("/report/<int:rid>/chat/clear")
@login_required
def chat_clear(rid):
    m = _own_meeting(rid)
    Session.query(ChatMessage).filter_by(user_id=g.user.id, meeting_id=m.id).delete()
    Session.commit()
    return redirect(url_for("report", rid=rid))


def _live_key(platform, native):
    import re
    if platform not in ("google_meet", "teams", "zoom") or not re.fullmatch(r"[\w.\-]{1,200}", native):
        abort(404)
    return f"{platform}:{native}"


@app.route("/live/<platform>/<native>")
@login_required
@needs_keys
def live(platform, native):
    key = _live_key(platform, native)
    return render_template("live.html", platform=platform, native=native,
                           widget=_widget("the meeting that is running now",
                                          url_for("live_ask", platform=platform, native=native), None,
                                          {"meeting_id": None, "live_key": key},
                                          ["What have I missed so far?", "What is being discussed right now?",
                                           "Any decisions so far?", "Was my name mentioned?"], opened=True))


@app.post("/live/<platform>/<native>/ask")
@login_required
@needs_keys
def live_ask(platform, native):
    from . import analytics, reports
    key = _live_key(platform, native)
    try:
        data = vexa.live_transcript(g.user.tx_key, platform, native)
    except Exception as e:
        print("Live transcript failed:", e)
        return jsonify(error="Couldn't read this meeting right now. If it has ended, open its report card "
                             "from the dashboard and chat there."), 502
    segs = analytics.normalise([x for x in (data.get("segments") or [])
                                if isinstance(x, dict) and (x.get("text") or "").strip()])
    return _chat_reply("live", {"meeting_id": None, "live_key": key},
                       rows=analytics.transcript_rows(segs, TZ, reports.JUNK))


@app.post("/report/<int:rid>/share")
@login_required
def share(rid):
    m = _own_meeting(rid)
    typed = request.form.get("extra", "").replace(";", ",").replace("\n", ",").split(",")
    emails = []
    for e in request.form.getlist("to") + typed:
        e = e.strip().lower()
        if _valid_email(e) and e not in emails:
            emails.append(e)
    if not emails:
        flash("Choose at least one person, or type an email address.")
    elif len(emails) > 30:
        flash("That's more than 30 people. Send to fewer people at a time.")
    else:
        try:
            base = os.environ.get("PUBLIC_URL", "").rstrip("/")
            if tasks.share_report(g.user, m, emails, f"{base}/report/{m.id}" if base else None):
                flash(f"Report sent to {len(emails)} {'person' if len(emails) == 1 else 'people'}: {', '.join(emails)}")
            else:
                flash("Email isn't set up on this server, so the report couldn't be sent.")
        except Exception as e:
            flash(f"Couldn't send the report: {e}")
    return redirect(url_for("report", rid=rid))


# ---------- Tasks (action items from every meeting) ----------

@app.route("/tasks")
@login_required
def tasks_page():
    show = request.args.get("show", "open")
    q = Session.query(Task).filter_by(user_id=g.user.id)
    counts = {"open": q.filter_by(done=False).count(), "done": q.filter_by(done=True).count()}
    if show in ("open", "done"):
        q = q.filter_by(done=(show == "done"))
    rows = q.order_by(Task.done, Task.created_at.desc(), Task.id).limit(300).all()
    return render_template("tasks.html", rows=rows, show=show, counts=counts)


def _own_task(tid):
    t = Session.get(Task, tid)
    if not t or t.user_id != g.user.id:
        abort(404)
    return t


@app.post("/tasks/<int:tid>/toggle")
@login_required
def task_toggle(tid):
    t = _own_task(tid)
    t.done = not t.done
    Session.commit()
    return redirect(url_for("tasks_page", show=request.form.get("show", "open")))


@app.post("/tasks/<int:tid>/delete")
@login_required
def task_delete(tid):
    Session.delete(_own_task(tid))
    Session.commit()
    return redirect(url_for("tasks_page", show=request.form.get("show", "open")))


# ---------- Recording (audio kept by Vexa) ----------

AUDIO_PIECE = 2 * 1024 * 1024   # sent to the browser in small pieces so it also works on Vercel
LINK_FRESH_SECONDS = 300        # ready-to-play links from Vexa can expire, so look them up again


def _recording(m):
    """Find (and remember) where this meeting's audio is. None when there isn't any."""
    import json
    try:
        ref = json.loads(m.recording_json or "null")
    except ValueError:
        ref = None
    if ref and (vexa.is_vexa_url(ref["url"]) or time.time() - ref.get("at", 0) < LINK_FRESH_SECONDS):
        return ref
    if not m.vexa_id or not g.user.ready:
        return None
    ref = vexa.find_recording({"tx": g.user.tx_key, "bot": g.user.bot_key}, m.vexa_id)
    if ref:
        ref["at"] = int(time.time())
        m.recording_json = json.dumps(ref)
        Session.commit()
    return ref


def _audio_shift(m, ref):
    """Seconds between the start of the recording and second 0 of the transcript."""
    from . import analytics
    base = analytics.clock_base(analytics.segments(m))
    if not base or not ref.get("started"):
        return 0
    try:
        started = datetime.fromisoformat(str(ref["started"]).replace("Z", "+00:00"))
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        shift = (base - started).total_seconds()
        return round(shift, 1) if 0 <= shift < 6 * 3600 else 0
    except ValueError:
        return 0


@app.route("/report/<int:rid>/audio/check")
@login_required
def audio_check(rid):
    m = _own_meeting(rid)
    try:
        ref = _recording(m)
        if not ref:
            return jsonify(ok=False)
        if vexa.is_vexa_url(ref["url"]):
            key = g.user.tx_key if ref.get("key") == "tx" else g.user.bot_key
            if vexa.audio_bytes(key, ref["url"], 0, 2).status_code not in (200, 206):
                return jsonify(ok=False)
        return jsonify(ok=True, shift=_audio_shift(m, ref))
    except Exception as e:
        print("Recording check failed:", e)
        return jsonify(ok=False)


@app.route("/report/<int:rid>/audio")
@login_required
def audio(rid):
    import re
    from flask import Response
    m = _own_meeting(rid)
    ref = _recording(m)
    if not ref:
        abort(404)
    if not vexa.is_vexa_url(ref["url"]):
        return redirect(ref["url"])
    match = re.match(r"bytes=(\d+)-(\d*)", request.headers.get("Range", ""))
    start = int(match.group(1)) if match else 0
    size = AUDIO_PIECE
    if match and match.group(2):
        size = max(1, min(AUDIO_PIECE, int(match.group(2)) - start + 1))
    key = g.user.tx_key if ref.get("key") == "tx" else g.user.bot_key
    up = vexa.audio_bytes(key, ref["url"], start, size)
    if up.status_code not in (200, 206):
        abort(404)
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=3600"}
    if up.headers.get("Content-Range"):
        headers["Content-Range"] = up.headers["Content-Range"]
    return Response(up.content, status=up.status_code, headers=headers,
                    mimetype=up.headers.get("Content-Type", "audio/webm").split(";")[0])


@app.route("/report/<int:rid>/mom")
@login_required
def mom(rid):
    m = _own_meeting(rid)
    _, people, part_rows, _ = tasks.meeting_people(Session(), g.user, m)
    html = markdown.markdown(m.mom_md or "", extensions=["tables", "sane_lists"])
    return render_template("mom.html", m=m, mom_html=html, people=people, prepared_for=g.user)


@app.post("/report/<int:rid>/mom/generate")
@login_required
def mom_generate(rid):
    m = _own_meeting(rid)
    try:
        m.mom_md = tasks.build_mom(Session(), g.user, m)
        Session.commit()
        flash("Minutes of Meeting are ready.")
    except Exception as e:
        flash(f"Couldn't write the Minutes of Meeting: {e}")
    return redirect(url_for("mom", rid=rid))


@app.route("/report/<int:rid>/attendance.csv")
@login_required
def attendance_csv(rid):
    import csv
    import io
    from flask import Response
    m = _own_meeting(rid)
    _, people, _, _ = tasks.meeting_people(Session(), g.user, m)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Name", "Email", "Designation", "Status", "Invite response", "Participation %", "Talk time (min)",
                "First spoke", "Last spoke"])
    for p in people:
        w.writerow([p["name"], p["email"], p["designation"], p["status"], p["invite"], p["pct"], p["minutes"],
                    p.get("first", ""), p.get("last", "")])
    safe = _safe_name(m)
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="attendance-{safe}.csv"'})


# ---------- People directory ----------

@app.route("/people", methods=["GET", "POST"])
@login_required
def people_page():
    from .models import Person
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if name:
            Session.add(Person(user_id=g.user.id, name=name, email=request.form.get("email", "").strip(),
                               designation=request.form.get("designation", "").strip()))
            Session.commit()
            flash(f"Added {name}.")
        return redirect(url_for("people_page"))
    rows = Session.query(Person).filter_by(user_id=g.user.id).order_by(Person.name).all()
    return render_template("people.html", rows=rows)


@app.post("/people/import")
@login_required
def people_import():
    import csv
    import io
    from .models import Person
    f = request.files.get("file")
    if not f:
        flash("Choose a CSV file first.")
        return redirect(url_for("people_page"))
    text = f.read().decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    existing = {(p.email or p.name).lower() for p in Session.query(Person).filter_by(user_id=g.user.id)}
    added = 0
    for row in reader:
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        name = row.get("name") or row.get("full name") or ""
        email = row.get("email") or row.get("email address") or ""
        if not name or (email or name).lower() in existing:
            continue
        Session.add(Person(user_id=g.user.id, name=name, email=email,
                           designation=row.get("designation") or row.get("title") or row.get("role") or ""))
        existing.add((email or name).lower())
        added += 1
    Session.commit()
    flash(f"Imported {added} people." if added else "No new people found. The CSV needs a 'name' column (and optionally 'email', 'designation').")
    return redirect(url_for("people_page"))


@app.post("/people/<int:pid>/delete")
@login_required
def people_delete(pid):
    from .models import Person
    p = Session.get(Person, pid)
    if p and p.user_id == g.user.id:
        Session.delete(p)
        Session.commit()
        flash(f"Removed {p.name}.")
    return redirect(url_for("people_page"))


# ---------- Settings ----------

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    u = g.user
    if request.method == "POST":
        bot = request.form.get("bot_key", "").strip()
        tx = request.form.get("tx_key", "").strip()
        if bot:
            if not bot.startswith("vxa_bot_"):
                flash("The Bot key should start with vxa_bot_. Check you copied the Bot key, not the Transcription key.")
                return _back("settings")
            u.bot_key = bot
        if tx:
            if not tx.startswith("vxa_tx_"):
                flash("The Transcription key should start with vxa_tx_. On Vexa, choose 'Transcription Key' when creating it.")
                return _back("settings")
            u.tx_key = tx
        if "report_to" in request.form:
            u.report_to = request.form["report_to"].strip()
        if request.form.get("bot_form"):
            u.bot_name = request.form.get("bot_name", "").strip()[:60] or None
            u.greet_on = bool(request.form.get("greet_on"))
            u.greet_text = request.form.get("greet_text", "").strip()[:500] or None
            u.brief_on = bool(request.form.get("brief_on"))
            u.always_join = bool(request.form.get("always_join"))
        try:
            if "ask_minutes" in request.form:
                u.ask_minutes = max(2, min(30, int(request.form["ask_minutes"])))
        except ValueError:
            pass
        Session.commit()
        flash("Saved.")
        if request.form.get("next") == "setup":
            return redirect(url_for("setup"))
        return redirect(url_for("index"))
    tg_user = os.environ.get("TELEGRAM_BOT_USERNAME", "")
    tg_link = f"https://t.me/{tg_user}?start={u.tg_token}" if tg_user else None
    return render_template("settings.html", tg_link=tg_link)


@app.route("/setup")
@login_required
def setup():
    steps, cals = setup_state(g.user)
    tg_user = os.environ.get("TELEGRAM_BOT_USERNAME", "")
    tg_link = f"https://t.me/{tg_user}?start={g.user.tg_token}" if tg_user else None
    return render_template("setup.html", steps=steps, cals=cals, tg_link=tg_link,
                           done=sum(steps.values()), total=len(steps))


@app.route("/help")
@login_required
def help_page():
    return render_template("help.html")


# ---------- Help chatbot on the sign-in pages ----------

def is_owner(user):
    """The person who runs this copy of Meeting Agent: OWNER_EMAILS, else the address mail is sent from."""
    listed = os.environ.get("OWNER_EMAILS") or os.environ.get("MAIL_FROM") or os.environ.get("SMTP_USER") or ""
    emails = {e.strip(" <>").lower() for e in listed.replace("<", ",").replace(">", ",").split(",") if "@" in e}
    return bool(user and (user.email or "").lower() in emails)


@app.context_processor
def _owner_flag():
    return {"is_owner": is_owner(getattr(g, "user", None))}


@app.post("/help/ask")
def help_ask():
    """The sign-in chatbot had no written answer for this question: save it for the owner to see."""
    from . import help_bot
    data = request.get_json(silent=True) or {}
    ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or "?")
    try:
        saved = help_bot.save_miss(data.get("question"), ip)
    except Exception as e:
        Session.rollback()
        print("Couldn't save a visitor question:", repr(e))
        saved = False
    return jsonify(saved=saved)


@app.route("/help/questions", methods=["GET", "POST"])
@login_required
def help_questions():
    """What visitors asked the sign-in chatbot. Only the owner can open this."""
    from .models import HelpQuestion
    if not is_owner(g.user):
        abort(404)
    if request.method == "POST":
        Session.query(HelpQuestion).delete()
        Session.commit()
        flash("The list was cleared.")
        return redirect(url_for("help_questions"))
    rows = Session.query(HelpQuestion).order_by(HelpQuestion.id.desc()).limit(300).all()
    return render_template("help_questions.html", rows=rows)


@app.post("/settings/telegram/disconnect")
@login_required
def tg_disconnect():
    g.user.telegram_chat_id = None
    Session.commit()
    flash("Telegram disconnected.")
    return redirect(url_for("settings"))


@app.post("/settings/delete")
@login_required
def delete_account():
    Session.query(Asked).filter_by(user_id=g.user.id).delete()
    Session.query(Briefed).filter_by(user_id=g.user.id).delete()
    Session.query(Greeted).filter_by(user_id=g.user.id).delete()
    Session.query(Task).filter_by(user_id=g.user.id).delete()
    Session.query(ChatMessage).filter_by(user_id=g.user.id).delete()
    Session.delete(g.user)
    Session.commit()
    session.clear()
    return redirect(url_for("login"))


# ---------- Background work ----------

@app.route("/cron/tick")
def cron_tick():
    secret = os.environ.get("CRON_SECRET")
    if not secret or request.args.get("key") != secret:
        abort(403)
    return jsonify(tasks.tick())


@app.route("/healthz")
def healthz():
    return "ok"


def _local_loop():
    while True:
        try:
            tasks.tick()
        except Exception as e:
            print("Background check failed:", e)
        time.sleep(30)


if __name__ == "__main__":
    threading.Thread(target=_local_loop, daemon=True).start()
    print("Meeting Agent running at http://localhost:5050")
    app.run(host="127.0.0.1", port=5050, debug=False)
