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


def _models(fast):
    """Which Gemini models to try, in order. Each model has its own free limit, so when one is
    full (or missing) the next one can still answer.

    Chat tries GEMINI_CHAT_MODEL first when it is set. Everything then goes to GEMINI_MODEL, and
    last to a backup: GEMINI_BACKUP_MODEL, or the "-lite" sister of a "...-flash" model.
    """
    main = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    backup = os.environ.get("GEMINI_BACKUP_MODEL")
    if backup is None and main.endswith("-flash"):
        backup = main + "-lite"
    order = []
    for m in ((os.environ.get("GEMINI_CHAT_MODEL") if fast else None), main, backup):
        if m and m not in order and ("gone", m) not in _no_fast:
            order.append(m)
    return order or [main]


def _quick_for(model):
    """Flash models think before answering, which is most of the wait in a chat. Turn that right down."""
    if model in _no_fast:
        return None
    return {"thinkingBudget": 0} if "2.5" in model else {"thinkingLevel": "minimal"}


def _ask(prompt, config, fast, stream):
    """Send one request, moving down the list of models until one accepts it.

    Returns the successful response. Raises Busy when every model's free limit is full.
    """
    busy, last = False, None
    for model in _models(fast):
        verb = "streamGenerateContent?alt=sse" if stream else "generateContent"
        for attempt in range(4):
            quick = _quick_for(model) if fast else None
            cfg = {**config, **({"thinkingConfig": quick} if quick else {})}
            r = last = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:{verb}",
                headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"], "Content-Type": "application/json"},
                json={"contents": [{"parts": [{"text": prompt}]}], **({"generationConfig": cfg} if cfg else {})},
                stream=stream, timeout=120)
            if r.ok:
                return r
            if r.status_code == 400 and quick:          # this model doesn't take the quick setting
                _no_fast.add(model)
                continue
            if r.status_code == 404:                    # this model doesn't exist for this key: skip it from now on
                _no_fast.add(("gone", model))
                break
            if r.status_code == 429:                    # this model's free limit is full: try the next one
                busy = True
                break
            if r.status_code in (500, 502, 503, 504) and attempt < 2:
                time.sleep(1.5 * (attempt + 1))         # Gemini is busy for a moment; try again
                continue
            break
    if busy:
        raise Busy("Gemini's free limit is used up for the moment")
    last.raise_for_status()
    return last


