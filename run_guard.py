"""24/7 watchdog for the Q-See DVR viewer.

Launches the Uvicorn server with this interpreter (run it with the venv's Python) and
restarts it after any unexpected exit. Press Ctrl+C to shut down cleanly.

    venv\\Scripts\\python.exe run_guard.py      (Windows)
    venv/bin/python run_guard.py               (macOS/Linux)

The guard does not poll: it blocks on the server process (no CPU use) and only wakes when
the server exits. It then waits GUARD_RESTART_DELAY seconds (default 3) before restarting.
If the server keeps dying right after starting (e.g. port 8000 is taken), the wait doubles
on each quick failure, up to 60 seconds, and resets once the server stays up.

Settings are read on launch from the environment, or from a .env file next to this script
(see .env.example). A real environment variable wins over the same key in .env. Values in
.env are also passed on to the server, so DVR_IP / DVR_PASS etc. can live there too.

Events are logged to the console and to run_guard.log next to this script.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CMD = [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

DEFAULT_RESTART_DELAY = 3.0    # seconds before restarting after an exit
MIN_RESTART_DELAY = 1.0        # never restart faster than this, whatever the setting says
MAX_RESTART_DELAY = 300.0
MAX_BACKOFF = 60.0             # cap for the delay while the server keeps failing at startup
QUICK_FAILURE = 10.0           # an exit within this many seconds of starting counts as a quick failure
SHUTDOWN_TIMEOUT = 10          # seconds to let the server stop on its own before killing it

log = logging.getLogger("run_guard")


def _setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s run_guard: %(message)s")
    log.setLevel(logging.INFO)
    for handler in (logging.StreamHandler(sys.stdout),
                    logging.FileHandler(ROOT / "run_guard.log", encoding="utf-8")):
        handler.setFormatter(fmt)
        log.addHandler(handler)


def _load_env_file(path: Path) -> None:
    """Minimal .env reader (KEY=VALUE lines, # comments, optional quotes). Existing
    environment variables are never overridden."""
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        log.warning("could not read %s (%s); ignoring it", path, exc)
        return
    for n, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key:
            log.warning("%s line %d is not KEY=VALUE; ignoring it", path.name, n)
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _restart_delay() -> float:
    raw = os.environ.get("GUARD_RESTART_DELAY", "").strip()
    if not raw:
        return DEFAULT_RESTART_DELAY
    try:
        value = float(raw)
    except ValueError:
        log.warning("GUARD_RESTART_DELAY=%r is not a number; using %ss", raw, DEFAULT_RESTART_DELAY)
        return DEFAULT_RESTART_DELAY
    if not MIN_RESTART_DELAY <= value <= MAX_RESTART_DELAY:
        clamped = min(max(value, MIN_RESTART_DELAY), MAX_RESTART_DELAY)
        log.warning("GUARD_RESTART_DELAY=%s is outside %s-%ss; using %ss",
                    raw, MIN_RESTART_DELAY, MAX_RESTART_DELAY, clamped)
        return clamped
    return value


def _sigterm_to_interrupt(signum, frame) -> None:
    # Treat a service-manager stop (SIGTERM) like Ctrl+C so the same clean path runs.
    raise KeyboardInterrupt


def _stop(proc: subprocess.Popen) -> None:
    """Stop the server. On Ctrl+C the child usually got the same signal from the console,
    so give it time to exit gracefully before terminating / killing it."""
    if proc.poll() is not None:
        return
    try:
        proc.wait(timeout=SHUTDOWN_TIMEOUT)
        return
    except subprocess.TimeoutExpired:
        pass
    log.warning("server did not stop within %ss, terminating", SHUTDOWN_TIMEOUT)
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        log.warning("server still running, killing it")
        proc.kill()
        proc.wait()


def main() -> int:
    _setup_logging()
    _load_env_file(ROOT / ".env")
    base_delay = _restart_delay()
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _sigterm_to_interrupt)

    log.info("guard started: %s (cwd %s); restart delay %ss", " ".join(CMD), ROOT, f"{base_delay:g}")
    proc: subprocess.Popen | None = None
    restarts = 0
    delay = base_delay
    try:
        while True:
            started = time.monotonic()
            proc = subprocess.Popen(CMD, cwd=ROOT)
            log.info("server started (pid %s)", proc.pid)
            code = proc.wait()                     # blocks until the server exits; no polling
            uptime = time.monotonic() - started
            restarts += 1
            if uptime < QUICK_FAILURE:
                if restarts > 1:
                    delay = min(delay * 2, max(MAX_BACKOFF, base_delay))
                log.warning("server died %.1fs after starting - check that port 8000 is free "
                            "and the config is valid", uptime)
            else:
                delay = base_delay                 # it ran fine for a while: back to the normal delay
            log.error("server exited unexpectedly (code %s) after %.1fs; restart #%d in %ss",
                      code, uptime, restarts, f"{delay:g}")
            time.sleep(delay)
    except KeyboardInterrupt:
        log.info("shutdown requested, stopping server")
        if proc is not None:
            try:
                _stop(proc)
            except KeyboardInterrupt:  # second Ctrl+C: stop waiting
                proc.kill()
        log.info("guard stopped")
        return 0


if __name__ == "__main__":
    sys.exit(main())
