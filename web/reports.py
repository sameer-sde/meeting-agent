"""Turns a transcript into a report card, using Gemini online (or Ollama when running locally)."""
import os
import re
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


def words_note(words):
    """A line telling the AI how team names and terms are spelled, or "" when there are none."""
    items = [w.strip() for w in re.split(r"[\n,]+", words or "") if w.strip()][:80]
    if not items:
        return ""
    return ("\nNAMES AND TERMS used by this team, spelled correctly. The transcript may spell or hear them "
            "wrongly; always write them exactly like this: " + ", ".join(items) + "\n")


def _with_words(prompt, words):
    note = words_note(words)
    return prompt.replace("\nTRANSCRIPT:\n", note + "\nTRANSCRIPT:\n", 1) if note else prompt


def make_report(lines, words=""):
    return _llm(_with_words(PROMPT, words) + "\n".join(lines))


MOM_PROMPT = """You are writing the Minutes of Meeting (MOM) from a meeting transcript.
Speakers may use English, Hindi, Telugu or a mix, and some lines may be mis-transcribed.
Understand the meaning and write everything in clear, professional English.

Meeting title: {title}
Date: {date}
Attendees (already listed separately, do not repeat them as a section): {attendees}

Write in markdown, in exactly this shape and nothing else:

{shape}
{agenda_block}## Open questions
- <a question someone asked that was not answered or settled in the meeting> (asked by <name>)

## Follow-up tasks
- **<Short task title>:** <what has to be done, as one clear sentence>. (<owner name, or several names separated by commas>)

## Action items
| SN | Action Item | Action By | Target Date | Status |
|---|---|---|---|---|
| 1 | <short action> | <owner> | <deadline as it was said> | Open |

Rules:
{shape_rules}
- Follow-up tasks and Action items list the same tasks. Write "Not set" when no owner or date was said.
  If there were no tasks, write "No follow-up tasks were recorded." under Follow-up tasks and leave out the table.
- Open questions: only real questions left without an answer or decision. If there were none, write
  "No open questions." under that heading.
- Only include what was actually said. Do not invent names, numbers, dates or decisions.

TRANSCRIPT:
"""

KINDS = {"standard": "Standard (Teams style)", "status": "Daily status update", "interview": "Interview"}

SHAPES = {
    "standard": ("""## Meeting notes
- **<Topic title>:** <one or two sentences: who discussed it (their names) and what was explained or agreed>
    - **<Sub-point title>:** <one or two sentences with the detail, naming who said what>
    - **<Sub-point title>:** <...>
- **<Next topic title>:** <...>
    - **<Sub-point title>:** <...>
""", """- Topics follow the order of the meeting. Use 2 topics for a short meeting and up to 8 for a long one.
- Every topic has 2 to 5 sub-points, indented with exactly 4 spaces.
- Topic and sub-point titles are short (2 to 6 words) and in Title Case."""),
    "status": ("""## Team updates
- **<Person's name>:**
    - **Done:** <what they finished since the last update>
    - **Next:** <what they will work on next>
    - **Blockers:** <what is stopping them, or "None">
- **<Next person's name>:**
    - **Done:** <...>
    - **Next:** <...>
    - **Blockers:** <...>

## Blockers to follow up
- **<Short blocker title>:** <what is blocked, who is affected and who can help> (<person>)

## Decisions
- <what was decided>
""", """- This is a daily status / stand-up meeting. Give one entry per person who gave an update, in the order they spoke.
- Done, Next and Blockers are indented with exactly 4 spaces. Write "Not mentioned" when a person did not say it.
- Under "Blockers to follow up" list only real blockers; write "No blockers." when there were none.
- Under "Decisions" write "No decisions were recorded." when there were none."""),
    "interview": ("""## Candidate summary
<two or three sentences: who the candidate is, the role, and the overall impression from what was said>

## Questions and answers
- **<Topic of the question>:** <what was asked and, in one or two sentences, how the candidate answered>

## Strengths
- <a strength shown in the answers>

## Concerns
- <a gap or concern from the answers, or "None noted.">

## Next steps
- <what the interviewers said happens next>
""", """- This is a job interview. Use the candidate's and interviewers' names as said.
- Only report what was said. Do not score or judge the candidate beyond what the interviewers themselves said."""),
}


def kind_of(title, chosen=None):
    """The meeting type: the one picked by the user, else a guess from the title."""
    if chosen in SHAPES:
        return chosen
    t = (title or "").lower()
    if re.search(r"\b(status|stand-?up|daily|scrum|sync-?up)\b", t):
        return "status"
    if re.search(r"\binterview", t):
        return "interview"
    return "standard"


AGENDA_BLOCK = """## Agenda check
- **<agenda point, as written in the agenda>:** <Covered | Partly covered | Not covered>. <one short sentence on what was said, or "Not discussed.">

"""

AGENDA_RULES = """
AGENDA (written before the meeting, one point per line). Under "## Agenda check" list every agenda
point in this order and say whether it was Covered, Partly covered or Not covered in the meeting:
{agenda}
"""


def make_mom(lines, title, date, attendees, agenda=None, kind="standard", words=""):
    points = [p.strip(" -*•\t") for p in (agenda or "").splitlines() if p.strip(" -*•\t")]
    shape, shape_rules = SHAPES.get(kind) or SHAPES["standard"]
    prompt = MOM_PROMPT.format(title=title, date=date, attendees=", ".join(attendees) or "Not recorded",
                               agenda_block=AGENDA_BLOCK if points else "", shape=shape, shape_rules=shape_rules)
    prompt = _with_words(prompt, words)
    if points:
        prompt = prompt.replace("\nTRANSCRIPT:\n", AGENDA_RULES.replace("{agenda}", "\n".join(f"- {p}" for p in points))
                                + "\nTRANSCRIPT:\n", 1)
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


