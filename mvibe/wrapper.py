"""PTY wrapper: run `claude` so the local terminal is byte-for-byte identical to
running it directly, while a flag file decides whether remote keystrokes (read
from the inject FIFO) drive the child instead of local stdin.

Design:
- One child process (`claude`) under a PTY. The wrapper owns the PTY master.
- Child output always goes to the local terminal (so a human can watch even
  while remote drives). Remote *output* mirroring is a separate concern handled
  by the transcript tailer in bridge.py, not here.
- Input mux by flag:
    local  -> local stdin -> PTY ; inject FIFO ignored
    remote -> inject FIFO -> PTY ; local stdin ignored (avoid two typists)
- The inject FIFO is opened O_RDWR so it never reaches EOF and select() only
  wakes on real data (we keep a write end open ourselves).
"""

from __future__ import annotations

import errno
import fcntl
import os
import pty
import select
import signal
import struct
import sys
import termios
import time
import tty

from . import paths

_BUF = 65536


def _winsize() -> tuple[int, int]:
    """Return (cols, rows) of the controlling terminal."""
    try:
        packed = fcntl.ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
        rows, cols = struct.unpack("HHHH", packed)[:2]
        return (cols or 80, rows or 24)
    except OSError:
        return (80, 24)


def _set_winsize_from_stdin(master_fd: int) -> None:
    try:
        packed = fcntl.ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
    except OSError:
        packed = struct.pack("HHHH", 24, 80, 0, 0)
    try:
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, packed)
    except OSError:
        pass


def _open_inject_fifo() -> int:
    """Create + open the inject FIFO O_RDWR (never EOFs, non-blocking reads)."""
    paths.ensure_home()
    fifo = paths.INJECT_FIFO
    if not fifo.exists():
        os.mkfifo(fifo, 0o600)
    fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
    return fd


def run(cmd: list[str], on_ready=None, on_prompt=None) -> int:
    """Spawn cmd under a PTY and mux I/O until it exits. Returns exit code.

    `on_ready` (if given) is called once the PTY child and inject FIFO reader are
    live but before the mux loop — used by `mvibe up` to start the bridge threads
    in-process only after a FIFO reader exists (so injects never hit ENXIO).

    `on_prompt` (if given) receives a prompt_detect.Prompt when an interactive
    confirmation appears on screen, and None when it clears — used to forward
    confirmations to the phone.
    """
    if not cmd:
        cmd = ["claude"]

    paths.ensure_home()
    # Default routing to local on each fresh start.
    if paths.read_flag() != "local":
        paths.write_flag("local")

    watcher = None
    if on_prompt is not None:
        from .prompt_detect import PromptWatcher

        cols, rows = _winsize()
        watcher = PromptWatcher(cols, rows, on_prompt)

    pid, master_fd = pty.fork()
    if pid == 0:  # child
        os.execvp(cmd[0], cmd)
        os._exit(127)  # unreachable on success

    def _on_winch(*_):
        _set_winsize_from_stdin(master_fd)
        if watcher is not None:
            watcher.resize(*_winsize())

    _set_winsize_from_stdin(master_fd)
    signal.signal(signal.SIGWINCH, _on_winch)

    inject_fd = _open_inject_fifo()

    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()
    old_attr = None
    if os.isatty(stdin_fd):
        old_attr = termios.tcgetattr(stdin_fd)
        tty.setraw(stdin_fd)

    if on_ready is not None:
        try:
            on_ready()
        except Exception:
            pass  # bridge startup must never take down the local TUI

    try:
        while True:
            try:
                rlist, _, _ = select.select([stdin_fd, master_fd, inject_fd], [], [])
            except InterruptedError:
                continue  # e.g. SIGWINCH

            mode = paths.read_flag()

            if master_fd in rlist:
                try:
                    data = os.read(master_fd, _BUF)
                except OSError:
                    data = b""
                if not data:
                    break  # child exited / PTY closed
                os.write(stdout_fd, data)
                if watcher is not None:
                    watcher.feed(data)

            if stdin_fd in rlist:
                try:
                    data = os.read(stdin_fd, _BUF)
                except OSError:
                    data = b""
                if data:
                    # Local activity always reclaims control: the human at the
                    # keyboard outranks remote. This is the escape hatch out of
                    # remote mode (so you can always type, e.g. `/mvibe-off`).
                    if mode == "remote":
                        paths.write_flag("local")
                        mode = "local"
                    os.write(master_fd, data)

            if inject_fd in rlist:
                try:
                    data = os.read(inject_fd, _BUF)
                except OSError as exc:
                    if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                        data = b""
                    else:
                        data = b""
                if data and mode == "remote":
                    os.write(master_fd, data)
                # local mode: injected bytes are dropped on purpose.
    finally:
        if watcher is not None:
            watcher.stop()
        if old_attr is not None:
            termios.tcsetattr(stdin_fd, termios.TCSAFLUSH, old_attr)
        try:
            os.close(inject_fd)
        except OSError:
            pass

    _, status = os.waitpid(pid, 0)
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return 0


