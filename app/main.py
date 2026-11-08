"""Q-See 8-channel DVR web monitoring dashboard - FastAPI backend.

Each DVR channel gets a background worker thread that holds an RTSP connection
open via cv2.VideoCapture, reconnects with exponential backoff on failure, and
keeps the most recent frame JPEG-encoded in memory. HTTP clients read from that
buffer, so any number of viewers costs only one RTSP session per stream.

Sub-streams (subtype=1) run continuously for enabled channels only; disabled
channels hold no thread and no RTSP session. Main streams (subtype=0, HD) are
started on demand and shut down when idle.

Every page, API and stream requires a logged-in session (see app/auth.py).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Optional
from urllib.parse import quote

# Must be set before cv2 is imported. RTSP over TCP avoids the smeared / grey frames
# UDP produces on most DVRs; nobuffer + low_delay stop FFmpeg queueing frames ahead
# of the decoder, which is the main source of latency. Connect/read timeouts are
# passed per-capture (CAP_PROP_*_TIMEOUT_MSEC) below.
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay"

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from fastapi import Depends, FastAPI, HTTPException, Query, Request  # noqa: E402
from fastapi.exception_handlers import http_exception_handler  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402
from starlette.exceptions import HTTPException as StarletteHTTPException  # noqa: E402

from app import auth  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent.parent
# PyInstaller builds: bundled files (static/) live in the unpack dir (sys._MEIPASS), which
# is temporary/read-only, so the config must live in a per-user data directory instead.
FROZEN = bool(getattr(sys, "frozen", False))
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR))
STATIC_DIR = RESOURCE_DIR / "static"


def user_data_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "QSeeDVR"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "QSeeDVR"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "qseedvr"


CONFIG_PATH = Path(os.environ.get("DVR_CONFIG") or (user_data_dir() if FROZEN else BASE_DIR) / "dvr_config.json")
CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

NUM_CHANNELS = 8
SUB, MAIN = 1, 0
MAIN_IDLE_TIMEOUT = 20.0  # seconds without viewers before an HD worker stops
MAX_BACKOFF = 30.0
MAX_CONSECUTIVE_READ_FAILURES = 25

log = logging.getLogger("qseedvr")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


# --------------------------------------------------------------------------- config
class ChannelConfig(BaseModel):
    id: int = Field(ge=1, le=NUM_CHANNELS)
    name: str = Field(default="", max_length=64)
    enabled: bool = True


class UserRecord(BaseModel):
    username: str
    password_hash: str
    role: str = "viewer"
    must_change_password: bool = False


def default_admin() -> UserRecord:
    # Default login is admin / admin; it can't be used for anything except
    # setting a new password (must_change_password is enforced server-side).
    return UserRecord(username="admin", password_hash=auth.hash_password("admin"), role="admin",
                      must_change_password=True)


class DVRConfig(BaseModel):
    dvr_ip: str = "192.168.1.100"
    rtsp_port: int = Field(default=554, ge=1, le=65535)
    username: str = "admin"
    password: str = ""
    path_template: str = "/cam/realmonitor?channel={channel}&subtype={subtype}"
    grid_fps: int = Field(default=5, ge=1, le=30)
    jpeg_quality: int = Field(default=75, ge=30, le=95)
    channels: list[ChannelConfig] = Field(
        default_factory=lambda: [ChannelConfig(id=i, name=f"Camera {i}") for i in range(1, NUM_CHANNELS + 1)]
    )
    users: list[UserRecord] = Field(default_factory=lambda: [default_admin()])

    def user(self, username: str) -> Optional[UserRecord]:
        for u in self.users:
            if u.username.lower() == username.lower():
                return u
        return None

    def rtsp_url(self, channel: int, subtype: int) -> str:
        cred = ""
        if self.username:
            cred = quote(self.username, safe="")
            if self.password:
                cred += ":" + quote(self.password, safe="")
            cred += "@"
        path = self.path_template.format(channel=channel, subtype=subtype)
        if not path.startswith("/"):
            path = "/" + path
        return f"rtsp://{cred}{self.dvr_ip}:{self.rtsp_port}{path}"

    def channel(self, ch: int) -> ChannelConfig:
        for c in self.channels:
            if c.id == ch:
                return c
        return ChannelConfig(id=ch, name=f"Camera {ch}")


class ConfigUpdate(BaseModel):
    """Payload from the settings modal. password=None/'' keeps the stored one."""
    dvr_ip: str
    rtsp_port: int = Field(ge=1, le=65535)
    username: str
    password: Optional[str] = None
    path_template: str
    grid_fps: int = Field(ge=1, le=30)
    jpeg_quality: int = Field(ge=30, le=95)
    channels: list[ChannelConfig]


def load_config() -> DVRConfig:
    raw: dict = {}
    if CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            cfg = DVRConfig.model_validate(raw)
        except Exception as exc:  # corrupt file: fall back to defaults, don't crash
            log.error("Failed to parse %s (%s); using defaults", CONFIG_PATH, exc)
            raw, cfg = {}, DVRConfig()
    else:
        cfg = DVRConfig()
    # Older config files have no users; never start without a way to log in.
    # (Deleting the "users" key is also the way to reset access if locked out.)
    if not any(u.role == "admin" for u in cfg.users):
        log.warning("No admin account found; creating default admin/admin (password change required)")
        cfg = cfg.model_copy(update={"users": [*cfg.users, default_admin()]})
    if raw != cfg.model_dump():
        save_config(cfg)
    return cfg


def save_config(cfg: DVRConfig) -> None:
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg.model_dump(), indent=2), encoding="utf-8")
    tmp.replace(CONFIG_PATH)


def redact(url: str) -> str:
    if "@" in url:
        scheme, rest = url.split("://", 1)
        return f"{scheme}://***@{rest.split('@', 1)[1]}"
    return url


# --------------------------------------------------------------------------- placeholder frames
@lru_cache(maxsize=64)
def placeholder_jpeg(title: str, message: str, w: int = 704, h: int = 480) -> bytes:
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[:] = (24, 20, 17)
    font = cv2.FONT_HERSHEY_SIMPLEX
    for text, scale, y, color in ((title, 1.1, h // 2 - 10, (200, 200, 200)), (message, 0.7, h // 2 + 35, (90, 140, 230))):
        (tw, _), _ = cv2.getTextSize(text, font, scale, 2)
        cv2.putText(img, text, ((w - tw) // 2, y), font, scale, color, 2, cv2.LINE_AA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
    return buf.tobytes()


# --------------------------------------------------------------------------- shared frame cache
class FrameCache:
    """Thread-safe store of the newest JPEG per (channel, subtype).

    Workers write here; HTTP handlers only ever read from here, so a request never
    waits on an RTSP socket. Entries outlive their worker, so a restarted worker
    (config save, HD idle-reap) still has something to show instantly. Frame ids
    come from one global counter so they never repeat across worker restarts.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frames: dict[tuple[int, int], tuple[int, bytes, float]] = {}
        self._next_id = 0

    def put(self, ch: int, subtype: int, jpeg: bytes) -> None:
        with self._lock:
            self._next_id += 1
            self._frames[(ch, subtype)] = (self._next_id, jpeg, time.monotonic())

    def get(self, ch: int, subtype: int, max_age: Optional[float] = None) -> Optional[tuple[int, bytes]]:
        with self._lock:
            entry = self._frames.get((ch, subtype))
        if entry is None or (max_age is not None and time.monotonic() - entry[2] > max_age):
            return None
        return entry[0], entry[1]

    def drop(self, ch: int, subtype: int) -> None:
        with self._lock:
            self._frames.pop((ch, subtype), None)

    def clear(self) -> None:
        with self._lock:
            self._frames.clear()


