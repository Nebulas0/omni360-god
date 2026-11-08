import json
from pathlib import Path

import pytest
from app import create_app
from config import Config
from console_api import PREFIX
from test_console import setup, pair, seed_game, prepare, CSRF


def ready(setup):
    app, client, s = setup
    headers, device = pair(app, client, s)
    release, original = seed_game(s)
    row = prepare(client, s, headers, release)
    manifest = client.get(f"{PREFIX}/artifacts/{row['artifact_id']}/manifest", headers=headers).json
    return client, s, headers, device, row, manifest, original


def test_console_dashboard_names_preparation_and_hides_credentials(setup):
    app, client, s = setup; h, device = pair(app, client, s, 'Living room Xbox')
    release, _ = seed_game(s)
    response = client.post(PREFIX+'/install-requests', json={'release_id':release}, headers={**h,'Idempotency-Key':'console-progress-123456'})
    s.prepare(s.get_request(response.json['id']))
    row = s.get_request(response.json['id'])
    with s.connect() as c: c.execute("UPDATE jobs SET status='working',stage='converting',progress=42,message='Converting game' WHERE id=?",(row['job_id'],))
    data = client.get('/api/consoles').json; view = data['installs'][0]
    assert view['console_name']=='Living room Xbox' and view['name']=='Synthetic game'
    assert view['progress']==42 and view['jobs'][0]['stage']=='converting'
    assert view['can_delete'] is False and data['preparing']==1
    assert 'token_hash' not in json.dumps(data) and h['Authorization'][7:] not in json.dumps(data)
    assert client.get('/consoles').status_code==200
    assert client.get('/static/consoles.css').status_code==200


def test_download_position_is_separate_from_installed_result(setup):
    client,s,h,device,row,manifest,original=ready(setup)
    entry=manifest['components'][0]['files'][0]
    client.head(entry['download_path'],headers=h)
    client.get(entry['download_path'],headers={**h,'If-None-Match':'"'+entry['sha256']+'"'})
    assert client.get('/api/consoles').json['installs'][0]['transfer'] is None
    response=client.get(entry['download_path'],headers={**h,'Range':'bytes=0-31'})
    assert response.status_code==206 and len(response.data)==32
    view=client.get('/api/consoles').json['installs'][0]
    assert view['installation_status'] is None and view['state']=='ready'
    assert view['transfer']['file_count']==2 and view['transfer']['file_index']==1
    assert client.post(f"{PREFIX}/install-requests/{row['id']}/events",json={'event_id':row['id']+'_installed','outcome':'installed'},headers=h).status_code==200
    view=client.get('/api/consoles').json['installs'][0]
    assert view['installation_status']=='installed' and view['reported_at']


def test_iso_archive_ranges_track_part_number_without_claiming_receipt(setup):
    client,s,h,device,row,manifest,original=ready(setup)
    size=65*1024**2;path=s.root/row['artifact_id']/'source.zip'
    with path.open('wb') as f: f.truncate(size)
    source={'bytes':size,'sha256':'a'*64};manifest['source']=source
    with s.connect() as c: c.execute("UPDATE console_artifacts SET kind='source',manifest=? WHERE id=?",(json.dumps(manifest),row['artifact_id']))
    response=client.get(f"{PREFIX}/source-artifacts/{row['artifact_id']}/file",headers={**h,'Range':f'bytes={32*1024**2}-{32*1024**2+7}'})
    assert response.status_code==206 and len(response.data)==8
    view=client.get('/api/consoles').json['installs'][0]
    assert view['transfer']['archive']==1 and view['transfer']['file_index']==2 and view['transfer']['file_count']==3
    assert view['installation_status'] is None


