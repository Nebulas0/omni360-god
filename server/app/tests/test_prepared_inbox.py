import json
import time

from console_api import PREFIX
from test_console import setup, pair, seed_game as seed_original_game, CSRF
from test_disc_sets import seed_discs


def seed_game(service):
    release,original=seed_original_game(service)
    with service.connect() as c:
        c.execute("UPDATE jobs SET checks_json=? WHERE item_id=?",(json.dumps({'unlock_enabled':False}),int(release[8:])))
    return release,original


def submit(client, device, release=None, selection=None, key='website-prepare-test-0001'):
    return client.post(f'/api/consoles/{device}/prepare', headers=CSRF,
                       json={'request_key':key,'selection':selection or {'release_id':release,'conversion':'server','unlock_packages':False}})


def prepare_ready(service, rid):
    # The worker first attaches/reuses the conversion job, then publishes its files.
    for _ in range(3):
        row=service.get_request(rid)
        if row['state']=='ready':return
        service.prepare(row)


def test_prepare_while_xbox_off_and_owner_only_discovery(setup):
    app,client,s=setup;h,device=pair(app,client,s);other,other_device=pair(app,client,s,'Other Xbox')
    release,original=seed_game(s)
    r=submit(client,device,release);assert r.status_code==200,r.json
    rid=r.json['id'];assert r.json['inbox'] and r.json['state']=='queued'
    prepare_ready(s,rid)
    inbox=client.get(PREFIX+'/inbox',headers=h);assert inbox.status_code==200
    row=inbox.json['items'][0];assert row['id']==rid and row['state']=='ready' and row['selection']['unlock_packages'] is False
    assert row['expires_at']>time.time() and int(row['bytes'])>0
    assert client.get(PREFIX+'/inbox',headers=other).json['items']==[]
    assert client.post(f'{PREFIX}/inbox/{rid}/accept',json={},headers=other).status_code==404
    assert client.post(f'{PREFIX}/inbox/{rid}/accept',json={},headers=h).json['accepted']
    stamp=client.get(PREFIX+'/inbox',headers=h).json['items'][0]['accepted_at']
    assert client.post(f'{PREFIX}/inbox/{rid}/accept',json={},headers=h).status_code==200
    assert client.get(PREFIX+'/inbox',headers=h).json['items'][0]['accepted_at']==stamp
    assert client.post(f'{PREFIX}/install-requests/{rid}/events',headers=h,json={'event_id':rid+'_installed','outcome':'installed'}).status_code==200
    assert client.get(PREFIX+'/inbox',headers=h).json['items']==[]
    assert original.exists()


def test_browser_auth_csrf_and_revocation_still_apply(setup):
    app,client,s=setup;h,device=pair(app,client,s);release,_=seed_game(s)
    body={'request_key':'website-protected-key-0001','selection':{'release_id':release}}
    assert client.post(f'/api/consoles/{device}/prepare',json=body).status_code==403
    assert client.get(PREFIX+'/inbox').status_code==401
    client.post(f'/api/consoles/{device}/revoke',json={},headers=CSRF)
    assert submit(client,device,release).status_code==409
    assert client.get(PREFIX+'/inbox',headers=h).status_code==401
    for path in ('/consoles/prepare','/static/prepare.js','/static/prepare.css'):assert client.get(path).status_code==200


def test_submission_dedup_expiry_and_intentional_reinstall(setup):
    app,client,s=setup;h,device=pair(app,client,s);release,_=seed_game(s)
    first=submit(client,device,release).json;rid=first['id']
    assert submit(client,device,release).json['id']==rid
    assert submit(client,device,release,key='another-website-key-0002').json['id']==rid
    assert submit(client,device,selection={'release_id':release,'conversion':'server','unlock_packages':True}).status_code==409
    prepare_ready(s,rid)
    with s.connect() as c:c.execute('UPDATE console_artifacts SET expires=? WHERE id=?',(time.time()-1,s.get_request(rid)['artifact_id']))
    assert client.get(PREFIX+'/inbox',headers=h).json['items'][0]['state']=='expired'
    assert client.post(f'{PREFIX}/inbox/{rid}/accept',json={},headers=h).status_code==409
    new=submit(client,device,release,key='expired-website-key-0003').json
    assert new['id']!=rid
    prepare_ready(s,new['id'])
    client.post(f"{PREFIX}/install-requests/{new['id']}/events",headers=h,json={'event_id':new['id']+'_installed','outcome':'installed'})
    again=submit(client,device,release,key='reinstall-website-key-0004').json
    assert again['id'] not in {rid,new['id']}


