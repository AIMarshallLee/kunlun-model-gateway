"""One monotonic upstream window across retries and streaming; no retry on expiry."""

import asyncio
import time

from gateway import ProviderError

MANAGED_REQUEST_SECONDS = 90


async def await_with_deadline(operation, deadline):
    if deadline is None:
        return await operation
    try:
        return await asyncio.wait_for(operation, timeout=max(0, deadline - time.monotonic()))
    except TimeoutError:
        raise ProviderError(504, category="request_deadline_exceeded", safe_to_failover=False,
                            request_may_be_billable=True) from None


async def chunks_with_deadline(chunks, deadline):
    iterator = chunks.__aiter__()
    while True:
        try:
            chunk = await await_with_deadline(anext(iterator), deadline)
        except StopAsyncIteration:
            return
        yield chunk
