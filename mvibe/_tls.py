"""TLS CA setup for environments that intercept HTTPS (e.g. Cloudflare WARP).

If ~/tmp/cacert.pem exists, point SSL at it. Optional; no-op otherwise.
"""

from __future__ import annotations

import os
from pathlib import Path

_CA_BUNDLE = Path.home() / "tmp/cacert.pem"


def setup_tls_ca() -> None:
    if _CA_BUNDLE.is_file():
        os.environ.setdefault("SSL_CERT_FILE", str(_CA_BUNDLE))
        os.environ.setdefault("REQUESTS_CA_BUNDLE", str(_CA_BUNDLE))
