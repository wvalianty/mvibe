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
import tty

from . import paths

_BUF = 65536


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


def run(cmd: list[str]) -> int:
    """Spawn cmd under a PTY and mux I/O until it exits. Returns exit code."""
    if not cmd:
        cmd = ["claude"]

    paths.ensure_home()
    # Default routing to local on each fresh start.
    if paths.read_flag() != "local":
        paths.write_flag("local")

    pid, master_fd = pty.fork()
    if pid == 0:  # child
        os.execvp(cmd[0], cmd)
        os._exit(127)  # unreachable on success

    _set_winsize_from_stdin(master_fd)
    signal.signal(signal.SIGWINCH, lambda *_: _set_winsize_from_stdin(master_fd))

    inject_fd = _open_inject_fifo()

    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()
    old_attr = None
    if os.isatty(stdin_fd):
        old_attr = termios.tcgetattr(stdin_fd)
        tty.setraw(stdin_fd)

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

            if stdin_fd in rlist:
                try:
                    data = os.read(stdin_fd, _BUF)
                except OSError:
                    data = b""
                if data and mode == "local":
                    os.write(master_fd, data)
                # remote mode: local keystrokes are dropped on purpose.

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


def inject(text: str, submit: bool = True) -> None:
    """Write keystrokes into the inject FIFO. `submit` appends CR to send the line.

    Opens non-blocking: if no wrapper is reading the FIFO (no `mvibe run`), the
    open fails with ENXIO rather than blocking forever — we surface that as a
    clear error instead of hanging the caller's loop.
    """
    fifo = paths.INJECT_FIFO
    if not fifo.exists():
        raise FileNotFoundError(f"inject FIFO missing: {fifo} (is `mvibe run` active?)")
    payload = _sanitize_injection(text) + ("\r" if submit else "")
    try:
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno == errno.ENXIO:
            raise RuntimeError("`mvibe run` is not active (no reader on inject FIFO)") from exc
        raise
    try:
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)
