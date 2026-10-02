import os, glob, threading, time, requests, markdown
from flask import Flask, request, redirect, render_template_string, abort, url_for
from dotenv import load_dotenv
import watcher, attendance
from agent import send_bot, stop_bot

load_dotenv()
BASE = "https://api.cloud.vexa.ai"
BOT_H = {"X-API-Key": os.environ["VEXA_API_KEY"]}
app = Flask(__name__)

PAGE = r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Meeting Agent</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link href="https://fonts.googleapis.com/css2?family=Newsreader:ital,opsz,wght@0,6..72,400;1,6..72,400&family=Manrope:wght@400;500;600;700&family=JetBrains+Mono&display=swap" rel="stylesheet">
<style>
:root{--bg:#1B1A17;--panel:#23221E;--panel2:#2A2925;--line:#35332E;--ink:#ECE8E1;--muted:#9A958C;--accent:#E08A5A;--live:#6FBF8E;--teams:#9B9CF5;--gold:#E3C26B;--stop:#D9654B}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 Manrope,system-ui,sans-serif}
.mono{font-family:'JetBrains Mono',ui-monospace,monospace;font-size:12px;color:var(--muted)}
.brand{font-family:Newsreader,Georgia,serif;font-size:21px;display:flex;align-items:center;gap:7px;text-decoration:none;color:var(--ink)}
.brand i{width:7px;height:7px;border-radius:50%;background:var(--accent)}
.btn{font:600 14px Manrope,sans-serif;border:0;border-radius:10px;padding:11px 16px;cursor:pointer;background:var(--ink);color:#1B1A17;text-decoration:none;display:inline-block;text-align:center}
.btn.accent{background:var(--accent);color:#1B1A17}.btn.ghost{background:transparent;color:var(--ink);border:1px solid var(--line)}.btn.stop{background:var(--stop);color:#fff}
input[type=text],input[type=datetime-local]{width:100%;font:14px Manrope,sans-serif;padding:11px 13px;border:1px solid var(--line);border-radius:10px;background:var(--panel2);color:var(--ink);color-scheme:dark}
input::placeholder{color:#6F6B64}
.btn:focus-visible,input:focus-visible,a:focus-visible,button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.top{display:flex;align-items:center;justify-content:space-between;padding:22px 36px;border-bottom:1px solid var(--line)}
.hero{padding:40px 36px 8px;display:flex;align-items:flex-end;justify-content:space-between;gap:24px;flex-wrap:wrap}
.hero h1{font-family:Newsreader,Georgia,serif;font-weight:400;font-size:46px;line-height:1.1;margin:0}
.hero h1 em{color:var(--accent)}
.hero p{margin:12px 0 0;color:var(--muted);max-width:52ch}
.status{display:flex;align-items:center;gap:12px;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px 18px}
.dot{width:10px;height:10px;border-radius:50%;background:#5E5A53;flex:none}
.status.on{border-color:#3E5A48}.status.on .dot{background:var(--live);animation:pulse 1.8s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(111,191,142,.55)}70%{box-shadow:0 0 0 10px rgba(111,191,142,0)}100%{box-shadow:0 0 0 0 rgba(111,191,142,0)}}
@media (prefers-reduced-motion:reduce){.status.on .dot{animation:none}}
.toast{margin:20px 36px 0;padding:12px 16px;border-radius:10px;background:#26302A;color:#B9E3C8;border:1px solid #33463A}
.grid{display:grid;grid-template-columns:340px 1fr;gap:32px;padding:28px 36px 56px}
.side{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:22px;align-self:start}
.side section+section{border-top:1px solid var(--line);margin-top:22px;padding-top:22px}
h2{font-family:Newsreader,Georgia,serif;font-weight:400;font-size:22px;margin:0 0 4px}
.hint{color:var(--muted);font-size:13px}p.hint{margin:0 0 12px}
.stack{display:grid;gap:8px}
.seg{display:flex;gap:4px;background:var(--panel2);padding:4px;border-radius:10px;border:1px solid var(--line)}
.seg label{flex:1;text-align:center;padding:7px;border-radius:7px;font-size:13px;cursor:pointer;color:var(--muted);position:relative}
.seg input{position:absolute;opacity:0;inset:0;cursor:pointer}
.seg label:has(input:checked){background:var(--gold);color:#1B1A17;font-weight:600}
.cal{display:flex;align-items:center;justify-content:space-between;gap:8px;margin:8px 0;font-size:14px}.cal form{display:inline}
.link{background:none;border:0;color:var(--ink);font:600 13px Manrope,sans-serif;cursor:pointer;padding:4px 6px;text-decoration:underline;text-underline-offset:3px}.link.red{color:var(--stop)}
.section-title{display:flex;align-items:baseline;justify-content:space-between;margin:0 0 12px}
.list{list-style:none;margin:0 0 40px;padding:0;background:var(--panel);border:1px solid var(--line);border-radius:16px;overflow:hidden}
.list li+li{border-top:1px solid var(--line)}
.row{display:grid;grid-template-columns:62px 1fr auto;gap:16px;align-items:center;padding:14px 18px;color:inherit;text-decoration:none}
a.row:hover{background:var(--panel2)}
.day{text-align:center;line-height:1.05}.day b{display:block;font-family:Newsreader,Georgia,serif;font-weight:400;font-size:26px;color:var(--accent)}.day span{font-size:12px;color:var(--muted)}
.tag{font-size:12px;border-radius:999px;padding:3px 10px;white-space:nowrap;border:1px solid var(--line);color:var(--muted)}
.tag.meet{color:var(--live);border-color:#3E5A48;background:#22302A}.tag.teams{color:var(--teams);border-color:#45467A;background:#26263A}
.empty{color:var(--muted);background:var(--panel);border:1px dashed var(--line);border-radius:16px;padding:18px 20px;margin:0 0 40px}
.reader{max-width:760px;margin:0 auto;padding:32px 20px 64px}.back{display:inline-block;margin:0 0 18px;color:var(--muted);text-decoration:none}
article.report{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:44px 52px;font:18px/1.75 Newsreader,Georgia,serif}
article.report h1,article.report h2,article.report h3{font-family:Newsreader,Georgia,serif;font-weight:400;line-height:1.25}
article.report h1{font-size:34px;margin-top:0}article.report h2{font-size:24px;margin:30px 0 6px;color:var(--accent)}
@media(max-width:900px){.grid{grid-template-columns:1fr;padding:20px 16px 40px}.top,.hero{padding-left:16px;padding-right:16px}.toast{margin:16px 16px 0}.hero h1{font-size:34px}article.report{padding:26px 20px}}
</style></head><body>
<header class="top"><a class="brand" href="{{ url_for('index') }}">Meeting Agent <i></i></a><a class="btn ghost" href="{{ url_for('index') }}">Refresh</a></header>
{% if msg %}<div class="toast" role="status">{{ msg }}</div>{% endif %}
{% if report_html %}
<div class="reader"><a class="back" href="{{ url_for('index') }}">Back to all report cards</a><article class="report">{{ report_html|safe }}</article></div>
{% else %}
<div class="hero"><div><h1>You missed the meeting.<br><em>Not a word of it.</em></h1><p>The bot sits in on your calls, listens in any language, and hands you a report card with what was decided and who owes what.</p></div>
<div class="status {{ 'on' if bots else '' }}"><span class="dot" aria-hidden="true"></span><div>
{% if bots %}<b>Listening in {{ bots|length }} meeting{{ 's' if bots|length > 1 else '' }}</b><div class="mono">{% for b in bots %}{{ b.native_meeting_id }} ({{ b.status }}){% if not loop.last %}, {% endif %}{% endfor %}</div>
{% elif upcoming %}<b>Next: {{ upcoming[0].title }}</b><div class="mono">joins {{ upcoming[0].when }}</div>
{% else %}<b>Idle</b><div class="mono">no meetings right now</div>{% endif %}
</div>{% for b in bots %}<form method="post" action="{{ url_for('stop', platform=b.platform, mid=b.native_meeting_id) }}"><button class="btn stop">Stop bot</button></form>{% endfor %}</div></div>
<div class="grid"><aside class="side">
<section><h2>Join now</h2><p class="hint">Paste a Teams or Google Meet link.</p>
<form method="post" action="{{ url_for('send') }}" class="stack">
<input type="text" name="link" placeholder="Meeting link" required aria-label="Meeting link">
<div class="seg" role="radiogroup" aria-label="Transcript language"><label><input type="radio" name="mode" value="translate" checked>English</label><label><input type="radio" name="mode" value="transcribe">Original</label></div>
<button class="btn accent">Send bot</button></form></section>
<section><h2>Schedule</h2><p class="hint">The bot joins a minute before it starts.</p>
<form method="post" action="{{ url_for('schedule') }}" class="stack">
<input type="text" name="title" placeholder="Title, e.g. Weekly sync" aria-label="Title">
<input type="text" name="link" placeholder="Meeting link" required aria-label="Meeting link">
<input type="datetime-local" name="when" required aria-label="Start time">
<button class="btn ghost">Schedule meeting</button></form></section>
<section><h2>Calendars</h2><p class="hint">Events with a meeting link join automatically.</p>
{% for c in cals %}<div class="cal"><span>{{ c.name }} <span class="hint">{{ 'auto-join on' if c.auto_join else 'auto-join off' }}</span></span><span><form method="post" action="{{ url_for('cal_sync', cid=c.id) }}"><button class="link">Sync</button></form><form method="post" action="{{ url_for('cal_delete', cid=c.id) }}"><button class="link red">Remove</button></form></span></div>{% endfor %}
<form method="post" action="{{ url_for('cal_connect') }}" class="stack" style="margin-top:12px">
<input type="text" name="name" placeholder="Name, e.g. Work" required aria-label="Calendar name">
<input type="text" name="ics_url" placeholder="Secret iCal address" required aria-label="Secret iCal address">
<button class="btn ghost">Connect calendar</button></form></section>
</aside><main>
<div class="section-title"><h2>Coming up</h2><span class="hint">{{ upcoming|length }} scheduled</span></div>
{% if upcoming %}<ul class="list">{% for m in upcoming %}<li class="row"><span class="mono">{{ m.when }}</span><span>{{ m.title }}</span><span class="tag {{ 'meet' if m.platform == 'google_meet' else ('teams' if m.platform == 'teams' else '') }}">{{ 'Google Meet' if m.platform == 'google_meet' else ('Teams' if m.platform == 'teams' else m.platform) }}</span></li>{% endfor %}</ul>
{% else %}<p class="empty">Nothing scheduled. Add a meeting on the left, or connect a calendar.</p>{% endif %}
<div class="section-title"><h2>Report cards</h2><span class="hint">{{ reports|length }} total</span></div>
{% if reports %}<ul class="list">{% for r in reports %}<li><a class="row" href="{{ url_for('report', name=r.file) }}"><span class="day"><b>{{ r.dd }}</b><span>{{ r.mon }}</span></span><span>{{ r.name }}</span><span class="mono">{{ r.time }}</span></a></li>{% endfor %}</ul>
{% else %}<p class="empty">Report cards show up here a few minutes after a meeting ends.</p>{% endif %}
</main></div>
{% endif %}</body></html>'''

def running_bots():
    try:
        r = requests.get(f"{BASE}/bots/status", headers=BOT_H, timeout=10)
        return r.json().get("running", []) if r.ok else []
    except Exception:
        return []

def report_files():
    out = []
    for p in sorted(glob.glob("reports/*_report.md"), reverse=True):
        f = os.path.basename(p)
        parts = f.replace("_report.md", "").split("_")
        label = f"{parts[0]}  {parts[1][:2]}:{parts[1][2:]}  -  meeting {'_'.join(parts[2:])}" if len(parts) >= 3 else f
        out.append({"file": f, "label": label})
    return out

def report_files():
    from datetime import datetime
    out = []
    for p in sorted(glob.glob("reports/*_report.md"), reverse=True):
        f = os.path.basename(p)
        parts = f.replace("_report.md", "").split("_")
        try:
            dt = datetime.strptime(parts[0] + parts[1], "%Y-%m-%d%H%M")
            dd, mon, tm = dt.strftime("%d"), dt.strftime("%b"), dt.strftime("%I:%M %p")
        except Exception:
            dd, mon, tm = "", "", ""
        name = "_".join(parts[2:]) or f
        out.append({"file": f, "dd": dd, "mon": mon, "time": tm,
                    "name": ("Meeting " + name) if name.isdigit() else name})
    return out

@app.route("/")
def index():
    return render_template_string(PAGE, bots=running_bots(), reports=report_files(), cals=calendars(), upcoming=upcoming(),
                                  msg=request.args.get("msg"), report_html=None)

@app.post("/send")
def send():
    try:
        platform, mid = send_bot(request.form["link"].strip(), request.form.get("mode", "translate"))
        msg = f"Bot sent to {mid}. Admit 'Meeting Agent' when it asks to join."
    except SystemExit as e:
        msg = str(e)
    return redirect(url_for("index", msg=msg))

@app.post("/stop/<platform>/<mid>")
def stop(platform, mid):
    stop_bot(platform, mid)
    return redirect(url_for("index", msg=f"Stopping bot in {mid}. Report will follow shortly."))

@app.route("/report/<name>")
def report(name):
    if name not in {r["file"] for r in report_files()}:
        abort(404)
    html = markdown.markdown(open(os.path.join("reports", name)).read(), extensions=["nl2br","sane_lists"])
    return render_template_string(PAGE, report_html=html, msg=None, bots=[], reports=[], cals=[], upcoming=[])

TX_H = {"X-API-Key": os.environ["VEXA_TX_KEY"]}

def calendars():
    try:
        r = requests.get(f"{BASE}/user/calendars", headers=BOT_H, timeout=10)
        d = r.json() if r.ok else []
        return d if isinstance(d, list) else d.get("calendars", d.get("items", []))
    except Exception:
        return []

def upcoming():
    from datetime import datetime
    try:
        r = requests.get(f"{BASE}/meetings", headers=TX_H, params={"status": "scheduled", "limit": 20}, timeout=10)
        d = r.json() if r.ok else []
        rows = d if isinstance(d, list) else d.get("meetings", [])
    except Exception:
        return []
    out = []
    for m in rows:
        if m.get("platform") in (None, "unknown"):
            continue
        when = (m.get("data") or {}).get("scheduled_at") or m.get("scheduled_at") or ""
        try:
            when = datetime.fromisoformat(when.replace("Z", "+00:00")).astimezone().strftime("%d %b, %I:%M %p")
        except Exception:
            pass
        out.append({"when": when, "title": ((m.get("data") or {}).get("title") or m.get("title") or m.get("native_meeting_id") or "Meeting").lstrip(": "), "platform": m.get("platform")})
    return out

@app.post("/calendar/connect")
def cal_connect():
    r = requests.post(f"{BASE}/user/calendars", headers=BOT_H, timeout=20,
        json={"name": request.form["name"].strip(), "ics_url": request.form["ics_url"].strip(),
              "auto_join": True, "bot_name": "Meeting Agent"})
    msg = "Calendar connected! Meetings will be joined automatically." if r.ok else f"Could not connect: {r.text}"
    return redirect(url_for("index", msg=msg))

@app.post("/calendar/<cid>/sync")
def cal_sync(cid):
    r = requests.post(f"{BASE}/user/calendars/{cid}/sync", headers=BOT_H, timeout=30)
    d = r.json() if r.ok else {}
    if d.get("last_error"):
        msg = "Sync problem: " + d["last_error"]
    elif r.ok:
        c = d.get("counts", {})
        msg = f"Synced: {c.get('created',0)} new, {c.get('updated',0)} updated, {c.get('cancelled',0)} cancelled."
    else:
        msg = f"Sync failed: {r.text}"
    return redirect(url_for("index", msg=msg))

@app.post("/calendar/<cid>/delete")
def cal_delete(cid):
    r = requests.delete(f"{BASE}/user/calendars/{cid}", headers=BOT_H, timeout=20)
    return redirect(url_for("index", msg="Calendar disconnected." if r.ok else f"Error: {r.text}"))

@app.post("/schedule")
def schedule():
    from datetime import datetime, timezone
    when = datetime.strptime(request.form["when"], "%Y-%m-%dT%H:%M").astimezone().astimezone(timezone.utc)
    r = requests.post(f"{BASE}/meetings", headers=TX_H, timeout=20, json={
        "title": request.form.get("title", "").strip() or "Meeting",
        "scheduled_at": when.isoformat(), "meeting_url": request.form["link"].strip(), "auto_join": True})
    msg = "Scheduled! The bot will join about 1 minute before start." if r.ok else f"Could not schedule: {r.text}"
    return redirect(url_for("index", msg=msg))

def watch_loop():
    done = watcher.load_done()
    if not os.path.exists(watcher.DONE_FILE):
        done = {m["id"] for m in watcher.completed_meetings()}
        watcher.save_done(done)
    while True:
        try:
            for m in watcher.completed_meetings():
                if m["id"] not in done:
                    print("Meeting", m["id"], "finished - making report...")
                    watcher.process(m)
                    done.add(m["id"])
                    watcher.save_done(done)
        except Exception as e:
            print("Watcher check failed, will retry:", e)
        time.sleep(120)

if __name__ == "__main__":
    threading.Thread(target=watch_loop, daemon=True).start()
    threading.Thread(target=attendance.loop, daemon=True).start()
    print("Dashboard running at http://localhost:5050")
    app.run(host="127.0.0.1", port=5050, debug=False)