latest_frames = FrameCache()


# --------------------------------------------------------------------------- stream worker
class StreamWorker(threading.Thread):
    def __init__(self, channel: int, subtype: int, cfg: DVRConfig):
        super().__init__(daemon=True, name=f"cam{channel}-{'main' if subtype == MAIN else 'sub'}")
        self.channel = channel
        self.subtype = subtype
        self.url = cfg.rtsp_url(channel, subtype)
        self.jpeg_quality = cfg.jpeg_quality
        # Main stream is for a single enlarged view: allow a higher encode rate.
        self.encode_interval = 1.0 / (15 if subtype == MAIN else max(cfg.grid_fps, 1))

        self._stop_evt = threading.Event()
        self.status = "connecting"
        self.error = ""
        self.resolution = ""
        self.fps = 0.0
        self.last_frame_at = 0.0
        self.last_access = time.monotonic()
        self.reconnects = 0

    # -- public
    def stop(self) -> None:
        self._stop_evt.set()

    @property
    def stopped(self) -> bool:
        return self._stop_evt.is_set()

    def touch(self) -> None:
        self.last_access = time.monotonic()

    def info(self) -> dict:
        stale = self.last_frame_at and time.monotonic() - self.last_frame_at > 10
        return {
            "status": "stalled" if (self.status == "online" and stale) else self.status,
            "error": self.error,
            "resolution": self.resolution,
            "fps": round(self.fps, 1),
            "reconnects": self.reconnects,
        }

    # -- thread body
    def run(self) -> None:
        backoff = 1.0
        log.info("[%s] starting -> %s", self.name, redact(self.url))
        while not self._stop_evt.is_set():
            self.status = "connecting"
            cap = cv2.VideoCapture(
                self.url,
                cv2.CAP_FFMPEG,
                [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 8000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 8000],
            )
            if not cap.isOpened():
                cap.release()
                self.status, self.error = "offline", "Unable to open RTSP stream"
                log.warning("[%s] connect failed; retry in %.0fs", self.name, backoff)
                self._stop_evt.wait(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
                self.reconnects += 1
                continue

            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            log.info("[%s] connected", self.name)
            self.error = ""
            failures = 0
            last_encode = 0.0
            fps_window_start, fps_frames = time.monotonic(), 0

            while not self._stop_evt.is_set():
                # Always read (to drain the decoder) but only encode at the target rate.
                ok, frame = cap.read()
                if not ok or frame is None:
                    failures += 1
                    if failures >= MAX_CONSECUTIVE_READ_FAILURES:
                        self.error = "Stream lost"
                        break
                    time.sleep(0.02)
                    continue
                failures = 0
                backoff = 1.0
                now = time.monotonic()
                fps_frames += 1
                if now - fps_window_start >= 2.0:
                    self.fps = fps_frames / (now - fps_window_start)
                    fps_window_start, fps_frames = now, 0
                if now - last_encode < self.encode_interval:
                    continue
                last_encode = now
                ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality])
                if not ok:
                    continue
                latest_frames.put(self.channel, self.subtype, buf.tobytes())
                self.last_frame_at = now
                self.status = "online"
                self.resolution = f"{frame.shape[1]}x{frame.shape[0]}"

            cap.release()
            if not self._stop_evt.is_set():
                self.status = "offline"
                self.reconnects += 1
                log.warning("[%s] stream dropped; reconnecting in %.0fs", self.name, backoff)
                self._stop_evt.wait(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
        self.status = "stopped"
        log.info("[%s] stopped", self.name)


# --------------------------------------------------------------------------- manager
class StreamManager:
    def __init__(self) -> None:
        self.cfg = load_config()
        self._lock = threading.Lock()
        self._workers: dict[tuple[int, int], StreamWorker] = {}
        self._hd_viewers: dict[int, int] = {}  # channel -> open HD stream connections
        self._reaper_stop = threading.Event()
        self._reaper = threading.Thread(target=self._reap_loop, daemon=True, name="reaper")

    def start(self) -> None:
        with self._lock:
            for c in self.cfg.channels:
                if c.enabled:
                    self._spawn(c.id, SUB)
        self._reaper.start()

    def shutdown(self) -> None:
        self._reaper_stop.set()
        with self._lock:
            for w in self._workers.values():
                w.stop()
            self._workers.clear()

    # Settings that are baked into a running worker; changing any of them needs a reconnect.
    _STREAM_FIELDS = ("dvr_ip", "rtsp_port", "username", "password", "path_template", "grid_fps", "jpeg_quality")

    def apply_config(self, cfg: DVRConfig) -> None:
        """Apply new settings, touching only the RTSP sessions that must change.

        Connection settings changed -> reconnect every enabled channel.
        Otherwise -> stop workers of newly disabled channels, start newly enabled
        ones, and leave every other live stream (and its cached frame) untouched,
        so toggling one camera or renaming it causes no reconnect anywhere else.
        """
        with self._lock:
            old = self.cfg
            # Users are managed via /api/users only; a DVR settings save never touches them.
            cfg = cfg.model_copy(update={"users": old.users})
            save_config(cfg)
            self.cfg = cfg
            full_restart = any(getattr(old, f) != getattr(cfg, f) for f in self._STREAM_FIELDS)
            enabled = {c.id for c in cfg.channels if c.enabled}
            for key, w in list(self._workers.items()):
                if full_restart or key[0] not in enabled:
                    w.stop()  # threads exit on their own; blocking reads time out
                    del self._workers[key]
                    latest_frames.drop(*key)
            if full_restart:
                latest_frames.clear()  # frames may belong to a different DVR / channel mapping
            for ch in sorted(enabled):
                if (ch, SUB) not in self._workers:
                    self._spawn(ch, SUB)
            active = sorted({k[0] for k in self._workers})
        log.info("Configuration applied (%s); active channels: %s",
                 "full reconnect" if full_restart else "incremental", active or "none")

    def update_users(self, users: list[UserRecord]) -> None:
        with self._lock:
            self.cfg = self.cfg.model_copy(update={"users": users})
            save_config(self.cfg)

    def _spawn(self, ch: int, subtype: int) -> StreamWorker:
        w = StreamWorker(ch, subtype, self.cfg)
        self._workers[(ch, subtype)] = w
        w.start()
        return w

    def get(self, ch: int, subtype: int) -> Optional[StreamWorker]:
        """Return the worker, starting it on demand. None if channel disabled."""
        with self._lock:  # checked under the lock so a concurrent config save can't race a respawn
            if not self.cfg.channel(ch).enabled:
                return None
            w = self._workers.get((ch, subtype))
            if w is None or w.stopped or not w.is_alive():
                w = self._spawn(ch, subtype)
            w.touch()
            return w

    # -- HD (main-stream) viewer tracking. Opening the fullscreen view = one MJPEG viewer.
    def acquire_hd(self, ch: int) -> None:
        with self._lock:
            self._hd_viewers[ch] = self._hd_viewers.get(ch, 0) + 1

    def release_hd(self, ch: int) -> None:
        """Called when an HD stream connection closes. When the last viewer of a channel
        leaves, its subtype=0 RTSP session is torn down immediately (not after the idle
        timeout); the grid keeps running on the always-on subtype=1 stream."""
        with self._lock:
            n = self._hd_viewers.get(ch, 1) - 1
            if n > 0:
                self._hd_viewers[ch] = n
                return
            self._hd_viewers.pop(ch, None)
            w = self._workers.pop((ch, MAIN), None)
        if w:
            w.stop()
            latest_frames.drop(ch, MAIN)  # never show a stale HD frame on the next open
            log.info("[%s] last HD viewer left; released main stream", w.name)

    def _reap_loop(self) -> None:
        # Fallback for HD workers started without a stream viewer (e.g. an HD snapshot download).
        while not self._reaper_stop.wait(5):
            now = time.monotonic()
            with self._lock:
                for key, w in list(self._workers.items()):
                    if key[1] == MAIN and not self._hd_viewers.get(key[0]) and now - w.last_access > MAIN_IDLE_TIMEOUT:
                        log.info("[%s] idle; stopping", w.name)
                        w.stop()
                        del self._workers[key]

    def status(self) -> dict:
        out = {}
        for c in self.cfg.channels:
            sub = self._workers.get((c.id, SUB))
            main = self._workers.get((c.id, MAIN))
            out[c.id] = {
                "name": c.name or f"Camera {c.id}",
                "enabled": c.enabled,
                "sub": sub.info() if sub else {"status": "disabled" if not c.enabled else "stopped"},
                "main": main.info() if main else None,
            }
        return out


manager = StreamManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    manager.start()
    yield
    manager.shutdown()


app = FastAPI(title="Q-See DVR Dashboard", lifespan=lifespan)

NO_CACHE = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}