def _text_of(payload):
    try:
        parts = payload["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, ValueError, TypeError):
        return ""
    return "".join(p.get("text", "") for p in parts if not p.get("thought"))


def _gemini(prompt, as_json=False, fast=False):
    """fast=True is for the chat: a quicker model if one is set, and no long "thinking" first."""
    config = {"responseMimeType": "application/json"} if as_json else {}
    for quickly in ((True, False) if fast else (False,)):
        r = _ask(prompt, config, quickly, stream=False)
        try:
            out = _text_of(r.json()).strip()
        except ValueError:
            out = ""
        if out:
            return out               # an empty reply with the quick setting: ask the normal way once
    raise RuntimeError("Gemini sent back an empty answer")


def chat_stream(prompt):
    """The chat answer, piece by piece as Gemini writes it (so the page can show words right away)."""
    import json
    if not os.environ.get("GEMINI_API_KEY"):
        yield _ollama(prompt)
        return
    r = _ask(prompt, {}, True, stream=True)
    r.encoding = "utf-8"
    for line in r.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        try:
            text = _text_of(json.loads(line[5:]))
        except ValueError:
            continue
        if text:
            yield text


def section_bullets(md, heading):
    """The bullet points under one "## heading" of a report card, without "None mentioned" lines."""
    out, inside = [], False
    for line in (md or "").splitlines():
        t = line.strip()
        if t.startswith("#"):
            inside = t.lstrip("# ").lower().startswith(heading.lower())
            continue
        if inside and t[:1] in "-*•" and len(t) > 2:
            item = t[1:].replace("**", "").strip()
            if item and "none mentioned" not in item.lower() and not item.lower().startswith("no decisions"):
                out.append(item)
    return out


def plain(md, heading=None):
    """A report card (or one "## heading" of it) as plain text, ready to paste into a chat or email."""
    out, inside = [], heading is None
    for line in (md or "").splitlines():
        t = line.strip()
        if t.startswith("#"):
            name = t.lstrip("# ").strip()
            if heading is None:
                out += ["", name.upper()]
            inside = heading is None or name.lower().startswith(heading.lower())
            continue
        if not inside or not t:
            continue
        t = t.replace("**", "").replace("__", "")
        out.append("• " + t[1:].strip() if t[:1] in "-*•" and len(t) > 1 and t[1:2] == " " else t)
    return "\n".join(out).strip()


def _ollama(prompt):
    url = os.environ.get("OLLAMA_URL", "http://localhost:11434")
    model = os.environ.get("OLLAMA_MODEL", "llama3.1")
    r = requests.post(f"{url}/api/generate", json={"model": model, "prompt": prompt, "stream": False}, timeout=300)
    r.raise_for_status()
    return r.json()["response"].strip()


def make_report(lines):
    return _llm(PROMPT + "\n".join(lines))


MOM_PROMPT = """You are writing the Minutes of Meeting (MOM) from a meeting transcript.
Speakers may use English, Hindi, Telugu or a mix, and some lines may be mis-transcribed.
Understand the meaning and write everything in clear, professional English.

Meeting title: {title}
Date: {date}
Attendees (already listed separately, do not repeat them as a section): {attendees}

Write in markdown, in exactly this shape and nothing else:

## Meeting notes
- **<Topic title>:** <one or two sentences: who discussed it (their names) and what was explained or agreed>
    - **<Sub-point title>:** <one or two sentences with the detail, naming who said what>
    - **<Sub-point title>:** <...>
- **<Next topic title>:** <...>
    - **<Sub-point title>:** <...>

## Follow-up tasks
- **<Short task title>:** <what has to be done, as one clear sentence>. (<owner name, or several names separated by commas>)

## Action items
| SN | Action Item | Action By | Target Date | Status |
|---|---|---|---|---|
| 1 | <short action> | <owner> | <deadline as it was said> | Open |

Rules:
- Topics follow the order of the meeting. Use 2 topics for a short meeting and up to 8 for a long one.
- Every topic has 2 to 5 sub-points, indented with exactly 4 spaces.
- Topic and sub-point titles are short (2 to 6 words) and in Title Case.
- Follow-up tasks and Action items list the same tasks. Write "Not set" when no owner or date was said.
  If there were no tasks, write "No follow-up tasks were recorded." under Follow-up tasks and leave out the table.
- Only include what was actually said. Do not invent names, numbers, dates or decisions.

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
 "tasks": [{"task": "what has to be done", "owner": "person's name", "due": "deadline as it was said"}],
 "followups": [{"id": 12, "status": "done", "note": "one short line on what was said about it"}]}

chapters: split the meeting into its main topics, in order. "line" is the number of the line where
that topic starts. Use 1 chapter for a very short meeting and at most 10 for a long one.
tasks: every action item someone agreed to or was asked to do. Use "" for owner or due when it
was not said. Use an empty list when there are none.
followups: PREVIOUS TASKS (below) are promises from the earlier meeting. For each one, say what THIS
meeting shows, using its id and one of these for status:
  "done" (someone said it is finished), "in_progress" (it was talked about but is not finished),
  "blocked" (it is stuck or delayed), "not_mentioned" (nobody talked about it).
Use an empty list when there are no PREVIOUS TASKS.
Only use what was actually said. Do not invent names, dates or tasks.

PREVIOUS TASKS:
{previous}

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


def make_extras(numbered_lines, previous=""):
    """Chapters, action items and follow-ups on earlier tasks: {"chapters", "tasks", "followups"}."""
    prompt = EXTRAS_PROMPT.replace("{previous}", previous or "(none)")
    data = _json_from(_llm(prompt + "\n".join(numbered_lines), as_json=True))
    return {k: [x for x in data.get(k) or [] if isinstance(x, dict)] for k in ("chapters", "tasks", "followups")}


def chat_json(prompt):
    """One chat turn. The model is asked for JSON; if it sends plain text, that text is the answer."""
    raw = _llm(prompt, as_json=True, fast=True)
    data = _json_from(raw)
    if not isinstance(data.get("answer"), str):
        data = {"answer": raw}
    return data
