"""A person's clear must never be attributed to the reader (paste item42)."""
import json
from io import BytesIO

import pytest
from docx import Document
from fastapi.testclient import TestClient
from pypdf import PdfReader

from checks import event_checks
from report.data import build_view, load_run_dir
from report.docx_report import build_docx
from report.pdf_report import build_pdf
from server.app import create_app
from tests.test_pivot_proposal_flow import make_run, CASES, AT

CLEAR_REASON = 'You cleared the date wording; there is no printed wording left to compare.'


@pytest.mark.parametrize('cleared', [None, ''])
def test_person_clear_is_attributed_in_bound_check_reload_and_reports(tmp_path, cleared):
    raw = 'the first quarter of 2027'
    make_run(CASES[1], tmp_path / 'synthetic', event_updates={'date_text': raw})
    with TestClient(create_app(runs_dir=tmp_path)) as client:
        endpoint = '/api/runs/synthetic'
        reply = client.post(endpoint + '/decisions', json={
            'target': 'e1', 'action': 'fix', 'fix': {'field': 'date_text', 'from': raw, 'to': cleared}})
        assert reply.status_code == 200, reply.text
        target = client.get(endpoint).json()['state']['targets']['e1']
        assert target['date_text_cleared'] and not target['accepted']
        assert not target['checks_stale']
        [check] = [c for c in target['effective_checks'] if c['name'] == 'date_match']
        assert check['result'] == 'could_not_run' and check['reason'] == CLEAR_REASON
        log = (tmp_path / 'synthetic/decisions.jsonl').read_bytes()
        rc = json.loads(log.splitlines()[-1])['recheck']
        assert rc['result'] == 'could_not_run' and CLEAR_REASON in rc['reason']
        # The original reader supplied a real quarter and remains a passing before-value.
        [before] = [c for c in rc['before']['checks'] if c['name'] == 'date_match']
        assert before['result'] == 'pass' and CLEAR_REASON not in before['reason']
        assert client.get(endpoint).json()['state']['targets']['e1']['date_text_cleared']
        assert (tmp_path / 'synthetic/decisions.jsonl').read_bytes() == log
    run, decisions = load_run_dir(tmp_path / 'synthetic')
    view = build_view(run, decisions, generated_at=AT)
    pdf = '\n'.join(p.extract_text() or '' for p in PdfReader(BytesIO(build_pdf(view))).pages)
    doc = Document(BytesIO(build_docx(view)))
    word = '\n'.join(p.text for p in doc.paragraphs)
    for text in (pdf, word):
        assert CLEAR_REASON in ' '.join(text.split())
        assert 'The reader gave no date text' not in text


def test_missing_reader_wording_keeps_the_pinned_reader_attribution():
    result = event_checks.date_match(None, None, 'No date is stated.')
    assert result.result == 'could_not_run'
    assert result.reason == 'The reader gave no date text for this row.'


@pytest.mark.parametrize('raw,cleared', [(None, ''), ('', ''), ('   ', '')])
def test_blank_to_blank_person_edit_does_not_claim_wording_was_cleared(tmp_path, raw, cleared):
    make_run(CASES[1], tmp_path / 'synthetic', event_updates={'date_text': raw})
    with TestClient(create_app(runs_dir=tmp_path)) as client:
        endpoint = '/api/runs/synthetic'
        current = client.get(endpoint).json()['state']['targets']['e1']['values']['date_text']
        response = client.post(endpoint + '/decisions', json={
            'target': 'e1', 'action': 'fix', 'fix': {'field': 'date_text', 'from': current, 'to': cleared}})
        assert response.status_code == 200, response.text
        target = client.get(endpoint).json()['state']['targets']['e1']
        assert not target['date_text_cleared'] and not target['accepted']
        assert not target['checks_stale']
        check = next(c for c in target['effective_checks'] if c['name'] == 'date_match')
        assert check['result'] == 'could_not_run'
        assert check['reason'] == 'No date wording is supplied for this row.'
        assert 'You cleared' not in json.dumps(target)
    run, decisions = load_run_dir(tmp_path / 'synthetic')
    view = build_view(run, decisions, generated_at=AT)
    pdf = '\n'.join(p.extract_text() or '' for p in PdfReader(BytesIO(build_pdf(view))).pages)
    word = '\n'.join(p.text for p in Document(BytesIO(build_docx(view))).paragraphs)
    for text in (pdf, word):
        assert 'No date wording is supplied for this row.' in ' '.join(text.split())
        assert 'You cleared' not in text
