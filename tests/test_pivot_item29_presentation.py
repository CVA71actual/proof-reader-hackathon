import json,shutil
from pathlib import Path
from fastapi.testclient import TestClient
from server.app import create_app,RunStore
from report.manifest import write_report_manifest
from report.data import report_filenames
from checks.event_checks import date_match

REPO=Path(__file__).resolve().parents[1]
REASON='Saved report is out of date. Rebuild it to show the current results.'
def test_changed_saved_report_has_an_honest_server_reason(tmp_path):
 d=tmp_path/'ptgx-r5';shutil.copytree(REPO/'runs/ptgx-r5',d)
 store=RunStore(tmp_path);run=store.run(d.name);decisions=store.decisions(d.name)
 write_report_manifest(d/'report',run,decisions)
 pdf,_=report_filenames(run['run']['number'])
 c=TestClient(create_app(runs_dir=tmp_path,ui_dir=tmp_path/'none',reader=object(),second_reader=None,wonder_reader=None,sam_reader=None))
 assert c.get('/runs/ptgx-r5/report/'+pdf).status_code==200
 p=d/'run.json';raw=json.loads(p.read_text());raw['rows'][0]['title']+=' changed';p.write_text(json.dumps(raw));before=p.read_bytes()
 detail=c.get('/api/runs/ptgx-r5').json()
 assert detail.get('report_stale') is True
 assert detail['report_stale_reason']==REASON
 assert c.get('/runs/ptgx-r5/report/'+pdf).json()['detail']==REASON
 assert c.head('/runs/ptgx-r5/report/'+pdf).headers['X-Report-Reason']==REASON
 assert p.read_bytes()==before

def test_equal_period_different_words_does_not_claim_verbatim_match():
 quote='The company expects initiation in Q1 2027.'
 c=date_match('first quarter of 2027','2027-01-01',quote,'2027-03-31')
 assert c.result=='pass'
 assert c.label=='Date period found in quote'
 assert quote in c.reason and '2027-01-01 to 2027-03-31' in c.reason
 d=date_match('first quarter of 2027','2027-01-01','The company expects initiation in Q2 2027.','2027-03-31')
 assert d.result=='fail'


def test_report_status_distinguishes_current_missing_and_unbound(tmp_path):
 from server.app import report_status
 d=tmp_path/'ptgx-r5';shutil.copytree(REPO/'runs/ptgx-r5',d)
 store=RunStore(tmp_path);run=store.run(d.name);decisions=store.decisions(d.name)
 write_report_manifest(d/'report',run,decisions)
 assert report_status(d/'report',run,decisions)=={'report_stale':False,'report_stale_reason':None}
 (d/'report/manifest.json').unlink()
 assert report_status(d/'report',run,decisions)=={'report_stale':True,'report_stale_reason':REASON}
 shutil.rmtree(d/'report')
 assert report_status(d/'report',run,decisions)=={'report_stale':False,'report_stale_reason':None}
 run['report_stale']=True
 assert report_status(d/'report',run,decisions)=={'report_stale':True,'report_stale_reason':REASON}
