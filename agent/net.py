"""Retry helper for transient network failures.

CLAUDE.md documents 1-1.9s RTT from India to these US-hosted providers;
occasional timeouts and connection drops are expected here, not exceptional.
One retry after a short pause meaningfully improves benchmark completion
rate without masking genuine failures - a second failure still raises.
"""
import asyncio

import httpx

TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}


async def with_retry(fn, attempts: int = 2, delay: float = 0.5):
    """Call async `fn()` (no args), retrying once on transient network errors."""
    for i in range(attempts):
        try:
            return await fn()
        except httpx.HTTPStatusError as e:
            if e.response.status_code not in TRANSIENT_STATUS or i == attempts - 1:
                raise
        except (httpx.TimeoutException, httpx.ConnectError):
            if i == attempts - 1:
                raise
        await asyncio.sleep(delay)
