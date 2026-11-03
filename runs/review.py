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
    if row.get('research'):
        # RESEARCHER-FULL-001: a research row's Keep binds its whole evidence selection, not only the primary page.
        # Added only for research rows, so every other row's basis (and its digest) is byte-identical to before.
        material['research'] = research_basis(run, row)
    raw = json.dumps(material, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    return {'version': 1, 'sha256': hashlib.sha256(raw.encode()).hexdigest()}


RESEARCH_MATERIAL = ('revision', 'primary_span_index', 'action', 'modality', 'relevance_kind',
                     'placement_proposal', 'prior_row_ids', 'conditions_state')


def research_basis(run: dict, row: dict) -> dict:
    """The research proposal's material: its revision and research-only fields, and for every evidence span its
    source, page, quote, roles and word ids with that source's document lineage and the page's artifact hashes.
    Check results, issues, linked duplicates, costs and trail prose are not material and are left out."""
    research = row['research']
    sources = {s.get('id'): s for s in run.get('sources', [])}
    spans = []
    for span in research.get('evidence') or []:
        src = sources.get(span.get('source_id')) or {}
        page = next((p for p in src.get('pages', []) if p.get('page_number') == span.get('page_number')), {})
        spans.append({
            'source_id': span.get('source_id'), 'page_number': span.get('page_number'), 'quote': span.get('quote'),
            'roles': span.get('roles'), 'word_ids': span.get('word_ids'),
            'source_sha256': src.get('sha256'),
            'original_sha256': (src.get('retrieval') or {}).get('original_sha256'),
            'rendered_from': (src.get('rendered_from') or {}).get('sha256'),
            'image_sha256': page.get('image_sha256'), 'words_sha256': page.get('words_sha256'),
            # Manus 17:07: the artifact actually consumed (derived words when present) and the publication context
            # that dates and 'today' are read against are material too.
            'derived_words': {key: (page.get('derived_words') or {}).get(key) for key in ('file', 'sha256')}
                             if page.get('derived_words') else None,
            'published_date': src.get('published_date')})
    return {**{key: research.get(key) for key in RESEARCH_MATERIAL}, 'evidence': spans,
            'timing': research.get('timing'), 'timing_release_day': research.get('timing_release_day')}


def matches_review_basis(stored, current: dict) -> bool:
    return (isinstance(stored, dict) and type(stored.get('version')) is int
            and stored == current)


# CALENDAR-RULE-001 (CVA 2026-11-02): conflicts go to review; a clean proposal is placed by the calendar rule and
# is never presented as a person's Keep.
INCLUDED_LABEL = 'Included — not reviewed by you'
CONTEXT_LABEL = 'Context (past)'
UNDATED_LABEL = 'Undated: needs timing'
LATER_LABEL = 'Later — after the calendar window'
UNPLACED_LABEL = 'Not placed — the run has no reference date'
INCLUSIONS = ('kept', 'included', 'review', 'context', 'later', 'undated', 'unplaced', 'excluded')
# runs.calendar.interval_bucket placement -> the inclusion of a clean, undecided proposal
_CLEAN_INCLUSION = {'calendar': 'included', 'past': 'context', 'later': 'later', 'contingent': 'undated',
                    'undated': 'undated', 'timing_unresolved': 'undated'}
_CLEAN_LABEL = {'included': INCLUDED_LABEL, 'context': CONTEXT_LABEL, 'later': LATER_LABEL,
                'undated': UNDATED_LABEL, 'unplaced': UNPLACED_LABEL}


def review_projection(state: dict) -> dict:
    """One derived acceptance and calendar view; mechanical/edit groups are not clearance.

    This returns presentation/eligibility fields, never a persisted decision.
    Historical Keeps remain available separately and cannot override this view.

    CALENDAR-RULE-001, the one inclusion predicate. A row is, in this order:
      kept      a current, bound human Keep ('Kept by you'), wherever its dates fall;
      excluded  removed by the person or by a code rule;
      review    it carries issues (state['issues']: failed checks, open factual disagreement, unreadable page or
                answer, an expired Keep, reviewer findings). Not on the calendar until a person keeps it. Issues not
                yet derived (state['issues'] is None) also mean review: fail closed;
      otherwise a clean, undecided proposal placed by its interval (state['placement'], from
                runs.calendar.interval_bucket, tested on the period end against the run-anchored window, and
                honouring a research row's own past/undated placement): in the window -> included
                ('Included — not reviewed by you'); past -> context; after the window -> later; no date -> undated;
                no run reference date -> unplaced.
    on_calendar keeps its API meaning, eligibility for the interval partition: true for kept and every clean row,
    so the partition (and only the partition) decides Calendar / Past / Later / Undated. needs_attention is true
    only for review. accepted still means a current human Keep and nothing else.
    """
    semantic = state['semantic_status']
    accepted = semantic == 'human_accepted'
    excluded = not accepted and (semantic == 'human_removed' or state['group'] == 'removed')
    action = state.get('last_action')
    issues = state.get('issues')
    if accepted:
        inclusion = 'kept'
    elif excluded:
        inclusion = 'excluded'
    elif issues is None or issues:
        inclusion = 'review'
    else:
        inclusion = _CLEAN_INCLUSION.get(state.get('placement'), 'unplaced')
    if accepted:
        label, decision = 'Accepted by you', 'Kept by you'
    elif excluded:
        label = 'Removed by you' if semantic == 'human_removed' else 'Excluded'
        decision = label if semantic == 'human_removed' else None
    elif inclusion != 'review':
        # Clean: the calendar rule placed it; no person's acceptance is claimed. A person's field edit stays named.
        label = _CLEAN_LABEL[inclusion]
        decision = 'Fixed by you — not yet accepted' if action == 'fix' else None
    elif action == 'keep':
        label, decision = 'Previous Keep — needs your review', 'Previous Keep — needs your review'
    elif state['group'] == 'corrected' or action == 'fix':
        label, decision = 'Corrected, awaiting your review', ('Fixed by you — not yet accepted' if action == 'fix' else None)
    else:
        label, decision = 'Proposed — needs review', None
    return {'accepted': accepted, 'needs_attention': inclusion == 'review',
            'on_calendar': inclusion not in ('review', 'excluded'), 'inclusion': inclusion,
            'review_label': label, 'decision_label': decision,
            'review_bucket': 'accepted' if accepted else 'excluded' if excluded else 'proposed'}
