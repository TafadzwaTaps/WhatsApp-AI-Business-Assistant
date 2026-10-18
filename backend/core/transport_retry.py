"""
core/transport_retry.py — retry transient network errors on idempotent reads.

Why: postgrest-py only retries HTTP 503/520 *responses*. A dropped/stale pooled
connection raises httpx.ReadError ("[Errno 11] Resource temporarily
unavailable") instead, which was surfacing as a 500 on pages like /products.
A fresh attempt (httpx discards the broken connection) normally succeeds.

Only GET/HEAD are retried (safe to repeat); writes are never retried. Fails open:
if the library layout differs, install() does nothing.
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

_RETRY_METHODS = ("GET", "HEAD")
_MAX_EXTRA_ATTEMPTS = 2
_DELAY_SECONDS = 0.25


def _transient_errors() -> tuple:
    try:
        import httpx
        return (httpx.ReadError, httpx.ConnectError, httpx.RemoteProtocolError,
                httpx.WriteError, httpx.PoolTimeout)
    except Exception:
        return ()


def install(request_config_cls) -> bool:
    """Wrap request_config_cls.send with a bounded retry. Idempotent."""
    original = getattr(request_config_cls, "send", None)
    errors = _transient_errors()
    if original is None or not errors or getattr(original, "_wz_retry", False):
        return False

    def send(self, additional_headers, *a, **kw):
        method = str(getattr(self, "http_method", "")).upper()
        attempts = _MAX_EXTRA_ATTEMPTS if method in _RETRY_METHODS else 0
        for attempt in range(attempts + 1):
            try:
                return original(self, additional_headers, *a, **kw)
            except errors as exc:
                if attempt >= attempts:
                    raise
                log.warning("db: transient %s on %s %s — retry %d/%d",
                            type(exc).__name__, method, getattr(self, "path", ""),
                            attempt + 1, attempts)
                time.sleep(_DELAY_SECONDS * (attempt + 1))

    send._wz_retry = True
    request_config_cls.send = send
    return True
