"""Thin client for the Vexa meeting-bot API. Every call uses the signed-in user's own keys."""
import os
from datetime import datetime, timezone

import requests

BASE = os.environ.get("VEXA_BASE", "https://api.cloud.vexa.ai")
TIMEOUT = 20


class VexaError(Exception):
    pass


def _h(key):
    return {"X-API-Key": key, "Content-Type": "application/json"}


def _rows(data, *names):
    if isinstance(data, list):
        return data
    for n in names:
        if isinstance(data.get(n), list):
            return data[n]
    return []


def _err(r):
    try:
        d = r.json()
        detail = d.get("detail", d)
    except Exception:
        detail = r.text
    return VexaError(f"{r.status_code}: {detail}")


def send_bot(bot_key, link, task="translate", bot_name="Meeting Agent"):
    body = {"meeting_url": link, "bot_name": bot_name, "task": task}
    # Ask Vexa to keep the meeting audio; fall back to the plain request if it doesn't accept that.
    r = requests.post(f"{BASE}/bots", headers=_h(bot_key), timeout=TIMEOUT, json={**body, "recording_enabled": True})
    if r.status_code in (400, 422):
        r = requests.post(f"{BASE}/bots", headers=_h(bot_key), timeout=TIMEOUT, json=body)
    if not r.ok:
        raise _err(r)
    d = r.json()
    return d["platform"], d["native_meeting_id"]


def chat(bot_key, platform, native_id, text):
    """Post a message in the meeting chat as the bot. True when Vexa accepted it."""
    try:
        r = requests.post(f"{BASE}/bots/{platform}/{native_id}/chat", headers=_h(bot_key),
                          timeout=TIMEOUT, json={"text": text})
        return r.ok
    except requests.RequestException:
        return False


def running(bot_key):
    r = requests.get(f"{BASE}/bots/status", headers=_h(bot_key), timeout=TIMEOUT)
    if not r.ok:
        raise _err(r)
    d = r.json()
    return d.get("running") or d.get("running_bots") or []


def stop(bot_key, platform, native_id):
    r = requests.delete(f"{BASE}/bots/{platform}/{native_id}", headers=_h(bot_key), timeout=TIMEOUT)
    if not r.ok:
        raise _err(r)


def meetings(tx_key, status, limit=50):
    r = requests.get(f"{BASE}/meetings", headers=_h(tx_key), timeout=TIMEOUT,
                     params={"status": status, "limit": limit})
    if not r.ok:
        raise _err(r)
    return _rows(r.json(), "meetings", "items")


def transcript(tx_key, meeting_id):
    r = requests.get(f"{BASE}/transcripts/by-id/{meeting_id}", headers=_h(tx_key), timeout=TIMEOUT)
    if not r.ok:
        raise _err(r)
    return r.json()


def participants(tx_key, platform, native_id):
    """People invited (from the calendar invite, with emails) and people heard speaking."""
    if not platform or not native_id:
        return []
    try:
        r = requests.get(f"{BASE}/meetings/{platform}/{native_id}/participants", headers=_h(tx_key), timeout=TIMEOUT)
        return r.json().get("participants", []) if r.ok else []
    except (requests.RequestException, ValueError):
        return []


def _get_json(key, path, **params):
    try:
        r = requests.get(f"{BASE}{path}", headers=_h(key), timeout=TIMEOUT, params=params or None)
        return r.json() if r.ok else None
    except (requests.RequestException, ValueError):
        return None


