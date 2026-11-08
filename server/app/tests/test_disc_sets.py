import json
import hashlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from compatibility import compatibility_rule
from console_api import ConsoleError, PREFIX, ConsoleService
from disc_sets import disc_info, disc_plan, known_disc_sets
from package_fixtures import write_god, write_tu
from test_console import setup, pair
from test_pipeline import make_pipeline, write_stfs


def source(i, name): return {"release_id": f"catalog-{i}", "name": name, "source_bytes": "1"}


def test_disc_pairing_preserves_region_language_revision_and_reports_missing():
    names = ["Example (USA) (En,Fr) (Disc 1).zip", "Example (USA) (En,Fr) (Disc 2).zip",
             "Example (Europe) (En,Fr) (Disc 2).zip", "Example (USA) (En,Fr) (Disc 2) (Rev 1).zip"]
    rows = [source(i+1, n) for i, n in enumerate(names)]
    known = known_disc_sets([{"disc_sets": [names[:2]]}])
    plan = disc_plan(rows, 'catalog-1', known)
    assert [r['release_id'] for r in plan['items']] == ['catalog-1', 'catalog-2']
    assert plan['expected_count'] == 2 and plan['missing_count'] == 0
    assert disc_plan(rows[:1], 'catalog-1', known)['missing_count'] == 1
    assert disc_plan(rows, 'catalog-4', known)['items'] == [{**rows[3], **disc_info(names[3])}]


def test_game_specific_profiles_and_ambiguous_source_are_conservative():
    assert disc_info('Grand Theft Auto V (World) (Disc 1).zip')['disc_role'] == 'content'
    assert disc_info('Grand Theft Auto V (World) (Disc 2).zip')['disc_role'] == 'play'
    assert disc_info("Tom Clancy's Splinter Cell - Blacklist (USA) (Disc 2).zip")['disc_role'] == 'mixed'
    assert disc_info('Watch_Dogs (World) (Disc 2).zip')['recommended'] is False
    assert disc_info("Assassin's Creed IV - Black Flag (World) (Disc 2).zip")['recommended'] is False
    assert 'launch Disc 1 once' in disc_info('Wolfenstein - The New Order (World) (Disc 1).zip')['disc_note']
    for game in ('Mass Effect 2', 'Mass Effect 3'):
        assert compatibility_rule(game + ' (World) (Disc 2).zip') is None
    rows = [source(1, 'Example (USA) (Disc 1).zip'), source(2, 'Example (USA) (Disc 1).zip'), source(3, 'Example (USA) (Disc 2).zip')]
    assert len(disc_plan(rows, 'catalog-1')['items']) == 1


def test_localized_disc_numbers_pair_and_apply_the_same_content_rules():
    rows=[source(1,'Mass Effect 2 (Canada) (Disque 1).zip'),source(2,'Mass Effect 2 (Canada) (Disque 2).zip')]
    assert [r['disc_number'] for r in disc_plan(rows,'catalog-2')['items']]==[1,2]
    assert disc_info('Grand Theft Auto V (World) (Disco 1).zip')['disc_role']=='content'


def seed_discs(service):
    names = [f'Synthetic game (World) (Disc {i}).zip' for i in (1, 2)]
    service.database.upsert_catalog('disc', [{'name':n, 'source_path':n, 'size':1} for n in names])
    root = service.config.output_dir / 'Synthetic game' / '415607E1'
    service.database.put_title('415607E1', 'Synthetic game', str(root))
    ids = []
    for i, name in enumerate(names, 1):
        with service.connect() as c: item_id = c.execute('SELECT id FROM catalog_items WHERE name=?',(name,)).fetchone()[0]
        job = service.database.enqueue_items([item_id], options={'unlock_packages':True})[0]
        header = write_god(root, media_id=0x12345678+i, disc_number=i, disc_count=2)
        files = [str(header), str(header.with_name(header.name+'.data')/'Data0000')]
        with service.connect() as c:
            c.execute("UPDATE jobs SET status='complete',artifact_paths=? WHERE id=?",(json.dumps(files), job['job_id']))
            c.execute("UPDATE catalog_items SET status='complete',title_id='415607E1' WHERE id=?",(item_id,))
        ids.append(f'catalog-{item_id}')
    return ids