def _check_channel(ch: int) -> None:
    if not 1 <= ch <= NUM_CHANNELS:
        raise HTTPException(404, f"Channel must be 1-{NUM_CHANNELS}")


def _subtype(q: str) -> int:
    return MAIN if q == "main" else SUB


def _frame_or_placeholder(ch: int, subtype: int, w: Optional[StreamWorker]) -> tuple[int, bytes]:
    """Read-only lookup in the in-memory cache; never touches the network."""
    name = manager.cfg.channel(ch).name or f"Camera {ch}"
    if w is None:
        return -1, placeholder_jpeg(name, "CHANNEL DISABLED")
    if subtype == MAIN:
        # Use the HD frame only if it is live. While the HD stream is still
        # connecting (or was idle-reaped), show the always-warm sub-stream frame
        # so the enlarged view is never blank.
        hit = latest_frames.get(ch, MAIN, max_age=3.0) or latest_frames.get(ch, SUB)
    else:
        hit = latest_frames.get(ch, SUB)
    if hit is None:
        msg = {"connecting": "CONNECTING...", "offline": "NO SIGNAL - RECONNECTING"}.get(w.status, w.status.upper())
        return -1, placeholder_jpeg(name, msg)
    return hit


# --------------------------------------------------------------------------- auth dependencies
class AuthContext:
    def __init__(self, token: str, user: UserRecord):
        self.token, self.user = token, user


