"""Bind a human Keep to its effective event proposal and cited evidence.

The effective values include check-derived period bounds shown to the person.
A change to those displayed bounds can therefore expire acceptance even if the
raw proposal wording did not change. Trail/cost/presentation prose is excluded.

Source lineage (source.sha256 and rendered_from format/sha256) is part of the
basis. The basis version stays 1: Keeps recorded before lineage was bound hash
differently and therefore expire fail-closed to history (never restamped).

The decision log is the only acceptance store. This module computes an identity;
it neither certifies meaning nor migrates an old decision to today's evidence.
"""
import hashlib
import json

from runs.claim_assessment import page_evidence, row_source


def review_basis(run: dict, row: dict, values: dict) -> dict:
    source = row_source(run, row)
    second = row.get('second_reader_proposal') or {}
    material = {
        'version': 1,
        'run_id': (run.get('run') or {}).get('id'),
        'target': row.get('id'),
        'proposal': values,
        'proposed_by': row.get('proposed_by'),
        'source': {key: source.get(key) for key in ('id', 'kind', 'url', 'published_date', 'title', 'publisher')},
        # The document the cited pages were cut from (and, for HTML, the filed
        # HTML they were rendered from). A changed document is a changed source.
        'source_sha256': source.get('sha256'),
        'rendered_from': {key: (source.get('rendered_from') or {}).get(key) for key in ('format', 'sha256')}
                         if source.get('rendered_from') else None,
        'evidence': page_evidence(source, row.get('page_number')),
        'box': row.get('box'),
        'second_reader': {'model': second.get('model'), 'proposal': second.get('proposal')},
        'escalation_context_changes': row.get('escalation_context_changes'),
        'escalation_proposal': {key: (row.get('escalation_proposal') or {}).get(key)
                               for key in ('model', 'source_id', 'page_number', 'proposal')},
        'timing_proposal': {key: (row.get('timing_proposal') or {}).get(key)
                            for key in ('model', 'source_id', 'page_number', 'proposal')},
    }
    raw = json.dumps(material, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    return {'version': 1, 'sha256': hashlib.sha256(raw.encode()).hexdigest()}


def matches_review_basis(stored, current: dict) -> bool:
    return (isinstance(stored, dict) and type(stored.get('version')) is int
            and stored == current)


def review_projection(state: dict) -> dict:
    """One derived acceptance view; mechanical/edit groups are not clearance.

    This returns presentation/eligibility fields, never a persisted decision.
    Historical Keeps remain available separately and cannot override this view.
    """
    semantic = state['semantic_status']
    accepted = semantic == 'human_accepted'
    excluded = not accepted and (semantic == 'human_removed' or state['group'] == 'removed')
    needs_review = not accepted and not excluded
    action = state.get('last_action')
    if accepted:
        label, decision = 'Accepted by you', 'Kept by you'
    elif excluded:
        label = 'Removed by you' if semantic == 'human_removed' else 'Excluded'
        decision = label if semantic == 'human_removed' else None
    elif action == 'keep':
        label, decision = 'Previous Keep — needs your review', 'Previous Keep — needs your review'
    elif state['group'] == 'corrected' or action == 'fix':
        label, decision = 'Corrected, awaiting your review', ('Fixed by you — not yet accepted' if action == 'fix' else None)
    else:
        label, decision = 'Proposed — needs review', None
    return {'accepted': accepted, 'needs_attention': needs_review,
            'on_calendar': accepted, 'review_label': label, 'decision_label': decision,
            'review_bucket': 'accepted' if accepted else 'excluded' if excluded else 'proposed'}