def test_disc_set_one_manifest_all_files_and_idempotent_replay(setup):
    app, client, s = setup; h, _ = pair(app, client, s); ids = seed_discs(s)
    body = {'kind':'disc_bundle', 'release_ids':ids[::-1], 'conversion':'server', 'unlock_packages':False}
    headers = {**h, 'Idempotency-Key':'disc-set-install-test'}
    result = client.post(PREFIX+'/install-requests',json=body,headers=headers)
    assert result.status_code == 200, result.json
    assert client.post(PREFIX+'/install-requests',json=result.json['selection'],headers=headers).json['id'] == result.json['id']
    s.prepare(s.get_request(result.json['id'])); row = s.get_request(result.json['id']); assert row['state'] == 'ready'
    manifest = client.get(f"{PREFIX}/artifacts/{row['artifact_id']}/manifest",headers=h).json
    assert manifest['release_ids'] == sorted(ids)
    assert [c['disc_number'] for c in manifest['components']] == [1, 2]
    assert len(manifest['components']) == 2 and sum(len(c['files']) for c in manifest['components']) == 4
    assert all(c['kind']=='god' and not c['unlock_applied'] for c in manifest['components'])
    for component in manifest['components']:
        for file in component['files']: assert client.get(file['download_path'],headers=h).status_code == 200
    export = __import__('test_console').export_fixture
    export('multi-disc.json', manifest)


@pytest.mark.parametrize('unlock', [False, True])
def test_disc_set_manual_subset_and_preparation_recovery(setup, unlock):
    app, client, s = setup; h, _ = pair(app, client, s); ids = seed_discs(s)
    with s.connect() as c:
        second = c.execute('SELECT last_job_id FROM catalog_items WHERE id=?',(int(ids[1][8:]),)).fetchone()[0]
        c.execute("UPDATE jobs SET status='queued' WHERE id=?",(second,))
        c.execute("UPDATE catalog_items SET status='queued' WHERE id=?",(int(ids[1][8:]),))
    result = client.post(PREFIX+'/install-requests',json={'kind':'disc_bundle','release_ids':ids,'unlock_packages':unlock},headers={**h,'Idempotency-Key':'disc-restart-test-key'})
    s.prepare(s.get_request(result.json['id'])); row = s.get_request(result.json['id']); assert row['state']=='waiting' and '1 of 2' in row['message']
    with s.connect() as c:
        jobs = c.execute('SELECT count(*) FROM jobs').fetchone()[0];c.execute("UPDATE jobs SET status='complete' WHERE id=?",(second,))
        c.execute("UPDATE catalog_items SET status='complete' WHERE id=?",(int(ids[1][8:]),))
    restarted = ConsoleService(s.config,s.database,s.runner,s.remote)
    restarted.prepare(restarted.get_request(row['id'])); assert restarted.get_request(row['id'])['state']=='ready'
    with s.connect() as c: assert c.execute('SELECT count(*) FROM jobs').fetchone()[0] == jobs
    subset = client.post(PREFIX+'/install-requests',json={'kind':'disc_bundle','release_ids':[ids[1]]},headers={**h,'Idempotency-Key':'disc-manual-subset-key'})
    assert subset.status_code==200; s.prepare(s.get_request(subset.json['id'])); assert s.get_request(subset.json['id'])['state']=='ready'


@pytest.mark.parametrize('change', ['region','revision','console','wrong-disc'])
def test_disc_set_rejects_mixed_editions_modes_and_wrong_disc(setup, change):
    app, client, s = setup; h, _ = pair(app, client, s); ids=seed_discs(s)
    body={'kind':'disc_bundle','release_ids':ids}
    if change in {'region','revision'}:
        name='Synthetic game (Europe) (Disc 2).zip' if change=='region' else 'Synthetic game (World) (Disc 2) (Rev 1).zip'
        with s.connect() as c: c.execute('UPDATE catalog_items SET name=? WHERE id=?',(name,int(ids[1][8:])))
    if change=='console':body['conversion']='console'
    if change=='wrong-disc':
        root=s.config.output_dir/'Synthetic game'/'415607E1';__import__('shutil').rmtree(root/'00007000'/'1234567A.data');write_god(root,media_id=0x1234567A,disc_number=1,disc_count=2)
    result=client.post(PREFIX+'/install-requests',json=body,headers={**h,'Idempotency-Key':'disc-rejection-test'})
    if change=='wrong-disc':
        assert result.status_code==200
        with pytest.raises(ConsoleError,match='disc number'): s.prepare(s.get_request(result.json['id']))
    else: assert result.status_code in (400,409),result.json
    with s.connect() as c: assert c.execute('SELECT count(*) FROM console_artifacts').fetchone()[0]==0


def test_mixed_disc_extracts_content_and_keeps_playable_god(tmp_path):
    pipeline=make_pipeline(tmp_path); iso=tmp_path/'input'/'disc.iso';iso.parent.mkdir();iso.write_bytes(b'ISO')
    root=pipeline.config.output_dir/'Game'/'555308B6'; extracted=tmp_path/'extracted';extracted.mkdir()
    pipeline._inspect_iso_layout=MagicMock(return_value={'is_content_disc':True})
    pipeline._extract_content_disc=MagicMock(return_value=extracted)
    pipeline._content_tree_root=MagicMock(return_value=extracted)
    pipeline._process_digital_archive=MagicMock(return_value=[root])
    converted=tmp_path/'god'/'555308B6';converted.mkdir(parents=True)
    pipeline._run_converter=MagicMock(return_value=(converted,'Game'));pipeline._merge_tree=MagicMock();pipeline._update=MagicMock()
    result=pipeline._process_disc_archive(1,{'name':"Tom Clancy's Splinter Cell - Blacklist (World) (Disc 2).zip"},iso.parent)
    assert pipeline._process_digital_archive.call_count==1 and pipeline._run_converter.call_count==1
    assert result


