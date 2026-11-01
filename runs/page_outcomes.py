"""One page-outcome record: capture, attempt, interpretation and disposition are separate facts."""

from __future__ import annotations


FACTS = ("captured", "attempted", "read", "unreadable", "skipped", "unread")


def interpreted_page(source: dict, page: dict, *, attempted: bool, read: bool,
                     unreadable: bool = False, reason: str | None = None) -> dict:
    """Create one page outcome from the interpretation result, never from source presence alone."""
    ticker = source.get("ticker") or (source.get("title") or "").split()[0]
    company = source.get("company") or (source.get("publisher") or "").split(" (filed")[0]
    outcome = {"source_id": source["id"], "ticker": ticker, "company": company,
               "page_number": page.get("page_number"), "captured": True, "attempted": bool(attempted),
               "read": bool(read), "unreadable": bool(unreadable and not read), "skipped": False,
               "unread": not read}
    if not read and reason:
        outcome["reason"] = reason
    return outcome


def _warning(record: dict) -> dict | None:
    keys = ("capture_incomplete", "capture_incomplete_reason", "certified_complete",
            "completeness_not_established", "completeness_reason")
    warning = {key: record[key] for key in keys if key in record}
    return warning or None


def initialize(meta: dict, selected_pages: list[dict], run: dict, job: dict) -> list[dict]:
    """Carry saved outcomes forward; adapt older run/job facts once, conservatively."""
    saved = {item["source_id"]: item for item in meta.get("page_outcomes", [])
             if isinstance(item, dict) and item.get("source_id")}
    sources = {source["id"]: source for source in run.get("sources", []) if source.get("id")}
    skipped = job.get("skipped") or {}
    outcomes = []
    for page in selected_pages:
        record = page.get("record") or {}
        sid = page["source_id"]
        item = dict(saved.get(sid) or {
            "source_id": sid,
            "ticker": record.get("ticker") or page.get("ticker") or "",
            "company": record.get("company") or page.get("company") or "",
            "page_number": record.get("page_number") or page.get("page_number"),
            "captured": False,
            "attempted": False,
            "read": False,
            "unreadable": False,
            "skipped": False,
            "unread": True,
        })
        warning = _warning(record)
        if warning is not None:
            item["capture_warning"] = warning
        source = sources.get(sid)
        if source and sid not in saved:
            # A legacy source proves capture; it cannot establish whether a reader call was attempted or parsed.
            # In particular, older jobs counted unreadable answers as read, so do not infer success or attempt here.
            item.update(captured=True, attempted=False, unreadable=False,
                        read=False, skipped=False, unread=True)
        if sid in skipped:
            why = skipped[sid]
            item.update(skipped=True, read=False, unreadable=False, unread=True,
                        reason=why.get("reason") if isinstance(why, dict) else str(why))
        item.setdefault("capture_warning", None)
        outcomes.append(item)
    meta["page_outcomes"] = outcomes
    meta["pages_total"] = len(outcomes)
    meta["pages_read"] = sum(1 for item in outcomes if item["read"])
    meta["pages_unread"] = sum(1 for item in outcomes if item["unread"])
    return outcomes


def update(meta: dict, source_id: str, **facts) -> dict:
    """Update the one saved record and keep its independent facts internally consistent."""
    item = next((x for x in meta.get("page_outcomes", []) if x.get("source_id") == source_id), None)
    if item is None:
        raise KeyError(f"No page outcome was initialized for {source_id}.")
    for key, value in facts.items():
        if key not in {*FACTS, "reason", "capture_warning"}:
            raise ValueError(f"Unknown page outcome field {key!r}.")
        item[key] = value
    if item.get("read"):
        item["unread"] = False
        item["unreadable"] = False
        item["skipped"] = False
    else:
        item["unread"] = True
    meta["pages_read"] = sum(1 for outcome in meta.get("page_outcomes", []) if outcome.get("read"))
    meta["pages_unread"] = sum(1 for outcome in meta.get("page_outcomes", []) if outcome.get("unread"))
    return item


def summary(outcomes: list[dict]) -> dict:
    """Count only explicit facts; never infer a successful read from a captured source."""
    return {fact: sum(1 for item in outcomes if item.get(fact) is True) for fact in FACTS}


def interpretation(reads: list[dict], attempted: bool) -> dict:
    """A scan page is read only when every required region reply parsed, including an empty events list."""
    if not attempted and not reads:
        return {"captured": True, "attempted": False, "read": False, "unreadable": False,
                "skipped": False, "unread": True, "reason": "No interpretation was attempted."}
    failed = [item for item in reads if not item.get("parse_ok", isinstance(item.get("events"), list))]
    if failed or not reads:
        return {"captured": True, "attempted": bool(attempted), "read": False, "unreadable": True,
                "skipped": False, "unread": True,
                "reason": failed[0].get("parse_problem") if failed else "No readable region was returned."}
    return {"captured": True, "attempted": bool(attempted), "read": True, "unreadable": False,
            "skipped": False, "unread": False}


def by_company(outcomes: list[dict]) -> list[dict]:
    grouped = {}
    for item in outcomes:
        key = (item.get("ticker") or "", item.get("company") or "")
        grouped.setdefault(key, []).append(item)
    return [{"ticker": ticker, "company": company, "pages": len(items), **summary(items)}
            for (ticker, company), items in sorted(grouped.items())]


def read_view(outcomes) -> dict:
    """Versioned-state payload shared by API, screen and report builders."""
    if not isinstance(outcomes, list):
        return {"recorded": False, "pages": None, "items": None, "counts": None, "by_company": []}
    return {"recorded": True, "pages": len(outcomes), "items": outcomes,
            "counts": summary(outcomes), "by_company": by_company(outcomes)}
