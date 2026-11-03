"""Brave Search, the research agent's PRIMARY search (BRAVE-PRIMARY-001, CVA 2026-11-02). Meta's built-in web_search
stays available as the secondary search.

    GET https://api.search.brave.com/res/v1/web/search?q=<query>&count=<n>
    headers: Accept: application/json, X-Subscription-Token: <BRAVE_API_KEY>

The key is read from the environment (BRAVE_API_KEY) or ~/.config/proof-reader/env and is never printed, logged,
saved or put in a reply. One request is one attempt: nothing is retried (a 429 is a recorded limitation).

STORAGE POLICY (Brave terms): Brave's result payload is not stored. The titles and snippets live only in memory for
the turn that returns them to the model. What is kept on disk (run.json, the Agent trail, research/turns/, the
manifest) is the query, the result URLs, and whatever the agent then fetches from the original sites itself.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
NAME = "Brave Search"
MAX_COUNT = 20
TIMEOUT_S = 30
NO_KEY = "Brave Search is unavailable in this run: no BRAVE_API_KEY is set."


@dataclass
class BraveResult:
    """One Brave request. `results` (title, url, snippet) is for the model's turn only; never written to disk."""
    query: str
    results: list = field(default_factory=list)
    error: str | None = None
    http_status: int | None = None
    rate_limited: bool = False
    may_have_been_billed: bool = True

    @property
    def urls(self) -> list[str]:
        return [r["url"] for r in self.results if r.get("url")]


def parse(query: str, data) -> BraveResult:
    web = (data or {}).get("web") if isinstance(data, dict) else None
    items = web.get("results") if isinstance(web, dict) else None
    if not isinstance(items, list):
        return BraveResult(query, error="Brave answered with no web results list.", may_have_been_billed=True)
    out = [{"title": r.get("title"), "url": r.get("url"), "snippet": r.get("description")}
           for r in items if isinstance(r, dict) and isinstance(r.get("url"), str)]
    return BraveResult(query, results=out)


class LiveBrave:
    name = NAME

    def _key(self) -> str:
        from capture import read_table
        read_table.load_env_file()
        return os.environ.get("BRAVE_API_KEY", "").strip()

    def ready(self) -> str | None:
        return None if self._key() else NO_KEY

    def search(self, query: str, count: int = 10) -> BraveResult:
        import httpx
        count = max(1, min(int(count or 10), MAX_COUNT))
        try:
            with httpx.Client(timeout=httpx.Timeout(TIMEOUT_S, connect=10)) as client:
                resp = client.get(ENDPOINT, params={"q": query, "count": count},
                                  headers={"Accept": "application/json", "X-Subscription-Token": self._key()})
        except Exception as e:  # the type only: nothing that could carry the key
            connect = isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout))
            return BraveResult(query, error=f"Brave Search did not answer ({type(e).__name__}).",
                               may_have_been_billed=not connect)
        if resp.status_code == 429:
            return BraveResult(query, error="Brave Search rate limit reached (HTTP 429).", http_status=429,
                               rate_limited=True)
        if resp.status_code >= 400:
            return BraveResult(query, error=f"Brave Search answered HTTP {resp.status_code}.",
                               http_status=resp.status_code, may_have_been_billed=resp.status_code not in (401, 403))
        try:
            data = resp.json()
        except ValueError:
            return BraveResult(query, error="Brave Search answered with something that is not JSON.",
                               http_status=resp.status_code)
        got = parse(query, data)
        got.http_status = resp.status_code
        return got
