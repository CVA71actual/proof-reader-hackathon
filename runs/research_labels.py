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
not open and every search that did not complete is named after a dash.

ROW-LABEL-001: a row's label is its own. A row the complete review assessed no_issue on exactly its current material
reads reviewed (no findings) even when a sibling row of the same company has a finding; that finding stays on the
sibling, the company and the run. A no_issue never clears another row, an incomplete review, or a changed row."""

from __future__ import annotations

from runs.research_contract import LABEL

RESEARCHER_PROVIDER = "meta"
RESEARCHER_FAMILY = "Muse Spark (Meta)"
PRIMARY_REVIEWER = {"name": "Claude Opus 5.5", "family": "Claude"}


def review_label(kind: str, who: dict | None) -> str:
    """The label of one kind (clean, findings, limited, partial, none) for the reviewer `who` ({name, family})."""
    if kind == "none" or not who or not who.get("name"):
        return LABEL
    same = who.get("family") == RESEARCHER_FAMILY
    by = who["name"] + (" (same model family as the researcher)" if same
                        else " (different model family from the researcher)" if who.get("family") else "")
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
            "searches_not_completed": sum(1 for x in lims if x.get("kind") == "search"),
            # REVIEW-SETTLE-001: a source the reviewer fetched and did not conclude before code closed the review
            "reviewer_sources_not_concluded": sum(1 for x in lims if x.get("kind") == "source_not_concluded")}


LIMIT_KEYS = ("sources_not_opened", "searches_not_completed", "reviewer_sources_not_concluded")


def limits_text(limits: dict) -> str:
    n, m = limits.get("sources_not_opened", 0), limits.get("searches_not_completed", 0)
    u = limits.get("reviewer_sources_not_concluded", 0)
    parts = ([f"{n} source{'s' if n != 1 else ''} could not be opened"] if n else []) + \
            ([f"{m} search{'es' if m != 1 else ''} did not complete"] if m else []) + \
            ([f"{u} source{'s' if u != 1 else ''} the reviewer opened {'were' if u != 1 else 'was'} not concluded"]
             if u else [])
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


def review_phrase(who: dict | None) -> str:
    """The trail's wording for a review, from the reviewer that ran (REVIEW-TRAIL-FAMILY): a same-family reviewer's
    review is never called independent."""
    if not who or not who.get("name"):
        return "Review"
    if who.get("family") == RESEARCHER_FAMILY:
        return f"Review by {who['name']} (same model family as the researcher)"
    return f"Independent review by {who['name']}"


def material_digest(run: dict, row: dict) -> str | None:
    """REVIEW-LABEL-CURRENCY: the row's canonical material identity, the same one evidence and Keep use
    (runs.review.research_basis: revision, research fields, every span's source, page, quote, roles, word ids, the
    consumed artifacts' hashes including derived words, the publication context and the timing snapshot)."""
    import hashlib
    import json
    try:
        from runs.review import research_basis
        basis = research_basis(run, row)
    except Exception:
        return None
    return hashlib.sha256(json.dumps(basis, sort_keys=True, default=str).encode()).hexdigest()


def row_covered(run: dict, row: dict, entry: dict | None) -> bool:
    """A current row is covered by a COMPLETE review of exactly its current material (revision and material)."""
    r = row.get("research") or {}
    review = r.get("review") or {}
    if not entry or entry.get("status") != "complete" or review.get("reviewed_revision") != r.get("revision"):
        return False
    seen = review.get("reviewed_material")
    return seen is None or seen == material_digest(run, row)      # None: saved before materials were recorded


def finding_current(run: dict, row: dict, f: dict) -> bool:
    if not f.get("current"):
        return False
    seen = f.get("material")
    return seen is None or seen == material_digest(run, row)


def row_no_issue(row: dict, entry: dict) -> bool:
    """ROW-LABEL-001: the COMPLETE review's own assessment of THIS row, on its current revision, is no_issue. Both the
    record's assessment and the verdict saved on the row by that same attempt must say so; any disagreement, a missing
    assessment, or a verdict saved by another attempt is not a no_issue (the less-reviewed label wins)."""
    r = row.get("research") or {}
    review = r.get("review") or {}
    rev = str(r.get("revision"))
    record = entry.get("review") or {}
    verdicts = [a.get("verdict") for a in record.get("assessments") or []
                if isinstance(a, dict) and str(a.get("row_id")) == str(row.get("id")) and str(a.get("revision_id")) == rev]
    if verdicts != ["no_issue"] or review.get("verdict") != "no_issue" or review.get("status") != "complete":
        return False
    attempt = review.get("attempt")
    return attempt is None or entry.get("attempt") is None or attempt == entry.get("attempt")


def row_clean_kind(record: dict, limits: dict) -> str:
    """The label kind of a covered row the complete review assessed no_issue. Another row's finding does not touch
    it; what the review as a whole could not do still does: a rejected finding attempt or a cited page the reviewer
    could not read may concern any row, so the row is not called reviewed; retrieval limits make it 'limited'."""
    if record.get("rejected") or record.get("cited_pages_unread"):
        return "none"
    if record.get("clean"):
        return "clean"
    if record.get("limitations") or record.get("sources_not_given"):
        return "limited" if any(limits.values()) else "none"     # a limitation this label cannot name: not reviewed
    return "clean"


def coverage_note(uncovered: int, total: int) -> str:
    return (f" — {uncovered} of {total} row{'s' if total != 1 else ''} changed or new since the review"
            if uncovered else "")


def derive_review_labels(run: dict) -> dict:
    """Set every research label in `run` (in place) from the recorded review AND the current rows. Returns run."""
    meta = run.get("run") or {}
    research = meta.get("research")
    if not isinstance(research, dict):
        return run
    reviews = research.get("review") if isinstance(research.get("review"), dict) else {}
    entries = {t: e for t, e in reviews.items() if isinstance(e, dict) and "stage" in e}
    rows_of: dict[str, list] = {}
    for row in run.get("rows", []):
        r = row.get("research")
        if not isinstance(r, dict):
            continue
        ticker = str(r.get("ticker") or row.get("ticker") or "").upper()
        rows_of.setdefault(ticker, []).append(row)
        entry = entries.get(ticker)
        if entry is None:
            r["label"] = LABEL
            continue
        record = entry.get("review") or {}
        status, who, limits = entry.get("status"), reviewer_of(entry), review_limits(record)
        current = [f for f in r.get("review_findings") or [] if finding_current(run, row, f)]
        covered = row_covered(run, row, entry)
        stale = (r.get("review") or {}).get("reviewed_material") not in (None, material_digest(run, row))
        if status == "complete" and current:
            kind = "findings"
        elif current:
            kind = "partial"
        elif status == "complete" and covered and (record.get("clean") or row_no_issue(row, entry)):
            kind = row_clean_kind(record, limits)
        elif only_retrieval_limits(record, limits) and covered:
            kind = "limited"
        else:
            kind = "none"
        r["label"] = with_limits(kind, limits, who) + (" — changed since its review" if stale and kind == "none"
                                                         else "")
    uncovered_total = 0
    for ticker, entry in entries.items():
        record = entry.get("review") or {}
        limits = review_limits(record)
        rows = rows_of.get(ticker, [])
        has = bool(entry.get("unbound_findings")) or any(
            finding_current(run, row, f) for row in rows for f in row["research"].get("review_findings") or [])
        kind = label_kind(entry.get("status"), bool(record.get("clean")), has, only_retrieval_limits(record, limits))
        uncovered = sum(1 for row in rows if not row_covered(run, row, entry)) if entry.get("status") == "complete" \
            else 0
        uncovered_total += uncovered
        entry["label_kind"], entry["limits"] = kind, limits
        entry["coverage"] = {"rows": len(rows), "uncovered": uncovered}
        note = coverage_note(uncovered, len(rows)) if kind != "none" else ""
        entry["label"] = with_limits(kind, limits, reviewer_of(entry)) + note
    total = sum(len(v) for v in rows_of.values())
    run_note = coverage_note(uncovered_total, total) if uncovered_total else ""
    research["label"] = run_label(research, entries) + run_note + cap_note(research)
    return run


def cap_note(research: dict) -> str:
    """CAP-001: a run whose booked spend passed its cap says so, by how much and why."""
    over = research.get("cap_exceeded")
    if not isinstance(over, dict) or not isinstance(over.get("by_usd"), (int, float)):
        return ""
    return f" — spending cap exceeded by ${over['by_usd']:.2f} ({over.get('reason') or 'reason not recorded'})"


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
              for k in LIMIT_KEYS}
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