def find_recording(keys, meeting_id):
    """Where the audio of a meeting can be streamed from, or None when Vexa kept no recording.

    keys: {"tx": ..., "bot": ...}. Returns {"url", "key", "started"}; `key` says which of the
    user's keys opened it, `started` is the clock time the recording began (when Vexa says).
    """
    for label, key in keys.items():
        if not key:
            continue
        recs, started = [], None
        data = _get_json(key, f"/transcripts/by-id/{meeting_id}")
        if isinstance(data, dict):
            recs = [r for r in (data.get("recordings") or []) if isinstance(r, dict)]
            started = data.get("start_time")
        if not recs:
            listing = _get_json(key, "/recordings", limit=100)
            if listing is not None:
                recs = [r for r in _rows(listing, "recordings", "items")
                        if isinstance(r, dict) and str(r.get("meeting_id")) == str(meeting_id)]
        for rec in recs:
            rid = rec.get("id") or rec.get("recording_id")
            if rid is None:
                continue
            media = [m for m in (rec.get("media_files") or []) if isinstance(m, dict)]
            audio = next((m for m in media if "audio" in str(m.get("type") or m.get("media_type") or "")),
                         media[0] if media else None)
            url = None
            master = _get_json(key, f"/recordings/{rid}/master", type="audio")
            if isinstance(master, dict):
                url = master.get("raw_url") or master.get("url")
            if not url and audio and audio.get("id") is not None:
                url = f"/recordings/{rid}/media/{audio['id']}/raw"
            if url:
                return {"url": url, "key": label,
                        "started": rec.get("started_at") or rec.get("start_time") or rec.get("created_at") or started}
    return None


def is_vexa_url(url):
    """True for addresses on Vexa's API (need the user's key); False for ready-to-play links."""
    return url.startswith("/") or url.startswith(BASE)


def audio_bytes(key, url, start, size):
    """Fetch one piece of a recording. Returns the upstream response (status 200 or 206)."""
    full = BASE + url if url.startswith("/") else url
    return requests.get(full, headers={"X-API-Key": key, "Range": f"bytes={start}-{start + size - 1}"},
                        timeout=60)


def schedule(tx_key, title, link, when_utc, bot_name=None):
    body = {"title": title or "Meeting", "scheduled_at": when_utc.isoformat(),
            "meeting_url": link, "auto_join": True}
    r = None
    if bot_name:  # not every Vexa version accepts a name here; fall back to the plain request
        r = requests.post(f"{BASE}/meetings", headers=_h(tx_key), timeout=TIMEOUT, json={**body, "bot_name": bot_name})
    if r is None or r.status_code in (400, 422):
        r = requests.post(f"{BASE}/meetings", headers=_h(tx_key), timeout=TIMEOUT, json=body)
    if not r.ok:
        raise _err(r)


def set_auto_join(tx_key, meeting_id, on):
    r = requests.patch(f"{BASE}/meetings/{meeting_id}", headers=_h(tx_key), timeout=TIMEOUT,
                       json={"auto_join": on})
    return r.ok


def calendars(bot_key):
    r = requests.get(f"{BASE}/user/calendars", headers=_h(bot_key), timeout=TIMEOUT)
    if not r.ok:
        raise _err(r)
    return _rows(r.json(), "calendars", "items")


def cal_connect(bot_key, name, ics_url, bot_name="Meeting Agent"):
    r = requests.post(f"{BASE}/user/calendars", headers=_h(bot_key), timeout=TIMEOUT, json={
        "name": name, "ics_url": ics_url, "auto_join": True, "bot_name": bot_name})
    if not r.ok:
        raise _err(r)


def cal_sync(bot_key, cal_id):
    r = requests.post(f"{BASE}/user/calendars/{cal_id}/sync", headers=_h(bot_key), timeout=60)
    if not r.ok:
        raise _err(r)
    return r.json()


def cal_delete(bot_key, cal_id):
    r = requests.delete(f"{BASE}/user/calendars/{cal_id}", headers=_h(bot_key), timeout=TIMEOUT)
    if not r.ok:
        raise _err(r)


def title_of(m):
    t = (m.get("data") or {}).get("title") or m.get("title") or m.get("native_meeting_id") or "Meeting"
    return t.lstrip(": ").strip() or "Meeting"


def _when(v):
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def times_of(m):
    """When the bot joined and left a finished meeting, as (start, end). Either can be None."""
    d = m.get("data") or {}
    return _when(m.get("start_time") or d.get("start_time")), _when(m.get("end_time") or d.get("end_time"))


def start_of(m):
    at = (m.get("data") or {}).get("scheduled_at") or m.get("scheduled_at")
    if not at:
        return None
    try:
        dt = datetime.fromisoformat(at.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def upcoming(tx_key):
    out = []
    for m in meetings(tx_key, "scheduled", 50):
        if m.get("platform") in (None, "", "unknown"):
            continue
        start = start_of(m)
        if start:
            out.append({"id": m["id"], "title": title_of(m), "platform": m.get("platform"), "start": start})
    out.sort(key=lambda x: x["start"])
    return out
