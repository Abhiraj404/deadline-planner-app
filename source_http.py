"""Shared HTTP helper for provider requests with bounded retries."""

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from time import sleep

import requests

REQUEST_TIMEOUT = 30
RETRY_STATUS = {408, 429, 500, 502, 503, 504}
MAX_RETRY_DELAY = 5


def retry_delay(response, attempt):
    """Seconds to wait before retrying, honouring Retry-After."""
    value = response.headers.get('Retry-After')
    if value:
        try:
            return max(0, int(value))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
                return max(0, (retry_at - datetime.now(UTC)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
    return 2**attempt


def get_with_retries(get, url, *, job=None, **kwargs):
    """GET with a timeout and up to three attempts for transient failures."""
    for attempt in range(3):
        if job:
            job.check()
        try:
            response = get(url, timeout=REQUEST_TIMEOUT, **kwargs)
        except (requests.ConnectionError, requests.Timeout):
            if attempt == 2:
                raise
            sleep(2**attempt)
            continue
        if response.status_code in RETRY_STATUS and attempt < 2:
            delay = retry_delay(response, attempt)
            if delay <= MAX_RETRY_DELAY:
                response.close()
                sleep(delay)
                continue
        try:
            response.raise_for_status()
        except requests.HTTPError:
            response.close()
            raise
        return response
