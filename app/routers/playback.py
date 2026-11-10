"""DVR HDD playback: search a channel + date/time range and stream the recording to the dashboard.

The DVR serves recordings over Dahua's time-shifted RTSP endpoint:

    rtsp://user:pwd@ip:port/cam/playback?channel=N&subtype=0
        &starttime=YYYY_MM_DD_HH_MM_SS&endtime=YYYY_MM_DD_HH_MM_SS

POST /api/playback/stream-url  validates a search and returns its metadata (and, for admins when
                               enabled in Settings, the RTSP URI for VLC).
GET  /api/playback/stream      plays a recording as MJPEG (multipart/x-mixed-replace), the same
                               format the live tiles use, so it works in a plain <img>.
GET  /api/playback/status      position / state of a stream, for the seek bar.
GET  /api/playback/frame       the stream's current frame as a JPEG, shown while paused.

An MJPEG stream can't seek or pause, so the player does both by restarting the DVR stream:
seeking opens a new stream whose start_time is the chosen moment, and pausing freezes the
current frame and closes the stream (resuming opens one at the paused position). That also
means a paused player holds no DVR connection.

Each login session plays at most one recording at a time - a new stream (seek / resume)
replaces the previous one - and at most MAX_SESSIONS play at once in total, so playback can't
starve the live cameras of DVR connections.

The DVR connection comes from the same saved settings the live streams use
(dvr_config.json, edited in the Settings panel; 192.168.5.11:554 on this setup). Each value
can be overridden with an environment variable: DVR_IP, DVR_PORT, DVR_USER, DVR_PASS.
Streaming never exposes credentials: the server builds the URI.

The router is built by create_router() with main.py's settings and auth hooks passed
in, so this module never imports app.main (which would be a circular import).
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Optional
from urllib.parse import quote

import cv2
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from app import auth

log = logging.getLogger("qseedvr")

NUM_CHANNELS = 8
INPUT_FORMAT = "%Y-%m-%d %H:%M:%S"     # what the API accepts
DAHUA_FORMAT = "%Y_%m_%d_%H_%M_%S"     # what the DVR expects in starttime / endtime
PLAYBACK_PATH = "/cam/playback?channel={channel}&subtype=0&starttime={start}&endtime={end}"
MAX_RANGE = timedelta(hours=24)        # longest single search
MAX_SESSIONS = 2                       # concurrent playback streams (each is one DVR RTSP session)
ENCODE_FPS = 15                        # same rate as the live HD view
MAX_READ_FAILURES = 25                 # consecutive failed reads = recording over / stream lost
SID_PATTERN = r"^[A-Za-z0-9_-]{8,64}$"
NO_CACHE = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}


# --------------------------------------------------------------------------- request / URI
class PlaybackRequest(BaseModel):
    channel: int = Field(ge=1, le=NUM_CHANNELS)
    start_time: str = Field(description="YYYY-MM-DD HH:MM:SS (DVR local time)")
    end_time: str = Field(description="YYYY-MM-DD HH:MM:SS (DVR local time)")


def parse_time(value: str, field: str) -> datetime:
    try:
        return datetime.strptime(value.strip(), INPUT_FORMAT)
    except ValueError:
        raise HTTPException(422, f"{field} must be in the format YYYY-MM-DD HH:MM:SS")


def parse_range(start_time: str, end_time: str) -> tuple[datetime, datetime]:
    start = parse_time(start_time, "start_time")
    end = parse_time(end_time, "end_time")
    if end <= start:
        raise HTTPException(422, "end_time must be after start_time")
    if end - start > MAX_RANGE:
        raise HTTPException(422, "The time range can be at most 24 hours")
    return start, end


@dataclass
class DVRTarget:
    ip: str
    port: int
    username: str
    password: str


def dvr_target(cfg: Any) -> DVRTarget:
    """Saved settings, with any DVR_IP / DVR_PORT / DVR_USER / DVR_PASS env var taking precedence."""
    port = os.getenv("DVR_PORT")
    try:
        port_num = int(port) if port else cfg.rtsp_port
    except ValueError:
        raise HTTPException(500, "DVR_PORT environment variable must be a number")
    return DVRTarget(
        ip=os.getenv("DVR_IP") or cfg.dvr_ip,
        port=port_num,
        username=os.getenv("DVR_USER") or cfg.username,
        password=os.getenv("DVR_PASS") or cfg.password,
    )


def playback_url(dvr: DVRTarget, channel: int, start: datetime, end: datetime, with_credentials: bool = True) -> str:
    cred = ""
    if dvr.username:
        if with_credentials:
            cred = quote(dvr.username, safe="")
            if dvr.password:
                cred += ":" + quote(dvr.password, safe="")
        else:
            cred = "***"
        cred += "@"
    path = PLAYBACK_PATH.format(channel=channel, start=start.strftime(DAHUA_FORMAT), end=end.strftime(DAHUA_FORMAT))
    return f"rtsp://{cred}{dvr.ip}:{dvr.port}{path}"


# --------------------------------------------------------------------------- playback session
class PlaybackSession(threading.Thread):
    """One stream of a recording: reads the DVR playback stream and keeps the newest JPEG.

    No reconnect loop (unlike live workers): reopening would restart the recording from
    its start time. When reads keep failing the recording is treated as finished.
    """

    def __init__(self, url: str, safe_url: str, channel: int, duration: float, jpeg_quality: int):
        super().__init__(daemon=True, name=f"cam{channel}-playback")
        self.url, self.safe_url = url, safe_url
        self.jpeg_quality = jpeg_quality
        # Safety stop in case the DVR keeps sending past end_time.
        self.max_runtime = duration * 2 + 120
        self._stop_evt = threading.Event()
        self._lock = threading.Lock()
        self.frame: Optional[tuple[int, bytes]] = None
        self.status = "connecting"     # connecting -> playing -> ended | no_recording | stopped
        self.position = 0.0            # seconds of footage played since this stream's start_time
        self.done = False

    def stop(self) -> None:
        self._stop_evt.set()

    @property
    def stopped(self) -> bool:
        return self._stop_evt.is_set()

    def latest(self) -> Optional[tuple[int, bytes]]:
        with self._lock:
            return self.frame

    def info(self) -> dict:
        return {"status": self.status, "position": round(self.position, 1)}

    def run(self) -> None:
        log.info("[%s] starting -> %s", self.name, self.safe_url)
        started = time.monotonic()
        cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG,
                               [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 10000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 10000])
        try:
            if not cap.isOpened():
                self.status = "stopped" if self.stopped else "no_recording"
                if not self.stopped:
                    log.warning("[%s] could not open playback stream", self.name)
                return
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            failures, last_encode, frame_id = 0, 0.0, 0
            clock: Optional[tuple[float, float]] = None   # (first frame's video ms, wall time)
            first_frame_at: Optional[float] = None
            while not self._stop_evt.is_set():
                if time.monotonic() - started > self.max_runtime:
                    break
                ok, frame = cap.read()
                if not ok or frame is None:
                    failures += 1
                    if failures >= MAX_READ_FAILURES:
                        break
                    time.sleep(0.02)
                    continue
                failures = 0
                # Pace by the video's own timestamps so footage plays at real speed even if
                # the DVR sends faster than real time. (A real-time source needs no waiting.)
                pos = cap.get(cv2.CAP_PROP_POS_MSEC)
                if pos > 0:
                    if clock is None:
                        clock = (pos, time.monotonic())
                    ahead = (pos - clock[0]) / 1000 - (time.monotonic() - clock[1])
                    if ahead > 0:
                        self._stop_evt.wait(min(ahead, 1.0))
                    self.position = max(self.position, (pos - clock[0]) / 1000)
                else:                                          # no timestamps: fall back to wall time
                    first_frame_at = first_frame_at or time.monotonic()
                    self.position = time.monotonic() - first_frame_at
                now = time.monotonic()
                if now - last_encode < 1.0 / ENCODE_FPS:
                    continue
                last_encode = now
                ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality])
                if ok:
                    frame_id += 1
                    with self._lock:
                        self.frame = (frame_id, buf.tobytes())
                    self.status = "playing"
            if self.stopped:
                self.status = "stopped"
            elif self.status == "connecting":      # opened, but the DVR sent no video
                self.status = "no_recording"
            else:
                self.status = "ended"
        finally:
            cap.release()
            self.done = True
            log.info("[%s] stopped (%s)", self.name, self.status)


class PlaybackRegistry:
    """Tracks streams per login session (auth token): one active stream per token, at most
    `limit` tokens streaming at once. The last stream's final state is kept per token so the
    seek bar can still see "ended" after the stream has closed."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._lock = threading.Lock()
        self._active: dict[str, tuple[str, PlaybackSession]] = {}
        self._last: dict[str, tuple[str, dict]] = {}

    def start(self, token: str, sid: str, session: PlaybackSession) -> bool:
        with self._lock:
            prev = self._active.get(token)
            if prev is None and len(self._active) >= self.limit:
                return False
            self._active[token] = (sid, session)     # a seek / resume takes over the same slot
        if prev:
            prev[1].stop()
        return True

    def finish(self, token: str, sid: str, session: PlaybackSession) -> None:
        with self._lock:
            cur = self._active.get(token)
            if cur and cur[1] is session:
                del self._active[token]
            self._last[token] = (sid, session.info())
            if len(self._last) > 200:                # tokens expire; don't grow forever
                self._last.pop(next(iter(self._last)))

    def get(self, token: str, sid: str) -> Optional[PlaybackSession]:
        with self._lock:
            cur = self._active.get(token)
        return cur[1] if cur and cur[0] == sid else None

    def info(self, token: str, sid: str) -> dict:
        s = self.get(token, sid)
        if s:
            return s.info()
        with self._lock:
            last = self._last.get(token)
        return last[1] if last and last[0] == sid else {"status": "none", "position": 0}


