"""mvibe's own config + WeChat state store under ~/.mvibe.

Fully self-contained: holds the bot_token obtained by `mvibe login`, the
long-poll sync cursor, and per-user context_tokens observed from inbound
messages. No dependency on any avibe install.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import paths

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"


def _secure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _secure_write(path: Path, text: str) -> None:
    """Write a secret file as 0600 (create restricted, then write)."""
    _secure_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        path.chmod(0o600)  # tighten if the file pre-existed with looser perms
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# config.json
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    try:
        return json.loads(paths.CONFIG_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_config(patch: dict) -> None:
    """Shallow-merge top-level sections (e.g. {"wechat": {...}})."""
    cfg = load_config()
    for key, val in patch.items():
        if isinstance(val, dict) and isinstance(cfg.get(key), dict):
            cfg[key].update(val)
        else:
            cfg[key] = val
    _secure_write(paths.CONFIG_PATH, json.dumps(cfg, indent=2, ensure_ascii=False))


def wechat_creds() -> dict:
    """Resolve {base_url, bot_token, user_id} from mvibe config."""
    mine = load_config().get("wechat") or {}
    return {
        "base_url": mine.get("base_url") or DEFAULT_BASE_URL,
        "bot_token": mine.get("bot_token") or "",
        "user_id": mine.get("user_id") or "",
    }


# --------------------------------------------------------------------------- #
# long-poll sync cursor
# --------------------------------------------------------------------------- #
def load_sync_buf() -> str:
    try:
        return paths.WECHAT_SYNC_BUF.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def save_sync_buf(buf: str) -> None:
    _secure_write(paths.WECHAT_SYNC_BUF, buf or "")


# --------------------------------------------------------------------------- #
# context_tokens (needed to send; observed from inbound)
# --------------------------------------------------------------------------- #
def _load_tokens_file(path: Path) -> dict[str, dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    raw = data.get("tokens") if isinstance(data, dict) else None
    out: dict[str, dict] = {}
    if isinstance(raw, dict):
        for uid, rec in raw.items():
            if isinstance(rec, dict):
                tok = str(rec.get("context_token") or "")
                obs = rec.get("observed_at") or 0
            else:
                tok, obs = str(rec or ""), 0
            if uid and tok:
                out[str(uid)] = {"context_token": tok, "observed_at": obs}
    return out


def load_tokens() -> dict[str, dict]:
    return _load_tokens_file(paths.WECHAT_TOKENS)


def remember_context_token(user_id: str, context_token: str) -> None:
    if not user_id or not context_token:
        return
    tokens = _load_tokens_file(paths.WECHAT_TOKENS)
    tokens[user_id] = {"context_token": context_token, "observed_at": time.time()}
    _secure_write(paths.WECHAT_TOKENS, json.dumps({"tokens": tokens}, ensure_ascii=False))


# --------------------------------------------------------------------------- #
# inbound authorization
# --------------------------------------------------------------------------- #
def allowed_users() -> set[str]:
    """Users permitted to drive the session: bound user_id + wechat.allowed_users."""
    wc = load_config().get("wechat") or {}
    allowed: set[str] = set()
    bound = wc.get("user_id")
    if bound:
        allowed.add(str(bound))
    extra = wc.get("allowed_users")
    if isinstance(extra, list):
        allowed.update(str(u) for u in extra if u)
    return allowed


def allowlist_active() -> bool:
    return bool(allowed_users())


def is_authorized(user_id: str) -> bool:
    """True if `user_id` may drive the session. Open (True) only when no
    allowlist is configured — callers should warn loudly in that case."""
    allowed = allowed_users()
    if not allowed:
        return True
    return user_id in allowed


def pick_user(want: str | None = None) -> tuple[str, str]:
    """Return (user_id, context_token); prefer `want`, else most recently active."""
    tokens = load_tokens()
    if not tokens:
        raise RuntimeError("no context_token yet — phone must message the bot first")
    if want:
        rec = tokens.get(want)
        if not rec:
            raise RuntimeError(f"user {want} has no context_token; known: {list(tokens)}")
        return want, rec["context_token"]
    uid = max(tokens, key=lambda u: tokens[u].get("observed_at") or 0)
    return uid, tokens[uid]["context_token"]
