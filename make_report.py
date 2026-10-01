import json, requests

JUNK = ["thank you so much for watching", "thanks for watching",
        "ご視聴ありがとうございました", "ありがとうございました"]

data = json.load(open("transcript.json"))
lines = []
for s in data.get("segments", []):
    text = (s.get("text") or "").strip()
    if not text or text.lower().strip(" .。") in [j.strip(" .。") for j in JUNK]:
        continue
    lines.append(f"{s.get('speaker')} ({s.get('language')}): {text}")

transcript = "\n".join(lines)
prompt = f"""You are a meeting assistant. Below is a meeting transcript.
Speakers may use English, Hindi, Telugu or a mix, and some lines may be
mis-transcribed or written in the wrong script. Understand the meaning as best
you can and write everything in clear English.

Write a MEETING REPORT CARD with these sections:
1. Summary (2-4 sentences)
2. Key decisions
3. Action items (task, owner, deadline if mentioned)
4. Who said what (one line per person)
5. Open questions or unclear parts
If something isn't in the transcript, write "None mentioned". Don't invent facts.

TRANSCRIPT:
{transcript}
"""

r = requests.post("http://localhost:11434/api/generate",
                  json={"model": "llama3.1", "prompt": prompt, "stream": False})
report = r.json()["response"]
open("report.md", "w").write(report)
print(report)
print("\nSaved to report.md")
