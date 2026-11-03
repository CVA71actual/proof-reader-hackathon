"""The research result labels (CLAIM-001 / UNKNOWN-001 / MULTI-FAMILY-001), derived from the RECORDED review only.

One function, `derive_review_labels(run)`, sets every label a person sees (each research row's research.label, each
company's run.research.review[t].label and the run's run.research.label) from what the review record establishes:
its status, the findings still current on the rows, the retrieval limits in its limitations, and which reviewer ran.
runs/research.py calls it when it saves a review; runs/check_load.bind_on_load calls it on every load, so the API, the
report view, the PDF and the DOCX always show the label the record supports, and a run saved by an older build gets
the honest label without its file being rewritten.

Never "verified". "independently reviewed" only for a COMPLETE review by a reviewer of another model family than the
researcher; a same-family reviewer reads "reviewed by <name> (same model family as the researcher)"; findings from a
review that did not complete read "partly reviewed ... (incomplete), findings so far"; every source the review could
not open and every search that did not complete is named after a dash."""

from __future__ import annotations

from runs.research_contract import LABEL

RESEARCHER_PROVIDER = "meta"
RESEARCHER_FAMILY = "Muse Spark (Meta)"
PRIMARY_REVIEWER = {"name": "DeepSeek V4 Pro", "family": "DeepSeek"}


def review_label(kind: str, who: dict | None) -> str:
    """The label of one kind (clean, findings, limited, partial, none) for the reviewer `who` ({name, family})."""
    if kind == "none" or not who or not who.get("name"):
        return LABEL
    same = who.get("family") == RESEARCHER_FAMILY
    by = who["name"] + (" (same model family as the researcher)" if same else "")
    reviewed = ("reviewed by " if same else "independently reviewed by ") + by
    return {"clean": f"Agent research — {reviewed} (no findings)",
            "findings": f"Agent research — {reviewed} with findings",
            "limited": f"Agent research — {reviewed}",
            "partial": f"Agent research — partly reviewed by {by} (incomplete), findings so far"}[kind]


def review_limits(record: dict) -> dict:
    """What the review could not do, counted from its record: sources it could not open (a failed fetch, a captured
    source whose text could not be given) and searches that did not complete."""
    lims = record.get("limitations") or []
    return {"sources_not_opened": sum(1 for x in lims if x.get("kind") == "fetch")
                                  + len(record.get("sources_not_given") or []),
            "searches_not_completed": sum(1 for x in lims if x.get("kind") == "search")}


def limits_text(limits: dict) -> str:
    n, m = limits.get("sources_not_opened", 0), limits.get("searches_not_completed", 0)
    parts = ([f"{n} source{'s' if n != 1 else ''} could not be opened"] if n else []) + \
            ([f"{m} search{'es' if m != 1 else ''} did not complete"] if m else [])
    return (" — " + "; ".join(parts)) if parts else ""


def with_limits(kind: str, limits: dict, who: dict | None) -> str:
    """The label plus the recorded retrieval limits. A clean label has none by definition; a "not independently
    reviewed" label after a review that ran also names them, so no attempted review hides what it could not open."""
    label = review_label(kind, who)
    return label if kind == "clean" else label + limits_text(limits)


def label_kind(status: str | None, clean: bool, has_findings: bool, only_limits: bool) -> str:
    if status == "complete":
        if clean:
            return "clean"
        if has_findings:
            return "findings"
        return "limited" if only_limits else "none"
    return "partial" if has_findings else "none"


def reviewer_of(entry: dict) -> dict | None:
    """Who reviewed, from the entry; an entry saved before this was recorded falls back to its record."""
    who = entry.get("reviewer_ran")
    if isinstance(who, dict) and who.get("name"):
        return who
    record = entry.get("review") or {}
    name = entry.get("reviewer") or record.get("reviewer")
    model = str(record.get("model") or record.get("requested_model") or "")
    family = record.get("family")
    if not name and "muse-spark-open" in model:
        name = "Muse Spark Open"
    if not family and (name == "Muse Spark Open" or "muse-spark" in model):
        family = RESEARCHER_FAMILY
    return {"name": name, "family": family} if name else None


