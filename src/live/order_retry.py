"""Retry wrapper for live order sending with exponential backoff.

Only retries on transient failures (COM errors, timeouts).
Logical rejections (invalid params, insufficient margin) are not retried.

Usage::

    from src.live.order_retry import send_with_retry

    code, msg = send_with_retry(
        lambda: sk.SKOrderLib_SendFutureProxyOrderCLR(...),
        logger_fn=my_log,
    )
"""

from __future__ import annotations

import logging
import time

_log = logging.getLogger(__name__)

# Backoff delays between retries (seconds)
RETRY_DELAYS = (5, 10)
MAX_ATTEMPTS = 3


def send_with_retry(send_fn, *, is_transient_fn=None, logger_fn=None):
    """Execute *send_fn* with exponential backoff on transient failures.

    Args:
        send_fn: ``() -> (code: int, message: str)`` -- the order send
            function.  Return code 0 = success.
        is_transient_fn: ``(error) -> bool`` -- classify whether an error
            (int return code or exception) is retryable.  Defaults to
            :func:`is_transient_error`.
        logger_fn: ``(msg: str) -> None`` -- log function for retries.

    Returns:
        ``(code, message)`` from the successful attempt, or
        ``(-1, "Failed after N retries: ...")`` if all attempts fail.
    """
    log = logger_fn or _log.warning
    classify = is_transient_fn or is_transient_error

    last_error = ""
    for attempt in range(MAX_ATTEMPTS):
        try:
            code, message = send_fn()
            if code == 0:
                return code, message
            # Non-zero return code
            if not classify(code):
                return code, message  # permanent failure, don't retry
            last_error = f"COM code {code}: {message}"
        except Exception as e:
            if not classify(e):
                raise  # permanent, propagate
            last_error = str(e)

        if attempt < MAX_ATTEMPTS - 1:
            delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
            log(
                f"Order send failed (attempt {attempt + 1}/{MAX_ATTEMPTS}): "
                f"{last_error}. Retrying in {delay}s..."
            )
            time.sleep(delay)

    log(f"Order send FAILED after {MAX_ATTEMPTS} attempts: {last_error}")
    return -1, f"Failed after {MAX_ATTEMPTS} retries: {last_error}"


def is_transient_error(error) -> bool:
    """Classify an error as transient (retryable) or permanent.

    Args:
        error: An ``int`` (COM return code) or ``Exception``.

    Returns:
        True if the error is likely transient and worth retrying.
    """
    if isinstance(error, int):
        # Negative COM codes are typically system/connection errors
        return error < 0

    if isinstance(error, Exception):
        msg = str(error).lower()
        return any(
            kw in msg
            for kw in (
                "timeout", "connection", "rpc", "com_error",
                "server", "network", "busy", "unavailable",
            )
        )

    return False
