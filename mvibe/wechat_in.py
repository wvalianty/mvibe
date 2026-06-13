"""Inbound WeChat via iLink long-poll (getUpdates).

mvibe runs its own poll loop against its own bot_token, so it receives messages
directly — no public webhook, no avibe service. Each inbound message yields
(text, user_id); its context_token is remembered so replies can be sent.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from . import _tls, config
from .ilink import wechat_api

_POLL_TIMEOUT_MS = 30_000
_SHORT_RETRY_S = 2
_LONG_RETRY_S = 15
_MAX_FAILS = 5

OnMessage = Callable[[str, str], Awaitable[None] | None]


def _extract_text(msg: dict) -> str:
    """Concatenate text from iLink item_list (type 1=text, 3=voice transcript)."""
    parts: list[str] = []
    for item in msg.get("item_list", []) or []:
        itype = item.get("type", 0)
        if itype == 1 or itype in ("TEXT", "text"):
            ti = item.get("text_item") or {}
            content = ti.get("text") or item.get("content", "")
            if content:
                parts.append(str(content))
        elif itype == 3 or itype in ("VOICE", "voice"):
            content = (item.get("voice_item") or {}).get("text", "")
            if content:
                parts.append(str(content))
    return "".join(parts).strip()


async def poll_forever(on_message: OnMessage, stop: asyncio.Event | None = None) -> None:
    _tls.setup_tls_ca()

    creds = config.wechat_creds()
    base_url, bot_token = creds["base_url"], creds["bot_token"]
    if not bot_token:
        raise RuntimeError("no bot_token — run `mvibe login` first")

    sync_buf = config.load_sync_buf()
    seen: set[str] = set()
    fails = 0

    while stop is None or not stop.is_set():
        try:
            resp = await wechat_api.get_updates(
                base_url, bot_token, sync_buf, timeout_ms=_POLL_TIMEOUT_MS
            )
        except Exception as exc:
            fails += 1
            print(f"[wechat_in] poll error: {exc}", flush=True)
            await asyncio.sleep(_LONG_RETRY_S if fails >= _MAX_FAILS else _SHORT_RETRY_S)
            continue

        ret = resp.get("ret")
        errcode = resp.get("errcode")
        if (ret not in (None, 0)) or (errcode not in (None, 0)):
            fails += 1
            print(f"[wechat_in] getUpdates ret={ret} errcode={errcode} "
                  f"msg={resp.get('errmsg') or resp.get('msg')}", flush=True)
            await asyncio.sleep(_LONG_RETRY_S if fails >= _MAX_FAILS else _SHORT_RETRY_S)
            continue

        fails = 0
        new_buf = resp.get("get_updates_buf", "")
        if new_buf:
            sync_buf = new_buf
            config.save_sync_buf(sync_buf)

        for msg in resp.get("msgs", []) or []:
            mid = str(msg.get("message_id", ""))
            if mid and mid in seen:
                continue
            if mid:
                seen.add(mid)
            user_id = msg.get("from_user_id", "")
            if not user_id:
                continue
            config.remember_context_token(user_id, msg.get("context_token", ""))
            text = _extract_text(msg)
            if not text:
                continue
            result = on_message(text, user_id)
            if asyncio.iscoroutine(result):
                await result
