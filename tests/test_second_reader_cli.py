"""The CLI's paid-fallback gate: without --allow-paid-fallback no OpenRouter call happens on a primary failure."""
import pytest

from capture import read_table
from capture.reader import Reply, timeout_reply
from capture.second_reader import BASETEN_MODEL, OPENROUTER_MODEL, SecondReader, main
import capture.second_reader as second_reader


@pytest.fixture
def seam(monkeypatch, tmp_path):
    monkeypatch.setattr(read_table, "ENV_FILE", tmp_path / "absent")
    for k in ("BASETEN_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BASETEN_API_KEY", "bt-test")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
    monkeypatch.setattr(second_reader.read_events, "load_words", lambda: {})
    monkeypatch.setattr(second_reader, "load_entry", lambda pin: {})
    monkeypatch.setattr(second_reader.read_events, "build_prompt", lambda w, e: "prompt")
    monkeypatch.setattr(second_reader.read_events, "parse_events", lambda raw: ([{"q": 1}], None))
    monkeypatch.setattr(second_reader.read_events, "_record", lambda **kw: kw)
    calls = []
    return calls


def test_without_the_flag_a_primary_failure_makes_no_openrouter_call(seam, tmp_path, monkeypatch, capsys):
    def ask(self, route, prompt, image=None):
        seam.append(route)
        return Reply(None, self.model, error="503 service unavailable", extra={"route": route})
    monkeypatch.setattr(SecondReader, "_ask", ask)
    assert main(["--words", "--out", str(tmp_path / "r.json")]) == 1
    assert seam == ["baseten"], "no OpenRouter call without the flag"
    assert "--allow-paid-fallback" in capsys.readouterr().out
    import json as _json
    extra = _json.loads((tmp_path / "r.json").read_text())["extra"]
    assert extra["fallback_skipped"] is True and extra["fallback_refused_reason"] == second_reader.CLI_REFUSED


def test_with_the_flag_the_fallback_is_called_once(seam, tmp_path, monkeypatch):
    def ask(self, route, prompt, image=None):
        seam.append(route)
        if route == "baseten":
            return timeout_reply("Muse Spark Open", BASETEN_MODEL, extra={"route": route})
        return Reply('{"events": []}', OPENROUTER_MODEL, extra={"route": route})
    monkeypatch.setattr(SecondReader, "_ask", ask)
    assert main(["--words", "--allow-paid-fallback", "--out", str(tmp_path / "r.json")]) == 0
    assert seam == ["baseten", "openrouter"]


def test_a_successful_primary_never_touches_the_fallback(seam, tmp_path, monkeypatch):
    def ask(self, route, prompt, image=None):
        seam.append(route)
        return Reply('{"events": []}', BASETEN_MODEL, extra={"route": route})
    monkeypatch.setattr(SecondReader, "_ask", ask)
    assert main(["--words", "--allow-paid-fallback", "--out", str(tmp_path / "r.json")]) == 0
    assert seam == ["baseten"]


# Network level: the real SecondReader.call/_ask run against a fake openai client. Any OpenRouter request without
# an explicit success mode raises, so a stray paid call fails the test instead of passing silently.
import json
import types

import httpx

from capture import http


class _StatusError(Exception):
    def __init__(self, code):
        super().__init__(f"status {code}")
        self.status_code = code


@pytest.fixture
def net(monkeypatch, tmp_path):
    monkeypatch.setattr(read_table, "ENV_FILE", tmp_path / "absent")
    monkeypatch.setenv("BASETEN_API_KEY", "bt-test")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
    monkeypatch.setattr(second_reader.read_events, "load_words", lambda: {})
    monkeypatch.setattr(second_reader, "load_entry", lambda pin: {})
    monkeypatch.setattr(second_reader.read_events, "build_prompt", lambda w, e: "prompt")
    monkeypatch.setattr(second_reader.read_events, "parse_events", lambda raw: ([], None))
    monkeypatch.setattr(second_reader.read_events, "_record",
                        lambda **kw: {"model": kw["model"], "problem": kw["problem"], "extra": kw["extra"]})
    monkeypatch.setattr(http, "RETRY_DELAY_S", 0, raising=False)
    calls, mode = [], {}

    def client(base, key):
        def create(**kw):
            calls.append(base)
            if base == second_reader.OPENROUTER_BASE_URL:
                if mode.get("openrouter_ok"):
                    msg = types.SimpleNamespace(content='{"events": []}')
                    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)], usage=None, model="m")
                raise AssertionError("OpenRouter network call made")
            if mode["baseten"] == "timeout":
                raise httpx.ReadTimeout("timed out")
            raise _StatusError(mode["baseten"])
        return types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))

    monkeypatch.setattr(http, "openai_client", client)
    return calls, mode, tmp_path / "r.json"


