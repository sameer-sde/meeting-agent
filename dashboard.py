import os, glob, threading, time, requests, markdown
from flask import Flask, request, redirect, render_template_string, abort, url_for
from dotenv import load_dotenv
import watcher, attendance
from agent import send_bot, stop_bot

load_dotenv()
BASE = "https://api.cloud.vexa.ai"
BOT_H = {"X-API-Key": os.environ["VEXA_API_KEY"]}
app = Flask(__name__)

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Meeting Agent</title><style>
body{font-family:-apple-system,Arial,sans-serif;background:#f5f7fb;margin:0;color:#222}
header{background:#1a73e8;color:#fff;padding:18px 24px}header h1{margin:0;font-size:22px}
main{max-width:900px;margin:24px auto;padding:0 16px}
.card{background:#fff;border-radius:10px;padding:20px;margin-bottom:20px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
h2{font-size:17px;margin:0 0 14px}input[type=text]{width:100%;padding:10px;border:1px solid #ccc;border-radius:6px;box-sizing:border-box}
.row{display:flex;gap:10px;align-items:center;margin-top:10px;flex-wrap:wrap}
button{background:#1a73e8;color:#fff;border:0;padding:9px 16px;border-radius:6px;cursor:pointer}
button.stop{background:#d93025}.msg{background:#e6f4ea;padding:10px 14px;border-radius:6px;margin-bottom:16px}
table{width:100%;border-collapse:collapse}td{padding:8px 4px;border-bottom:1px solid #eee}
a{color:#1a73e8;text-decoration:none}.muted{color:#888}.report{line-height:1.6}
</style></head><body><header><h1>Meeting Agent</h1></header><main>
{% if msg %}<div class="msg">{{ msg }}</div>{% endif %}
{% if report_html %}
<div class="card"><a href="{{ url_for('index') }}">&larr; Back</a><div class="report">{{ report_html|safe }}</div></div>
{% else %}
<div class="card"><h2>Send the bot to a meeting</h2>
<form method="post" action="{{ url_for('send') }}">
<input type="text" name="link" placeholder="Paste a Teams or Google Meet link" required>
<div class="row"><label><input type="radio" name="mode" value="translate" checked> English translation</label>
<label><input type="radio" name="mode" value="transcribe"> Original language</label>
<button type="submit">Send bot</button></div></form></div>
<div class="card"><h2>Calendar auto-join</h2>
<form method="post" action="{{ url_for('cal_connect') }}">
<input type="text" name="name" placeholder="Name, e.g. Personal or Work" required>
<div class="row"><input type="text" name="ics_url" placeholder="Paste your calendar's SECRET iCal address" required></div>
<div class="row"><button type="submit">Connect calendar</button></div></form>
{% if cals %}<table style="margin-top:14px">{% for c in cals %}<tr><td><b>{{ c.name }}</b></td>
<td class="muted">{{ 'Auto-join ON' if c.auto_join else 'Auto-join OFF' }}</td>
<td><form method="post" action="{{ url_for('cal_sync', cid=c.id) }}"><button>Sync now</button></form></td>
<td><form method="post" action="{{ url_for('cal_delete', cid=c.id) }}"><button class="stop">Disconnect</button></form></td></tr>{% endfor %}</table>{% endif %}
<h2 style="margin-top:18px">Upcoming meetings (bot will join)</h2>
{% if upcoming %}<table>{% for m in upcoming %}<tr><td>{{ m.when }}</td><td>{{ m.title }}</td><td class="muted">{{ m.platform }}</td></tr>{% endfor %}</table>
{% else %}<p class="muted">No upcoming meetings with a Meet/Teams link.</p>{% endif %}</div>
<div class="card"><h2>Bots in meetings now</h2>
{% if bots %}<table>{% for b in bots %}<tr><td>{{ b.platform }}</td><td>{{ b.native_meeting_id }}</td><td>{{ b.status }}</td>
<td><form method="post" action="{{ url_for('stop', platform=b.platform, mid=b.native_meeting_id) }}"><button class="stop">Stop</button></form></td></tr>{% endfor %}</table>
{% else %}<p class="muted">No bots running.</p>{% endif %}</div>
<div class="card"><h2>Report cards</h2>
{% if reports %}<table>{% for r in reports %}<tr><td><a href="{{ url_for('report', name=r.file) }}">{{ r.label }}</a></td></tr>{% endfor %}</table>
{% else %}<p class="muted">No reports yet.</p>{% endif %}
<p class="muted">Reports are created automatically a few minutes after a meeting ends.</p></div>
{% endif %}</main></body></html>"""

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
        out.append({"when": when, "title": m.get("title") or m.get("native_meeting_id"), "platform": m.get("platform")})
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
