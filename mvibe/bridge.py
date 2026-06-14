"""Remote bridge: mirror Claude's output to WeChat and inject WeChat input back.

Both halves are decoupled from the PTY wrapper via the flag file + inject FIFO:

  output: tail the session transcript -> push assistant text to the phone
          (while the remote gate is on)
  input:  long-poll WeChat for messages -> inject them as keystrokes

No network listener: WeChat inbound is an outbound long-poll, and local control
goes through the CLI (`mvibe send` / `flag` / `remote`) + files directly.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from . import paths, wrapper
from .tailer import follow

_MAX_CHUNK = 1800

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


_window_warned = False  # dedup the "push window closed" hint so it logs once


def _safe_send(text: str, user: str | None = None) -> bool:
    """Send to the phone, logging the window-closed hint only once until a send
    succeeds again. Returns True on success."""
    global _window_warned
    from . import wechat_out

    try:
        wechat_out.send_text(text, user=user)
        if _window_warned:
            _window_warned = False
            _log("[wechat] push window reopened — sends working again")
        return True
    except wechat_out.WindowClosedError as exc:
        if not _window_warned:
            _window_warned = True
            _log(f"[wechat] {exc}")
        return False
    except Exception as exc:
        _log(f"[wechat] send failed: {exc}")
        return False


def _mirror_loop(cwd: Path | None, always: bool, user: str | None) -> None:
    # Output mirroring follows the remote *gate* (the /mvibe-on/off switch), NOT
    # the routing flag. The flag flips to local the instant you touch the local
    # keyboard (input reclaim); tying output to it would silently drop replies.
    for text in follow(cwd):
        if not always and not paths.remote_enabled():
            continue
        ok = all(_safe_send(piece, user) for piece in _chunk(text))
        if ok:
            _log(f"[mirror] -> phone: {text[:50]!r}")


def _inbound_drive(text: str) -> None:
    """A remote message takes over: route to remote and inject as keystrokes."""
    paths.write_flag("remote")
    try:
        wrapper.inject(text, submit=True)
    except Exception as exc:
        _log(f"[inbound] inject failed: {exc}")


# ---- interactive confirmation forwarding (screen-scraped) ------------------ #
_pending: dict | None = None  # {"options": [(digit,label)], "kind": "numbered"|"yn"}
_pending_lock = threading.Lock()

_YES_WORDS = {"y", "yes", "是", "确认", "ok", "好", "同意", "允许"}
_NO_WORDS = {"n", "no", "否", "取消", "不", "拒绝"}


def on_prompt_change(prompt) -> None:
    """Called by the wrapper's screen watcher. Forward a new confirmation to the
    phone (only while remote-driving), or clear pending when it disappears."""
    global _pending
    if prompt is None:
        with _pending_lock:
            _pending = None
        return
    # Forward whenever the remote gate is on — same rule as output mirroring, NOT
    # the routing flag (which flips to local the moment you touch the keyboard).
    # The phone can answer, and the local TUI can still answer too.
    if not paths.remote_enabled():
        return
    with _pending_lock:
        # One outstanding confirmation at a time: re-renders of the same prompt
        # (cursor blink, spinner) arrive as fresh detections — skip them. Cleared
        # when the screen stops being a prompt (on_change(None)).
        if _pending is not None:
            return
        _pending = {"options": prompt.options, "kind": prompt.kind}
    hint = "回复 yes / no" if prompt.kind == "yn" else "回复选项数字，或 yes / no"
    msg = f"⚠️ 需要确认：\n\n{prompt.text}\n\n（{hint}）"
    if _safe_send(msg):
        _log(f"[approve] forwarded prompt ({prompt.kind}, {len(prompt.options)} opts)")
    else:
        with _pending_lock:
            _pending = None  # send failed; allow a retry on the next render


def _map_reply(text: str, pending: dict) -> str | None:
    """Map a phone reply to the keystroke that answers the prompt, or None."""
    t = text.strip().lower()
    if pending["kind"] == "yn":
        if t in _YES_WORDS:
            return "y"
        if t in _NO_WORDS:
            return "n"
        return None
    options = pending["options"]  # [(digit, label)]
    digits = {d for d, _ in options}
    if t in digits:
        return t
    if t in _YES_WORDS:
        for d, label in options:
            if "yes" in label.lower():
                return d
        return options[0][0] if options else None
    if t in _NO_WORDS:
        for d, label in options:
            ll = label.lower()
            if "no" in ll or "cancel" in ll or "reject" in ll:
                return d
        return options[-1][0] if options else None
    return None


def _try_answer_pending(text: str) -> bool:
    """If a confirmation is pending, treat `text` as the answer. Returns True if
    handled (so it is not injected as an ordinary message)."""
    global _pending
    with _pending_lock:
        pending = _pending
    if pending is None:
        return False
    key = _map_reply(text, pending)
    if key is None:
        _safe_send("没听懂，请回复选项数字或 yes / no")
        return True  # swallow: don't inject a stray message into the prompt
    paths.write_flag("remote")
    try:
        wrapper.inject(key, submit=False)  # a single key selects; no Enter
    except Exception as exc:
        _log(f"[approve] reply inject failed: {exc}")
    with _pending_lock:
        _pending = None
    _log(f"[approve] reply {text!r} -> key {key!r}")
    return True


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
        if _try_answer_pending(text):  # a pending confirmation consumes this reply
            return
        _log(f"[wechat_in] <{user_id}> {text[:60]}")
        _inbound_drive(text)

    try:
        asyncio.run(wechat_in.poll_forever(on_message))
    except Exception as exc:
        _log(f"[wechat_in] stopped: {exc}")


def start_background(
    cwd: Path | None,
    *,
    always: bool = False,
    user: str | None = None,
    wechat: bool = True,
    log_path: Path | None = None,
) -> None:
    """Start mirror + WeChat poll as daemon threads.

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
    _log(f"[bridge] cwd={cwd}  always={always}  wechat={wechat}")


def serve(
    cwd: Path | None,
    *,
    always: bool = False,
    user: str | None = None,
    wechat: bool = True,
) -> int:
    start_background(cwd, always=always, user=user, wechat=wechat)
    try:
        while True:
            threading.Event().wait(3600)
    except KeyboardInterrupt:
        pass
    return 0