def current_session(request: Request) -> AuthContext:
    """Any logged-in user, including one who still has to change the default password."""
    token = request.cookies.get(auth.SESSION_COOKIE)
    s = auth.sessions.get(token)
    user = manager.cfg.user(s.username) if s else None
    if user is None:  # no/expired session, or the account was deleted
        auth.sessions.revoke(token)
        raise HTTPException(401, "Not authenticated")
    return AuthContext(token, user)


def require_user(ctx: AuthContext = Depends(current_session)) -> AuthContext:
    if ctx.user.must_change_password:
        raise HTTPException(403, "password_change_required")
    return ctx


def require_admin(ctx: AuthContext = Depends(require_user)) -> AuthContext:
    if ctx.user.role != "admin":
        raise HTTPException(403, "Administrator access required")
    return ctx


@app.exception_handler(StarletteHTTPException)
async def auth_redirect(request: Request, exc: StarletteHTTPException):
    """Browser page loads without a session go to /login; API/stream calls get a JSON 401."""
    if exc.status_code == 401 and not request.url.path.startswith(("/api/", "/stream/")):
        return RedirectResponse("/login", status_code=303)
    return await http_exception_handler(request, exc)


def _me(user: UserRecord) -> dict:
    return {"username": user.username, "role": user.role, "must_change_password": user.must_change_password}


