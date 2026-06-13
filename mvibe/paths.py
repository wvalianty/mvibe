"""Filesystem layout and Claude transcript discovery.

mvibe keeps its own runtime state under ~/.mvibe and never mutates ~/.avibe or
~/.vibe_remote. It only *reads* the Claude transcript directory and (for output)
the avibe WeChat config/tokens.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

MVIBE_HOME = Path(os.environ.get("MVIBE_HOME", Path.home() / ".mvibe"))
STATE_DIR = MVIBE_HOME / "state"

# Routing flag: file content is "local" or "remote".
FLAG_PATH = MVIBE_HOME / "active"
# Keystroke injection channel: bridge writes raw bytes, wrapper forwards to PTY.
INJECT_FIFO = MVIBE_HOME / "inject"
# Optional: SessionStart hook may write the active session id / transcript path here.
SESSION_PATH = MVIBE_HOME / "session"

# mvibe owns its own WeChat bot credentials/state (does not touch ~/.vibe_remote).
CONFIG_PATH = MVIBE_HOME / "config.json"
WECHAT_SYNC_BUF = STATE_DIR / "wechat_sync_buf"
WECHAT_TOKENS = STATE_DIR / "wechat_context_tokens.json"

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"


def ensure_home() -> None:
    MVIBE_HOME.mkdir(parents=True, exist_ok=True)
    try:
        MVIBE_HOME.chmod(0o700)  # state holds the inject FIFO + routing flag
    except OSError:
        pass


def read_flag() -> str:
    """Return 'local' (default) or 'remote'. Missing/garbage -> 'local'."""
    try:
        val = FLAG_PATH.read_text(encoding="utf-8").strip().lower()
    except FileNotFoundError:
        return "local"
    except OSError:
        return "local"
    return "remote" if val == "remote" else "local"


def write_flag(mode: str) -> None:
    ensure_home()
    mode = "remote" if mode == "remote" else "local"
    FLAG_PATH.write_text(mode, encoding="utf-8")


def encode_cwd(cwd: Path) -> str:
    """Reproduce Claude Code's project-dir encoding: non-alnum -> '-'.

    /Users/wangyong/code/python/avibe -> -Users-wangyong-code-python-avibe
    """
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def project_dir_for_cwd(cwd: Path) -> Path:
    return CLAUDE_PROJECTS / encode_cwd(cwd)


def newest_transcript(cwd: Path | None = None) -> Path | None:
    """Most recently modified *.jsonl for cwd's project dir.

    Falls back to scanning every project dir if the encoded dir is absent or
    empty (robust against encoding edge cases).
    """
    candidates: list[Path] = []
    if cwd is not None:
        pdir = project_dir_for_cwd(cwd)
        if pdir.is_dir():
            candidates = list(pdir.glob("*.jsonl"))
    if not candidates and CLAUDE_PROJECTS.is_dir():
        candidates = list(CLAUDE_PROJECTS.glob("*/*.jsonl"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)
