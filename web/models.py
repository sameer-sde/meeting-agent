"""Database models. SQLite locally, Postgres (e.g. Neon) in production via DATABASE_URL."""
import base64
import hashlib
import os
import secrets
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import (Boolean, DateTime, ForeignKey, Integer, String, Text,
                        UniqueConstraint, create_engine)
from sqlalchemy.orm import (DeclarativeBase, Mapped, mapped_column,
                            relationship, scoped_session, sessionmaker)


def _db_url():
    url = os.environ.get("DATABASE_URL", "sqlite:///local.db")
    # Neon/Heroku style URLs -> SQLAlchemy psycopg3 driver
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


engine = create_engine(_db_url(), pool_pre_ping=True, future=True)
Session = scoped_session(sessionmaker(bind=engine, expire_on_commit=False))


def _fernet():
    secret = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def encrypt(value):
    return _fernet().encrypt(value.encode()).decode() if value else None


def decrypt(value):
    if not value:
        return None
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken:
        return None


def now():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    picture: Mapped[str] = mapped_column(String(500), default="")
    google_sub: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    email_verified: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    code_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    code_purpose: Mapped[str | None] = mapped_column(String(16), nullable=True)
    code_expires: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    code_attempts: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    code_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    bot_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    tx_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_to: Mapped[str] = mapped_column(Text, default="")
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tg_token: Mapped[str] = mapped_column(String(64), unique=True, default=lambda: secrets.token_urlsafe(12))
    ask_minutes: Mapped[int] = mapped_column(Integer, default=5)
    baseline_done: Mapped[bool] = mapped_column(Boolean, default=False)
    bot_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    greet_on: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=True)
    greet_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    brief_on: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=True)
    always_join: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    meetings = relationship("Meeting", back_populates="user", cascade="all, delete-orphan")
    people = relationship("Person", cascade="all, delete-orphan")

    @property
    def bot_key(self):
        return decrypt(self.bot_key_enc)

    @bot_key.setter
    def bot_key(self, v):
        self.bot_key_enc = encrypt(v)

    @property
    def tx_key(self):
        return decrypt(self.tx_key_enc)

    @tx_key.setter
    def tx_key(self, v):
        self.tx_key_enc = encrypt(v)

    @property
    def first_name(self):
        raw = (self.name or "").strip() or (self.email or "").split("@")[0]
        first = raw.replace(".", " ").replace("_", " ").split()[0] if raw.strip() else "Your"
        return first[:1].upper() + first[1:]

    @property
    def agent_name(self):
        """What the bot is called inside the meeting, e.g. "Sameer's Notetaker"."""
        return (self.bot_name or "").strip() or f"{self.first_name}'s Notetaker"

    @property
    def greets(self):
        return self.greet_on is not False

    def greeting(self, absent=False):
        """The message the bot posts in the meeting chat after it is let in."""
        custom = (self.greet_text or "").strip()
        if custom:
            return custom
        if absent:
            return (f"Hi everyone, I'm {self.agent_name}. {self.first_name} couldn't join this meeting, "
                    f"so I'm taking notes on {self.first_name}'s behalf. I only listen, and I'll send a summary afterwards.")
        return (f"Hi everyone, I'm {self.agent_name}. I'm here to take notes for {self.first_name}. "
                f"I only listen, and I'll send a summary after the meeting.")

    @property
    def briefs(self):
        return self.brief_on is not False

    @property
    def ready(self):
        return bool(self.bot_key and self.tx_key)

    @property
    def recipients(self):
        emails = [e.strip() for e in (self.report_to or "").replace(";", ",").split(",") if e.strip()]
        return emails or [self.email]


class Meeting(Base):
    """A finished meeting we have handled (report written, or nothing was said)."""
    __tablename__ = "meetings"
    __table_args__ = (UniqueConstraint("user_id", "vexa_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    vexa_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    platform: Mapped[str] = mapped_column(String(32), default="")
    native_id: Mapped[str] = mapped_column(String(255), default="")
    title: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(32), default="done")  # writing | done | empty | skipped | error | deleted
    report_md: Mapped[str] = mapped_column(Text, default="")
    transcript_json: Mapped[str] = mapped_column(Text, default="")
    mom_md: Mapped[str] = mapped_column(Text, default="")
    participants_json: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_min: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None = not worked out yet
    tries: Mapped[int | None] = mapped_column(Integer, nullable=True)        # attempts at writing the report
    writing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # last attempt began
    followups_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # promise tracker; None = not looked for
    chapters_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # None = not looked for yet
    recording_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # where the audio is, once found
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    user = relationship("User", back_populates="meetings")
    tasks = relationship("Task", back_populates="meeting", cascade="all, delete-orphan")
    chat = relationship("ChatMessage", cascade="all, delete-orphan", order_by="ChatMessage.id")


class Task(Base):
    """An action item picked out of a meeting, shown on the Tasks page."""
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    meeting_id: Mapped[int] = mapped_column(ForeignKey("meetings.id"))
    text: Mapped[str] = mapped_column(Text, default="")
    owner: Mapped[str] = mapped_column(String(255), default="")
    due: Mapped[str] = mapped_column(String(120), default="")
    done: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    meeting = relationship("Meeting", back_populates="tasks")


class ChatMessage(Base):
    """One line of a chat with a meeting: a question from the user or an answer from the bot."""
    __tablename__ = "chat_messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    meeting_id: Mapped[int | None] = mapped_column(ForeignKey("meetings.id"), nullable=True)
    live_key: Mapped[str | None] = mapped_column(String(300), nullable=True)  # "platform:native id" while running
    role: Mapped[str] = mapped_column(String(8), default="user")  # user | bot
    text: Mapped[str] = mapped_column(Text, default="")
    extra_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # proof lines and any action (bot only)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Person(Base):
    """The user's team directory: used to add email and designation to attendance."""
    __tablename__ = "people"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str] = mapped_column(String(255), default="")
    designation: Mapped[str] = mapped_column(String(255), default="")


class Asked(Base):
    """Telegram 'are you joining?' questions already sent."""
    __tablename__ = "asked"
    __table_args__ = (UniqueConstraint("user_id", "vexa_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    vexa_id: Mapped[int] = mapped_column(Integer)
    answer: Mapped[str | None] = mapped_column(String(8), nullable=True)


class HelpQuestion(Base):
    """A question a visitor typed into the sign-in page chatbot that its written answers didn't cover."""
    __tablename__ = "help_questions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    text: Mapped[str] = mapped_column(Text, default="")
    how: Mapped[str] = mapped_column(String(8), default="none")  # none = no written answer yet
    who: Mapped[str] = mapped_column(String(32), default="")    # a hash for counting, never an address
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Briefed(Base):
    """Scheduled meetings for which the before-meeting brief was already sent."""
    __tablename__ = "briefed"
    __table_args__ = (UniqueConstraint("user_id", "vexa_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    vexa_id: Mapped[int] = mapped_column(Integer)


class Greeted(Base):
    """Meetings where the bot has already posted its hello in the chat."""
    __tablename__ = "greeted"
    __table_args__ = (UniqueConstraint("user_id", "key"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    key: Mapped[str] = mapped_column(String(400))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class KV(Base):
    __tablename__ = "kv"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


def init_db():
    Base.metadata.create_all(engine)
    _add_missing_columns()


def _add_missing_columns():
    """create_all() makes new tables but never adds columns to existing ones; do that here."""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in have:
                continue
            ddl = col.type.compile(dialect=engine.dialect)
            with engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {col.name} {ddl}'))
