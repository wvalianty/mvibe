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
import json
import re
import threading
import time
from pathlib import Path

import pyte

# A numbered option line, e.g. "❯ 1. Yes" / "│ 2. No ... │" (leading box/cursor
# chars allowed; trailing box chars stripped from the captured label).
_OPT_RE = re.compile(r"^[\s>❯▶►●○*\-│┃|]*([0-9])[.)]\s+(\S.*?)[\s│┃|]*$")

_BOX = "│┃┆┇╎╏┊┋ ╭╮╰╯─━┌┐└┘├┤ ❯▶►➤●○"

# Detection keywords live in config so they can be tuned without touching logic.
_RULES_FILE = Path(__file__).with_name("prompt_rules.json")  # shipped defaults
_RULES_OVERRIDE = Path.home() / ".mvibe" / "prompt_rules.json"  # optional user override
_rules_cache: dict | None = None


def _load_rules() -> dict:
    global _rules_cache
    if _rules_cache is not None:
        return _rules_cache
    rules: dict = {}
    for path in (_RULES_FILE, _RULES_OVERRIDE):
        try:
            rules.update(json.loads(path.read_text(encoding="utf-8")))
        except (FileNotFoundError, json.JSONDecodeError):
            continue
    # Pre-lower keyword lists for case-insensitive matching.
    rules["confirm_phrases"] = [s.lower() for s in rules.get("confirm_phrases", [])]
    rules["option_keywords"] = [s.lower() for s in rules.get("option_keywords", [])]
    rules["yesno_option_prefixes"] = tuple(
        s.lower() for s in rules.get("yesno_option_prefixes", [])
    )
    rules["drop_line_phrases"] = [s.lower() for s in rules.get("drop_line_phrases", [])]
    rules.setdefault("min_options", 2)
    rules.setdefault("tail_lines", 16)
    rules.setdefault("separator_chars", "─━—-=_")
    rules.setdefault("separator_min_len", 10)
    rules.setdefault("context_lines_above", 6)
    rules["_yn"] = re.compile(rules["yn_regex"]) if rules.get("yn_regex") else None
    _rules_cache = rules
    return rules


class Prompt:
    __slots__ = ("text", "options", "kind", "hash")

    def __init__(self, text: str, options: list[tuple[str, str]], kind: str):
        self.text = text
        self.options = options  # [(digit, label), ...] for kind == "numbered"
        self.kind = kind  # "numbered" | "yn"
        self.hash = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def _strip_box(line: str) -> str:
    return line.strip(_BOX).rstrip()


def _is_separator(line: str, rules: dict) -> bool:
    s = line.strip()
    if len(s) < rules["separator_min_len"]:
        return False
    sep = set(rules["separator_chars"])
    return all(c in sep or c == " " for c in s)


def _extract_box(tail: list[str], rules: dict) -> str:
    """Trim the rendered tail to just the confirmation box: everything after the
    last horizontal rule (Claude draws one before the box), or — if there is no
    rule — from a few lines above the confirm question. Box chars and bottom hint
    lines are dropped."""
    sep_idx = -1
    for i, ln in enumerate(tail):
        if _is_separator(ln, rules):
            sep_idx = i
    if sep_idx >= 0:
        block = tail[sep_idx + 1 :]
    else:
        qi = next(
            (i for i, ln in enumerate(tail)
             if any(p in ln.lower() for p in rules["confirm_phrases"])),
            None,
        )
        block = tail[max(0, qi - rules["context_lines_above"]) :] if qi is not None else tail

    drops = rules["drop_line_phrases"]
    out: list[str] = []
    for ln in block:
        s = _strip_box(ln)
        if not s:
            continue
        low = s.lower()
        if any(d in low for d in drops):
            continue
        out.append(s)
    return "\n".join(out)


def detect(lines: list[str]) -> Prompt | None:
    """Return a Prompt if the rendered screen looks like a confirmation awaiting a
    choice. Discriminates real confirmations from ordinary numbered menus (slash
    commands, autocomplete, model picker) using the keyword config — those menus
    lack a confirm phrase and Yes/No-style options. Case-insensitive."""
    rules = _load_rules()
    nonempty = [ln for ln in lines if ln.strip()]
    if not nonempty:
        return None
    tail = nonempty[-rules["tail_lines"] :]
    joined = " ".join(tail)
    low = joined.lower()

    options: list[tuple[str, str]] = []
    for ln in tail:
        m = _OPT_RE.match(ln)
        if m:
            options.append((m.group(1), m.group(2).strip()))

    text = _extract_box(tail, rules)

    if len(options) >= rules["min_options"]:
        has_phrase = any(p in low for p in rules["confirm_phrases"])
        kws = rules["option_keywords"]
        prefixes = rules["yesno_option_prefixes"]
        opt_signal = any(
            any(k in label.lower() for k in kws)
            or (prefixes and label.lower().startswith(prefixes))
            for _, label in options
        )
        if has_phrase or opt_signal:
            return Prompt(text, options, "numbered")
    if rules["_yn"] is not None and rules["_yn"].search(joined):
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
                if cell is None:
                    chars.append(" ")
                elif cell.data == "":
                    # Wide-char continuation cell (the second half of a CJK/emoji
                    # glyph). pyte stores "" here — skip it, don't pad a space.
                    continue
                else:
                    chars.append(cell.data)
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
