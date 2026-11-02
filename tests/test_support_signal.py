"""The support signal is findings-only: supported / unsupported_spans / could_not_run, never a gate."""
from capture.support_signal import MODEL, support_signal


class FakeDetector:
    model = MODEL
    weights_sha256 = "fakesha"

    def __init__(self, spans=(), tokens=100, err=None):
        self.spans, self.tokens, self.err = spans, tokens, err

    def count_tokens(self, passage, claim):
        return self.tokens

    def predict(self, passage, claim):
        if self.err:
            raise self.err
        return self.spans


CLAIM = {"claim": "results are expected in Q1 2027", "program": "PN-X"}


def test_flagged_spans_are_recut_from_the_submitted_claim():
    out = support_signal(CLAIM, "the cited passage", detector=FakeDetector([{"start": 4, "end": 11}]))
    assert out["result"] == "unsupported_spans"
    assert out["spans"] == [{"start": 4, "end": 11, "text": CLAIM["claim"][4:11]}]
    assert out["model"] == MODEL and out["weights_sha256"] == "fakesha"


def test_no_spans_is_supported_and_never_a_gate():
    out = support_signal(CLAIM, "the cited passage", detector=FakeDetector([]))
    assert out["result"] == "supported" and out["spans"] == []
    assert not {"acceptance", "accepted", "group", "calendar", "status"} & set(out)


def test_missing_weights_are_could_not_run(monkeypatch, tmp_path):
    monkeypatch.delenv("LETTUCE_WEIGHTS_PATH", raising=False)
    out = support_signal(CLAIM, "the cited passage", weights_path=str(tmp_path / "absent"))
    assert out["result"] == "could_not_run" and out["spans"] == []


def test_over_limit_empty_and_failed_calls_are_could_not_run():
    assert support_signal(CLAIM, "x", detector=FakeDetector(tokens=5000))["result"] == "could_not_run"
    assert support_signal({"claim": ""}, "x", detector=FakeDetector())["result"] == "could_not_run"
    assert support_signal(CLAIM, "x", detector=FakeDetector(err=RuntimeError("boom")))["result"] == "could_not_run"


def test_a_detector_returning_an_invalid_span_is_could_not_run():
    out = support_signal(CLAIM, "x", detector=FakeDetector([{"start": 9, "end": 4}]))
    assert out["result"] == "could_not_run" and out["spans"] == []