def test_deleted_copy_disappears_and_can_prepare_fresh(setup):
    app,client,s=setup;h,device=pair(app,client,s);release,original=seed_game(s)
    rid=submit(client,device,release).json['id'];prepare_ready(s,rid)
    assert client.post(f'/api/consoles/requests/{rid}/delete-files',headers=CSRF,json={'confirm':True}).status_code==200
    assert client.get(PREFIX+'/inbox',headers=h).json['items']==[]
    assert submit(client,device,release,key='fresh-after-delete-0001').json['id']!=rid
    assert original.exists()


def test_disc_set_and_both_modes_use_existing_validation(setup):
    app,client,s=setup;h,device=pair(app,client,s);ids=seed_discs(s)
    selection={'kind':'disc_bundle','release_ids':ids,'conversion':'server','unlock_packages':False}
    r=submit(client,device,selection=selection);assert r.status_code==200,r.json
    s.prepare(s.get_request(r.json['id']));view=client.get(PREFIX+'/inbox',headers=h).json['items'][0]
    assert view['state']=='ready' and view['selection']['release_ids']==sorted(ids)
    assert submit(client,device,selection={**selection,'conversion':'console'},key='bad-disc-console-0001').status_code==400
    assert submit(client,device,ids[0],selection={'release_id':ids[0],'conversion':'console'},key='single-disc-console-0001').status_code==200
    assert submit(client,device,selection={'kind':'title_update'},key='bad-title-update-0001').status_code==400


def test_bounded_inbox_and_invalid_input(setup):
    app,client,s=setup;h,device=pair(app,client,s);release,_=seed_game(s)
    assert submit(client,device,release,key='short').status_code==400
    with s.connect() as c:
        for i in range(32):
            rid=f'{i:032x}'
            c.execute('INSERT INTO console_requests(id,device_id,idem,payload,state,message,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                      (rid,device,'pending-'+str(i),json.dumps({'release_id':'catalog-99999','conversion':'server'}),'queued','Waiting','2026-11-08','2026-11-08'))
            c.execute('INSERT INTO console_inbox(request_id) VALUES(?)',(rid,))
    assert submit(client,device,release).status_code==429
    assert len(client.get(PREFIX+'/inbox',headers=h).json['items'])==32


def test_browser_catalog_and_disc_checklist(setup):
    app,client,s=setup;h,device=pair(app,client,s);ids=seed_discs(s)
    result=client.get('/api/consoles/catalog?q=synthetic%20game&kind=game').json
    assert {i['release_id'] for i in result['items']}==set(ids)
    detail=client.get('/api/consoles/releases/'+ids[0]).json
    assert [d['release_id'] for d in detail['disc_set']['items']]==ids
    assert client.get('/api/consoles/catalog?kind=bad').status_code==400
    assert client.get('/api/consoles/catalog?cursor=-1').status_code==400


def test_new_browser_routes_require_basic_auth(tmp_path,monkeypatch):
    from app import create_app
    from config import Config
    monkeypatch.setenv('WORK_DIR',str(tmp_path/'work'));monkeypatch.setenv('OUTPUT_DIR',str(tmp_path/'output'))
    monkeypatch.setenv('APP_AUTH_USER','admin');monkeypatch.setenv('APP_AUTH_PASSWORD','password')
    client=create_app(Config.from_env(),start_background=False).test_client()
    for path in ('/consoles/prepare','/api/consoles/catalog','/api/consoles/releases/catalog-1'):
        assert client.get(path).status_code==401
    assert client.post('/api/consoles/'+'a'*32+'/prepare',headers=CSRF,json={}).status_code==401
