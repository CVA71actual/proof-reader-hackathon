"""Red Team round 10, server half. A failure is a finding: each assertion is the result the code should have given.

Product main 33c05a7b6c2a7f67924f3915f473d1ddf993015c. The voice route is Job 29. The picture-page path is Job 27,
including the round-9 fixes already on this main. Tests only. Fakes only. No model call, no network, no key.
"""

import io
import json
import shutil
import struct
import wave
from datetime import date

import pytest
from conftest import REPO
from fastapi.testclient import TestClient

from capture import voice
from checks import coverage
from runs import picture, prices
from runs.coverage import possible_event_rows
from server.app import create_app


def _wav(seconds=1.0, rate=16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00" * int(seconds * rate) * 2)
    return buf.getvalue()


def _float_wav() -> bytes:
    """One second of mono 16 kHz audio whose fmt chunk says IEEE float (format code 3), not PCM."""
    frames = 16000
    fmt = struct.pack("<HHLLHH", 3, 1, 16000, 32000, 2, 16)
    data = b"\x00\x00" * frames
    body = b"fmt " + struct.pack("<L", 16) + fmt + b"data" + struct.pack("<L", len(data)) + data
    return b"RIFF" + struct.pack("<L", 4 + len(body)) + b"WAVE" + body


def _voice(tmp_path, transcriber, limit=None):
    d = tmp_path / "runs"
    shutil.copytree(REPO / "runs" / "sample", d / "sample")
    path = d / "sample" / "run.json"
    doc = json.loads(path.read_text())
    if limit is not None:
        doc["run"]["choices"]["spending_limit_usd"] = limit
        path.write_text(json.dumps(doc))
    client = TestClient(create_app(runs_dir=d, ui_dir=d / "no-ui", transcriber=transcriber))
    return client, d, doc["rows"][0]["id"]


def _spend(d):
    return json.loads((d / "sample" / "run.json").read_text())["run"]["spend_usd"]


# ---------------------------------------------------------------- voice route


def test_a_float_wav_is_not_plain_pcm():
    """check_wav allows only plain PCM. A 16-bit IEEE-float file has the width and rate of a PCM file, so the
    width check does not refuse it. The format code does."""
    data = _float_wav()
    # read the header by hand: wave.open itself refuses format 3 on some Pythons (3.12), which would hide the point
    code, channels, rate, _, _, bits = struct.unpack_from("<HHLLHH", data, 20)
    assert code == 3
    assert bits == 16 and channels == 1 and rate == 16000
    with pytest.raises(voice.VoiceError, match="PCM"):
        voice.check_wav(data)


def test_a_second_sentence_is_not_part_of_the_command():
    """Stripping a period joins two sentences into one shape. Each sentence on its own is not that command."""
    assert voice.propose("Fix the date.") is None
    assert voice.propose("To March third twenty twenty seven.") is None
    assert voice.propose("Fix the date. To March third twenty twenty seven.") is None

    assert voice.propose("change the program.") is None
    assert voice.propose("To ABC-123.") is None
    assert voice.propose("change the program. To ABC-123.") is None

    assert voice.propose("No, keep it.") is None
    assert voice.propose("Remove it. No, keep it.") is None


def test_a_negative_audio_duration_does_not_lower_the_spend(tmp_path):
    """The service's audioDurationMs is the billed length. A negative length is not a length, and the run's
    spend has a floor of zero."""
    ms = -3_600_000

    def fake(data):
        voice.check_wav(data)
        return {"transcript": "keep it", "audio_ms": ms, "cost_usd": prices.audio_cost_usd(voice.NAME, ms)}

    client, d, row = _voice(tmp_path, fake)
    before = _spend(d)
    client.post(f"/api/runs/sample/voice/decide?target={row}", content=_wav(),
                headers={"content-type": "audio/wav"})
    after = _spend(d)
    assert after >= before
    assert after >= 0


def test_a_voice_turn_does_not_spend_past_the_limit(tmp_path):
    """Reading stops before a call once spend_usd is at choices.spending_limit_usd. A spoken decision is another
    priced call, and it does not add cost once the run is already at that limit."""

    def fake(data):
        voice.check_wav(data)
        ms = 3_600_000
        return {"transcript": "keep it", "audio_ms": ms, "cost_usd": prices.audio_cost_usd(voice.NAME, ms)}

    at_limit = json.loads((REPO / "runs" / "sample" / "run.json").read_text())["run"]["spend_usd"]
    client, d, row = _voice(tmp_path, fake, limit=at_limit)
    before = _spend(d)
    assert before == at_limit
    client.post(f"/api/runs/sample/voice/decide?target={row}", content=_wav(),
                headers={"content-type": "audio/wav"})
    assert _spend(d) <= at_limit
    assert _spend(d) == before


def test_the_price_snapshot_keeps_the_voice_hour_price():
    """run.prices is the table a run was tallied at, so a voice turn can be repriced later. The hour price is
    part of that table."""
    snap = prices.snapshot()
    found = [table[voice.NAME] for table in snap.values() if isinstance(table, dict) and voice.NAME in table]
    assert found == [0.18]


# ---------------------------------------------------------------- picture pages


def test_a_picture_sentence_does_not_cross_a_region_break():
    """A region break is an empty word that a quote does not cross. Dated-sentence coverage builds the rows a
    person sees for sentences the reader skipped, so a sentence must not take its words from two regions."""
    title = {"id": "r1", "concept": "title", "box": [40.0, 100.0, 300.0, 140.0]}
    body = {"id": "r2", "concept": "text block", "box": [40.0, 500.0, 400.0, 560.0]}
    doc = picture.build_words(612.0, 792.0, 3, [
        {"region": title, "text": "Phase 2b initiation"},
        {"region": body, "text": "in March 12, 2027."},
    ])
    assert any(w.get("break") for w in doc["words"])
    crossed = [s for s in coverage.sentences(doc["words"]) if "initiation" in s["text"] and "2027" in s["text"]]
    assert crossed == []
    source = {"id": "s", "pages": [{"page_number": 3}]}
    rows = possible_event_rows(doc, [], source, first_index=1, at="t", today=date(2026, 10, 30),
                               ticker="PTGX", company="Protagonist Therapeutics, Inc.")
    assert [r for r in rows if "initiation" in r["quote"] and "2027" in r["quote"]] == []


def test_an_unpriced_sam_call_is_not_counted_as_free():
    """A Model step with no recorded cost stays null, so the run's spend is not a sum that left a call out.
    One of the picture page's SAM calls has no cost."""
    regions = [{"id": "r1", "concept": "title", "box": [40.0, 100.0, 300.0, 140.0]}]
    transcriptions = [{"region": regions[0], "text": "Hello"}]
    reads = [{"usage": {"prompt_tokens": 100, "completion_tokens": 10}}]
    calls = [{"images": 1, "cost_usd": 0.0025}, {"images": 1, "cost_usd": None},
             {"images": 1, "cost_usd": 0.0025}, {"images": 1, "cost_usd": 0.0025}]
    steps = picture.trail_steps(regions, transcriptions, calls, reads, 1, "t", n_rows=3, row_index=0)
    assert steps[0]["tag"] == "Model"
    assert steps[0]["cost_usd"] is None


def _post(client, row):
    return client.post(f"/api/runs/sample/voice/decide?target={row}", content=_wav(30),
                       headers={"content-type": "audio/wav"})


def _counting(calls):
    def fake(data):
        calls.append(1)
        ms = 30_000
        return {"transcript": "keep it", "audio_ms": ms, "cost_usd": prices.audio_cost_usd(voice.NAME, ms)}
    return fake


def test_a_run_exactly_at_the_limit_is_refused(tmp_path):
    calls = []
    at = json.loads((REPO / "runs" / "sample" / "run.json").read_text())["run"]["spend_usd"]
    client, d, row = _voice(tmp_path, _counting(calls), limit=at)
    assert _post(client, row).status_code == 409 and not calls


def test_a_run_just_below_the_limit_is_refused_before_the_paid_call(tmp_path):
    calls = []
    at = json.loads((REPO / "runs" / "sample" / "run.json").read_text())["run"]["spend_usd"]
    client, d, row = _voice(tmp_path, _counting(calls), limit=round(at + 0.0001, 6))
    r = _post(client, row)
    assert r.status_code == 409 and "spending limit" in r.json()["detail"]
    assert not calls and _spend(d) == at


def test_a_run_with_room_is_transcribed_and_priced(tmp_path):
    calls = []
    at = json.loads((REPO / "runs" / "sample" / "run.json").read_text())["run"]["spend_usd"]
    client, d, row = _voice(tmp_path, _counting(calls), limit=round(at + 0.01, 6))
    r = _post(client, row)
    assert r.status_code == 200 and calls and r.json()["cost_usd"] == 0.0015
    assert _spend(d) == round(at + 0.0015, 6)


# ---------------------------------------------------------------- Job 31d: reserve = max billable; one call at a time per run

def _at():
    return json.loads((REPO / "runs" / "sample" / "run.json").read_text())["run"]["spend_usd"]


def _reply(ms, calls):
    def fake(data):
        calls.append(1)
        return {"transcript": "keep it", "audio_ms": ms, "cost_usd": prices.audio_cost_usd(voice.NAME, ms)}
    return fake


def test_a_30s_wav_with_exactly_twice_the_31s_cost_left_is_taken(tmp_path):
    # the reservation covers two transcriptions: a retry after a read timeout may follow a billed first request
    calls, cost = [], prices.audio_cost_usd(voice.NAME, 31_000)
    limit = round(_at() + 2 * cost, 6)
    client, d, row = _voice(tmp_path, _reply(31_000, calls), limit=limit)
    assert _post(client, row).status_code == 200
    assert _spend(d) <= limit


def test_a_30s_wav_with_less_than_twice_the_31s_cost_is_refused(tmp_path):
    calls, cost = [], prices.audio_cost_usd(voice.NAME, 31_000)
    client, d, row = _voice(tmp_path, _reply(31_000, calls), limit=round(_at() + 2 * cost - 0.01, 6))
    assert _post(client, row).status_code == 409 and not calls
    assert _spend(d) == _at()


def test_a_reply_above_the_reserve_is_an_impossible_length_and_costs_nothing(tmp_path):
    calls = []
    client, d, row = _voice(tmp_path, _reply(31_001, calls), limit=round(_at() + 1, 6))
    r = _post(client, row)
    assert r.status_code == 422 and "impossible audio length" in r.json()["detail"]
    assert _spend(d) == _at()


def test_two_concurrent_calls_with_headroom_for_one_take_one(tmp_path):
    import asyncio
    import httpx
    calls, cost = [], prices.audio_cost_usd(voice.NAME, 31_000)
    limit = round(_at() + 2 * cost, 6)      # one call reserves two transcriptions' worst case
    client, d, row = _voice(tmp_path, _reply(30_000, calls), limit=limit)

    async def go():
        t = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=t, base_url="http://t") as c:
            return await asyncio.gather(*[c.post(f"/api/runs/sample/voice/decide?target={row}", content=_wav(30),
                                                 headers={"content-type": "audio/wav"}) for _ in range(2)])

    codes = sorted(r.status_code for r in asyncio.run(go()))
    assert codes == [200, 409]
    assert len(calls) == 1 and _spend(d) <= limit