@pytest.mark.parametrize('unlock', [False, True])
def test_install_disc_is_dlc_not_a_second_god_and_respects_license_choice(setup, unlock):
    app,client,s=setup;h,_=pair(app,client,s);ids=seed_discs(s)
    root=s.config.output_dir/'Synthetic game'/'415607E1';package=root/'00000002'/'install-content'
    raw=bytearray(write_tu(package));raw[0x344:0x348]=(2).to_bytes(4,'big');raw[0x32C:0x340]=hashlib.sha1(raw[0x344:0xB000]).digest();package.write_bytes(raw)
    original=package.read_bytes();god=(root/'00007000'/'12345679').read_bytes()
    with s.connect() as c:
        c.execute("UPDATE catalog_items SET name='Synthetic game (World) (Disc 2) (Install Disc).zip' WHERE id=?",(int(ids[1][8:]),))
        c.execute('UPDATE jobs SET artifact_paths=? WHERE id=(SELECT last_job_id FROM catalog_items WHERE id=?)',(json.dumps([str(package)]),int(ids[1][8:])))
    result=client.post(PREFIX+'/install-requests',json={'kind':'disc_bundle','release_ids':ids,'unlock_packages':unlock},headers={**h,'Idempotency-Key':'install-play-disc-test'})
    assert result.status_code==200,result.json;s.prepare(s.get_request(result.json['id']));row=s.get_request(result.json['id']);assert row['state']=='ready'
    manifest=client.get(f"{PREFIX}/artifacts/{row['artifact_id']}/manifest",headers=h).json
    assert sorted(c['kind'] for c in manifest['components'])==['dlc','god']
    dlc=next(c for c in manifest['components'] if c['kind']=='dlc');assert dlc['files'][0]['relative_path']=='415607E1/00000002/install-content'
    assert dlc['unlock_applied'] is unlock
    assert package.read_bytes()==original and (root/'00007000'/'12345679').read_bytes()==god
    # Disc-selection hints also appear in the real native Store endpoint.
    page=client.get(PREFIX+'/store/titles?kind=game&cursor=0',headers=h).json
    detail=client.get(PREFIX+'/store/titles/'+page['items'][0]['group_id']+'?cursor=0',headers=h).json
    plan=detail['releases'][0]['disc_set'];assert len(plan['items'])==2


def test_legacy_mixed_disc_missing_god_is_rebuilt_once_before_publication(setup):
    app,client,s=setup;h,_=pair(app,client,s);ids=seed_discs(s)
    root=s.config.output_dir/'Synthetic game'/'415607E1';package=root/'00000002'/'mixed-content'
    raw=bytearray(write_tu(package));raw[0x344:0x348]=(2).to_bytes(4,'big');raw[0x32C:0x340]=hashlib.sha1(raw[0x344:0xB000]).digest();package.write_bytes(raw)
    with s.connect() as c:
        for i,release in enumerate(ids,1):c.execute('UPDATE catalog_items SET name=? WHERE id=?',(f"Tom Clancy's Splinter Cell - Blacklist (World) (Disc {i}).zip",int(release[8:])))
        c.execute('UPDATE jobs SET artifact_paths=? WHERE id=(SELECT last_job_id FROM catalog_items WHERE id=?)',(json.dumps([str(package)]),int(ids[1][8:])))
    result=client.post(PREFIX+'/install-requests',json={'kind':'disc_bundle','release_ids':ids,'unlock_packages':False},headers={**h,'Idempotency-Key':'mixed-disc-rebuild-test'})
    assert result.status_code==200,result.json;s.prepare(s.get_request(result.json['id']))
    assert s.get_request(result.json['id'])['state']=='waiting'
    with s.connect() as c:
        assert c.execute('SELECT count(*) FROM console_artifacts').fetchone()[0]==0
        job=c.execute('SELECT job_id FROM console_request_sources WHERE request_id=? AND release_id=?',(result.json['id'],ids[1])).fetchone()[0]
        assert c.execute('SELECT reprocess_of FROM jobs WHERE id=?',(job,)).fetchone()[0] is not None
        god=root/'00007000'/'1234567A'
        c.execute("UPDATE jobs SET status='complete',artifact_paths=? WHERE id=?",(json.dumps([str(package),str(god),str(god.with_name(god.name+'.data')/'Data0000')]),job))
        c.execute("UPDATE catalog_items SET status='complete' WHERE id=?",(int(ids[1][8:]),))
    s.prepare(s.get_request(result.json['id']));assert s.get_request(result.json['id'])['state']=='ready'