def make_extras(numbered_lines, previous="", words=""):
    """Chapters, action items and follow-ups on earlier tasks: {"chapters", "tasks", "followups"}."""
    prompt = _with_words(EXTRAS_PROMPT.replace("{previous}", previous or "(none)"), words)
    data = _json_from(_llm(prompt + "\n".join(numbered_lines), as_json=True))
    return {k: [x for x in data.get(k) or [] if isinstance(x, dict)] for k in ("chapters", "tasks", "followups")}


FOLLOWUP_PROMPT = """Write a short follow-up email after a meeting. It is sent by {sender} to the people who
attended. Use only the meeting notes below; do not invent anything.

Meeting: {title}
Date: {date}

Write plain text (no markdown, no ** or #), in this order:
Subject: <short subject line>

Hi all,

<one sentence thanking everyone and naming the meeting>

<"Key points:" followed by 2 to 5 lines starting with "- ", the main decisions or outcomes>

<"Action items:" followed by one line per task starting with "- ": task – owner – due date (leave out
the parts that were not set). Leave this part out if there are no tasks.>

<one sentence on next steps or the next meeting, only if the notes mention it>

<one short closing line inviting corrections>

Regards,
{sender}

Keep it under 180 words, polite and professional. Write everything in clear English.
{words}
MEETING NOTES:
{notes}

TASKS:
{tasks}
"""


def make_followup(title, date, sender, notes, tasks, words=""):
    prompt = (FOLLOWUP_PROMPT.replace("{title}", title or "the meeting").replace("{date}", date)
              .replace("{sender}", sender or "me").replace("{words}", words_note(words))
              .replace("{notes}", notes or "(none)").replace("{tasks}", tasks or "(none)"))
    text = _llm(prompt).strip().replace("**", "")
    return re.sub(r"^```\w*\n?|\n?```$", "", text).strip()


NEXT_AGENDA_PROMPT = """Draft the agenda for the NEXT meeting of this series: "{title}".
Use only the notes below from the meeting that just happened: tasks still open, questions nobody
answered, agenda points that were not covered or only partly covered, and anything people said
they would come back to next time.

Reply with 3 to 8 lines and nothing else. Each line is one agenda point, short (3 to 10 words),
starting with "- ". Put the most important first. Name the owner in brackets when there is one,
for example "- Architecture diagram review (Sunil)". Do not invent topics. Write in clear English.
{words}
OPEN TASKS:
{tasks}

REPORT CARD:
{report}

MINUTES:
{mom}
"""


def make_next_agenda(title, report_md, mom_md, tasks, words=""):
    prompt = (NEXT_AGENDA_PROMPT.replace("{title}", title or "the meeting").replace("{words}", words_note(words))
              .replace("{tasks}", tasks or "(none)").replace("{report}", (report_md or "(none)")[:8000])
              .replace("{mom}", (mom_md or "(none)")[:8000]))
    lines = [x.strip() for x in _llm(prompt).splitlines() if x.strip()]
    bullets = [x for x in lines if re.match(r"^([-*•]|\d+[.)])\s+", x)]
    points = []
    # only bullet lines are points: an intro like "Here is the agenda:" or a "Nothing to add" is not
    if not bullets and len(lines) >= 3 and not any(x.endswith(":") for x in lines):
        bullets = lines                        # a plain list without dashes still counts
    for line in bullets:
        p = " ".join(re.sub(r"^([-*•]|\d+[.)])\s+", "", line).replace("**", "").split())
        if 2 < len(p) <= 200 and p not in points:
            points.append(p)
    return points[:8]


STORY_PROMPT = """Below are the report cards of several meetings that belong to one project: "{name}".
They are in date order, oldest first. Read them as one story.

Reply with JSON only, in exactly this shape:
{"summary": "2 or 3 sentences: where this project stands now",
 "timeline": [{"meeting_id": 3, "headline": "the main outcome of that meeting, one short sentence"}],
 "changes": [{"topic": "short name", "before": "what was said or decided earlier", "before_meeting": 3,
              "after": "what it changed to later", "after_meeting": 7}],
 "open": [{"item": "a task or question still not closed", "owner": "name or empty",
           "since_meeting": 3, "times_raised": 2}],
 "people": [{"name": "name", "role": "their part in this project, a few words"}],
 "update": "a status update of 80 to 120 words, plain text, that can be pasted into an email or chat"}

Rules:
- timeline: one entry per meeting, oldest first.
- changes: ONLY real changes, where a later meeting changed, reversed or replaced something an earlier
  meeting said or decided (a scope, a date, an owner, a number, an approach). Use an empty list when
  nothing changed. Never list things that were simply decided for the first time.
- open: tasks and questions that are still open at the latest meeting. "since_meeting" is the first
  meeting where it came up; "times_raised" is in how many meetings it came up. Use the OPEN TASKS too.
- Use only what the report cards say. Do not invent names, dates or decisions. Write in clear English.
{words}
OPEN TASKS:
{tasks}

REPORT CARDS:
{cards}
"""


def make_story(name, cards, tasks, words=""):
    prompt = (STORY_PROMPT.replace("{name}", name or "this project").replace("{words}", words_note(words))
              .replace("{tasks}", tasks or "(none)").replace("{cards}", cards))
    return _json_from(_llm(prompt, as_json=True))


def chat_json(prompt):
    """One chat turn. The model is asked for JSON; if it sends plain text, that text is the answer."""
    raw = _llm(prompt, as_json=True, fast=True)
    data = _json_from(raw)
    if not isinstance(data.get("answer"), str):
        data = {"answer": raw}
    return data
