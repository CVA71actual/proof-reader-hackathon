"""Check: does the value the reader gave appear in the page's own text?

Shown on screen as "Matches source text".
"""

from __future__ import annotations

from checks.common import COULD_NOT_RUN, FAIL, PASS, CheckResult, join_words, normalize

CHECK_ID = "text_layer_match"
LABEL = "Matches source text"

# A printed value is at most a few tokens, e.g. "$", "(381,684", ")".
MAX_RUN = 6


def text_layer_match(value: str | None, words: list[dict], *, transcribed: bool = False) -> CheckResult:
    """Pass if the normalized value equals some run of consecutive page words.
    `transcribed`: the words are the page's transcribed/derived words, not a visible native text layer
    (checks/restriction.py says the page is restricted); the reason then says what was compared."""
    want = normalize(value)
    where = "the page's transcribed words" if transcribed else "the page text"

    def result(res: str, reason: str, **compared) -> CheckResult:
        return CheckResult(CHECK_ID, LABEL, res, reason, {"value": want, **compared})

    if not words:
        return result(COULD_NOT_RUN, "The page has no text layer to compare against.")
    if not want:
        return result(COULD_NOT_RUN, "The reader gave no value for this cell.")

    for i in range(len(words)):
        for n in range(1, MAX_RUN + 1):
            run = words[i : i + n]
            if len(run) < n:
                break
            if normalize(join_words(run)) == want:
                return result(
                    PASS,
                    f"The value appears in {where}.",
                    found=join_words(run),
                    word_ids=[w["id"] for w in run],
                )
    return result(FAIL, f"The value does not appear anywhere in {where}.", found=None)
