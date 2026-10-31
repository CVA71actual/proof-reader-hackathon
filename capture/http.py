"""One place for how long a model service gets to answer, and the one retry.

Every call to a model service (the Meta Model API, Baseten, OpenRouter, SAM) is built with `openai_client` and run through
`call_with_retry`: connect 20 s, read 120 s, and on a timeout or a connection error ONE more try after 5 s. The
OpenAI library's own retries are off (max_retries=0) so this is the only retry. The voice route (capture/voice.py) keeps
its 60 s. A call that still gets no answer is a Reply with `timed_out` and the plain reason `did_not_answer(name)`.
"""

from __future__ import annotations

import threading
import time

import httpx

CONNECT_TIMEOUT_S = 20
READ_TIMEOUT_S = 120
VOICE_TIMEOUT_S = 60
RETRY_DELAY_S = 5
# Every text model call asks for at most this many completion tokens, so a call's worst cost is known: a try that got no
# answer is charged prompt tokens + this many (server/jobs.py). The largest completion in the saved runs is 5902 tokens;
# 12288 leaves 2x headroom (reasoning tokens count).
MAX_COMPLETION_TOKENS = 12288

_SILENT_NAMES = {"APITimeoutError", "APIConnectionError", "TimeoutException", "TransportError"}


def did_not_answer(name: str) -> str:
    return f"The {name} service did not answer in time."


def timeout() -> httpx.Timeout:
    return httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S)


def openai_client(base_url: str, api_key: str):
    from openai import OpenAI  # imported here so tests and offline use need no network client

    return OpenAI(base_url=base_url, api_key=api_key, timeout=timeout(), max_retries=0)


def is_silent(e: BaseException) -> bool:
    """True for a service that did not answer: a timeout or a connection that could not be made or was dropped."""
    if isinstance(e, (httpx.TransportError, TimeoutError, ConnectionError)):
        return True
    return any(c.__name__ in _SILENT_NAMES for c in type(e).__mro__)


def _is_connect_failure(e: BaseException | None) -> bool:
    """True when nothing was sent: httpx.ConnectTimeout / httpx.ConnectError, or an error whose cause is one of those."""
    seen = 0
    while e is not None and seen < 8:
        if isinstance(e, (httpx.ConnectTimeout, httpx.ConnectError)):
            return True
        e, seen = (e.__cause__ or e.__context__), seen + 1
    return False


def may_have_been_billed(e: BaseException) -> bool:
    """A silent failure that is not a connect-phase one: the request may have been sent, processed and billed."""
    return is_silent(e) and not _is_connect_failure(e)


_local = threading.local()


def take_billed() -> bool:
    """True if a call_with_retry on this thread retried after a possibly-billed timeout since the last take. Clears it."""
    billed = getattr(_local, "billed", False)
    _local.billed = False
    return billed


def call_with_retry(fn, on_retry=None):
    """fn(), and once more after RETRY_DELAY_S if the service did not answer. Any other error is raised at once.
    on_retry(e, billed) is called before the second attempt; billed says the first try may have been billed."""
    try:
        return fn()
    except Exception as e:
        if not is_silent(e):
            raise
        billed = may_have_been_billed(e)
        _local.billed = getattr(_local, "billed", False) or billed
        if on_retry is not None:
            on_retry(e, billed)
    time.sleep(RETRY_DELAY_S)
    return fn()
