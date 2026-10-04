"""Turns a transcript into a report card, using Gemini online (or Ollama when running locally)."""
import os
import time

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


class Busy(Exception):
    """Gemini's free limit was reached; waiting a minute fixes it."""


_no_fast = set()  # models that refused the "answer quickly" setting, so it isn't tried again


def _gemini(prompt, as_json=False, fast=False):
    """fast=True is for the chat: a quicker model if one is set, and no long "thinking" first."""
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    if fast:
        model = os.environ.get("GEMINI_CHAT_MODEL") or model
    config = {"responseMimeType": "application/json"} if as_json else {}
    quick = None
    if fast and model not in _no_fast:
        # Flash models think before answering, which is most of the wait. Turn that right down.
        quick = {"thinkingBudget": 0} if "2.5" in model else {"thinkingLevel": "minimal"}

    def call(cfg):
        r = None
        for attempt in range(3):   # Gemini's free tier is sometimes busy for a moment; try again
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"], "Content-Type": "application/json"},
                json={"contents": [{"parts": [{"text": prompt}]}], **({"generationConfig": cfg} if cfg else {})},
                timeout=120)
            if r.status_code not in (429, 500, 502, 503, 504) or attempt == 2:
                break
            time.sleep(1.5 * (attempt + 1))
        return r

    def text_of(r):
        try:
            parts = r.json()["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError, ValueError, TypeError):
            return ""
        return "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()

    r = call({**config, "thinkingConfig": quick} if quick else config)
    if quick and r.status_code == 400:   # this model doesn't take that setting; ask the normal way
        _no_fast.add(model)
        r = call(config)
    if r.status_code == 429:
        raise Busy("Gemini's free limit is used up for the moment")
    r.raise_for_status()
    out = text_of(r)
    if not out and quick:                # an empty reply with the quick setting: ask the normal way once
        r = call(config)
        r.raise_for_status()
        out = text_of(r)
    if not out:
        raise RuntimeError("Gemini sent back an empty answer")
    return out


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


def _llm(prompt, as_json=False, fast=False):
    if os.environ.get("GEMINI_API_KEY"):
        return _gemini(prompt, as_json, fast)
    return _ollama(prompt)


EXTRAS_PROMPT = """Below is a meeting transcript. Every line starts with its line number in square brackets.
Speakers may use English, Hindi, Telugu or a mix. Write everything in clear English.

Reply with JSON only, in exactly this shape:
{"chapters": [{"title": "short topic name", "line": 0, "summary": "one sentence on what was discussed"}],
 "tasks": [{"task": "what has to be done", "owner": "person's name", "due": "deadline as it was said"}]}

chapters: split the meeting into its main topics, in order. "line" is the number of the line where
that topic starts. Use 1 chapter for a very short meeting and at most 10 for a long one.
tasks: every action item someone agreed to or was asked to do. Use "" for owner or due when it
was not said. Use an empty list when there are none.
Only use what was actually said. Do not invent names, dates or tasks.

TRANSCRIPT:
"""


def _json_from(text):
    """The JSON object inside a model's reply, even when it is wrapped in ``` or extra words."""
    import json
    a, b = text.find("{"), text.rfind("}")
    if a == -1 or b <= a:
        return {}
    try:
        data = json.loads(text[a:b + 1])
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def make_extras(numbered_lines):
    """Chapters and action items as plain data: {"chapters": [...], "tasks": [...]}."""
    data = _json_from(_llm(EXTRAS_PROMPT + "\n".join(numbered_lines), as_json=True))
    return {"chapters": [c for c in data.get("chapters") or [] if isinstance(c, dict)],
            "tasks": [t for t in data.get("tasks") or [] if isinstance(t, dict)]}


def chat_json(prompt):
    """One chat turn. The model is asked for JSON; if it sends plain text, that text is the answer."""
    raw = _llm(prompt, as_json=True, fast=True)
    data = _json_from(raw)
    if not isinstance(data.get("answer"), str):
        data = {"answer": raw}
    return data
