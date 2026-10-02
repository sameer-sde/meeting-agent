"""Turns a transcript into a report card, using Gemini online (or Ollama when running locally)."""
import os

import requests

# Phrases Whisper invents on silence; never worth reporting.
JUNK = {"thank you so much for watching", "thanks for watching", "thank you for watching",
        "ご視聴ありがとうございました", "ありがとうございました"}

PROMPT = """You are a meeting assistant. Below is a meeting transcript.
Speakers may use English, Hindi, Telugu or a mix, and some lines may be
mis-transcribed or written in the wrong script. Understand the meaning and
write everything in clear English.

Write a MEETING REPORT CARD with these sections, using markdown (## for each
section title and - for bullet points):
## Summary (2-4 sentences)
## Key decisions
## Action items (task, owner, deadline if mentioned)
## Who said what (one line per person)
## Open questions
If something isn't in the transcript, write "None mentioned".
Only report what was actually said. Do not guess reasons, feelings or motives,
and do not add questions that nobody asked.

TRANSCRIPT:
"""


def clean_lines(data):
    lines = []
    for s in data.get("segments", []) or []:
        text = (s.get("text") or "").strip()
        if not text or text.lower().strip(" .。!") in JUNK:
            continue
        speaker = s.get("speaker") or "Unknown speaker"
        lang = s.get("language") or "?"
        lines.append(f"{speaker} ({lang}): {text}")
    return lines


def _gemini(prompt):
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"], "Content-Type": "application/json"},
        json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=120)
    r.raise_for_status()
    parts = r.json()["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts).strip()


def _ollama(prompt):
    url = os.environ.get("OLLAMA_URL", "http://localhost:11434")
    model = os.environ.get("OLLAMA_MODEL", "llama3.1")
    r = requests.post(f"{url}/api/generate", json={"model": model, "prompt": prompt, "stream": False}, timeout=300)
    r.raise_for_status()
    return r.json()["response"].strip()


def make_report(lines):
    return _llm(PROMPT + "\n".join(lines))


MOM_PROMPT = """You are writing formal Minutes of Meeting (MOM) from a meeting transcript.
Speakers may use English, Hindi, Telugu or a mix, and some lines may be mis-transcribed.
Understand the meaning and write everything in clear, professional English.

Meeting title: {title}
Date: {date}
Attendees (already listed separately, do not repeat as a section): {attendees}

Write the MOM in markdown with exactly these sections:
## Purpose of the meeting
One or two sentences.
## Agenda covered
A numbered list of the topics discussed, in the order they came up.
## Discussion
For each agenda item, a ### heading with the topic and 2-5 bullet points of what was said,
naming who raised each point.
## Decisions
A numbered list. If none, write "No decisions were recorded."
## Action items
A markdown table with columns: No. | Action | Owner | Due date
Use "Not set" when no owner or date was mentioned.
## Next steps
Bullet points, including any next meeting mentioned.

Only include what was actually said. Do not invent names, numbers, dates or decisions.

TRANSCRIPT:
"""


def make_mom(lines, title, date, attendees):
    prompt = MOM_PROMPT.format(title=title, date=date, attendees=", ".join(attendees) or "Not recorded")
    return _llm(prompt + "\n".join(lines))


def _llm(prompt):
    if os.environ.get("GEMINI_API_KEY"):
        return _gemini(prompt)
    return _ollama(prompt)