# --------------------------------------------------------------------------- auth routes
class LoginBody(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


class PasswordChange(BaseModel):
    current_password: Optional[str] = Field(default=None, max_length=256)
    new_password: str = Field(max_length=256)


class NewUser(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)
    role: str = "viewer"


@app.get("/login", include_in_schema=False)
def login_page(request: Request):
    if auth.sessions.get(request.cookies.get(auth.SESSION_COOKIE)):
        return RedirectResponse("/", status_code=303)
    return FileResponse(STATIC_DIR / "login.html", headers=NO_CACHE)


@app.post("/api/login")
def login(body: LoginBody, request: Request):
    ip = request.client.host if request.client else "?"
    wait = auth.throttle.retry_after(ip)
    if wait:
        raise HTTPException(429, f"Too many failed attempts. Try again in {wait}s.",
                            headers={"Retry-After": str(wait)})
    user = manager.cfg.user(body.username.strip())
    if user is None:
        auth.burn_time(body.password)
    if user is None or not auth.verify_password(body.password, user.password_hash):
        auth.throttle.fail(ip)
        log.warning("Failed login for %r from %s", body.username[:64], ip)
        raise HTTPException(401, "Invalid username or password")
    auth.throttle.reset(ip)
    token = auth.sessions.create(user.username)
    log.info("Login: %s from %s", user.username, ip)
    resp = JSONResponse(_me(user))
    resp.set_cookie(auth.SESSION_COOKIE, token, max_age=auth.SESSION_MAX_AGE, httponly=True,
                    samesite="strict", secure=request.url.scheme == "https", path="/")
    return resp


@app.post("/api/logout")
def logout(request: Request):
    auth.sessions.revoke(request.cookies.get(auth.SESSION_COOKIE))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.SESSION_COOKIE, path="/")
    return resp


