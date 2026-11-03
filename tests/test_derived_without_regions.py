"""Check 41: no promotion from derived transcription geometry on missing SAM evidence."""
import json
import pytest
from fastapi.testclient import TestClient
from server.app import create_app
from tests.test_r27_picture import picture_index,run,FakeSam,BOXES,Transcriber,RUN_SCHEMA

@pytest.mark.parametrize('mode,expected', [('empty','could_not_run'),('missing','could_not_run'),('real','pass'),('whole_page','could_not_run'),('outside','fail')])
def test_derived_geometry_stays_narrow(tmp_path,mode,expected):
 index=picture_index.__wrapped__(tmp_path)
 doc,run_dir,_=run(tmp_path,index,FakeSam(BOXES),Transcriber())
 page=doc['sources'][0]['pages'][0]
 if mode=='empty':page['regions']=[]
 if mode=='missing':page.pop('regions',None)
 if mode=='whole_page':page['regions']=[{'id':'r0','concept':'whole page','box':[0,0,612,792]}]
 if mode=='outside':doc['rows'][0]['box']=[590,750,600,760]
 RUN_SCHEMA.validate(doc)
 (run_dir/'run.json').write_text(json.dumps(doc))
 with TestClient(create_app(runs_dir=run_dir.parent,background=False)) as client:
  response=client.get('/api/runs/run-1')
 assert response.status_code==200,response.text
 data=response.json(); row=data['rows'][0]
 check=next(c for c in row['checks'] if c['name']=='selector_match')
 assert check['result']==expected,check['reason']
 if expected!='pass':
  assert data['state']['targets'][row['id']]['status']!='matched'
  if data['state']['targets'][row['id']]['issues']:assert not data['state']['targets'][row['id']]['on_calendar']
  else:assert not data['state']['targets'][row['id']]['accepted']
 else:assert 'region SAM' in check['reason']