def _sanitize_injection(text: str) -> str:
    """Strip C0 control chars (incl. ESC) from remote-supplied text before it is
    fed to the PTY, so a crafted message cannot inject terminal escape sequences
    or TUI control keys. Tab and newline are kept as ordinary whitespace."""
    return "".join(ch for ch in text if ch in "\t\n" or ord(ch) >= 0x20)


# Symbolic navigation keys -> raw terminal byte sequences. Used to drive TUI
# menus that ignore digit keys (the AskUserQuestion carousel navigates with
# arrows + Enter). These are mvibe-generated control codes, written raw on
# purpose: _sanitize_injection (which strips ESC) is bypassed for this fixed
# whitelist only — never for remote free text.
_KEY_BYTES: dict[str, bytes] = {
    "up": b"\x1b[A",
    "down": b"\x1b[B",
    "right": b"\x1b[C",
    "left": b"\x1b[D",
    "enter": b"\r",
    "return": b"\r",
    "space": b" ",
    "tab": b"\t",
    "esc": b"\x1b",
}


def _open_inject_write() -> int:
    """Open the inject FIFO for writing (non-blocking). Raises a clear error when
    no `mvibe run` wrapper is reading it, instead of blocking forever."""
    fifo = paths.INJECT_FIFO
    if not fifo.exists():
        raise FileNotFoundError(f"inject FIFO missing: {fifo} (is `mvibe run` active?)")
    try:
        return os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno == errno.ENXIO:
            raise RuntimeError("`mvibe run` is not active (no reader on inject FIFO)") from exc
        raise


def inject(text: str, submit: bool = True) -> None:
    """Write keystrokes into the inject FIFO.

    When `submit`, the text and the Enter key are written as two separate writes
    with a short delay between them, so the TUI registers the text first and then
    a distinct Enter — sending them together can be coalesced into a paste (the
    Enter becomes a literal newline in the input box instead of submitting).
    Submit key and delay come from config (submit_key / submit_delay_ms).
    """
    fd = _open_inject_write()
    try:
        os.write(fd, _sanitize_injection(text).encode("utf-8"))
        if submit:
            rules = paths.load_rules()
            time.sleep(max(0, int(rules.get("submit_delay_ms", 80))) / 1000)
            os.write(fd, str(rules.get("submit_key", "\r")).encode("utf-8"))
    finally:
        os.close(fd)


def inject_keys(names: list[str], delay_ms: int = 40) -> None:
    """Inject a sequence of symbolic navigation keys (see _KEY_BYTES), one at a
    time with a short gap so the TUI processes each as a distinct keypress.
    Unknown names are skipped. Bypasses the text sanitizer by design — only the
    fixed _KEY_BYTES whitelist is emitted."""
    fd = _open_inject_write()
    try:
        first = True
        for name in names:
            b = _KEY_BYTES.get(name.strip().lower())
            if b is None:
                continue
            if not first:
                time.sleep(max(0, delay_ms) / 1000)
            os.write(fd, b)
            first = False
    finally:
        os.close(fd)
