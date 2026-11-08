"""Password hashing, server-side sessions and login throttling.

Hashing uses scrypt from the standard library (memory-hard, like bcrypt/argon2)
instead of passlib, which is unmaintained and breaks with bcrypt >= 4.1.
Stored format:  scrypt$<n>$<r>$<p>$<salt b64>$<hash b64>
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
SESSION_COOKIE = "qsd_session"
SESSION_IDLE_TIMEOUT = 12 * 3600       # seconds of inactivity before a session expires
SESSION_MAX_AGE = 7 * 24 * 3600        # absolute lifetime regardless of activity
MAX_FAILED_LOGINS = 5                  # per client IP ...
LOCKOUT_WINDOW = 300                   # ... within this many seconds

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
MIN_PASSWORD_LEN = 8
ROLES = ("admin", "viewer")


# --------------------------------------------------------------------------- hashing
def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, expected = stored.split("$")
        if algo != "scrypt":
            return False
        expected_b = base64.b64decode(expected)
        dk = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                            dklen=len(expected_b), maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(dk, expected_b)
    except (ValueError, TypeError):
        return False


# Verified against when the username doesn't exist, so a login for an unknown user
# takes as long as one for a real user (no username enumeration by timing).
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def burn_time(password: str) -> None:
    verify_password(password, _DUMMY_HASH)


def password_problem(password: str, username: str) -> Optional[str]:
    if len(password) < MIN_PASSWORD_LEN:
        return f"Password must be at least {MIN_PASSWORD_LEN} characters"
    if len(password) > 256:
        return "Password is too long"
    if password.lower() == username.lower():
        return "Password must not equal the username"
    return None


# --------------------------------------------------------------------------- sessions
@dataclass
class Session:
    username: str
    created: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)


class SessionStore:
    """In-memory sessions: logout and password changes revoke immediately.
    A server restart signs everyone out, which is acceptable for a LAN dashboard."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}

    def create(self, username: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._purge()
            self._sessions[token] = Session(username)
        return token

    def get(self, token: Optional[str]) -> Optional[Session]:
        if not token:
            return None
        now = time.time()
        with self._lock:
            s = self._sessions.get(token)
            if s is None:
                return None
            if now - s.last_seen > SESSION_IDLE_TIMEOUT or now - s.created > SESSION_MAX_AGE:
                del self._sessions[token]
                return None
            s.last_seen = now
            return s

    def alive(self, token: str) -> bool:
        """Cheap check for long-lived streams; doesn't extend the session."""
        with self._lock:
            return token in self._sessions

    def revoke(self, token: Optional[str]) -> None:
        with self._lock:
            self._sessions.pop(token or "", None)

    def revoke_user(self, username: str, keep: Optional[str] = None) -> None:
        with self._lock:
            for t in [t for t, s in self._sessions.items() if s.username.lower() == username.lower() and t != keep]:
                del self._sessions[t]

    def _purge(self) -> None:
        now = time.time()
        for t in [t for t, s in self._sessions.items()
                  if now - s.last_seen > SESSION_IDLE_TIMEOUT or now - s.created > SESSION_MAX_AGE]:
            del self._sessions[t]


class LoginThrottle:
    """Blocks a client IP after MAX_FAILED_LOGINS failures inside LOCKOUT_WINDOW."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fails: dict[str, list[float]] = {}

    def retry_after(self, ip: str) -> int:
        now = time.time()
        with self._lock:
            recent = [t for t in self._fails.get(ip, []) if now - t < LOCKOUT_WINDOW]
            self._fails[ip] = recent
            if len(recent) >= MAX_FAILED_LOGINS:
                return int(LOCKOUT_WINDOW - (now - recent[0])) + 1
            return 0

    def fail(self, ip: str) -> None:
        with self._lock:
            self._fails.setdefault(ip, []).append(time.time())

    def reset(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)


sessions = SessionStore()
throttle = LoginThrottle()
