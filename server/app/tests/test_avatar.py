import hashlib
import json

import pytest
from console_api import PREFIX
from formats import CONTENT_AVATAR, parse_stfs_header, package_kind
from unlock import unlock_package
from test_console import setup, pair, CSRF, export_fixture
from package_fixtures import write_tu


def avatar_file(path,number=1,title=0x415607E1):
    data=bytearray(write_tu(path,title_id=title))
    data[0x344:0x348]=CONTENT_AVATAR.to_bytes(4,'big')
    data[0x3E1:0x3F1]=number.to_bytes(12,'big')+title.to_bytes(4,'big')
    data[0x233:0x23C]=b'\xff'+bytes(7)+b'\x01'
    data[0x32C:0x340]=hashlib.sha1(data[0x344:0xB000]).digest()
    path.write_bytes(data);return bytes(data)


@pytest.mark.parametrize('unlock',[False,True])
def test_avatar_pack_33_items_preserves_server_licenses_and_prepares_for_xbox(setup,unlock):
    app,client,s=setup;h,device=pair(app,client,s)
    name='Synthetic Avatar Items (World) (Addon).zip'
    s.database.upsert_catalog('digital',[{'name':name,'source_path':'digital/avatar.zip','size':100}])
    item=s.database.catalog('digital','',1,10)[0][0];jid=s.database.enqueue_items([item['id']])[0]['job_id']
    root=s.config.output_dir/'Synthetic'/'415607E1';s.database.put_title('415607E1','Synthetic',str(root));paths=[];originals={}
    for i in range(33):
        path=root/'00009000'/f'{i+1:024X}415607E1';originals[str(path)]=avatar_file(path,i+1);paths.append(str(path))
    with s.connect() as c:
        c.execute("UPDATE jobs SET status='complete',artifact_paths=? WHERE id=?",(json.dumps(paths),jid))
        c.execute("UPDATE catalog_items SET status='complete',title_id='415607E1' WHERE id=?",(item['id'],))
    r=client.post(f'/api/consoles/{device}/prepare',headers=CSRF,json={'request_key':'avatar-prepare-request-0001','selection':{'release_id':f"catalog-{item['id']}",'unlock_packages':unlock}})
    assert r.status_code==200,r.json
    for _ in range(2):s.prepare(s.get_request(r.json['id']))
    row=s.get_request(r.json['id']);assert row['state']=='ready'
    manifest=client.get(f"{PREFIX}/artifacts/{row['artifact_id']}/manifest",headers=h).json
    assert len(manifest['components'])==33
    for c in manifest['components']:
        assert c['kind']=='avatar' and c['content_type']=='00009000' and c['avatar_unlock']==unlock
        assert c['license_state']==('on_console' if unlock else 'preserved') and not c['unlock_applied']
        f=c['files'][0];assert f['relative_path'].startswith('415607E1/00009000/')
        staged=s.root/row['artifact_id']/f['root']/f['relative_path'];assert staged.read_bytes()==originals[str(root/'00009000'/staged.name)]
        assert hashlib.sha256(staged.read_bytes()).hexdigest()==f['sha256']
    inbox=client.get(PREFIX+'/inbox',headers=h).json['items'][0]
    assert inbox['contains_avatar'] is True and inbox['avatar_unlock']==unlock
    if unlock:export_fixture('avatar.json',manifest)


def test_generic_unlock_does_not_erase_avatar_ownership(tmp_path):
    path=tmp_path/'avatar';original=avatar_file(path)
    assert package_kind(parse_stfs_header(path))=='avatar'
    result=unlock_package(path,tmp_path/'backups')
    assert result.skipped and not result.unlocked and path.read_bytes()==original
    assert not (tmp_path/'backups').exists()