@app.get("/api/me")
def me(ctx: AuthContext = Depends(current_session)):
    return _me(ctx.user)


@app.get("/api/users")
def list_users(_: AuthContext = Depends(require_admin)):
    return [_me(u) for u in manager.cfg.users]


@app.post("/api/users", status_code=201)
def add_user(body: NewUser, ctx: AuthContext = Depends(require_admin)):
    username = body.username.strip()
    if not auth.USERNAME_RE.match(username):
        raise HTTPException(422, "Username must be 1-32 characters: letters, digits, '.', '_' or '-'")
    if body.role not in auth.ROLES:
        raise HTTPException(422, "Role must be one of: " + ", ".join(auth.ROLES))
    problem = auth.password_problem(body.password, username)
    if problem:
        raise HTTPException(422, problem)
    if manager.cfg.user(username):
        raise HTTPException(409, f"User '{username}' already exists")
    # The admin chose this password, so the new user must replace it on first login.
    new = UserRecord(username=username, password_hash=auth.hash_password(body.password), role=body.role,
                     must_change_password=True)
    manager.update_users([*manager.cfg.users, new])
    log.info("User %s added %s (%s)", ctx.user.username, username, body.role)
    return _me(new)


@app.put("/api/users/{username}/password")
def change_password(username: str, body: PasswordChange, ctx: AuthContext = Depends(current_session)):
    target = manager.cfg.user(username)
    if target is None:
        raise HTTPException(404, "No such user")
    is_self = target.username == ctx.user.username
    if is_self:
        if not body.current_password or not auth.verify_password(body.current_password, target.password_hash):
            raise HTTPException(400, "Current password is incorrect")
        if auth.verify_password(body.new_password, target.password_hash):
            raise HTTPException(400, "New password must be different from the current one")
    elif ctx.user.role != "admin" or ctx.user.must_change_password:
        raise HTTPException(403, "Administrator access required")
    problem = auth.password_problem(body.new_password, target.username)
    if problem:
        raise HTTPException(422, problem)
    updated = target.model_copy(update={
        "password_hash": auth.hash_password(body.new_password),
        # Own change clears the flag; an admin reset makes the user pick their own password next login.
        "must_change_password": not is_self,
    })
    manager.update_users([updated if u.username == target.username else u for u in manager.cfg.users])
    # Sign the account out everywhere else (every session, for an admin reset).
    auth.sessions.revoke_user(target.username, keep=ctx.token if is_self else None)
    log.info("Password for %s changed by %s", target.username, ctx.user.username)
    return _me(updated)


