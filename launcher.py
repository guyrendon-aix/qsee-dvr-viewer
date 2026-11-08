"""Entry point for the packaged desktop builds (PyInstaller) - also works from source:

    python launcher.py [--host 127.0.0.1] [--port 8000] [--no-browser]

Starts the dashboard server and opens it in the default browser.
"""
from __future__ import annotations

import argparse
import socket
import sys
import threading
import webbrowser


def _redirect_output_if_windowless() -> None:
    """A macOS .app (or any windowed build) has no usable console: send logs to a file
    in the user data dir so problems can still be diagnosed."""
    if sys.stdout is not None and sys.stderr is not None and not (getattr(sys, "frozen", False) and sys.platform == "darwin"):
        return
    from app.main import user_data_dir
    log_dir = user_data_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log = open(log_dir / "qseedvr.log", "a", buffering=1, encoding="utf-8")
    sys.stdout = sys.stderr = log


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def main() -> int:
    p = argparse.ArgumentParser(prog="qseedvr", description="Q-See DVR web dashboard")
    p.add_argument("--host", default="127.0.0.1",
                   help="interface to listen on (use 0.0.0.0 for LAN access - trusted networks only)")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--no-browser", action="store_true", help="don't open the dashboard in a browser")
    args = p.parse_args()

    _redirect_output_if_windowless()
    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}/"

    if not _port_free(args.host, args.port):
        # Most likely the app is already running: just bring the dashboard up.
        print(f"Port {args.port} is already in use; opening {url}", flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        return 0

    import uvicorn
    from app.main import CONFIG_PATH, app

    print(f"Q-See DVR dashboard on {url}  (config: {CONFIG_PATH})", flush=True)
    if not args.no_browser:
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
