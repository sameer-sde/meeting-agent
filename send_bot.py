import os, json, requests
from dotenv import load_dotenv

load_dotenv()
KEY = os.environ["VEXA_API_KEY"]
BASE = "https://api.cloud.vexa.ai"

link = input("Paste the meeting link (Teams or Meet): ").strip()
mode = input("Type 1 for English translation, 2 for original language [1]: ").strip() or "1"
task = "translate" if mode == "1" else "transcribe"

r = requests.post(
    f"{BASE}/bots",
    headers={"X-API-Key": KEY, "Content-Type": "application/json"},
    json={"meeting_url": link, "bot_name": "Meeting Agent", "task": task},
)
print("Status:", r.status_code)
data = r.json()
if r.ok:
    json.dump({"platform": data["platform"],
               "native_meeting_id": data["native_meeting_id"]},
              open("last_meeting.json", "w"))
    print(f"Bot sent in '{task}' mode. Admit 'Meeting Agent' when it asks to join.")
else:
    print(json.dumps(data, indent=2))