def _unpriced_voice(tmp_path, limit):
    calls = []

    def fake(data):
        calls.append(1)
        return {"transcript": "keep it", "audio_ms": 1000, "cost_usd": prices.audio_cost_usd(voice.NAME, 1000)}

    client, d, row = _voice(tmp_path, fake, limit=limit)
    rp = d / "sample" / "run.json"
    doc = json.loads(rp.read_text())
    doc["run"]["choices"]["spending_limit_usd"] = limit   # None really means no limit, whatever the sample had
    rp.write_text(json.dumps(doc))
    jp = d / "sample" / "job.json"
    job = json.loads(jp.read_text()) if jp.exists() else {}
    job["unpriced_calls"] = 1
    jp.write_text(json.dumps(job))
    return client, d, row, calls


def test_voice_is_refused_while_a_call_is_unpriced_under_a_limit(tmp_path):
    client, d, row, calls = _unpriced_voice(tmp_path, limit=1000)
    before = _spend(d)
    r = client.post(f"/api/runs/sample/voice/decide?target={row}", content=_wav(),
                    headers={"content-type": "audio/wav"})
    assert r.status_code == 409 and "could not be priced" in r.json()["detail"]
    assert calls == [] and _spend(d) == before


def test_voice_proceeds_with_an_unpriced_call_when_there_is_no_limit(tmp_path):
    client, d, row, calls = _unpriced_voice(tmp_path, limit=None)
    r = client.post(f"/api/runs/sample/voice/decide?target={row}", content=_wav(),
                    headers={"content-type": "audio/wav"})
    assert r.status_code == 200 and calls == [1], r.text