def only_retrieval_limits(record: dict, limits: dict) -> bool:
    return (record.get("status") == "complete" and not record.get("findings") and not record.get("rejected")
            and not record.get("cited_pages_unread") and not record.get("rows_not_assessed")
            and any(limits.values()))


def derive_review_labels(run: dict) -> dict:
    """Set every research label in `run` (in place) from the recorded review. Returns run."""
    meta = run.get("run") or {}
    research = meta.get("research")
    if not isinstance(research, dict):
        return run
    reviews = research.get("review") if isinstance(research.get("review"), dict) else {}
    entries = {t: e for t, e in reviews.items() if isinstance(e, dict) and "stage" in e}
    for row in run.get("rows", []):
        r = row.get("research")
        if not isinstance(r, dict):
            continue
        entry = entries.get(str(r.get("ticker") or row.get("ticker") or "").upper())
        if entry is None:
            r["label"] = LABEL
            continue
        record = entry.get("review") or {}
        status, who, limits = entry.get("status"), reviewer_of(entry), review_limits(record)
        current = [f for f in r.get("review_findings") or [] if f.get("current")]
        review = r.get("review") or {}
        reviewed_this = status == "complete" and review.get("reviewed_revision") == r.get("revision")
        if status == "complete" and current:
            kind = "findings"
        elif current:
            kind = "partial"
        elif status == "complete" and record.get("clean") and reviewed_this:
            kind = "clean"
        elif only_retrieval_limits(record, limits) and reviewed_this:
            kind = "limited"
        else:
            kind = "none"
        r["label"] = with_limits(kind, limits, who)
    for ticker, entry in entries.items():
        record = entry.get("review") or {}
        limits = review_limits(record)
        has = bool(entry.get("unbound_findings")) or any(
            f.get("current") for row in run.get("rows", []) if isinstance(row.get("research"), dict)
            and str(row["research"].get("ticker") or "").upper() == ticker
            for f in row["research"].get("review_findings") or [])
        kind = label_kind(entry.get("status"), bool(record.get("clean")), has, only_retrieval_limits(record, limits))
        entry["label_kind"], entry["limits"] = kind, limits
        entry["label"] = with_limits(kind, limits, reviewer_of(entry))
    research["label"] = run_label(research, entries)
    return run


def run_label(research: dict, entries: dict) -> str:
    """Reviewed (no findings) only when every company's review is complete and clean; 'independently reviewed ...'
    only when every company's review is complete; partly reviewed when findings exist but some review did not
    complete; otherwise not independently reviewed. Every company's retrieval limits are carried."""
    comp = research.get("companies") or {}
    companies = list(comp.keys()) or list(entries)
    done = [entries[t] for t in companies if t in entries]
    # DRYRUN-1: a company whose RESEARCH did not finish (failed, stopped, starved at the cap) makes the run partial:
    # never "independently reviewed", whatever its review of the partial draft said
    unfinished = [t for t in companies if isinstance(comp.get(t), dict) and comp[t].get("status") != "finished"]
    note = (f" — research did not finish for {len(unfinished)} of {len(companies)} "
            f"compan{'y' if len(companies) == 1 else 'ies'}") if unfinished else ""
    if not done:
        return LABEL + note
    limits = {k: sum((e.get("limits") or {}).get(k, 0) for e in done)
              for k in ("sources_not_opened", "searches_not_completed")}
    kinds = [e.get("label_kind", "none") for e in done]
    whos = [w for w in (reviewer_of(e) for e in done) if w]
    names = list(dict.fromkeys(w["name"] for w in whos))
    who = ({"name": " and ".join(names),
            "family": RESEARCHER_FAMILY if any(w.get("family") == RESEARCHER_FAMILY for w in whos)
            else whos[0].get("family")} if whos else None)
    if unfinished:
        kind = "partial" if any(k in ("findings", "partial") for k in kinds) else "none"
        return with_limits(kind, limits, who) + note
    every = len(done) == len(companies)
    if every and all(k == "clean" for k in kinds):
        return with_limits("clean", limits, who)
    if every and all(e.get("status") == "complete" for e in done):
        if any(k == "findings" for k in kinds):
            return with_limits("findings", limits, who)
        if all(k in ("clean", "limited") for k in kinds):
            return with_limits("limited", limits, who)
    if any(k in ("findings", "partial") for k in kinds):
        return with_limits("partial", limits, who)
    return LABEL
