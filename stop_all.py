import os, requests
from dotenv import load_dotenv
load_dotenv()
H = {"X-API-Key": os.environ["VEXA_API_KEY"]}
B = "https://api.cloud.vexa.ai"
running = requests.get(f"{B}/bots/status", headers=H).json().get("running", [])
for b in running:
    requests.delete(f"{B}/bots/{b['platform']}/{b['native_meeting_id']}", headers=H)
    print("Stopped bot in", b["native_meeting_id"])
print(f"Done. {len(running)} bot(s) stopped.")
