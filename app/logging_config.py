import logging
import os
import sys

_CONFIGURED = False


def configure_logging() -> None:
    """Set up server-side logging. Safe to call more than once - only the
    first call has an effect, so any module can import this to guarantee
    logging is ready before it runs, regardless of import order."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        stream=sys.stdout,
    )
    _CONFIGURED = True


configure_logging()
