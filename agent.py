import os, sys, json, time, smtplib, requests, markdown
from datetime import datetime
from email.message import EmailMessage
from dotenv import load_dotenv

load_dotenv()
BASE = "https://api.cloud.vexa.ai"
BOT_KEY = os.environ["VEXA_API_KEY"]
TX_KEY = os.environ["VEXA_TX_KEY"]
JUNK = {"thank you so much for watching", "thanks for watching",
        "ご視聴ありがとうございました", "ありがとうございました"}

def send_bot(link, task):
    r = requests.post(f"{BASE}/bots",
        headers={"X-API-Key": BOT_KEY, "Content-Type": "application/json"},
        json={"meeting_url": link, "bot_name": "Meeting Agent", "task": task})
    if not r.ok:
        sys.exit(f"Could not send bot: {r.status_code} {r.text}")
    d = r.json()
    return d["platform"], d["native_meeting_id"]

def still_running(platform, mid):
    r = requests.get(f"{BASE}/bots/status", headers={"X-API-Key": BOT_KEY})
    running = r.json().get("running", []) if r.ok else []
    return any(b["platform"] == platform and b["native_meeting_id"] == mid for b in running)

def stop_bot(platform, mid):
    requests.delete(f"{BASE}/bots/{platform}/{mid}", headers={"X-API-Key": BOT_KEY})

def get_transcript(platform, mid):
    r = requests.get(f"{BASE}/transcripts/{platform}/{mid}", headers={"X-API-Key": TX_KEY})
    r.raise_for_status()
    return r.json()

def clean_lines(data):
    lines = []
    for s in data.get("segments", []):
        text = (s.get("text") or "").strip()
        if text and text.lower().strip(" .。") not in JUNK:
            lines.append(f"{s.get('speaker')} ({s.get('language')}): {text}")
    return lines

def make_report(lines):
    prompt = f"""You are a meeting assistant. Below is a meeting transcript.
Speakers may use English, Hindi, Telugu or a mix, and some lines may be
mis-transcribed. Understand the meaning and write everything in clear English.

Write a MEETING REPORT CARD with these sections:
1. Summary (2-4 sentences)
2. Key decisions
3. Action items (task, owner, deadline if mentioned)
4. Who said what (one line per person)
5. Open questions or unclear parts
If something isn't in the transcript, write "None mentioned".
Only report what was actually said. Do not guess reasons, feelings or
motives, and do not add questions that nobody asked.
Use markdown: ## for section titles and - for bullet points.

TRANSCRIPT:
""" + "\n".join(lines)
    r = requests.post("http://localhost:11434/api/generate",
        json={"model": "llama3.1", "prompt": prompt, "stream": False})
    return r.json()["response"]

def email_report(subject, body, transcript_path):
    pw = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not pw or "PUT_APP" in pw:
        print("Email not set up yet - skipping email.")
        return
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, os.environ["GMAIL_ADDRESS"], os.environ["REPORT_TO"]
    msg.set_content(body)
    html = markdown.markdown(body)
    msg.add_alternative(f"""<div style="font-family:Arial,sans-serif;max-width:640px;margin:auto;color:#222">
<div style="background:#1a73e8;color:#fff;padding:16px 20px;border-radius:8px 8px 0 0">
<h2 style="margin:0">Meeting Report Card</h2>
<div style="opacity:.9;font-size:13px">{subject}</div></div>
<div style="border:1px solid #e0e0e0;border-top:none;padding:20px;border-radius:0 0 8px 8px;line-height:1.5">{html}</div>
<p style="font-size:12px;color:#888;text-align:center">Sent by Meeting Agent</p></div>""", subtype="html")
    msg.add_attachment(open(transcript_path, "rb").read(),
        maintype="application", subtype="json", filename="transcript.json")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(os.environ["GMAIL_ADDRESS"], pw)
        s.send_message(msg)
    print("Report emailed to", os.environ["REPORT_TO"])

def main():
    link = input("Paste the meeting link: ").strip()
    mode = input("1 = English translation, 2 = original language [1]: ").strip() or "1"
    task = "translate" if mode == "1" else "transcribe"

    platform, mid = send_bot(link, task)
    print(f"Bot sent ({task}). Admit 'Meeting Agent' when it asks to join.")
    print("I'll wait until the meeting ends. Press Control+C to stop early.\n")

    try:
        time.sleep(60)
        while still_running(platform, mid):
            print(datetime.now().strftime("%H:%M"), "- meeting in progress...")
            time.sleep(30)
    except KeyboardInterrupt:
        print("\nStopping the bot...")
        stop_bot(platform, mid)
        time.sleep(5)

    print("Meeting ended. Getting transcript...")
    data = get_transcript(platform, mid)
    lines = clean_lines(data)
    if not lines:
        sys.exit("No speech was captured, so there's no report.")

    os.makedirs("reports", exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    t_path = f"reports/{stamp}_{mid}_transcript.json"
    r_path = f"reports/{stamp}_{mid}_report.md"
    json.dump(data, open(t_path, "w"), indent=2, ensure_ascii=False)

    print("Writing report card...")
    report = make_report(lines)
    open(r_path, "w").write(report)
    print("\n" + report + f"\n\nSaved: {r_path}")

    email_report(f"Meeting Report Card - {datetime.now().strftime('%d %b %Y, %I:%M %p')}",
                 report, t_path)

if __name__ == "__main__":
    main()
