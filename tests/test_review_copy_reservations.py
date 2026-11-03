from server import jobs
from runs import research
from tests import research_fakes as F
from tools import research_smoke

def test_review_existing_releases_only_its_owned_reservations(tmp_path, monkeypatch):
    originals = tmp_path / 'originals'
    originals.mkdir()
    run = jobs.create_run(originals, jobs.validate_choices({'mode':'research', 'companies':['PTGX'],
                                                          'spending_limit_usd':1.0}), as_of='2026-11-02')
    rid = run['run']['id']
    Job = research.make_job_class()
    observed = {}
    def fake_review(self, note=None):
        observed['own'] = jobs.reserve_budget(rid, 0, 1, 0.05, owner=self)
        observed['voice'] = jobs.reserve_budget(rid, 0, 1, 0.90)
        assert observed['own'] is not None and observed['voice'] is not None
        observed['held_during_review'] = dict(jobs._RESERVED[rid])
        self.meta.setdefault('research', {})
        self.save()
    monkeypatch.setattr(Job, 'review_only', fake_review)
    monkeypatch.setattr(research, 'make_job_class', lambda: Job)
    own = None
    second = None
    try:
        copied, _ = research_smoke.review_existing(originals / rid, tmp_path / 'copies', 1.0, F.tools({}))
        observed['held_after_helper'] = dict(jobs._RESERVED.get(rid, {}))
        observed['owned_reference_after'] = observed['own'] in jobs._OWNERS
        second = jobs.reserve_budget(rid, 0, 1, 0.90)
        observed['second_090_admitted'] = second is not None
        assert observed['held_after_helper'] == {observed['voice']: 0.90}
        assert not observed['owned_reference_after']
        assert second is None
    finally:
        for token in [observed.get('own'), observed.get('voice'), second]:
            jobs.release_budget(rid, token)
        jobs.ACTIVE_RUNS.discard(rid)
