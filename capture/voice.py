"""Muse Voice Transcribe: a spoken decision, turned into words.

POST https://api.meta.ai/v1/asr/transcribe, multipart/form-data: a `request` part (JSON text, application/json)
and an `audio` part (the WAV, audio/wav). Key MODEL_API_KEY from the env loader, never printed. The audio must be a RIFF
WAV, mono, 16-bit PCM, 16 kHz or 24 kHz; this product takes at most 2 MB and 30 seconds. The price is per hour of audio,
by the audioDurationMs the service reports (runs/prices.py).
"""

from __future__ import annotations

import io
import json
import os
import wave

import httpx

from capture import read_table
from runs import prices

NAME = "Muse Voice Transcribe"
MODEL = "muse-voice-transcribe-1.0"
URL = "https://api.meta.ai/v1/asr/transcribe"
MAX_BYTES = 2 * 1024 * 1024
MAX_SECONDS = 30
RATES = (16000, 24000)
NO_KEY = "No model key was found, so nothing was transcribed. Add MODEL_API_KEY to ~/.config/proof-reader/env."


class VoiceError(Exception):
    """A plain sentence a person can read."""


def check_wav(data: bytes) -> float:
    """The length in seconds of a WAV that is mono, 16-bit PCM, 16 or 24 kHz and within the caps; else VoiceError."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise VoiceError("No audio was sent.")
    if len(data) > MAX_BYTES:
        raise VoiceError("The audio is larger than 2 MB; record a shorter decision.")
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise VoiceError("The audio is not a WAV file.")
    try:
        with wave.open(io.BytesIO(bytes(data))) as w:
            channels, width, rate, frames = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
    except (wave.Error, EOFError):
        raise VoiceError("The audio is not a WAV file of plain PCM samples.") from None
    if channels != 1:
        raise VoiceError("The audio must be mono (one channel).")
    if width != 2:
        raise VoiceError("The audio must be 16-bit.")
    if rate not in RATES:
        raise VoiceError("The audio must be 16 kHz or 24 kHz.")
    seconds = frames / rate
    if seconds > MAX_SECONDS:
        raise VoiceError("The audio is longer than 30 seconds; record a shorter decision.")
    return seconds


def _key() -> str:
    read_table.load_env_file()
    return os.environ.get("MODEL_API_KEY", "").strip()


def transcribe(wav_bytes: bytes) -> dict:
    """{transcript, audio_ms, cost_usd} for one WAV. VoiceError (a plain sentence) when the audio is refused or the call fails."""
    check_wav(wav_bytes)
    key = _key()
    if not key:
        raise VoiceError(NO_KEY)
    request = {"model": MODEL, "audioEncoding": "WAV", "mode": "PUSH_TO_TALK"}
    try:
        r = httpx.post(URL, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
                       files={"request": (None, json.dumps(request), "application/json"),
                              "audio": ("audio.wav", bytes(wav_bytes), "audio/wav")}, timeout=60)
    except httpx.HTTPError:
        raise VoiceError("The transcription service could not be reached.") from None
    if r.status_code != 200:
        raise VoiceError(f"The transcription service refused the audio (HTTP {r.status_code}).")
    try:
        doc = r.json()
        transcript, ms = str(doc["transcript"]).strip(), int(doc["audioDurationMs"])
    except (ValueError, KeyError, TypeError):
        raise VoiceError("The transcription service answered in a shape that was not understood.") from None
    return {"transcript": transcript, "audio_ms": ms, "cost_usd": prices.audio_cost_usd(NAME, ms)}


# ---------------------------------------------------------------- rules: words -> a proposal (no model)

import re  # noqa: E402

from checks import event_checks  # noqa: E402

_UNITS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
                                     "fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * i for i, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if w != "_"}
_ORD = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9,
        "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14, "fifteenth": 15,
        "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19, "twentieth": 20, "thirtieth": 30}
_MONTH_NAMES = set(event_checks._MONTHS)


def _below_100(tokens: list[str]) -> int | None:
    """'twenty seven' / 'seven' / 'twenty' -> number below 100; anything else None."""
    if len(tokens) == 1 and tokens[0] in _UNITS:
        return _UNITS[tokens[0]]
    if len(tokens) == 1 and tokens[0] in _TENS:
        return _TENS[tokens[0]]
    if len(tokens) == 2 and tokens[0] in _TENS and tokens[1] in _UNITS and 1 <= _UNITS[tokens[1]] <= 9:
        return _TENS[tokens[0]] + _UNITS[tokens[1]]
    return None


def _day(tokens: list[str]) -> int | None:
    """'third', 'twenty third', 'thirty first', '3', '3rd' -> day number."""
    if len(tokens) == 1:
        t = tokens[0]
        if t in _ORD:
            return _ORD[t]
        m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?", t)
        return int(m[1]) if m else None
    if len(tokens) == 2 and tokens[0] in _TENS and tokens[1] in _ORD and _ORD[tokens[1]] < 10:
        return _TENS[tokens[0]] + _ORD[tokens[1]]
    return None


def _year(tokens: list[str]) -> int | None:
    """'twenty twenty seven', 'two thousand twenty seven', 'two thousand', '2027' -> year."""
    if len(tokens) == 1 and re.fullmatch(r"\d{4}", tokens[0]):
        return int(tokens[0])
    if tokens[:2] == ["two", "thousand"]:
        if len(tokens) == 2:
            return 2000
        if tokens[2] == "and":
            tokens = tokens[:2] + tokens[3:]
        rest = _below_100(tokens[2:])
        return 2000 + rest if rest is not None and 0 < rest < 100 else None
    for cut in (1, 2):                      # 'twenty|twenty seven', 'nineteen|ninety nine', 'twenty|ten'
        head, tail = tokens[:cut], tokens[cut:]
        a = _below_100(head)
        if a is None or not 10 <= a <= 99 or not tail:
            continue
        if tail == ["hundred"]:
            return a * 100
        if tail[0] in ("oh", "o", "zero"):
            b = _below_100(tail[1:])
            return a * 100 + b if b is not None and 0 < b < 10 else None
        b = _below_100(tail)
        if b is not None and b >= 10:
            return a * 100 + b
        if b is not None and cut == 1 and len(tail) == 1 and tail[0] in _TENS:
            return a * 100 + b
    return None


def spoken_date_text(words: str) -> str:
    """Spoken date words -> written date text: 'March third twenty twenty seven' -> 'March 3, 2027'. A month
    alone, with a year ('March twenty twenty seven') -> 'March 2027'. Words not understood come back as they were."""
    toks = [t for t in re.sub(r"[,.]", " ", words.lower()).replace("-", " ").split() if t not in ("the", "of")]
    if toks and toks[0] in _MONTH_NAMES:
        month = toks[0].capitalize()
        rest = toks[1:]
        for k in (2, 1, 0):                  # the day is the first one or two words, or absent
            day = _day(rest[:k]) if k else None
            year = _year(rest[k:])
            if year is not None and (day is not None or k == 0):
                return f"{month} {day}, {year}" if day is not None else f"{month} {year}"
        return words.strip()
    for k in (4, 3, 2, 1):                   # a period then a spoken year: 'third quarter twenty twenty seven'
        if len(toks) > k and _year(toks[-k:]) is not None:
            head = " ".join(toks[:-k])
            y = _year(toks[-k:])
            head = re.sub(r"^q (one|two|three|four)$", lambda m: f"q{_UNITS[m[1]]}", head)
            return f"{head} {y}"
    return words.strip()


def _iso_or_period(words: str) -> str | None:
    """The date the words name: an ISO day, or the written period text, checked with checks/event_checks."""
    text = event_checks.clean_date_text(spoken_date_text(words))
    exact = event_checks.parse_exact_date(text)
    if exact is not None:
        return exact.isoformat()
    return text if event_checks.parse_period(text) is not None else None


_KEEP = re.compile(r"^(?:keep(?: it)?(?: as stated)?|keep it as stated)$", re.I)
_REMOVE = re.compile(r"^(?:remove|take it out)\b[\s,:-]*(.*)$", re.I)
_FIX = re.compile(r"^(?:fix|change) the (date|program|indication) to (.+)$", re.I)


def propose(transcript: str) -> dict | None:
    """A decision proposal from the words, by rules and no model; None when the words are not one of the shapes.
    It is only ever a proposal: nothing here writes a decision."""
    s = re.sub(r"\s+", " ", re.sub(r"[.!?,;]+(?=\s|$)", "", (transcript or "").strip())).strip()
    if not s:
        return None
    if _KEEP.match(s):
        return {"action": "keep"}
    m = _REMOVE.match(s)
    if m:
        return {"action": "remove", "note": m[1].strip() or None}
    m = _FIX.match(s)
    if m:
        field, words = m[1].lower(), m[2].strip()
        if field == "date":
            to = _iso_or_period(words)
            return None if to is None else {"action": "fix", "field": "date", "to": to, "note": words}
        return {"action": "fix", "field": field, "to": words}
    return None
