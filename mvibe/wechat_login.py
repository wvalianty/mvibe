"""WeChat QR-login binding for mvibe.

Runs the official iLink login flow (via avibe's WeChatAuthManager) and persists
the resulting bot_token / base_url / bound user_id into mvibe's *own* config
(~/.mvibe/config.json) — independent of any avibe install. Ported from
tmp/vibe/wechat_login.py, minus the avibe-specific config writes.
"""

from __future__ import annotations

import asyncio
import time

from . import _tls, config
from .ilink.wechat_auth import WeChatAuthManager

POLL_INTERVAL = 2
MAX_WAIT_S = 240


def _render_qr(url: str) -> None:
    try:
        import segno
    except ImportError:
        print(f"\n[QR] install `segno` to render inline. Open this URL as a QR:\n{url}\n")
        return
    segno.make(url, error="m").terminal(compact=True)
    print(f"\n(若二维码糊了，手动转此链接为二维码：{url})\n")


async def _login() -> int:
    _tls.setup_tls_ca()

    base_url = config.wechat_creds()["base_url"]
    mgr = WeChatAuthManager()
    start = await mgr.start_login(base_url=base_url)
    if start.get("error"):
        print("start_login failed:", start["error"])
        return 1

    session_key = start["session_key"]
    print("\n用微信扫一扫下面二维码，然后在手机上确认：\n")
    _render_qr(start["qrcode_url"])

    deadline = time.time() + MAX_WAIT_S
    last = None
    while time.time() < deadline:
        res = await mgr.poll_status(session_key)
        status = res.get("status")
        if status != last:
            print(f"状态: {status} - {res.get('message', '')}")
            last = status

        if status == "refreshed" and res.get("qrcode_url"):
            print("\n二维码已刷新，重新扫描：\n")
            _render_qr(res["qrcode_url"])

        if status == "confirmed":
            bot_token = res.get("bot_token") or ""
            if not bot_token:
                print("确认了但没拿到 bot_token，服务端异常。")
                return 1
            config.save_config(
                {
                    "wechat": {
                        "bot_token": bot_token,
                        "base_url": res.get("base_url") or base_url,
                        "user_id": res.get("user_id") or "",
                    }
                }
            )
            print("\n✅ 绑定成功，bot_token 已写入 mvibe config。")
            print("   现在: `mvibe bridge` 启动入站轮询，手机给 bot 发条消息即可驱动会话。")
            return 0

        if status in ("error", "expired"):
            print("登录失败:", res.get("message"))
            return 1

        await asyncio.sleep(POLL_INTERVAL)

    print("超时，请重新运行 `mvibe login`。")
    return 1


def login() -> int:
    return asyncio.run(_login())
