"""Local accounts and server-enforced roles. No default account password."""
from collections import OrderedDict
from contextlib import closing
from dataclasses import dataclass
import hashlib
import hmac
import secrets
import sqlite3
import threading
import time
import unicodedata

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from .catalog import identifier, now

ADMIN_ID = "00000000000000000000000000000001"
USER_FIELDS = "id,username,role,enabled,created_at,updated_at,password_hash IS NOT NULL AS password_set"


def clean_text(value):
    if any(unicodedata.category(c) in ("Cc", "Cs") for c in value):
        raise ValueError("Text contains invalid characters")
    return value


class AccountInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: SecretStr = Field(min_length=8, max_length=128)

    @field_validator("username")
    @classmethod
    def username_text(cls, value):
        value = clean_text(value.strip())
        if not value:
            raise ValueError("Username must not be empty")
        return value

    @field_validator("password")
    @classmethod
    def password_text(cls, value):
        clean_text(value.get_secret_value())
        return value


class AccountUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str | None = Field(default=None, min_length=1, max_length=64)
    password: SecretStr | None = Field(default=None, min_length=8, max_length=128)
    enabled: bool | None = None

    @field_validator("username")
    @classmethod
    def username_text(cls, value):
        return AccountInput.username_text(value) if value is not None else value

    @field_validator("password")
    @classmethod
    def password_text(cls, value):
        return AccountInput.password_text(value) if value is not None else value


def password_hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=32768, r=8, p=3,
                            dklen=32, maxmem=64 * 1024**2)
    return "scrypt$32768$8$3$" + salt.hex() + "$" + digest.hex()


def password_matches(password, stored):
    try:
        algorithm, n, r, p, salt, digest = stored.split("$")
        if (algorithm, n, r, p) != ("scrypt", "32768", "8", "3"):
            return False
        return hmac.compare_digest(password_hash(password, bytes.fromhex(salt)).split("$")[-1], digest)
    except (ValueError, UnicodeError):
        return False


@dataclass(frozen=True)
class Principal:
    id: str
    username: str
    role: str
    token_digest: str | None = None

    def public(self):
        return {"id": self.id, "username": self.username, "role": self.role}


class Accounts:
    def __init__(self, catalog, token):
        self.catalog, self.token = catalog, token
        self.hash_slots = threading.BoundedSemaphore(2)
        self.rate_lock, self.attempts = threading.Lock(), OrderedDict()
        self.dummy = password_hash(secrets.token_urlsafe(32))
        with closing(catalog.connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, username TEXT NOT NULL, username_key TEXT NOT NULL UNIQUE,
                    password_hash TEXT, role TEXT NOT NULL CHECK(role IN ('admin','user')),
                    enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    digest TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                    expires_at REAL NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
            """)
            db.execute("INSERT OR IGNORE INTO users VALUES(?,?,?,NULL,'admin',1,?,?)",
                       (ADMIN_ID, "admin", "admin", now(), now()))
            db.commit()

    def authenticate(self, authorization):
        if not authorization or not authorization.startswith("Bearer ") or len(authorization) > 1024:
            self.unauthorized()
        candidate = authorization[7:]
        with closing(self.catalog.connect()) as db:
            if hmac.compare_digest(candidate.encode(), self.token.encode()):
                user = db.execute("SELECT * FROM users WHERE id=?", (ADMIN_ID,)).fetchone()
                return Principal(user["id"], user["username"], "admin")
            digest = hashlib.sha256(candidate.encode()).hexdigest()
            user = db.execute("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
                              "WHERE s.digest=? AND s.expires_at>? AND u.enabled=1", (digest, time.time())).fetchone()
        if user is None:
            self.unauthorized()
        return Principal(user["id"], user["username"], user["role"], digest)

    @staticmethod
    def unauthorized():
        raise HTTPException(401, "Login required or session expired", headers={"WWW-Authenticate": "Bearer"})

    def throttle(self, address):
        current = time.monotonic()
        with self.rate_lock:
            for key, cap in (("global", 100), ("ip:" + address, 30)):
                start, count = self.attempts.get(key, (current, 0))
                if current - start >= 60:
                    start, count = current, 0
                if count >= cap:
                    raise HTTPException(429, "Too many login attempts; retry in one minute", headers={"Retry-After": "60"})
                self.attempts[key] = (start, count + 1)
                self.attempts.move_to_end(key)
            while len(self.attempts) > 2048:
                self.attempts.popitem(last=False)

    def login(self, body, address):
        self.throttle(address)
        if not self.hash_slots.acquire(blocking=False):
            raise HTTPException(429, "Login capacity is busy; retry shortly", headers={"Retry-After": "1"})
        try:
            with closing(self.catalog.connect()) as db:
                user = db.execute("SELECT * FROM users WHERE username_key=?", (body.username.casefold(),)).fetchone()
            stored = user["password_hash"] if user and user["password_hash"] else self.dummy
            valid = password_matches(body.password.get_secret_value(), stored)
        finally:
            self.hash_slots.release()
        if not valid or not user or not user["enabled"] or not user["password_hash"]:
            raise HTTPException(401, "Incorrect username or password")
        session = secrets.token_urlsafe(32)
        digest = hashlib.sha256(session.encode()).hexdigest()
        with self.catalog.transaction() as db:
            # Reset/disable between password verification and issuing a session must win.
            current = db.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
            if not current["enabled"] or current["password_hash"] != stored:
                raise HTTPException(401, "Incorrect username or password")
            db.execute("DELETE FROM sessions WHERE expires_at<=?", (time.time(),))
            db.execute("INSERT INTO sessions VALUES(?,?,?,?)", (digest, user["id"], time.time() + 86400, now()))
        return {"token": session, "expires_in": 86400, "user": Principal(user["id"], user["username"], user["role"]).public()}

    def public_user(self, user_id):
        with closing(self.catalog.connect()) as db:
            row = db.execute(f"SELECT {USER_FIELDS} FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    def list_users(self, limit):
        with closing(self.catalog.connect()) as db:
            return [dict(row) for row in db.execute(f"SELECT {USER_FIELDS} FROM users ORDER BY rowid LIMIT ?", (limit,))]

    def hash(self, password):
        if not self.hash_slots.acquire(blocking=False):
            raise HTTPException(429, "Login capacity is busy; retry shortly")
        try:
            return password_hash(password.get_secret_value())
        finally:
            self.hash_slots.release()

    def create(self, body):
        item_id, digest = identifier(), self.hash(body.password)
        try:
            with self.catalog.transaction() as db:
                db.execute("INSERT INTO users VALUES(?,?,?,?,'user',1,?,?)",
                           (item_id, body.username, body.username.casefold(), digest, now(), now()))
                self.catalog.audit(db, "user.create", item_id)
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Username already exists")
        return self.public_user(item_id)

    def update(self, user_id, body):
        digest = self.hash(body.password) if body.password is not None else None
        try:
            with self.catalog.transaction() as db:
                user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
                if user is None:
                    raise HTTPException(404, "User not found")
                if user["role"] == "admin" and body.enabled is False:
                    raise HTTPException(409, "Administrator cannot be disabled")
                username = body.username if body.username is not None else user["username"]
                enabled = body.enabled if body.enabled is not None else user["enabled"]
                db.execute("UPDATE users SET username=?,username_key=?,password_hash=?,enabled=?,updated_at=? WHERE id=?",
                           (username, username.casefold(), digest or user["password_hash"], enabled, now(), user_id))
                if digest is not None or not enabled:
                    db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
                self.catalog.audit(db, "user.update", user_id)
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Username already exists")
        return self.public_user(user_id)
