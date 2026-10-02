"""Copy the report cards saved by the old local dashboard (reports/*.md) into the new app.

Usage:  python import_local.py you@gmail.com
Sign in to the new app once first, so your account exists.
"""
import glob
import json
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

from web.models import Meeting, Session, User, init_db  # noqa: E402


def main():
    init_db()
    db = Session()
    email = sys.argv[1].lower() if len(sys.argv) > 1 else None
    user = db.query(User).filter_by(email=email).first() if email else db.query(User).order_by(User.id).first()
    if not user:
        sys.exit("No account found. Sign in to the app once, then run this again with your email.")
    added = 0
    for path in sorted(glob.glob("reports/*_report.md")):
        name = os.path.basename(path).replace("_report.md", "")
        parts = name.split("_")
        title = "_".join(parts[2:]) or name
        try:
            created = datetime.strptime(parts[0] + parts[1], "%Y-%m-%d%H%M").astimezone(timezone.utc)
        except (ValueError, IndexError):
            created = datetime.now(timezone.utc)
        if db.query(Meeting).filter_by(user_id=user.id, title=title, created_at=created).first():
            continue
        transcript = ""
        tpath = path.replace("_report.md", "_transcript.json")
        if os.path.exists(tpath):
            data = json.load(open(tpath))
            transcript = json.dumps(data.get("segments", data), ensure_ascii=False, indent=1)
        db.add(Meeting(user_id=user.id, title=("Meeting " + title) if title.isdigit() else title,
                       report_md=open(path).read(), transcript_json=transcript,
                       status="done", created_at=created))
        added += 1
    db.commit()
    print(f"Imported {added} report card(s) into {user.email}'s account.")


if __name__ == "__main__":
    main()
