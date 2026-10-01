import os, json, time, requests
from datetime import datetime
from dotenv import load_dotenv
from agent import clean_lines, make_report, email_report

load_dotenv()
BASE = "https://api.cloud.vexa.ai"
H = {"X-API-Key": os.environ["VEXA_TX_KEY"]}
DONE_FILE = "processed.json"

def load_done():
    return set(json.load(open(DONE_FILE))) if os.path.exists(DONE_FILE) else set()

def save_done(done):
    json.dump(sorted(done), open(DONE_FILE, "w"))

def completed_meetings():
    r = requests.get(f"{BASE}/meetings", headers=H, params={"status": "completed", "limit": 50})
    r.raise_for_status()
    d = r.json()
    return d if isinstance(d, list) else d.get("meetings", [])

def process(m):
    mid = m["id"]
    r = requests.get(f"{BASE}/transcripts/by-id/{mid}", headers=H)
    if not r.ok:
        print(f"  Couldn't get transcript for {mid}: {r.status_code}")
        return
    data = r.json()
    lines = clean_lines(data)
    if not lines:
        print(f"  Meeting {mid}: no speech captured, skipping.")
        return
    title = m.get("title") or m.get("native_meeting_id") or str(mid)
    os.makedirs("reports", exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    t_path = f"reports/{stamp}_{mid}_transcript.json"
    r_path = f"reports/{stamp}_{mid}_report.md"
    json.dump(data, open(t_path, "w"), indent=2, ensure_ascii=False)
    report = make_report(lines)
    open(r_path, "w").write(report)
    email_report(f"Meeting Report Card - {title}", report, t_path)
    print(f"  Report done for '{title}' -> {r_path}")

def main():
    done = load_done()
    if not os.path.exists(DONE_FILE):
        done = {m["id"] for m in completed_meetings()}
        save_done(done)
        print(f"First run: marked {len(done)} old meetings as already handled.")
    print("Watching for finished meetings (Control+C to stop)...")
    while True:
        try:
            for m in completed_meetings():
                if m["id"] not in done:
                    print(datetime.now().strftime("%H:%M"), f"- meeting {m['id']} finished, making report...")
                    process(m)
                    done.add(m["id"])
                    save_done(done)
        except Exception as e:
            print("Check failed, will retry:", e)
        time.sleep(120)

if __name__ == "__main__":
    main()
