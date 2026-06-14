"""Detect an interactive confirmation prompt by scraping the rendered terminal.

Backend-agnostic by design: we feed the PTY output through a headless terminal
emulator (pyte) to reconstruct the *rendered screen*, then match generic prompt
shapes (numbered option list with a selection cursor, or a y/n prompt). The
recognition rules live here; add a backend's prompt shape by extending them.

Why scrape instead of a Claude-specific hook: the same mechanism works for any
TUI agent (Claude, Codex, ...) — only these rules might need a new entry.
"""

from __future__ import annotations

import codecs
import hashlib
import re
import threading
import time

import pyte

# A numbered option line, e.g. "❯ 1. Yes" / "│ 2. No ... │" (leading box/cursor
# chars allowed; trailing box chars stripped from the captured label).
_OPT_RE = re.compile(r"^[\s>❯▶►●○*\-│┃|]*([0-9])[.)]\s+(\S.*?)[\s│┃|]*$")
# Selection cursors a TUI uses to mark the active choice.
_CURSORS = ("❯", "▶", "►", "➤", "●")
# y/n style prompt.
_YN_RE = re.compile(r"\(\s*[yY]\s*/\s*[nN]\s*\)|\[\s*[yY]\s*/\s*[nN]\s*\]")
# Phrases that strongly imply a waiting confirmation (lower-cased match).
_HINTS = ("do you want", "proceed", "permission", "allow", "trust", "(y/n)")

_BOX = "│┃┆┇╎╏┊┋ ╭╮╰╯─━┌┐└┘├┤ "


class Prompt:
    __slots__ = ("text", "options", "kind", "hash")

    def __init__(self, text: str, options: list[tuple[str, str]], kind: str):
        self.text = text
        self.options = options  # [(digit, label), ...] for kind == "numbered"
        self.kind = kind  # "numbered" | "yn"
        self.hash = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def _strip_box(line: str) -> str:
    return line.strip(_BOX).rstrip()


def detect(lines: list[str]) -> Prompt | None:
    """Return a Prompt if the rendered screen looks like it is awaiting a choice."""
    nonempty = [ln for ln in lines if ln.strip()]
    if not nonempty:
        return None
    tail = nonempty[-16:]
    joined = " ".join(tail)
    low = joined.lower()

    options: list[tuple[str, str]] = []
    for ln in tail:
        m = _OPT_RE.match(ln)
        if m:
            options.append((m.group(1), m.group(2).strip()))

    text = "\n".join(_strip_box(ln) for ln in tail if _strip_box(ln))

    has_cursor = any(c in joined for c in _CURSORS)
    has_hint = any(h in low for h in _HINTS)

    # Numbered prompt: at least two options AND a cursor or hint (so we don't
    # fire on an ordinary numbered list inside the assistant's normal output).
    if len(options) >= 2 and (has_cursor or has_hint):
        return Prompt(text, options, "numbered")
    # y/n prompt.
    if _YN_RE.search(joined) or "(y/n)" in low:
        return Prompt(text, [], "yn")
    return None


class PromptWatcher:
    """Feed PTY bytes; invoke on_change(prompt) when a stable prompt appears and
    on_change(None) when it goes away. Runs detection on its own thread so the
    PTY mux loop is never blocked (network sends happen in on_change)."""

    def __init__(self, cols: int, rows: int, on_change):
        self._screen = pyte.Screen(max(cols, 20), max(rows, 6))
        self._stream = pyte.Stream(self._screen)
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._on_change = on_change
        self._lock = threading.Lock()
        self._last_feed = 0.0
        self._dirty = False
        self._active_hash: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def feed(self, data: bytes) -> None:
        with self._lock:
            try:
                text = self._decoder.decode(data)  # incremental: holds split bytes
                if text:
                    self._stream.feed(text)
            except Exception:
                pass
            self._dirty = True
            self._last_feed = time.monotonic()

    def resize(self, cols: int, rows: int) -> None:
        with self._lock:
            try:
                self._screen.resize(max(rows, 6), max(cols, 20))
            except Exception:
                pass

    def stop(self) -> None:
        self._stop.set()

    def _snapshot(self) -> list[str]:
        """Render the screen from the buffer directly. Avoids pyte's Screen.display
        (its wcwidth render path raises IndexError on empty cells in some
        versions) and never mutates the buffer."""
        screen = self._screen
        out: list[str] = []
        for y in range(screen.lines):
            row = screen.buffer.get(y)
            if row is None:
                out.append("")
                continue
            chars = []
            for x in range(screen.columns):
                cell = row.get(x)
                chars.append(cell.data if cell is not None and cell.data else " ")
            out.append("".join(chars).rstrip())
        return out

    def _loop(self) -> None:
        while not self._stop.is_set():
            time.sleep(0.12)
            try:
                with self._lock:
                    if not self._dirty:
                        continue
                    # Wait for the screen to settle before reading it.
                    if time.monotonic() - self._last_feed < 0.3:
                        continue
                    lines = self._snapshot()
                    self._dirty = False

                prompt = detect(lines)
                if prompt is not None:
                    if prompt.hash != self._active_hash:
                        self._active_hash = prompt.hash
                        self._safe_emit(prompt)
                elif self._active_hash is not None:
                    self._active_hash = None
                    self._safe_emit(None)
            except Exception:
                # Never let a render/detect error kill the thread or print a
                # traceback into the TUI.
                self._dirty = False

    def _safe_emit(self, prompt) -> None:
        try:
            self._on_change(prompt)
        except Exception:
            pass
