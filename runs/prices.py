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

# US dollars per image, for a model that is priced per image: SAM 3.1, $2.50 per 1,000 images (confirmed).
PRICES_PER_IMAGE_USD = {
    "SAM 3.1": 0.0025,
}

# US dollars per hour of audio, for a model priced by audio length: Muse Voice Transcribe, $0.18 per hour (confirmed).
PRICES_PER_HOUR_USD = {
    "Muse Voice Transcribe": 0.18,
}

# Where each price comes from. "confirmed ..." was read from the vendor's price list; nothing is a placeholder now.
PRICE_STATUS = {
    "Muse Spark": "confirmed 2026-10-29, dev.meta.ai",
    "Muse Wonder": "confirmed 2026-10-29, dev.meta.ai",
    "SAM 3.1": "confirmed 2026-10-29, dev.meta.ai",
    "Muse Spark Open": "confirmed 2026-10-29, baseten.co",
    "Muse Voice Transcribe": "confirmed 2026-10-29, dev.meta.ai",
}

ROUND_TO = 6  # cost figures are kept to a millionth of a dollar


def cost_usd(model: str, prompt_tokens: int | None, completion_tokens: int | None) -> float | None:
    """The cost of one call: prompt tokens at the input price plus completion tokens at the output price.
    None when the model is not in the table or a token count was not recorded."""
    price = PRICES_PER_1M_USD.get(model)
    if price is None or not isinstance(prompt_tokens, int) or not isinstance(completion_tokens, int):
        return None
    return round((prompt_tokens * price["input"] + completion_tokens * price["output"]) / 1_000_000, ROUND_TO)


def image_cost_usd(model: str, images: int | None) -> float | None:
    """The cost of a call to a model priced per image; None when the model is not priced that way or the count is
    unknown."""
    price = PRICES_PER_IMAGE_USD.get(model)
    if price is None or not isinstance(images, int):
        return None
    return round(images * price, ROUND_TO)


def audio_cost_usd(model: str, audio_ms: int | None) -> float | None:
    """The cost of a call to a model priced per hour of audio, from the audio length the service reports."""
    price = PRICES_PER_HOUR_USD.get(model)
    if price is None or not isinstance(audio_ms, int) or isinstance(audio_ms, bool):
        return None
    return round(audio_ms / 3_600_000 * price, ROUND_TO)


def snapshot() -> dict:
    """The price table in force now, copied into run.json as run.prices when a run is made, so its cost can be
    recomputed later at the prices it was tallied at."""
    return {"per_1m_tokens_usd": {m: dict(p) for m, p in PRICES_PER_1M_USD.items()},
            "per_image_usd": dict(PRICES_PER_IMAGE_USD)}


def has_price(model: str) -> bool:
    """True when the model is in either table: a call to it can be priced."""
    return model in PRICES_PER_1M_USD or model in PRICES_PER_IMAGE_USD or model in PRICES_PER_HOUR_USD


def model_cost_fields(model: str, prompt_tokens: int | None, completion_tokens: int | None,
                      call_rows: int | None = None) -> dict:
    """The `tokens` and `cost_usd` fields for one Model trail step. `call_rows` is set when one call proposed
    several rows and this step carries its share of that call's tokens."""
    if not isinstance(prompt_tokens, int) or not isinstance(completion_tokens, int):
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
