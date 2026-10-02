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

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template,  # noqa: E402
                   request, session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix  # noqa: E402

from . import tasks, vexa  # noqa: E402
from .models import Asked, Meeting, Session, User, init_db  # noqa: E402

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


def _sign_in(email, name="", picture="", sub=None):
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
    user.google_sub = sub or user.google_sub
    user.name = name or user.name
    user.picture = picture or user.picture
    db.commit()
    session.clear()
    session["uid"] = user.id
    session.permanent = True
    return user


# ---------- Sign in ----------

@app.route("/login")
def login():
    if session.get("uid"):
        return redirect(url_for("index"))
    return render_template("login.html", google_on=GOOGLE_ON, local_login=LOCAL_LOGIN)


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
    _sign_in(info["email"].lower(), info.get("name", ""), info.get("picture", ""), info.get("sub"))
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


@app.route("/")
@login_required
@needs_keys
def index():
    u = g.user
    g.api_error = None
    bots, upcoming, cals = [], [], []
    if u.ready:
        bots = _safe(lambda: vexa.running(u.bot_key), [])
        upcoming = _safe(lambda: vexa.upcoming(u.tx_key), [])[:10]
        cals = _safe(lambda: vexa.calendars(u.bot_key), [])
    reports = (Session.query(Meeting).filter(Meeting.user_id == u.id, Meeting.status.in_(["done", "error"]))
               .order_by(Meeting.created_at.desc()).limit(50).all())
    steps, _ = setup_state(u)
    return render_template("dashboard.html", bots=bots, upcoming=upcoming, cals=cals,
                           reports=reports, api_error=g.api_error, ready=u.ready,
                           setup_done=sum(steps.values()), setup_total=len(steps))


@app.post("/send")
@login_required
@needs_keys
def send():
    try:
        _, mid = vexa.send_bot(g.user.bot_key, request.form["link"].strip(), request.form.get("mode", "translate"))
        flash(f"Bot sent to {mid}. Admit 'Meeting Agent' when it asks to join.")
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
                      local.astimezone(timezone.utc))
        flash("Scheduled. The bot joins about a minute before the start.")
    except Exception as e:
        flash(f"Couldn't schedule the meeting: {e}")
    return redirect(url_for("index"))


@app.post("/calendar/connect")
@login_required
@needs_keys
def cal_connect():
    try:
        vexa.cal_connect(g.user.bot_key, request.form["name"].strip(), request.form["ics_url"].strip())
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
    if not m or m.user_id != g.user.id:
        abort(404)
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
    return render_template("report.html", m=m, report_html=html, part_rows=part_rows,
                           mentions=ments, people=people)


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
    w.writerow(["Name", "Email", "Designation", "Status", "Invite response", "Participation %", "Talk time (min)"])
    for p in people:
        w.writerow([p["name"], p["email"], p["designation"], p["status"], p["invite"], p["pct"], p["minutes"]])
    safe = "".join(c if c.isalnum() else "-" for c in (m.title or "meeting"))[:40].strip("-") or "meeting"
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
