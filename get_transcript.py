import os, json, requests
from dotenv import load_dotenv

load_dotenv()
KEY = os.environ["VEXA_TX_KEY"]
BASE = "https://api.cloud.vexa.ai"

m = json.load(open("last_meeting.json"))
url = f"{BASE}/transcripts/{m['platform']}/{m['native_meeting_id']}"
r = requests.get(url, headers={"X-API-Key": KEY})
print("Status:", r.status_code)
data = r.json()
json.dump(data, open("transcript.json", "w"), indent=2, ensure_ascii=False)

for s in data.get("segments", []):
    if s.get("completed", True):
        print(f"[{s.get('speaker')}] ({s.get('language')}) {s.get('text')}")
print("\nSaved full transcript to transcript.json")
