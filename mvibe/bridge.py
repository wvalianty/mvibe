"""Remote bridge: mirror Claude's output to WeChat and inject WeChat input back.

Two halves, both decoupled from the PTY wrapper via the flag file + inject FIFO:

  output: tail the session transcript -> push assistant text to the phone
          (only while flag == remote, unless --always)
  input:  a tiny HTTP receiver. Anything that can POST text drives the session:
            POST /inbound        body = message text -> inject + flag=remote
            POST /flag/local     -> hand control back to the local terminal
            POST /flag/remote    -> take over from remote
            GET  /status         -> {"flag": ...}

Wiring an actual WeChat inbound source to POST /inbound is intentionally left as
a thin adapter (avibe already owns the bot's inbound webhook); for local testing
use `mvibe send` or curl.
"""

from __future__ import annotations

import asyncio
import hmac
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import paths, wrapper
from .tailer import follow

_MAX_CHUNK = 1800
_MAX_BODY = 64 * 1024  # cap inbound POST bodies (DoS guard)
# Optional shared secret; when set, /inbound and /flag/* require X-MVIBE-Token.
_HTTP_TOKEN = os.environ.get("MVIBE_HTTP_TOKEN", "")

# When the bridge runs inside `mvibe up` (same terminal as the claude TUI), its
# logs must NOT hit stdout or they corrupt the full-screen TUI. _LOG_PATH
# redirects them to a file instead.
_LOG_PATH: Path | None = None


def _log(msg: str) -> None:
    if _LOG_PATH is None:
        print(msg, flush=True)
        return
    try:
        with _LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(msg + "\n")
    except OSError:
        pass


def _chunk(text: str, size: int = _MAX_CHUNK):
    for i in range(0, len(text), size):
        yield text[i : i + size]


def _mirror_loop(cwd: Path | None, always: bool, user: str | None) -> None:
    from . import wechat_out

    # Output mirroring follows the remote *gate* (the /mvibe-on/off switch), NOT
    # the routing flag. The flag flips to local the instant you touch the local
    # keyboard (input reclaim); tying output to it would silently drop replies.
    # The gate is the explicit "phone is connected" switch.
    for text in follow(cwd):
        if not always and not paths.remote_enabled():
            continue
        ok = True
        for piece in _chunk(text):
            try:
                wechat_out.send_text(piece, user=user)
            except Exception as exc:  # keep the loop alive on transient errors
                _log(f"[mirror] send failed: {exc}")
                ok = False
                break
        if ok:
            _log(f"[mirror] -> phone: {text[:50]!r}")


def _inbound_drive(text: str) -> None:
    """A remote message takes over: route to remote and inject as keystrokes."""
    paths.write_flag("remote")
    try:
        wrapper.inject(text, submit=True)
    except Exception as exc:
        _log(f"[inbound] inject failed: {exc}")


def _wechat_inbound_loop() -> None:
    from . import config, wechat_in

    if not config.allowlist_active():
        _log(
            "[wechat_in] WARNING: no allowlist — ANY user who messages the bot can "
            "drive Claude (full tool access). Set wechat.user_id / wechat.allowed_users."
        )

    async def on_message(text: str, user_id: str) -> None:
        if not paths.remote_enabled():
            _log(f"[wechat_in] remote OFF, ignoring <{user_id}> {text[:40]}")
            return
        if not config.is_authorized(user_id):
            _log(f"[wechat_in] DROP unauthorized <{user_id}> {text[:40]}")
            return
        _log(f"[wechat_in] <{user_id}> {text[:60]}")
        _inbound_drive(text)

    try:
        asyncio.run(wechat_in.poll_forever(on_message))
    except Exception as exc:
        _log(f"[wechat_in] stopped: {exc}")


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):  # silence default logging
        pass

    def _reply(self, code: int, body: str = "ok") -> None:
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/status":
            self._reply(200, f'{{"flag": "{paths.read_flag()}"}}')
        else:
            self._reply(404, "not found")

    def _authed(self) -> bool:
        if not _HTTP_TOKEN:
            return True
        return hmac.compare_digest(self.headers.get("X-MVIBE-Token", ""), _HTTP_TOKEN)

    def do_POST(self):
        if not self._authed():
            self._reply(401, "unauthorized")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._reply(400, "bad length")
            return
        if length > _MAX_BODY:
            self._reply(413, "too large")
            return
        body = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
        if self.path == "/inbound":
            text = body.strip()
            if not text:
                self._reply(400, "empty")
                return
            try:
                _inbound_drive(text)
            except Exception as exc:
                self._reply(503, f"inject failed: {exc}")
                return
            self._reply(200, "injected")
        elif self.path == "/flag/local":
            paths.write_flag("local")
            self._reply(200, "local")
        elif self.path == "/flag/remote":
            paths.write_flag("remote")
            self._reply(200, "remote")
        else:
            self._reply(404, "not found")


def start_background(
    cwd: Path | None,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    always: bool = False,
    user: str | None = None,
    wechat: bool = True,
    log_path: Path | None = None,
) -> ThreadingHTTPServer:
    """Start mirror + WeChat poll + HTTP as daemon threads; return the httpd.

    Used by both `mvibe bridge` (foreground) and `mvibe up` (in-process beside
    the TUI). Pass log_path to keep logs off stdout when sharing the terminal.
    """
    global _LOG_PATH
    if log_path is not None:
        _LOG_PATH = log_path
    paths.ensure_home()
    threading.Thread(target=_mirror_loop, args=(cwd, always, user), daemon=True).start()
    if wechat:
        threading.Thread(target=_wechat_inbound_loop, daemon=True).start()
    httpd = ThreadingHTTPServer((host, port), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    _log(f"[bridge] http://{host}:{port}  cwd={cwd}  always={always}  wechat={wechat}")
    return httpd


def serve(
    cwd: Path | None,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    always: bool = False,
    user: str | None = None,
    wechat: bool = True,
) -> int:
    httpd = start_background(
        cwd, host=host, port=port, always=always, user=user, wechat=wechat
    )
    try:
        while True:
            threading.Event().wait(3600)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
    return 0
