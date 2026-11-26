"""core/safelog.py — make "best effort" failures visible instead of silent."""
import logging
import re
import time

_log = logging.getLogger("wazibot.swallowed")

_SENSITIVE_KEY = re.compile(
    r"passw(or)?d|secret|token|authorization|cookie|api[_-]?key|private[_-]?key|credential", re.I)
_DROP_WARN_INTERVAL = 300
_last_drop_warning: dict = {}


def redact(value, _depth: int = 0):
    """Copy of audit/event metadata with the values of secret-looking keys removed.
    Numbers, booleans and None are kept: they are counters and flags, not secrets."""
    if _depth > 6:
        return "[truncated]"
    if isinstance(value, dict):
        return {k: "[redacted]" if _SENSITIVE_KEY.search(str(k))
                and not isinstance(v, (bool, int, float, type(None)))
                else redact(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, _depth + 1) for v in value]
    return value


def record_dropped(where: str, what: str, exc: BaseException) -> None:
    """Warn (at most once per interval per source) that a security record was not saved.
    Only the exception type is logged: database errors can quote the row. Never raises."""
    try:
        now = time.monotonic()
        last = _last_drop_warning.get(where)
        if last is None or now - last >= _DROP_WARN_INTERVAL:
            _last_drop_warning[where] = now
            _log.warning("SECURITY RECORD NOT SAVED  source=%s  record=%s  error=%s  "
                         "(repeats muted for %ss)", where, what, type(exc).__name__, _DROP_WARN_INTERVAL)
    except Exception:
        pass


def swallow(where: str, exc: BaseException) -> None:
    """Log an intentionally-ignored error. Never raises."""
    try:
        _log.warning("ignored error in %s: %s: %s", where, type(exc).__name__, exc)
    except Exception:
        pass