def test_delete_only_prepared_copy_preserves_library_source_and_report(setup):
    client,s,h,device,row,manifest,original=ready(setup)
    before=original.read_bytes();cache=s.config.work_dir/'source-cache'/'keep.zip';cache.parent.mkdir(exist_ok=True);cache.write_bytes(b'original source')
    assert client.post(f"{PREFIX}/install-requests/{row['id']}/events",json={'event_id':row['id']+'_installed','outcome':'installed'},headers=h).status_code==200
    path=f"/api/consoles/requests/{row['id']}/delete-files"
    assert client.post(path,json={'confirm':True}).status_code==403
    assert client.post(path,json={},headers=CSRF).status_code==400
    result=client.post(path,json={'confirm':True},headers=CSRF)
    assert result.status_code==200 and result.json['released_bytes']>0
    assert not (s.root/row['artifact_id']).exists()
    assert original.read_bytes()==before and cache.read_bytes()==b'original source'
    assert client.get(f"{PREFIX}/artifacts/{row['artifact_id']}/manifest",headers=h).status_code==410
    view=client.get('/api/consoles').json['installs'][0]
    assert view['installation_status']=='installed' and view['error_code']=='FILES_REMOVED' and not view['can_delete']
    assert client.get('/api/consoles').json['storage_bytes']==0
    assert client.post(path,json={'confirm':True},headers=CSRF).json['released_bytes']==0


def test_delete_preparation_is_blocked_and_symlink_cannot_escape(setup,tmp_path):
    client,s,h,device,row,manifest,original=ready(setup)
    url=f"/api/consoles/requests/{row['id']}/delete-files";root=s.root/row['artifact_id']
    with s.connect() as c:c.execute("UPDATE console_requests SET state='preparing' WHERE id=?",(row['id'],))
    assert client.post(url,json={'confirm':True},headers=CSRF).status_code==409 and root.exists()
    with s.connect() as c:c.execute("UPDATE console_requests SET state='ready' WHERE id=?",(row['id'],))
    root.rename(s.root/'preserved-original-copy')
    outside=tmp_path/'outside';outside.mkdir();(outside/'keep').write_bytes(b'keep');root.symlink_to(outside,target_is_directory=True)
    result=client.post(url,json={'confirm':True},headers=CSRF)
    assert result.status_code==409 and (outside/'keep').read_bytes()==b'keep'


def test_removed_revoked_entries_keep_audit_and_cannot_authenticate(setup):
    client,s,h,device,row,manifest,original=ready(setup)
    assert client.post(f'/api/consoles/{device}/remove',json={},headers=CSRF).status_code==409
    assert client.post(f'/api/consoles/{device}/revoke',json={},headers=CSRF).status_code==200
    assert client.post(f'/api/consoles/{device}/remove',json={},headers=CSRF).status_code==200
    default=client.get('/api/consoles').json
    assert default['devices']==[] and default['installs']==[]
    included=client.get('/api/consoles?include_removed=1').json
    assert included['devices'][0]['hidden']==1 and included['installs'][0]['id']==row['id']
    assert client.get(PREFIX+'/catalog',headers=h).status_code==401
    assert (s.root/row['artifact_id']).exists()  # removing an entry is not deleting its files


def test_dashboard_history_pagination_and_console_filter(setup):
    app,client,s=setup;h,device=pair(app,client,s);release,_=seed_game(s)
    with s.connect() as c:
        for i in range(53):
            c.execute('INSERT INTO console_requests(id,device_id,idem,payload,state,message,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                      (f'{i:032x}',device,f'history-{i}',json.dumps({'release_id':release,'conversion':'server'}),'failed','Synthetic history','2026-11-08T00:00:00+00:00','2026-11-08T00:00:00+00:00'))
    first=client.get('/api/consoles').json;second=client.get('/api/consoles?cursor='+first['next_cursor']).json
    assert len(first['installs'])==50 and len(second['installs'])==3 and second['previous_cursor']=='0'
    assert client.get('/api/consoles?device='+'e'*32).json['installs']==[]
    assert client.get('/api/consoles?cursor=-1').status_code==400


def test_new_management_routes_keep_browser_authentication(tmp_path,monkeypatch):
    monkeypatch.setenv('WORK_DIR',str(tmp_path/'work'));monkeypatch.setenv('OUTPUT_DIR',str(tmp_path/'output'))
    monkeypatch.setenv('APP_AUTH_USER','admin');monkeypatch.setenv('APP_AUTH_PASSWORD','password')
    client=create_app(Config.from_env(),start_background=False).test_client()
    for path in ('/api/consoles/requests/'+'a'*32+'/delete-files','/api/consoles/'+'b'*32+'/remove'):
        assert client.post(path,json={'confirm':True},headers=CSRF).status_code==401
