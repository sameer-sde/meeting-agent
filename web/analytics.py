"""Numbers from a transcript: who spoke how much, who was mentioned where, and attendance.

Everything here is calculated directly from the transcript and invite list, no AI involved,
so the figures are exact and repeatable.
"""
import json
import re
from datetime import datetime, timezone

WORDS_PER_SECOND = 2.5  # fallback when a segment has no timing


def segments(meeting):
    """Transcript segments saved on a Meeting, as a list of dicts."""
    if not meeting.transcript_json:
        return []
    try:
        data = json.loads(meeting.transcript_json)
    except (ValueError, TypeError):
        return []
    if isinstance(data, dict):
        data = data.get("segments", [])
    return [s for s in data if isinstance(s, dict) and (s.get("text") or "").strip()]


def _duration(s):
    try:
        d = float(s.get("end")) - float(s.get("start"))
        if d > 0:
            return d
    except (TypeError, ValueError):
        pass
    return len((s.get("text") or "").split()) / WORDS_PER_SECOND


def clock_base(segs):
    """Wall-clock time of second 0 of the recording, if any segment carries an absolute time."""
    from datetime import timedelta
    for s in segs:
        abs_t = s.get("absolute_start_time")
        if not abs_t:
            continue
        try:
            dt = datetime.fromisoformat(str(abs_t).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt - timedelta(seconds=float(s.get("start") or 0))
        except (ValueError, TypeError):
            continue
    return None


def time_label(s, tz, base=None):
    """Clock time when known (e.g. 12:04:31 PM), else minutes into the call (e.g. 03:12)."""
    from datetime import timedelta
    try:
        sec = float(s.get("start", 0) or 0)
    except (TypeError, ValueError):
        sec = 0.0
    if base is not None:
        return (base + timedelta(seconds=sec)).astimezone(tz).strftime("%I:%M:%S %p")
    sec = int(sec)
    return f"{sec // 60:02d}:{sec % 60:02d}"


def speaker_of(s):
    return (s.get("speaker") or "").strip() or "Unknown speaker"


def participation(segs):
    """Per speaker: share of talk time, turns and words, plus a High/Medium/Low scale."""
    stats = {}
    for s in segs:
        p = stats.setdefault(speaker_of(s), {"seconds": 0.0, "turns": 0, "words": 0})
        p["seconds"] += _duration(s)
        p["turns"] += 1
        p["words"] += len((s.get("text") or "").split())
    total = sum(p["seconds"] for p in stats.values()) or 1
    fair_share = 100 / max(len(stats), 1)
    rows = []
    for name, p in stats.items():
        pct = p["seconds"] / total * 100
        if pct >= fair_share * 1.5:
            scale = "High"
        elif pct >= fair_share * 0.5:
            scale = "Medium"
        else:
            scale = "Low"
        rows.append({"name": name, "pct": round(pct, 1), "minutes": round(p["seconds"] / 60, 1),
                     "turns": p["turns"], "words": p["words"], "scale": scale})
    rows.sort(key=lambda r: r["pct"], reverse=True)
    return rows


def _first_name(name):
    parts = re.split(r"[\s._-]+", (name or "").strip())
    return parts[0] if parts and parts[0] else ""


def mentions(segs, people, tz):
    """How often each person's name came up in what others said, with time, speaker and sentence.

    `people` is a list of display names to look for (speakers, invitees, directory).
    Both the full name and the first name count, so "Ravi" finds "Ravi Kumar".
    """
    results = {}
    base = clock_base(segs)
    for person in people:
        full = (person or "").strip()
        first = _first_name(full)
        terms = {t for t in (full, first) if len(t) >= 3}
        if not terms:
            continue
        pattern = re.compile(r"\b(" + "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True)) + r")\b",
                             re.IGNORECASE)
        hits = []
        for s in segs:
            spk = speaker_of(s)
            if spk.lower() == full.lower() or _first_name(spk).lower() == first.lower():
                continue  # people saying their own name don't count
            text = s.get("text") or ""
            for m in pattern.finditer(text):
                a, b = max(0, m.start() - 60), min(len(text), m.end() + 60)
                snippet = ("…" if a else "") + text[a:b].strip() + ("…" if b < len(text) else "")
                hits.append({"time": time_label(s, tz, base), "by": spk, "snippet": snippet, "word": m.group(0),
                             "pre": ("…" if a else "") + text[a:m.start()],
                             "post": text[m.end():b] + ("…" if b < len(text) else "")})
        if hits:
            results[full] = hits
    return sorted(({"name": k, "count": len(v), "hits": v} for k, v in results.items()),
                  key=lambda r: r["count"], reverse=True)


def _norm(name):
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def attendance(segs, invitees, directory, part_rows):
    """Merge who was invited, who spoke and the team directory into one attendance list.

    invitees:  [{"name", "email", "response_status"}] from the calendar invite
    directory: [{"name", "email", "designation"}] from the People page
    """
    by_email = {(d.get("email") or "").lower(): d for d in directory if d.get("email")}
    by_name = {}
    for d in directory:
        by_name[_norm(d.get("name"))] = d
        by_name.setdefault(_norm(_first_name(d.get("name"))), d)
    pct = {r["name"]: r for r in part_rows}

    rows, seen = [], set()

    def lookup(name, email):
        return by_email.get((email or "").lower()) or by_name.get(_norm(name)) or by_name.get(_norm(_first_name(name))) or {}

    speakers = []
    for s in segs:
        spk = speaker_of(s)
        if spk not in speakers and spk != "Unknown speaker":
            speakers.append(spk)

    for inv in invitees:
        name = inv.get("name") or (inv.get("email") or "").split("@")[0]
        d = lookup(name, inv.get("email"))
        spoke = next((sp for sp in speakers if _norm(sp) == _norm(name) or _norm(_first_name(sp)) == _norm(_first_name(name))), None)
        key = _norm(spoke or name)
        if key in seen:
            continue
        seen.add(key)
        p = pct.get(spoke, {})
        rows.append({"name": name, "email": inv.get("email") or d.get("email", ""),
                     "designation": d.get("designation", ""),
                     "status": "Attended (spoke)" if spoke else "Invited, not heard",
                     "invite": (inv.get("response_status") or "").replace("_", " "),
                     "pct": p.get("pct", 0), "minutes": p.get("minutes", 0)})

    for spk in speakers:
        if _norm(spk) in seen:
            continue
        seen.add(_norm(spk))
        d = lookup(spk, None)
        p = pct.get(spk, {})
        rows.append({"name": spk, "email": d.get("email", ""), "designation": d.get("designation", ""),
                     "status": "Attended (spoke)", "invite": "", "pct": p.get("pct", 0), "minutes": p.get("minutes", 0)})
    rows.sort(key=lambda r: (r["status"] != "Attended (spoke)", -r["pct"], r["name"].lower()))
    return rows