# --------------------------------------------------------------------------- routes
def create_router(get_config: Callable[[], Any], require_user: Callable,
                  placeholder: Callable[[str, str], bytes]) -> APIRouter:
    """get_config() returns the live DVRConfig; require_user is the logged-in-user dependency;
    placeholder(title, message) renders a status JPEG like the live tiles show."""
    router = APIRouter(prefix="/api/playback", tags=["playback"])
    registry = PlaybackRegistry(MAX_SESSIONS)

    def channel_name(cfg: Any, ch: int) -> str:
        return cfg.channel(ch).name or f"Camera {ch}"

    @router.post("/stream-url")
    def stream_url(body: PlaybackRequest, ctx=Depends(require_user)):
        start, end = parse_range(body.start_time, body.end_time)
        cfg = get_config()
        dvr = dvr_target(cfg)
        # The full URI contains the DVR password: admins only, and only when enabled in Settings.
        share = ctx.user.role == "admin" and getattr(cfg, "playback_copy_link", False)
        masked = playback_url(dvr, body.channel, start, end, with_credentials=False)
        return {
            "success": True,
            "channel": body.channel,
            "channel_name": channel_name(cfg, body.channel),
            "start_time": start.strftime(INPUT_FORMAT),
            "end_time": end.strftime(INPUT_FORMAT),
            "duration_seconds": int((end - start).total_seconds()),
            "dvr_ip": dvr.ip,
            "rtsp_port": dvr.port,
            "subtype": 0,
            "playback_rtsp_url": playback_url(dvr, body.channel, start, end) if share else masked,
            "credentials_included": share,
            "rtsp_url_masked": masked,
        }

    @router.get("/stream")
    async def stream(request: Request,
                     channel: int = Query(ge=1, le=NUM_CHANNELS),
                     start_time: str = Query(), end_time: str = Query(),
                     sid: str = Query(pattern=SID_PATTERN),
                     ctx=Depends(require_user)):
        """MJPEG of the recording from start_time to end_time. Status (connecting / no recording /
        ended / busy) is sent as placeholder frames so the <img> always shows what is happening."""
        start, end = parse_range(start_time, end_time)
        cfg = get_config()
        dvr = dvr_target(cfg)
        title = f"{channel_name(cfg, channel)} - playback"

        def part(jpeg: bytes) -> bytes:
            return (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")

        async def gen():
            session = PlaybackSession(playback_url(dvr, channel, start, end),
                                      playback_url(dvr, channel, start, end, with_credentials=False),
                                      channel, (end - start).total_seconds(), cfg.jpeg_quality)
            if not registry.start(ctx.token, sid, session):
                yield part(placeholder(title, f"BUSY - {MAX_SESSIONS} PLAYBACKS ALREADY RUNNING"))
                return
            session.start()
            try:
                last_id, last_sent = None, 0.0
                while (not session.stopped and auth.sessions.alive(ctx.token)
                       and not await request.is_disconnected()):
                    hit = session.latest()
                    now = time.monotonic()
                    if hit and hit[0] != last_id:
                        last_id, last_sent = hit[0], now
                        yield part(hit[1])
                    elif session.done:
                        msg = {"no_recording": "NO RECORDING FOUND FOR THIS TIME RANGE"}.get(
                            session.status, "END OF RECORDING")
                        yield part(placeholder(title, msg))
                        return
                    elif hit is None and now - last_sent > 2.0:      # keep-alive while connecting
                        last_sent = now
                        yield part(placeholder(title, "CONNECTING TO DVR..."))
                    await asyncio.sleep(0.03)
            finally:
                session.stop()          # closes the DVR's RTSP session; the thread exits on its own
                registry.finish(ctx.token, sid, session)

        return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame", headers=NO_CACHE)

    @router.get("/status")
    def status(sid: str = Query(pattern=SID_PATTERN), ctx=Depends(require_user)):
        return registry.info(ctx.token, sid)

    @router.get("/frame")
    def frame(sid: str = Query(pattern=SID_PATTERN), ctx=Depends(require_user)):
        s = registry.get(ctx.token, sid)
        hit = s.latest() if s else None
        if hit is None:
            raise HTTPException(404, "No frame available")
        return Response(hit[1], media_type="image/jpeg", headers=NO_CACHE)

    return router
