"""PIVOT RED 2: one effective date display, derived once in the state.

An explicit person clear of date_text with the structured period retained
must show the structured period everywhere (API row, calendar entry, report
open row and accepted calendar row), never the raw pre-clear wording and
never 'Undated'. The cleared wording stays cleared ('' / null) and is
flagged as deliberately cleared.
"""
import json

import pytest
from fastapi.testclient import TestClient

from report.data import build_view
from runs.calendar import calendar_entries
from runs.state import current_state
from server.app import create_app
from tests.test_pivot_proposal_flow import make_run, CASES, AT

RAW = 'the first quarter of 2027'
PERIOD = 'Q1 2027 (2027-01-01 to 2027-03-31)'


def _clear(to):
    return {'at': AT, 'action': 'fix', 'by': 'person', 'target': 'e1', 'note': None,
            'fix': {'field': 'date_text', 'from': RAW, 'to': to}}


@pytest.mark.parametrize('to', [None, ''])
def test_state_derives_one_display_for_an_explicit_clear(to):
    _, run = make_run(CASES[1], event_updates={'date_text': RAW})
    before = current_state(run, [])['targets']['e1']
    assert before['date_display'] == RAW and before['date_text_cleared'] is False
    state = current_state(run, [_clear(to)])
    target = state['targets']['e1']
    assert target['values']['date_text'] == to
    assert target['timing_state'] == 'dated'
    assert target['date_display'] == PERIOD
    assert target['date_text_cleared'] is True
    view = build_view(run, [_clear(to)], generated_at=AT)
    [open_row] = view['open_rows']
    assert open_row['date_display'] == PERIOD and open_row['date_text_cleared'] is True
    assert open_row['date_text'] == ''      # the cleared wording stays cleared
    assert RAW not in open_row['date_display'] and 'Undated' not in open_row['date_display']


def test_undated_and_unresolved_rows_keep_their_honest_display():
    _, run = make_run(CASES[3])
    target = current_state(run, [])['targets']['e1']
    assert target['date_display'] == 'Undated · no timing stated'
    from runs.calendar import effective_date_display
    blank = {'date_text': None, 'date': None, 'date_end': None}
    assert effective_date_display(blank, 'timing_unresolved') == 'Timing unresolved'
    assert effective_date_display(blank, 'undated') == 'Undated · no timing stated'
    assert effective_date_display({**blank, 'date_text': 'mid-2027'}, 'timing_unresolved') == 'mid-2027'
    assert effective_date_display({**blank, 'date': '2027-02-03'}, 'dated') == '2027-02-03'


def test_api_row_calendar_entry_and_report_agree_after_clear_then_keep(tmp_path):
    make_run(CASES[1], tmp_path / 'synthetic', event_updates={'date_text': RAW})
    log_path = tmp_path / 'synthetic/decisions.jsonl'
    with TestClient(create_app(runs_dir=tmp_path)) as client:
        root = '/api/runs/synthetic'
        fixed = client.post(root + '/decisions', json={
            'target': 'e1', 'action': 'fix', 'fix': {'field': 'date_text', 'from': RAW, 'to': None}})
        assert fixed.status_code == 200, fixed.text
        row = client.get(root).json()['state']['targets']['e1']
        assert row['date_display'] == PERIOD and row['date_text_cleared'] is True
        assert row['values']['date_text'] is None and not row['on_calendar']
        kept = client.post(root + '/decisions', json={'target': 'e1', 'action': 'keep'})
        assert kept.status_code == 200, kept.text
        # The posted Keep line carries no raw pre-clear wording.
        keep_line = json.loads(log_path.read_text().splitlines()[-1])
        assert keep_line['action'] == 'keep' and RAW not in json.dumps(keep_line)
        row = client.get(root).json()['state']['targets']['e1']
        assert row['on_calendar'] and row['date_display'] == PERIOD
        [entry] = client.get(root + '/calendar').json()['entries']
        assert entry['date_display'] == PERIOD and entry['date_text_cleared'] is True
        assert entry['date_text'] is None
        # Undo the Keep: off calendar, same display; Undo the clear: raw wording back.
        assert client.post(root + '/decisions', json={'target': 'e1', 'action': 'undo'}).status_code == 200
        row = client.get(root).json()['state']['targets']['e1']
        assert not row['on_calendar'] and row['date_display'] == PERIOD and row['date_text_cleared'] is True
        assert client.post(root + '/decisions', json={'target': 'e1', 'action': 'undo'}).status_code == 200
        row = client.get(root).json()['state']['targets']['e1']
        assert row['date_display'] == RAW and row['date_text_cleared'] is False
        assert row['values']['date_text'] == RAW
    from report.data import load_run_dir
    run, decisions = load_run_dir(tmp_path / 'synthetic')
    decisions = decisions[:-2]        # the state at the Keep, before the two Undos
    view = build_view(run, decisions, generated_at=AT)
    shown = [r['date_display'] for r in view['calendar'] + view['past'] + view['later']
             + view['contingent'] + view['undated'] + view['timing_unresolved']]
    assert shown == [PERIOD]
    assert view['counts']['accepted'] == 1 and not view['open_rows']
    state = current_state(run, decisions)
    assert [e['date_display'] for e in calendar_entries(run, state)] == [PERIOD]


@pytest.mark.parametrize('to', [None, ''])
def test_pdf_and_docx_print_the_period_display_for_a_cleared_date_text(to):
    """Renderers must print the state's effective date_display — the structured period, never the raw
    pre-clear wording and never 'Undated'."""
    from io import BytesIO
    from pypdf import PdfReader
    from docx import Document
    from report.docx_report import build_docx
    from report.pdf_report import build_pdf
    _, run = make_run(CASES[1], event_updates={'date_text': RAW})
    view = build_view(run, [_clear(to)], generated_at=AT)
    text = "\n".join((p.extract_text() or "") for p in PdfReader(BytesIO(build_pdf(view))).pages)
    doc = Document(BytesIO(build_docx(view)))
    text += "\n" + "\n".join(p.text for p in doc.paragraphs)
    assert PERIOD in text
    assert 'Undated · no timing stated' not in text
    row_paras = [p.text for p in doc.paragraphs if 'quote:' in p.text]   # the open-row paragraph in the DOCX;
    assert row_paras and all(PERIOD in t for t in row_paras)             # the shared f-string feeds the PDF too
    for t in row_paras:
        assert RAW not in t.split('quote:')[0], f"raw pre-clear wording printed as the row's timing: {t[:120]}"