def _openrouter_calls(calls):
    return calls.count(second_reader.OPENROUTER_BASE_URL)


@pytest.mark.parametrize("baseten", [500, 503, 429, 400, 401, 403, "timeout"])
def test_network_without_the_flag_refuses_and_records_the_refusal(net, baseten, capsys):
    calls, mode, out = net
    mode["baseten"] = baseten
    assert main(["--words", "--out", str(out)]) == 1
    assert calls and _openrouter_calls(calls) == 0, calls
    assert second_reader.CLI_REFUSED in capsys.readouterr().out
    extra = json.loads(out.read_text())["extra"]
    assert extra["route"] == "baseten" and extra["call_failed"] is True
    assert extra["fallback_skipped"] is True
    assert extra["fallback_refused_reason"] == second_reader.CLI_REFUSED


@pytest.mark.parametrize("baseten", [401, 403])
def test_network_401_403_stay_unbilled_when_refused(net, baseten):
    calls, mode, out = net
    mode["baseten"] = baseten
    assert main(["--words", "--out", str(out)]) == 1
    extra = json.loads(out.read_text())["extra"]
    assert extra["http_status"] == baseten and extra["may_have_been_billed"] is False
    assert extra["fallback_skipped"] is True


def test_network_only_an_openrouter_key_without_the_flag_exits_2_with_no_call(net, monkeypatch, capsys):
    calls, mode, out = net
    monkeypatch.delenv("BASETEN_API_KEY")
    assert main(["--words", "--out", str(out)]) == 2
    assert calls == []
    assert second_reader.CLI_REFUSED in capsys.readouterr().out
    assert not out.exists()


def test_network_no_openrouter_key_says_the_fallback_is_not_configured(net, monkeypatch, capsys):
    calls, mode, out = net
    monkeypatch.delenv("OPENROUTER_API_KEY")
    mode["baseten"] = 503
    assert main(["--words", "--out", str(out)]) == 1
    printed = capsys.readouterr().out
    assert "--allow-paid-fallback" not in printed
    assert second_reader.CLI_NO_FALLBACK in printed
    extra = json.loads(out.read_text())["extra"]
    assert "fallback_skipped" not in extra and "fallback_refused_reason" not in extra
    assert _openrouter_calls(calls) == 0


@pytest.mark.parametrize("baseten", [500, 401, "timeout"])
def test_network_with_the_flag_exactly_one_openrouter_call(net, baseten):
    calls, mode, out = net
    mode.update(baseten=baseten, openrouter_ok=True)
    assert main(["--words", "--allow-paid-fallback", "--out", str(out)]) == 0
    assert _openrouter_calls(calls) == 1, calls
    extra = json.loads(out.read_text())["extra"]
    assert extra["route"] == "openrouter"
    assert "fallback_skipped" not in extra and "fallback_refused_reason" not in extra



_REAL_RECORD = second_reader.read_events._record     # taken at import, before any fixture patches them
_REAL_LOAD_ENTRY = second_reader.load_entry


def test_the_saved_refusal_survives_reload_through_the_run_build(net, monkeypatch, tmp_path):
    """The refused record is written by the real read_events._record, then read back by runs.build_entry's own
    --second path (the loader the two-reader run build uses). The dict build_entry_run receives still carries the
    refusal unchanged, and the build treats it as no second read."""
    from runs import build_entry
    monkeypatch.setattr(second_reader.read_events, "_record", _REAL_RECORD)
    monkeypatch.setattr(second_reader, "load_entry", _REAL_LOAD_ENTRY)
    calls, mode, out = net
    mode["baseten"] = 503
    assert main(["--words", "--out", str(out)]) == 1
    saved = out.read_text()
    received = []
    real_build = build_entry.build_entry_run

    def spy(reading, run_dir, **kw):
        received.append(kw["second_reading"])
        return real_build(reading, run_dir, **kw)
    monkeypatch.setattr(build_entry, "build_entry_run", spy)
    assert build_entry.main(["--reading", str(second_reader.read_events.READING_PATH), "--second", str(out),
                             "--out-dir", str(tmp_path / "run")]) == 0
    (loaded,) = received
    assert loaded["fallback_skipped"] is True
    assert loaded["fallback_refused_reason"] == second_reader.CLI_REFUSED
    assert loaded["call_failed"] is True and loaded["route"] == "baseten" and loaded["second_reader"] is True
    assert _openrouter_calls(calls) == 0
    assert out.read_text() == saved, "the build must not rewrite the saved record"
    run = json.loads((tmp_path / "run" / "run.json").read_text())
    assert "second_reader" not in run["run"], "a refused second read is not a second read"
