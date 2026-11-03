"""The fixed price table: what one model call costs, from its token counts.

Every Model step in a run's trail carries `tokens` {prompt, completion} and `cost_usd`, computed here from the
saved reading's usage. Nothing is looked up at run time and nothing is estimated: a step whose tokens were not
recorded has `tokens` null and `cost_usd` null, and a run with no recorded cost has `spend_usd` null.

Prices: Muse Spark, Muse Wonder and SAM 3.1 are the prices dev.meta.ai lists, confirmed 2026-10-29. Muse Spark Open
(Baseten, meta-models/muse-spark-open) is confirmed at input $1.25, output $4.25, cached input $0.15 per 1M tokens, read from
baseten.co/library/muse-spark-open/ and /pricing/ on 2026-10-29 (PRICE_STATUS says where each price comes from, and a test pins it).
A cached input price is kept for reference; the saved readings do not record cached tokens, so every input token is priced at the
input price (the higher figure: a cost is never understated for it).
"""

from __future__ import annotations

# US dollars per 1,000,000 tokens.
PRICES_PER_1M_USD = {
    "Muse Spark": {"input": 1.25, "output": 4.25, "cached_input": 0.15},     # muse-spark-1.3 (confirmed)
    "Muse Spark Open": {"input": 1.25, "output": 4.25, "cached_input": 0.15},  # the second reader (Baseten, confirmed)
    "Muse Wonder": {"input": 4.00, "output": 20.00, "cached_input": 0.20},   # muse-wonder-1.0 (confirmed)
}

# The independent reviewer's model (MULTI-FAMILY-001, CVA 2026-11-02): deepseek-ai/DeepSeek-V4-Pro-0813 on Baseten.
# Kept in its own table (priced exactly like the table above) so saved runs' price snapshots are unchanged; a research
# run records it in run.prices.reviewer_per_1m_tokens_usd.
REVIEWER_PRICES_PER_1M_USD = {
    "DeepSeek V4 Pro": {"input": 1.32, "output": 3.96, "cached_input": 0.132},
}
REVIEWER_PRICE_STATUS = {
    "DeepSeek V4 Pro": ("listed by Baseten's own model list (GET inference.baseten.co/v1/models, pricing per token: "
                        "prompt 0.00000132, completion 0.00000396), read 2026-11-02; the public baseten.co pricing "
                        "page could not be read by machine, so it is not yet confirmed against that page"),
}


def _token_price(model: str):
    return PRICES_PER_1M_USD.get(model) or REVIEWER_PRICES_PER_1M_USD.get(model)

# US dollars per image, for a model that is priced per image: SAM 3.1, $2.50 per 1,000 images (confirmed).
PRICES_PER_IMAGE_USD = {
    "SAM 3.1": 0.0025,
}

# US dollars per hour of audio, for a model priced by audio length: Muse Voice Transcribe, $0.18 per hour (confirmed).
PRICES_PER_HOUR_USD = {
    "Muse Voice Transcribe": 0.18,
}

# US dollars per 1,000 built-in web searches, for a model whose Responses API call can run its own web_search tool
# (search grounding). Muse Spark 1.3: $2.50 per 1,000 searches on top of tokens (dev.meta.ai, read 2026-11-02).
PRICES_PER_1K_SEARCHES_USD = {
    "Muse Spark": 2.50,
}

# Where each price comes from. "confirmed ..." was read from the vendor's price list; nothing is a placeholder now.
PRICE_STATUS = {
    "Muse Spark": "confirmed 2026-10-29, dev.meta.ai",
    "Muse Wonder": "confirmed 2026-10-29, dev.meta.ai",
    "SAM 3.1": "confirmed 2026-10-29, dev.meta.ai",
    "Muse Spark Open": "confirmed 2026-10-29, baseten.co",
    "Muse Voice Transcribe": "confirmed 2026-10-29, dev.meta.ai",
}
# US dollars per 1,000 requests, for a search API priced per request: Brave Search (BRAVE-PRIMARY-001, CVA 2026-11-02,
# $5 per 1,000 requests).
PRICES_PER_1K_REQUESTS_USD = {
    "Brave Search": 5.00,
}
REQUEST_PRICE_STATUS = {
    "Brave Search": "set by CVA 2026-11-02 (BRAVE-PRIMARY-001): $5 per 1,000 requests",
}


def request_cost_usd(name: str, requests: int | None) -> float | None:
    """The cost of requests to a per-request priced API; None when unpriced or the count is not a count."""
    price = PRICES_PER_1K_REQUESTS_USD.get(name)
    if price is None or not _count(requests):
        return None
    return round(requests * price / 1000, ROUND_TO)


SEARCH_PRICE_STATUS = {
    "Muse Spark": "confirmed 2026-11-02, dev.meta.ai/docs/search-grounding ($2.50 per 1,000 searches)",
}

ROUND_TO = 6  # cost figures are kept to a millionth of a dollar


def _count(n) -> bool:
    """A count a cost may be computed from: a whole number from 0. Booleans, negatives, floats and None are not
    counts (the call is then unpriced, never a negative or zero spend)."""
    return type(n) is int and 0 <= n <= MAX_COUNT


# A count beyond this is not a real usage report (10**15 tokens is ~a billion dollars at these prices): it is treated as
# unpriced, never converted to a float that overflows (Manus 17:07: 10**400 tokens raised OverflowError).
MAX_COUNT = 10 ** 15


