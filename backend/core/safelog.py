"""core/safelog.py — make "best effort" failures visible instead of silent."""
import logging

_log = logging.getLogger("wazibot.swallowed")


def swallow(where: str, exc: BaseException) -> None:
    """Log an intentionally-ignored error. Never raises."""
    try:
        _log.warning("ignored error in %s: %s: %s", where, type(exc).__name__, exc)
    except Exception:
        pass
