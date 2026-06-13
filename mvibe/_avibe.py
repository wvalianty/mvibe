"""Bridge to avibe's bundled iLink implementation.

mvibe does not vendor the WeChat protocol; it borrows avibe's `modules.im.*`
from the avibe uv-tool venv by splicing that venv's site-packages onto sys.path
on demand (same trick as skills/wechat/push.py). This keeps mvibe a thin,
standalone project while reusing the maintained protocol code.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_VENV_SITE = Path.home() / ".local/share/uv/tools/avibe-os/lib/python3.13/site-packages"
_CA_BUNDLE = Path.home() / "tmp/cacert.pem"


class AvibeUnavailable(RuntimeError):
    pass


def ensure_imports() -> None:
    """Make `modules.im.*` (and its deps like aiohttp) importable, or raise."""
    try:
        import aiohttp  # noqa: F401
        import modules.im.wechat_api  # noqa: F401

        return
    except Exception:
        pass
    if _VENV_SITE.is_dir() and str(_VENV_SITE) not in sys.path:
        sys.path.append(str(_VENV_SITE))
    try:
        import aiohttp  # noqa: F401
        import modules.im.wechat_api  # noqa: F401
    except Exception as exc:  # pragma: no cover - environment dependent
        raise AvibeUnavailable(
            f"cannot import avibe iLink deps from {_VENV_SITE}: {exc}"
        ) from exc


def setup_tls_ca() -> None:
    """Use the patched CA bundle when present (Cloudflare WARP/Gateway TLS)."""
    if _CA_BUNDLE.is_file():
        os.environ.setdefault("SSL_CERT_FILE", str(_CA_BUNDLE))
        os.environ.setdefault("REQUESTS_CA_BUNDLE", str(_CA_BUNDLE))
