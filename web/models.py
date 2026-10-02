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
    bot_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    tx_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_to: Mapped[str] = mapped_column(Text, default="")
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tg_token: Mapped[str] = mapped_column(String(64), unique=True, default=lambda: secrets.token_urlsafe(12))
    ask_minutes: Mapped[int] = mapped_column(Integer, default=5)
    baseline_done: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    meetings = relationship("Meeting", back_populates="user", cascade="all, delete-orphan")

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
    status: Mapped[str] = mapped_column(String(32), default="done")  # done | empty | skipped | error
    report_md: Mapped[str] = mapped_column(Text, default="")
    transcript_json: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    user = relationship("User", back_populates="meetings")


class Asked(Base):
    """Telegram 'are you joining?' questions already sent."""
    __tablename__ = "asked"
    __table_args__ = (UniqueConstraint("user_id", "vexa_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    vexa_id: Mapped[int] = mapped_column(Integer)
    answer: Mapped[str | None] = mapped_column(String(8), nullable=True)


class KV(Base):
    __tablename__ = "kv"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


def init_db():
    Base.metadata.create_all(engine)
