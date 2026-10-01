import os, requests
from dotenv import load_dotenv
load_dotenv()
d = requests.get(f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/getUpdates").json()
seen = {}
for u in d.get("result", []):
    m = u.get("message") or {}
    c = m.get("chat") or {}
    if c:
        seen[c["id"]] = c.get("first_name", "")
for cid, name in seen.items():
    print(f"{name}: TELEGRAM_CHAT_ID={cid}")
if not seen:
    print("No messages yet. Open the bot, tap Start, then run this again.")
