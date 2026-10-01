import os, json, requests
from dotenv import load_dotenv

load_dotenv()
m = json.load(open("last_meeting.json"))
r = requests.delete(
    f"https://api.cloud.vexa.ai/bots/{m['platform']}/{m['native_meeting_id']}",
    headers={"X-API-Key": os.environ["VEXA_API_KEY"]})
print("Bot stopped" if r.ok else f"Error {r.status_code}: {r.text}")
