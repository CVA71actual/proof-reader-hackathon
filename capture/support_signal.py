"""LettuceDetect support signal for researcher proposals — FINDINGS ONLY (RESEARCHER-FULL-001 / S3).

`support_signal(claim, evidence_text)` runs the pinned local LettuceDetect weights over one claim and its
cited evidence passage and reports character-offset spans of potentially unsupported wording. It is never a
gate: no check, acceptance, calendar or mistake-count reads it, and a finding (or its absence) changes
nothing. `could_not_run` is an explicit status — missing weights, over-limit input or a failed call is never
a silent pass or a statement that the claim is grounded.

Third-party components, with attribution (LETTUCE-RULES-001): the LettuceDetect library is by KR Labs (MIT,
https://github.com/KRLabsOrg/LettuceDetect); the model is KRLabsOrg/lettucedect-v2-mmbert-base under
Apache-2.0 (base model jhu-clsp/mmBERT-base, MIT); training data LettuceDetect hallucination datasets by
KR Labs, CC BY 4.0. Model weights are NOT committed: they are pinned by exact model revision
(8831ce06…) — the local snapshot directory must be named by that revision — and a content digest of the
weight files is reported as `weights_sha256`. Model code (torch/lettucedetect) lazy-imports inside
_LocalDetector only; tests use a stub detector and make no network calls.

Reuses the findings-demo logic from #159 (detect/lettuce_demo.py @ 3d89f5f, docs/LETTUCE_DEMO.md): same
pinned model + revision, same fixed question, same 4096-token limit (over-limit is refused, never
truncated), same span re-cutting from the submitted claim — never trusting detector offsets.
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

MODEL = "KRLabsOrg/lettucedect-v2-mmbert-base"
MODEL_REVISION = "8831ce063dff760b01efe4519b4188f5ee2456f2"
MAX_TOKENS = 4096                                # same limit as the pinned pilot runner
QUESTION = "What development and qualifications does this passage describe?"     # the pilot's fixed prompt
WEIGHTS_ENV = "LETTUCE_WEIGHTS_PATH"             # env var naming the pinned snapshot directory

_SHA_CACHE: dict[str, str] = {}


def _weights_sha256(weights_dir: Path) -> str:
    """Content digest of the pinned weight files (cached per directory)."""
    if str(weights_dir) in _SHA_CACHE:
        return _SHA_CACHE[str(weights_dir)]
    h = hashlib.sha256()
    for f in sorted(weights_dir.iterdir()):
        if f.is_file() and f.suffix in (".safetensors", ".bin"):
            h.update(f.suffix.encode()); h.update(f.name.encode()); h.update(f.read_bytes())
    digest = h.hexdigest()
    _SHA_CACHE[str(weights_dir)] = digest
    return digest


class _LocalDetector:
    """Pinned local CPU detector; offline, no remote code, no network. (#159 LocalDetector, verbatim shape.)"""
    model = MODEL
    revision = MODEL_REVISION

    def __init__(self, model_path: str, threads: int = 2):
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
        if Path(model_path).name != MODEL_REVISION:
            raise ValueError("Model path is not the pinned revision " + MODEL_REVISION)
        import torch
        from lettucedetect.models.inference import HallucinationDetector
        torch.set_num_threads(threads)
        self._det = HallucinationDetector(method="transformer", model_path=str(model_path), device="cpu",
                                          local_files_only=True, trust_remote_code=False, max_length=MAX_TOKENS)
        self.weights_sha256 = _weights_sha256(Path(model_path))

    def count_tokens(self, passage: str, claim: str) -> int:
        from lettucedetect.detectors.prompt_utils import PromptUtils
        tok = self._det.detector.tokenizer
        prompt = PromptUtils.format_context([passage], QUESTION, "en")
        return len(tok(prompt, add_special_tokens=False)["input_ids"]) + \
            len(tok(claim, add_special_tokens=False)["input_ids"]) + 3

    def predict(self, passage: str, claim: str) -> list:
        # library default span rule; min_confidence=0.0 means no extra filtering; no tuning
        return self._det.predict(context=[passage], question=QUESTION, answer=claim,
                                 output_format="spans", min_confidence=0.0)


def _claim_text(claim: dict) -> str:
    """The claim's wording: 'claim', 'text' or 'quote' — the first present non-empty string."""
    for key in ("claim", "text", "quote"):
        value = (claim or {}).get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _result(result: str, spans=(), model=MODEL, weights_sha256=None, **extra) -> dict:
    return {"result": result, "spans": list(spans), "model": model, "weights_sha256": weights_sha256, **extra}


def support_signal(claim: dict, evidence_text: str, *, detector=None, weights_path=None) -> dict:
    """One claim's support signal against its cited evidence passage.

    Returns {"result": "supported" | "unsupported_spans" | "could_not_run", "spans": [...], "model": ...,
    "weights_sha256": ...}. Spans are character offsets into the submitted claim text. FINDINGS ONLY — the
    caller may display this beside the proposal for a person; it must never drive a check or decision.
    """
    text = _claim_text(claim)
    det = detector
    wsha = getattr(det, "weights_sha256", None) if det is not None else None
    if det is None:
        path = weights_path or os.environ.get(WEIGHTS_ENV, "")
        if not path or not Path(path).is_dir():
            return _result("could_not_run", detail="pinned weights are not installed", weights_sha256=None)
        try:
            det = _LocalDetector(str(path))
            wsha = det.weights_sha256
        except Exception as exc:
            return _result("could_not_run", detail=f"detector could not start: {type(exc).__name__}: {exc}")
    if not text or not isinstance(evidence_text, str) or not evidence_text.strip():
        return _result("could_not_run", model=getattr(det, "model", MODEL), weights_sha256=wsha,
                       detail="claim text or evidence is missing or empty")
    try:
        tokens = det.count_tokens(evidence_text, text)
        if tokens > MAX_TOKENS:
            return _result("could_not_run", model=det.model, weights_sha256=wsha, tokens=tokens,
                           detail=f"{tokens} tokens > limit {MAX_TOKENS}; not truncated, not run")
        began = time.perf_counter()
        raw = det.predict(evidence_text, text)
        latency = time.perf_counter() - began
    except Exception as exc:                                     # explicit, never a silent pass
        return _result("could_not_run", model=det.model, weights_sha256=wsha,
                       detail=f"{type(exc).__name__}: {exc}")
    spans = []
    for s in raw or []:
        ok = (isinstance(s, dict) and isinstance(s.get("start"), int) and isinstance(s.get("end"), int)
              and not isinstance(s["start"], bool) and not isinstance(s["end"], bool)
              and 0 <= s["start"] < s["end"] <= len(text))
        if not ok:
            return _result("could_not_run", model=det.model, weights_sha256=wsha, tokens=tokens,
                           detail=f"detector returned an invalid span: {s!r}")
        span = {"start": s["start"], "end": s["end"], "text": text[s["start"]:s["end"]]}  # re-cut, never trusted
        if "confidence" in s:
            span["confidence"] = s["confidence"]
        spans.append(span)
    return _result("unsupported_spans" if spans else "supported", spans, model=det.model,
                   weights_sha256=wsha, tokens=tokens, latency_seconds=round(latency, 4))
