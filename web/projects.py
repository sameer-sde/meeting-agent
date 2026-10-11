"""Project Story: meetings that belong together (same series or client), and one story across them.

Meetings are grouped by their title, with dates and numbers taken out, or by the same meeting link.
The user can move a meeting to another project by hand; those are never regrouped. The story (what
changed, what is still open, a timeline and a status update) is written by one AI call and saved
until the project's meetings change.
"""
import hashlib
import json
import re
from datetime import datetime, timezone

from . import reports
from .models import Meeting, Project, Task

STORY_MEETINGS = 12      # newest meetings of a project read by the story
CARD_CHARS = 3500        # of each report card

_NOISE = re.compile(
    r"\b(\d+(st|nd|rd|th)?|jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|june?|july?|aug(ust)?|sep(t|tember)?|"
    r"oct(ober)?|nov(ember)?|dec(ember)?|mon(day)?|tue(s|sday)?|wed(nesday)?|thu(rs|rsday)?|fri(day)?|"
    r"sat(urday)?|sun(day)?|re|fw|fwd|copy|updated|new|meeting|call)\b")


def series_key(title):
    """"ADNOC - Daily Status Update (8 Oct)" -> "adnoc daily status update"."""
    t = re.sub(r"\(.*?\)|\[.*?\]", " ", (title or "").lower())
    t = re.sub(r"[^a-z0-9؀-ۿऀ-ॿ]+", " ", t)
    t = _NOISE.sub(" ", t)
    return " ".join(t.split())[:200]


def clean_name(title):
    t = re.sub(r"\((?:[^)]*\d[^)]*)\)", "", title or "")             # "(8 Oct)"
    t = re.sub(r"\s*[-–|·:]\s*\d.*$", "", t)                           # "Weekly sync - 08/10"
    return " ".join(t.split())[:200] or "Untitled project"


def assign(db, user):
    """Put every finished meeting that has no project yet into one. Returns how many were placed."""
    rows = (db.query(Meeting).filter(Meeting.user_id == user.id, Meeting.status == "done")
            .order_by(Meeting.created_at).all())
    projects = {p.id: p for p in db.query(Project).filter_by(user_id=user.id)}
    by_key = {k: p for p in projects.values() for k in (p.key or "").split("|") if k}
    by_link = {m.native_id: m.project_id for m in rows if m.project_id and m.native_id}
    placed = 0
    for m in rows:
        if m.project_id and m.project_id in projects:
            continue
        pid = by_link.get(m.native_id) if m.native_id else None
        if not pid:
            key = series_key(m.title) or f"meeting-{m.id}"
            p = by_key.get(key)
            if not p:
                p = Project(user_id=user.id, name=clean_name(m.title), key=key)
                db.add(p)
                db.flush()
                projects[p.id], by_key[key] = p, p
            pid = p.id
        m.project_id = pid
        if m.native_id:
            by_link.setdefault(m.native_id, pid)
        placed += 1
    if placed:
        db.commit()
    return placed


def meetings_of(db, user, project):
    return (db.query(Meeting).filter(Meeting.user_id == user.id, Meeting.status == "done",
                                     Meeting.project_id == project.id)
            .order_by(Meeting.created_at.desc()).all())


def basis(meetings):
    """Changes whenever a meeting is added, removed or its report is rewritten."""
    raw = "|".join(f"{m.id}:{len(m.report_md or '')}:{len(m.title or '')}" for m in meetings[:STORY_MEETINGS])
    return hashlib.sha256(raw.encode()).hexdigest()[:64]


def story(project):
    try:
        data = json.loads(project.story_json or "")
        return data if isinstance(data, dict) else None
    except ValueError:
        return None


def _local(dt, tz):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)


def write_story(db, user, project, tz):
    """One AI call: the project's story, checked and saved. Returns it."""
    meetings = meetings_of(db, user, project)
    recent = meetings[:STORY_MEETINGS][::-1]                     # oldest first, for the story
    ids = {m.id for m in recent}
    blocks = []
    for m in recent:
        d = _local(m.created_at, tz)
        blocks.append(f"=== MEETING id={m.id} | {d.day} {d.strftime('%b %Y')} | {m.title} ===\n"
                      f"{(m.report_md or '')[:CARD_CHARS]}")
    todo = (db.query(Task).filter(Task.meeting_id.in_(ids), Task.done.is_(False)).order_by(Task.id).all()
            if ids else [])
    todo_text = "\n".join(f"- {t.text} | owner: {t.owner or 'not set'} | due: {t.due or 'not set'} | "
                          f"meeting id={t.meeting_id}" for t in todo)
    raw = reports.make_story(project.name, "\n\n".join(blocks), todo_text, user.vocab or "")
    data = clean_story(raw, ids)
    project.story_json = json.dumps(data, ensure_ascii=False)
    project.story_basis = basis(meetings)
    project.story_at = datetime.now(timezone.utc)
    db.commit()
    return data


def _text(v, n=400):
    return " ".join(str(v or "").split())[:n]


def clean_story(raw, ids):
    """Keep only well-formed parts that point at this project's own meetings."""
    def mid(v):
        try:
            v = int(v)
        except (TypeError, ValueError):
            return None
        return v if v in ids else None

    out = {"summary": _text(raw.get("summary"), 800), "timeline": [], "changes": [], "open": [], "people": [],
           "update": str(raw.get("update") or "").strip()[:2000]}
    for x in raw.get("timeline") or []:
        if isinstance(x, dict) and mid(x.get("meeting_id")) and _text(x.get("headline")):
            out["timeline"].append({"meeting_id": mid(x["meeting_id"]), "headline": _text(x["headline"], 300)})
    for x in raw.get("changes") or []:
        if isinstance(x, dict) and _text(x.get("before")) and _text(x.get("after")):
            out["changes"].append({"topic": _text(x.get("topic"), 120), "before": _text(x["before"]),
                                   "before_meeting": mid(x.get("before_meeting")), "after": _text(x["after"]),
                                   "after_meeting": mid(x.get("after_meeting"))})
    for x in raw.get("open") or []:
        if isinstance(x, dict) and _text(x.get("item")):
            try:
                times = max(1, min(int(x.get("times_raised") or 1), 99))
            except (TypeError, ValueError):
                times = 1
            out["open"].append({"item": _text(x["item"], 300), "owner": _text(x.get("owner"), 120),
                                "since_meeting": mid(x.get("since_meeting")), "times_raised": times})
    for x in raw.get("people") or []:
        if isinstance(x, dict) and _text(x.get("name")):
            out["people"].append({"name": _text(x["name"], 120), "role": _text(x.get("role"), 200)})
    for k, n in (("timeline", 20), ("changes", 12), ("open", 15), ("people", 12)):
        out[k] = out[k][:n]
    return out