@app.delete("/api/users/{username}")
def delete_user(username: str, ctx: AuthContext = Depends(require_admin)):
    target = manager.cfg.user(username)
    if target is None:
        raise HTTPException(404, "No such user")
    if target.username == ctx.user.username:
        raise HTTPException(400, "You can't delete your own account")
    if target.role == "admin" and sum(u.role == "admin" for u in manager.cfg.users) <= 1:
        raise HTTPException(400, "Can't delete the last administrator")
    manager.update_users([u for u in manager.cfg.users if u.username != target.username])
    auth.sessions.revoke_user(target.username)
    log.info("User %s deleted by %s", target.username, ctx.user.username)
    return {"ok": True}


# --------------------------------------------------------------------------- dashboard routes
@app.get("/", include_in_schema=False)
def index(_: AuthContext = Depends(current_session)):
    return FileResponse(STATIC_DIR / "index.html", headers=NO_CACHE)


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/config")
def get_config(_: AuthContext = Depends(require_user)):
    data = manager.cfg.model_dump(exclude={"users"})
    data["has_password"] = bool(data.pop("password"))
    data["password"] = ""
    return data


@app.put("/api/config")
def put_config(update: ConfigUpdate, ctx: AuthContext = Depends(require_admin)):
    ids = sorted(c.id for c in update.channels)
    if ids != list(range(1, NUM_CHANNELS + 1)):
        raise HTTPException(422, f"channels must contain ids 1-{NUM_CHANNELS} exactly once")
    if "{channel}" not in update.path_template or "{subtype}" not in update.path_template:
        raise HTTPException(422, "path_template must contain {channel} and {subtype}")
    data = update.model_dump()
    if not data.get("password"):
        data["password"] = manager.cfg.password
    cfg = DVRConfig.model_validate(data)
    manager.apply_config(cfg)
    return get_config(ctx)


@app.get("/api/status")
def status(_: AuthContext = Depends(require_user)):
    return manager.status()


@app.get("/api/snapshot/{ch}")
def snapshot(ch: int, q: str = Query("sub", pattern="^(sub|main)$"), since: int = -2,
             _: AuthContext = Depends(require_user)):
    """Latest JPEG. If `since` equals the current frame id, returns 204 (no new frame)."""
    _check_channel(ch)
    subtype = _subtype(q)
    w = manager.get(ch, subtype)
    fid, jpeg = _frame_or_placeholder(ch, subtype, w)
    if fid >= 0 and fid == since:
        return Response(status_code=204, headers=NO_CACHE)
    return Response(jpeg, media_type="image/jpeg", headers={**NO_CACHE, "X-Frame-Id": str(fid)})


@app.get("/stream/{ch}")
async def mjpeg(ch: int, request: Request, q: str = Query("sub", pattern="^(sub|main)$"),
                ctx: AuthContext = Depends(require_user)):
    """multipart/x-mixed-replace MJPEG stream - usable in <img>, VLC, Home Assistant, etc."""
    _check_channel(ch)
    subtype = _subtype(q)

    async def gen():
        last_id = None
        last_sent = 0.0
        # An HD stream holds the channel's subtype=0 RTSP session open; closing the
        # connection (Minimize / Esc / tab closed) releases it right away.
        if subtype == MAIN:
            manager.acquire_hd(ch)
        try:
            # Stop streaming as soon as the session is logged out or revoked.
            while auth.sessions.alive(ctx.token) and not await request.is_disconnected():
                w = manager.get(ch, subtype)  # also refreshes idle timer / restarts worker
                fid, jpeg = _frame_or_placeholder(ch, subtype, w)
                now = time.monotonic()
                # Send new frames immediately; resend placeholders/unchanged frames every 2s as keep-alive.
                if fid != last_id or now - last_sent > 2.0:
                    last_id, last_sent = fid, now
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                           + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")
                await asyncio.sleep(0.03)
        finally:
            if subtype == MAIN:
                manager.release_hd(ch)

    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame", headers=NO_CACHE)
