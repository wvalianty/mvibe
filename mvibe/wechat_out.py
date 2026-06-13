"""Send text to the WeChat phone client via avibe's iLink send_message.

Uses mvibe's own bot_token/context_tokens (falling back to an existing avibe
install). iLink only allows pushing within a window after the phone last
messaged the bot; after a long idle gap a send can fail with errcode -14.
"""

from __future__ import annotations

import asyncio

from . import _tls, config
from .ilink import wechat_api

SESSION_EXPIRED_ERRCODE = -14


class WeChatError(RuntimeError):
    pass


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
    if str(code) == str(SESSION_EXPIRED_ERRCODE):
        raise WeChatError(f"session expired (code={code}); phone must message bot to refresh")
    raise WeChatError(f"wechat error code={code} msg={resp.get('errmsg') or resp.get('msg')}")
