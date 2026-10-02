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
    r = requests.post(f"{BASE}/bots", headers=_h(bot_key), timeout=TIMEOUT,
                      json={"meeting_url": link, "bot_name": bot_name, "task": task})
    if not r.ok:
        raise _err(r)
    d = r.json()
    return d["platform"], d["native_meeting_id"]


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


def schedule(tx_key, title, link, when_utc):
    r = requests.post(f"{BASE}/meetings", headers=_h(tx_key), timeout=TIMEOUT, json={
        "title": title or "Meeting", "scheduled_at": when_utc.isoformat(),
        "meeting_url": link, "auto_join": True})
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


def cal_connect(bot_key, name, ics_url):
    r = requests.post(f"{BASE}/user/calendars", headers=_h(bot_key), timeout=TIMEOUT, json={
        "name": name, "ics_url": ics_url, "auto_join": True, "bot_name": "Meeting Agent"})
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
