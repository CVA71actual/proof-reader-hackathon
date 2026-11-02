"""ONE-MODEL-RUN-001: a run may use Muse Spark alone.

The New Run choice `models` is "one" (Muse Spark only: no second reader, no Muse Wonder escalation, no SAM 3.1) or
"all" (the default: Muse Spark, checked by a second reader). The job, the run card, the trail and the reports read it
from run.json's choices through one_model(), and say it in ONE_MODEL_LINE. Where a surface would count the second
reader's catches, a one-model run shows NO_SECOND_READER instead of a zero."""
from __future__ import annotations

MODELS_CHOICES = ("one", "all")
ONE_MODEL_LINE = "One model: Muse Spark only (no second reader, no escalation)."
NO_SECOND_READER = "No second reader in this run"


def one_model(choices) -> bool:
    """True for a run started with only Muse Spark (choices.models "one"). A run saved before the choice is "all"."""
    return isinstance(choices, dict) and choices.get("models") == "one"


def run_is_one_model(run) -> bool:
    """one_model() of a whole run.json dict."""
    return isinstance(run, dict) and one_model((run.get("run") or {}).get("choices"))
