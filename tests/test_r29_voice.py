import io
import json
import shutil
import struct
import wave

import pytest
from conftest import REPO
from fastapi.testclient import TestClient

from capture import voice
from runs import prices
from server.app import create_app


def wav(seconds=1.0, rate=16000, channels=1, width=2) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(b"\x00" * int(seconds * rate) * channels * width)
    return buf.getvalue()


@pytest.mark.parametrize("data", [wav(), wav(rate=24000)])
def test_a_mono_16bit_16k_or_24k_wav_is_accepted(data):
    assert voice.check_wav(data) == 1.0


@pytest.mark.parametrize("data,words", [
    (wav(channels=2), "mono"), (wav(width=1), "16-bit"), (wav(rate=8000), "16 kHz or 24 kHz"),
    (wav(seconds=31), "30 seconds"), (b"hello", "not a WAV"), (b"", "No audio"),
    (b"RIFF" + b"\x00" * 4 + b"WAVE" + b"junk", "not a WAV"),
    (wav(seconds=1) + b"\x00" * (2 * 1024 * 1024), "2 MB")])
def test_other_audio_is_refused_with_a_plain_sentence(data, words):
    with pytest.raises(voice.VoiceError, match=words):
        voice.check_wav(data)


def test_the_price_is_per_hour_of_audio_by_the_reported_duration():
    assert prices.audio_cost_usd("Muse Voice Transcribe", 3_600_000) == 0.18
    assert prices.has_price("Muse Voice Transcribe")


def test_transcribe_sends_the_documented_request(monkeypatch):
    seen = {}

    class R:
        status_code = 200

        def json(self):
            return {"transcript": " keep it ", "turns": [], "sessionId": "s", "audioDurationMs": 3000}

    def post(url, headers, files, timeout):
        seen.update(url=url, headers=headers, files=files)
        return R()

    monkeypatch.setenv("MODEL_API_KEY", "k")
    monkeypatch.setattr(voice.httpx, "post", post)
    out = voice.transcribe(wav())
    assert out == {"transcript": "keep it", "audio_ms": 3000, "cost_usd": 0.00015}
    assert seen["url"] == "https://api.meta.ai/v1/asr/transcribe"
    assert seen["headers"]["Accept"] == "application/json" and seen["headers"]["Authorization"] == "Bearer k"
    name, text, ctype = seen["files"]["request"]
    assert ctype == "application/json"
    assert json.loads(text) == {"model": "muse-voice-transcribe-1.0", "audioEncoding": "WAV", "mode": "PUSH_TO_TALK"}
    assert seen["files"]["audio"][2] == "audio/wav"


def test_a_refused_wav_never_reaches_the_network(monkeypatch):
    monkeypatch.setattr(voice.httpx, "post", lambda *a, **k: pytest.fail("called"))
    with pytest.raises(voice.VoiceError):
        voice.transcribe(wav(channels=2))


@pytest.mark.parametrize("said,proposal", [
    ("Keep.", {"action": "keep"}), ("keep it", {"action": "keep"}), ("Keep as stated.", {"action": "keep"}),
    ("Remove, it is a duplicate.", {"action": "remove", "note": "it is a duplicate"}),
    ("take it out", {"action": "remove", "note": None}),
    ("Fix the date to March third twenty twenty seven.",
     {"action": "fix", "field": "date", "to": "2027-03-03", "note": "March third twenty twenty seven"}),
    ("change the date to March twenty third two thousand twenty seven",
     {"action": "fix", "field": "date", "to": "2027-03-23", "note": "March twenty third two thousand twenty seven"}),
    ("change the date to third quarter twenty twenty seven",
     {"action": "fix", "field": "date", "to": "third quarter 2027", "note": "third quarter twenty twenty seven"}),
    ("change the date to Q two twenty twenty seven",
     {"action": "fix", "field": "date", "to": "q2 2027", "note": "Q two twenty twenty seven"}),
    ("Change the program to ABC-123", {"action": "fix", "field": "program", "to": "ABC-123"}),
    ("fix the program to Zeta Nine", {"action": "fix", "field": "program", "to": "Zeta Nine"}),
    ("fix the indication to lung cancer", {"action": "fix", "field": "indication", "to": "lung cancer"}),
    ("change the indication to Asthma.", {"action": "fix", "field": "indication", "to": "Asthma"}),
    ("what is this", None), ("", None), ("fix the date to soon", None), ("keep going", None)])
def test_rules(said, proposal):
    assert voice.propose(said) == proposal


@pytest.fixture
def made(tmp_path):
    d = tmp_path / "runs"
    shutil.copytree(REPO / "runs" / "sample", d / "sample")
    said = {"text": "keep it", "calls": 0}

    def fake(data):
        voice.check_wav(data)
        said["calls"] += 1
        return {"transcript": said["text"], "audio_ms": 36_000 // 10, "cost_usd": prices.audio_cost_usd(voice.NAME, 3600)}

    client = TestClient(create_app(runs_dir=d, ui_dir=d / "no-ui", transcriber=fake))
    row = json.loads((d / "sample" / "run.json").read_text())["rows"][0]["id"]
    return client, d, said, row


def test_the_route_proposes_and_never_decides_and_prices_the_spend(made):
    client, d, said, row = made
    before = (d / "sample" / "decisions.jsonl").read_bytes()
    start = json.loads((d / "sample" / "run.json").read_text())["run"]["spend_usd"] or 0
    said["text"] = "fix the date to March third twenty twenty seven"
    r = client.post(f"/api/runs/sample/voice/decide?target={row}", content=wav(3), headers={"content-type": "audio/wav"})
    assert r.status_code == 200, r.text
    assert r.json() == {"transcript": said["text"], "audio_ms": 3600, "cost_usd": 0.00018,
                        "proposal": {"action": "fix", "field": "date", "to": "2027-03-03",
                                     "note": "March third twenty twenty seven"}}
    assert (d / "sample" / "decisions.jsonl").read_bytes() == before
    r = client.post(f"/api/runs/sample/voice/decide?target={row}", content=wav(3), headers={"content-type": "audio/wav"})
    assert json.loads((d / "sample" / "run.json").read_text())["run"]["spend_usd"] == round(start + 0.00036, 6)
    job = json.loads((d / "sample" / "job.json").read_text())
    assert [t["model"] for t in job["voice_turns"]] == ["Muse Voice Transcribe"] * 2
    assert job["voice_turns"][0]["audio_seconds"] == 3.6


def test_the_route_takes_a_multipart_audio_part_and_returns_null_for_other_words(made):
    client, d, said, row = made
    said["text"] = "hello there"
    r = client.post(f"/api/runs/sample/voice/decide?target={row}",
                    files={"audio": ("a.wav", wav(), "audio/wav")})
    assert r.status_code == 200 and r.json()["proposal"] is None and said["calls"] == 1


def test_the_route_refuses_bad_audio_and_unknown_targets(made):
    client, d, said, row = made
    r = client.post(f"/api/runs/sample/voice/decide?target={row}", content=wav(channels=2))
    assert r.status_code == 422 and "mono" in r.json()["detail"]
    r = client.post("/api/runs/sample/voice/decide?target=nope", content=wav())
    assert r.status_code == 404
    r = client.post(f"/api/runs/sample/voice/decide?target={row}", files={"other": ("a.wav", wav(), "audio/wav")})
    assert r.status_code == 422
    assert said["calls"] == 0
