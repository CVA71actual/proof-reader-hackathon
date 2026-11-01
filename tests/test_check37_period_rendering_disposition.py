"""Check 37 disposition: preserve no-date-text rendering without manufacturing PASS.
An explicit person Keep admits the proposed period; no automatic evidence claim.
"""
import pytest
from tests.redteam.test_round8 import _saved, _row, FIXED, _pdf_text, _docx_text
from report.data import load_run_dir, build_view
from report.build import build
from report import data


def _kept_view(tmp_path):
    keep = {'action': 'keep', 'target': 'e1', 'by': 'person', 'at': '2026-10-29T13:00:00Z',
            'note': 'Retain this proposed period for the rendering fixture; no passing date check is claimed.'}
    d = _saved(tmp_path, [_row(date_text=None, title='Window')], decisions=[keep])
    run, decisions = load_run_dir(d)
    before = build_view(run, [], generated_at=FIXED)
    assert not before['calendar']
    view = build_view(run, decisions, generated_at=FIXED)
    t = view['state']['targets']['e1']
    assert t['status'] != 'matched' and t['decided_by_person']
    assert t['values']['date_text'] is None
    assert not any(c['name'] == 'date_match' and c['result'] == 'pass' for c in t['effective_checks'])
    return d, view


def _assert_period(view):
    shown = view['calendar'][0]['date_display']
    assert '2027-01-01' in shown and '2027-03-31' in shown and shown != '2027-01-01'


def test_person_kept_unchecked_period_preserves_original_rendering_assertions(tmp_path):
    d, view = _kept_view(tmp_path)
    _assert_period(view)
    built = build('run-8', run_dir=d, out=tmp_path / 'out', generated_at=FIXED)
    for text in (_pdf_text(built['pdf']), _docx_text(built['docx'])):
        assert '2027-03-31' in text
        assert 'Window' in text


def test_first_day_only_mutation_is_caught(tmp_path, monkeypatch):
    monkeypatch.setattr(data, '_date_display', lambda values: values.get('date') or '—')
    _, view = _kept_view(tmp_path)
    with pytest.raises(AssertionError):
        _assert_period(view)
