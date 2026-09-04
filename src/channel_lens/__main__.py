"""Launcher: start the local server and open a browser at it.

Picking a free port when the configured one is taken matters more than it
sounds — the alternative is a double-clicked app that dies on startup with
"address already in use" behind a window that has already closed.
"""

from __future__ import annotations

import argparse
import logging
import socket
import sys
import threading
import webbrowser

import uvicorn

from .config import app_home, get_settings


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _pick_port(preferred: int) -> int:
    """Use the preferred port, or the next free one above it.

    >>> _pick_port(0) > 0
    True
    """
    if preferred and _port_is_free(preferred):
        return preferred
    for offset in range(1, 20):
        candidate = preferred + offset
        if _port_is_free(candidate):
            return candidate
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _make_console_unicode_safe() -> None:
    """Stop a non-UTF-8 console from crashing the app on a stray character.

    Windows consoles frequently default to cp1252, which cannot encode most of
    what this app prints. That is not a cosmetic problem: an un-encodable
    character raises ``UnicodeEncodeError`` from ``print`` or from a log
    handler, and on startup it kills the process outright.

    YouTube titles and channel names routinely contain emoji, CJK, and dashes
    that cp1252 has no mapping for, so this is a matter of when rather than if.
    UTF-8 is requested, and ``errors="replace"`` guarantees that even a console
    that refuses it degrades to a replacement character instead of an exception.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            # Redirected to something that can't be reconfigured; nothing to do.
            pass


def main() -> None:
    _make_console_unicode_safe()
    parser = argparse.ArgumentParser(prog="channel-lens", description=__doc__)
    parser.add_argument("--port", type=int, default=None, help="Port to serve on.")
    parser.add_argument("--no-browser", action="store_true", help="Don't open a browser.")
    parser.add_argument("--reload", action="store_true", help="Auto-reload on code changes.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    config = get_settings()
    port = _pick_port(args.port or config.port)
    if port != config.port:
        # Persist it so the OAuth redirect URI matches what's actually serving.
        config.port = port
        config.save()
        get_settings(refresh=True)

    url = f"http://localhost:{port}"
    print(f"\n  Channel Lens  →  {url}")
    print(f"  Data and settings: {app_home()}\n")

    if config.open_browser and not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    uvicorn.run(
        "channel_lens.main:app",
        host="127.0.0.1",
        port=port,
        reload=args.reload,
        log_level="debug" if args.verbose else "warning",
    )


if __name__ == "__main__":
    main()
