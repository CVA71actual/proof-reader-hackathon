"""Shared pieces for the code checks: one result shape and one way to compare values."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

PASS = "pass"
FAIL = "fail"
COULD_NOT_RUN = "could_not_run"
NOT_NEEDED = "not_needed"  # the check does not apply to this item (e.g. no exact date was claimed)
RESULTS = (PASS, FAIL, COULD_NOT_RUN, NOT_NEEDED)


@dataclass(frozen=True)
class CheckResult:
    """What every check returns, so later checks slot in beside these two."""

    check: str  # stable id, e.g. "text_layer_match"
    label: str  # plain-English name shown on screen
    result: str  # pass / fail / could_not_run / not_needed
    reason: str  # one short plain sentence
    compared: dict = field(default_factory=dict)  # what was compared with what
    kind: str = "Code"  # who ran it: always code for these checks

    def __post_init__(self) -> None:
        if self.result not in RESULTS:
            raise ValueError(f"unknown check result {self.result!r}")

    def to_dict(self) -> dict:
        return asdict(self)


_SPACES = re.compile(r"\s+")


def normalize(value: str | None) -> str:
    """Compare-form of a printed value.

    - whitespace collapsed to single spaces and trimmed
    - no space just inside parentheses: "(381,684 )" -> "(381,684)"
    - a leading "$" (with or without a space after it) is dropped
    Nothing else is changed: digits, commas, dots and dashes must match as printed.
    """
    if value is None:
        return ""
    s = _SPACES.sub(" ", str(value)).strip()
    s = re.sub(r"^\$\s*", "", s)
    s = re.sub(r"\(\s+", "(", s)
    s = re.sub(r"\s+\)", ")", s)
    return s


def join_words(words: list[dict]) -> str:
    return " ".join(w["text"] for w in words)
