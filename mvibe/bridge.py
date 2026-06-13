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


def _chunk(text: str, size: int = _MAX_CHUNK):
    for i in range(0, len(text), size):
        yield text[i : i + size]


def _mirror_loop(cwd: Path | None, always: bool, user: str | None) -> None:
    from . import wechat_out

    for text in follow(cwd):
        if not always and paths.read_flag() != "remote":
            continue
        for piece in _chunk(text):
            try:
                wechat_out.send_text(piece, user=user)
            except Exception as exc:  # keep the loop alive on transient errors
                print(f"[mirror] send failed: {exc}", flush=True)
                break


def _inbound_drive(text: str) -> None:
    """A remote message takes over: route to remote and inject as keystrokes."""
    paths.write_flag("remote")
    try:
        wrapper.inject(text, submit=True)
    except Exception as exc:
        print(f"[inbound] inject failed: {exc}", flush=True)


def _wechat_inbound_loop() -> None:
    from . import config, wechat_in

    if not config.allowlist_active():
        print(
            "[wechat_in] WARNING: no allowlist — ANY user who messages the bot can "
            "drive Claude (full tool access). Set wechat.user_id / wechat.allowed_users.",
            flush=True,
        )

    async def on_message(text: str, user_id: str) -> None:
        if not config.is_authorized(user_id):
            print(f"[wechat_in] DROP unauthorized <{user_id}> {text[:40]}", flush=True)
            return
        print(f"[wechat_in] <{user_id}> {text[:60]}", flush=True)
        _inbound_drive(text)

    try:
        asyncio.run(wechat_in.poll_forever(on_message))
    except Exception as exc:
        print(f"[wechat_in] stopped: {exc}", flush=True)


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


def serve(
    cwd: Path | None,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    always: bool = False,
    user: str | None = None,
    wechat: bool = True,
) -> int:
    paths.ensure_home()
    threading.Thread(target=_mirror_loop, args=(cwd, always, user), daemon=True).start()
    if wechat:
        threading.Thread(target=_wechat_inbound_loop, daemon=True).start()

    httpd = ThreadingHTTPServer((host, port), _Handler)
    print(
        f"[bridge] http://{host}:{port}  cwd={cwd}  always={always}  wechat={wechat}",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
    return 0
