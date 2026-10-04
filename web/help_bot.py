"""The help chatbot on the sign-in pages answers from written answers only (no AI).

When a visitor asks something it has no answer for, the question is saved here (text only) so the
owner can see what people ask and add an answer for the common ones.
"""
import hashlib
import os
from datetime import datetime, timedelta, timezone

from .models import HelpQuestion, Session

MAX_QUESTION = 300
PER_HOUR = 30      # saved questions per visitor per hour, so nobody can flood the list
PER_DAY = 1000     # saved questions per day in total


def visitor_key(ip):
    """A short code that stands for one visitor, used only for counting. The address itself isn't kept."""
    secret = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    return hashlib.sha256(f"{secret}:{ip}".encode()).hexdigest()[:16]


def save_miss(question, ip):
    """Remember a question the written answers didn't cover. Returns True when it was saved."""
    question = " ".join((question or "").split())[:MAX_QUESTION]
    if len(question) < 2:
        return False
    who = visitor_key(ip)
    now = datetime.now(timezone.utc)
    mine = Session.query(HelpQuestion).filter(HelpQuestion.who == who,
                                              HelpQuestion.created_at > now - timedelta(hours=1)).count()
    today = Session.query(HelpQuestion).filter(HelpQuestion.created_at > now - timedelta(hours=24)).count()
    if mine >= PER_HOUR or today >= PER_DAY:
        return False
    Session.add(HelpQuestion(text=question, how="none", who=who))
    Session.commit()
    return True
