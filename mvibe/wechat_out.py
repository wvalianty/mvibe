"""Send text to the WeChat phone client via the vendored iLink send_message.

iLink only allows pushing within a window after the phone last messaged the bot.
Once that window closes, send_message fails — observed as ret/errcode -14 (token
expired) or -2 (window closed). Both mean the same fix: message the bot from the
phone to reopen the window.
"""

from __future__ import annotations

import asyncio

from . import _tls, config
from .ilink import wechat_api

# Codes that mean "the push window is closed; the phone must message the bot".
WINDOW_CLOSED_CODES = {"-14", "-2"}


class WeChatError(RuntimeError):
    pass


class WindowClosedError(WeChatError):
    """Push failed because the phone hasn't messaged the bot recently."""


async def _send(base_url, bot_token, user_id, context_token, text) -> dict:
    item_list = [{"type": 1, "text_item": {"text": text}}]
    return await wechat_api.send_message(base_url, bot_token, user_id, context_token, item_list)


def list_users() -> list[str]:
    toks = config.load_tokens()
    return [u for u, _ in sorted(toks.items(), key=lambda kv: -(kv[1].get("observed_at") or 0))]


def send_text(text: str, user: str | None = None) -> str:
    """Push `text` to the phone. Returns the target user_id. Raises WeChatError."""
    text = (text or "").strip()
    if not text:
        raise WeChatError("empty text")
    _tls.setup_tls_ca()

    creds = config.wechat_creds()
    if not creds["bot_token"]:
        raise WeChatError("no bot_token — run `mvibe login` first")
    want = user or creds["user_id"] or None
    try:
        user_id, context_token = config.pick_user(want)
    except RuntimeError as exc:
        raise WeChatError(str(exc)) from exc

    resp = asyncio.run(_send(creds["base_url"], creds["bot_token"], user_id, context_token, text))
    errcode = resp.get("errcode")
    ret = resp.get("ret")
    code = errcode if errcode not in (None, 0) else ret
    if code in (None, 0):
        return user_id
    if str(code) in WINDOW_CLOSED_CODES:
        raise WindowClosedError(
            f"push window closed (code={code}) — send any message to the bot "
            "from your phone to reopen it, then retry"
        )
    raise WeChatError(f"wechat error code={code} msg={resp.get('errmsg') or resp.get('msg')}")
