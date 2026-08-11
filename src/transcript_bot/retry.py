from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TypeVar

import httpx


T = TypeVar("T")

# Keep retries bounded: they recover short-lived network/provider failures
# without making a user wait indefinitely or creating a second application job.
MAX_ATTEMPTS = 3
BASE_DELAY_SECONDS = 1.0

_LOGGER = logging.getLogger(__name__)


def request_with_retry(
    request: Callable[[], T],
    *,
    action: str,
    retry_request_errors: bool = True,
) -> T:
    """Run an HTTP request with bounded exponential backoff.

    Only connection/timeout errors and transient HTTP statuses (408, 425, 429,
    and 5xx) are retried.  Authentication, validation, and quota errors are
    returned immediately for the provider-specific caller to format safely.

    ``retry_request_errors`` is disabled for non-idempotent remote-job creation:
    if its response was lost, retrying could create a duplicate provider job.
    """

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = request()
        except httpx.RequestError:
            if not retry_request_errors or attempt == MAX_ATTEMPTS:
                raise
            _wait_before_retry(action, attempt, "network error")
            continue

        status_code = getattr(response, "status_code", None)
        if _is_retryable_response(response) and attempt < MAX_ATTEMPTS:
            _wait_before_retry(action, attempt, f"HTTP {status_code}")
            continue
        return response

    raise RuntimeError("unreachable")


def _is_retryable_status(status_code: object) -> bool:
    return isinstance(status_code, int) and (status_code in {408, 425, 429} or status_code >= 500)


def _is_retryable_response(response: object) -> bool:
    status_code = getattr(response, "status_code", None)
    if not _is_retryable_status(status_code):
        return False
    # 429 can mean a short rate limit (retryable), but some providers use it
    # for exhausted credits/quota (not retryable).  Avoid wasting attempts in
    # the latter case while keeping the user-facing provider message unchanged.
    detail = str(getattr(response, "text", "")).lower()
    return not any(marker in detail for marker in (
        "insufficient_quota",
        "credit_balance_exhausted",
        "quota exceeded",
        "quota_exceeded",
        "credits exhausted",
    ))


def _wait_before_retry(action: str, attempt: int, reason: str) -> None:
    delay = BASE_DELAY_SECONDS * (2 ** (attempt - 1))
    _LOGGER.warning("%s failed (%s); retrying in %ss (attempt %s/%s)", action, reason, delay, attempt + 1, MAX_ATTEMPTS)
    time.sleep(delay)
