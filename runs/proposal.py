"""Carry reader interpretations without treating their presence as a check.

These fields remain proposals. Copying an empty condition list does not establish
that an event is unconditional, nor does copying a relation establish identity.
"""

from copy import deepcopy


CONTEXT_FIELDS = ("type_hint", "relevance", "conditions", "subject_company", "relation")


def context_fields(proposal: dict) -> dict:
    """Keep supplied values, including explicit nulls, without inventing context.

    Old readers did not supply these keys. In particular their code-assigned
    event_type must never be promoted into a reader's type_hint.
    """
    result = {}
    for key in CONTEXT_FIELDS:
        value = proposal.get(key)
        if key == "conditions":
            valid = isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value)
        else:
            valid = isinstance(value, str) and bool(value.strip())
        # Missing and malformed values are unknown, never an invented empty
        # list. Preserve valid text verbatim; shape checking assesses no meaning.
        result[key] = deepcopy(value) if valid else None
    return result


def context_steps(proposal: dict, at: str) -> list[dict]:
    """Record rejected shapes without claiming the proposal's meaning is wrong."""
    shaped = context_fields(proposal)
    dropped = [key for key in CONTEXT_FIELDS
               if proposal.get(key) is not None and shaped[key] is None]
    if not dropped:
        return []
    return [{"step": "Proposal shape", "tag": "Code", "who": "runs.proposal", "at": at,
             "text": "Left malformed proposal fields unknown: "
                     + "; ".join(f"{key}={proposal.get(key)!r}" for key in dropped)
                     + (". An invalid conditions list is left wholly unknown" if "conditions" in dropped else "")
                     + ". This checks value shapes, not meaning, and counts no mistake."}]


def reader_context_fields(raw_event: dict) -> dict:
    """Adapt a raw reader event, never a saved/derived row.

    Older readers supplied their interpretation under event_type. Keep that
    exact text only when type_hint is absent; an explicit null or malformed
    hint remains unknown. The persisted raw reading is never modified.
    """
    context = context_fields(raw_event)
    legacy = raw_event.get("event_type")
    if "type_hint" not in raw_event and isinstance(legacy, str) and legacy.strip():
        context["type_hint"] = legacy
    return context


def reader_context_steps(raw_event: dict, at: str) -> list[dict]:
    """Make legacy reader-field adaptation explicit beside shape diagnostics."""
    steps = context_steps(raw_event, at)
    if "type_hint" not in raw_event and reader_context_fields(raw_event)["type_hint"] is not None:
        steps.append({"step": "Legacy reader type", "tag": "Code", "who": "runs.proposal", "at": at,
                      "text": "Preserved the raw reader's event type as its type hint: "
                              + repr(raw_event["event_type"].replace("_", " ")) + ". This is a proposal, not a check result."})
    return steps


def normalized_context(value):
    """Formatting-only comparison: case/whitespace, and each list item in order.

    No punctuation removal, synonyms, sorting, similarity, or keyword meaning.
    Null remains distinct from an explicitly empty list. Original text is kept.
    """
    if isinstance(value, str):
        return " ".join(value.split()).casefold()
    if isinstance(value, list):
        return [normalized_context(item) for item in value]
    return value


def context_changes(before: dict, after: dict) -> list[dict]:
    """Structured differences between already-shaped proposal context bundles."""
    return [{"field": field, "before": deepcopy(before.get(field)), "after": deepcopy(after.get(field))}
            for field in CONTEXT_FIELDS
            if normalized_context(before.get(field)) != normalized_context(after.get(field))]


def reader_event_type(raw_event: dict) -> str:
    """A display category from an exact reader label, never from quoted keywords.

    Free-form interpretations stay verbatim in type_hint. An unrecognised hint
    gets the display category unknown; it is neither rejected nor relabelled.
    Legacy event_type adapts only at the raw-reader boundary.
    """
    from checks.event_checks import EVENT_TYPES
    hint = reader_context_fields(raw_event)["type_hint"]
    category = "_".join(hint.casefold().split()) if hint else "unknown"
    return category if category in {*EVENT_TYPES, "earnings", "other", "unknown"} else "unknown"


def type_presentation(row: dict, values: dict, fixes: list[dict]) -> dict:
    """Describe type provenance from stored facts, never infer a category from a quote.

    Derived only: no saved proposal is rewritten and these display fields do
    not participate in acceptance. Missing legacy hints stay missing; human
    edits (including clears) are never attributed to the original reader.
    """
    edited = {fix["field"] for fix in fixes}
    hint = context_fields(values)["type_hint"]
    hint_origin = "person" if "type_hint" in edited else "reader" if hint is not None else "not_supplied"
    hint_label = "Type edited by you" if hint_origin == "person" else "Reader's proposed type"
    category = values.get("event_type")
    if "event_type" in edited:
        origin, label = "person", "Category edited by you"
    elif category in (None, "", "unknown"):
        origin, label = "unknown", "Category not established"
    elif "type_hint" not in row:
        origin, label = "legacy_keyword", "Category (from old keyword rule)"
    else:
        # A saved hint does not establish how an older category was assigned.
        origin, label = "stored", "Stored category"
    summary = f"{hint_label}: {hint if hint is not None else 'Not supplied'}"
    if origin in ("legacy_keyword", "person", "stored"):
        summary = f"{label}: {category if category not in (None, '') else 'Not supplied'}. " + summary
    return {"category": category, "category_origin": origin, "category_label": label,
            "type_hint": hint, "type_hint_origin": hint_origin, "type_hint_label": hint_label,
            "summary": summary}
