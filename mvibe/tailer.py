"""Follow a Claude Code session transcript (.jsonl) and emit clean assistant
text for mirroring to a chat surface.

Why the transcript and not the PTY: the TUI renders a full-screen ANSI app;
its raw bytes are unreadable in a chat bubble. The transcript holds structured
turns that Claude appends in real time, so we read *that* for remote output.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path

from . import paths


def _extract_assistant_text(obj: dict) -> str | None:
    """Return concatenated visible text of an assistant turn, or None to skip.

    Skips subagent sidechains and thinking/tool blocks; emits text blocks only.
    """
    if obj.get("type") != "assistant":
        return None
    if obj.get("isSidechain"):
        return None
    message = obj.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content.strip() or None
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            txt = block.get("text") or ""
            if txt.strip():
                parts.append(txt)
    joined = "".join(parts).strip()
    return joined or None


def follow(
    cwd: Path | None,
    *,
    poll: float = 0.4,
    from_start: bool = False,
) -> Iterator[str]:
    """Yield assistant text as it is appended to the active transcript.

    Re-resolves the newest transcript each poll, so a new session (new .jsonl)
    is picked up automatically. Dedupes by message uuid across file switches.
    """
    seen: set[str] = set()
    current: Path | None = None
    offset = 0

    while True:
        target = paths.newest_transcript(cwd)
        if target is None:
            time.sleep(poll)
            continue

        if target != current:
            current = target
            # Start at end for the live file unless asked to replay history.
            offset = 0 if from_start else current.stat().st_size

        try:
            with current.open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(offset)
                for line in fh:
                    if not line.endswith("\n"):
                        # Partial line; rewind and wait for the rest.
                        fh.seek(offset)
                        break
                    offset += len(line.encode("utf-8"))
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    uuid = obj.get("uuid")
                    if uuid and uuid in seen:
                        continue
                    text = _extract_assistant_text(obj)
                    if text is None:
                        continue
                    if uuid:
                        seen.add(uuid)
                    yield text
        except FileNotFoundError:
            current = None

        time.sleep(poll)