def cost_usd(model: str, prompt_tokens: int | None, completion_tokens: int | None) -> float | None:
    """The cost of one call: prompt tokens at the input price plus completion tokens at the output price.
    None when the model is not in the table or a token count was not recorded."""
    price = _token_price(model)
    if price is None or not _count(prompt_tokens) or not _count(completion_tokens):
        return None
    return round((prompt_tokens * price["input"] + completion_tokens * price["output"]) / 1_000_000, ROUND_TO)


def image_cost_usd(model: str, images: int | None) -> float | None:
    """The cost of a call to a model priced per image; None when the model is not priced that way or the count is
    unknown."""
    price = PRICES_PER_IMAGE_USD.get(model)
    if price is None or not _count(images):
        return None
    return round(images * price, ROUND_TO)


def audio_cost_usd(model: str, audio_ms: int | None) -> float | None:
    """The cost of a call to a model priced per hour of audio, from the audio length the service reports."""
    price = PRICES_PER_HOUR_USD.get(model)
    if price is None or not _count(audio_ms):
        return None
    return round(audio_ms / 3_600_000 * price, ROUND_TO)


def search_cost_usd(model: str, searches: int | None) -> float | None:
    """The cost of a call's built-in web searches. 0.0 for zero searches on a model priced for search; None when the
    count is unknown or the model has no search price (an unknown search cost is never priced as zero)."""
    if not _count(searches):
        return None
    if searches == 0:
        return 0.0
    price = PRICES_PER_1K_SEARCHES_USD.get(model)
    if price is None:
        return None
    return round(searches * price / 1000, ROUND_TO)


def responses_cost_usd(model: str, input_tokens: int | None, output_tokens: int | None,
                       searches: int | None) -> float | None:
    """One Responses API call: tokens at the token prices plus its searches at the search price. None when any
    part cannot be priced (the whole call is then unpriced, never partly counted)."""
    tokens = cost_usd(model, input_tokens, output_tokens)
    search = search_cost_usd(model, searches)
    if tokens is None or search is None:
        return None
    return round(tokens + search, ROUND_TO)


def responses_worst_case_usd(model: str, max_input_tokens: int, max_output_tokens: int,
                             max_searches: int) -> float | None:
    """The most one Responses call can cost under its own bounds (input tokens are bounded by the request's bytes,
    output by max_output_tokens, searches by max_tool_calls). None when the model has no price."""
    return responses_cost_usd(model, max_input_tokens, max_output_tokens, max_searches)


def snapshot() -> dict:
    """The price table in force now, copied into run.json as run.prices when a run is made, so its cost can be
    recomputed later at the prices it was tallied at."""
    return {"per_1m_tokens_usd": {m: dict(p) for m, p in PRICES_PER_1M_USD.items()},
            "per_image_usd": dict(PRICES_PER_IMAGE_USD), "per_hour_usd": dict(PRICES_PER_HOUR_USD)}


def request_snapshot() -> dict:
    """The per-request prices in force, added to run.prices by a research run."""
    return dict(PRICES_PER_1K_REQUESTS_USD)


def search_snapshot() -> dict:
    """The search prices in force, added to run.prices by a research run (page-mode snapshots are unchanged)."""
    return dict(PRICES_PER_1K_SEARCHES_USD)


def has_price(model: str) -> bool:
    """True when the model is in either table: a call to it can be priced."""
    return (model in PRICES_PER_1M_USD or model in REVIEWER_PRICES_PER_1M_USD or model in PRICES_PER_IMAGE_USD
            or model in PRICES_PER_HOUR_USD)


def model_cost_fields(model: str, prompt_tokens: int | None, completion_tokens: int | None,
                      call_rows: int | None = None) -> dict:
    """The `tokens` and `cost_usd` fields for one Model trail step. `call_rows` is set when one call proposed
    several rows and this step carries its share of that call's tokens."""
    if not _count(prompt_tokens) or not _count(completion_tokens):
        return {"tokens": None, "cost_usd": None}
    tokens = {"prompt": prompt_tokens, "completion": completion_tokens}
    if call_rows is not None and call_rows > 1:
        tokens["call_rows"] = call_rows
    return {"tokens": tokens, "cost_usd": cost_usd(model, prompt_tokens, completion_tokens)}


def split_tokens(total: int | None, parts: int) -> list[int | None]:
    """One call's token count split evenly across the rows it proposed, the remainder to the first rows, so
    the shares add up to the call's count exactly and the run's spend counts the call once."""
    if not isinstance(total, int) or parts < 1:
        return [None] * max(parts, 0)
    base, extra = divmod(total, parts)
    return [base + (1 if i < extra else 0) for i in range(parts)]


def run_spend_usd(rows: list[dict]) -> float | None:
    """The run's spend: the sum of the cost of every Model step in every row's trail. None when no Model
    step recorded a cost, or when any Model step's cost was not recorded (the sum would understate it)."""
    costs = [s.get("cost_usd") for r in rows for s in r.get("trail", []) if s.get("tag") == "Model"]
    if not costs or any(c is None for c in costs):
        return None
    return round(sum(costs), ROUND_TO)


def cost_per_kept_row_usd(spend_usd: float | None, kept_rows: int) -> float | None:
    """Spend divided by the rows now kept or corrected; None when the spend was not recorded or no row is kept."""
    if spend_usd is None or kept_rows <= 0:
        return None
    return round(spend_usd / kept_rows, ROUND_TO)
