import json

from console_api import PREFIX
from test_console import setup, pair, seed_addon_sources
from test_prepared_inbox import seed_game, submit, prepare_ready


def test_inbox_groups_selected_addons_with_exact_game_and_carries_artwork(setup):
    app,client,s=setup;headers,device=pair(app,client,s)
    release,god=seed_game(s);addons,paths=seed_addon_sources(s,god,3)
    base=submit(client,device,release,key='group-game-request-0001').json['id'];prepare_ready(s,base)
    chosen=[]
    for i,r in enumerate(addons[:2]):
        rid=submit(client,device,r,key=f'group-addon-request-{i:04d}').json['id'];prepare_ready(s,rid);chosen.append(rid)
    rows=client.get(PREFIX+'/inbox',headers=headers).json['items']
    assert len(rows)==3 and {r['group_id'] for r in rows}=={base}
    assert {r['id'] for r in rows}=={base,*chosen}
    assert all(r['art_title_id']=='415607E1' and r['title_id']=='415607E1' for r in rows)
    assert next(r for r in rows if r['id']==base)['content_kind']=='game'
    assert all(r['content_kind']=='addon' and r['base_request_id']==base and not r['wait_for_base'] for r in rows if r['id']!=base)
    # Requesting a known game that is still preparing holds its add-ons.
    s.update(base,state='waiting')
    rows=client.get(PREFIX+'/inbox',headers=headers).json['items']
    assert all(r['wait_for_base'] for r in rows if r['id']!=base)
    # After an installed report, remaining add-ons are a single independent group.
    client.post(f'{PREFIX}/install-requests/{base}/events',headers=headers,json={'event_id':base+'_installed','outcome':'installed'})
    rows=client.get(PREFIX+'/inbox',headers=headers).json['items']
    assert len(rows)==2 and len({r['group_id'] for r in rows})==1
    assert all('base_request_id' not in r and 'add-ons' in r['group_name'] for r in rows)


def test_grouping_does_not_guess_from_names_or_merge_ambiguous_base_editions(setup):
    app,client,s=setup
    def row(i,kind,tid):return {'id':str(i)*32,'name':'Identical browsing name','content_kind':kind,'title_id':tid,'art_title_id':'415607E1','state':'ready'}
    rows=[row(1,'game','415607E1'),row(2,'game','415607E1'),row(3,'addon','415607E1'),row(4,'addon','57520802'),row(5,'addon',None)]
    s.group_inbox(rows)
    assert rows[0]['group_id']!=rows[1]['group_id']
    assert rows[2]['group_id'] not in {rows[0]['group_id'],rows[1]['group_id']}
    assert len({r['group_id'] for r in rows})==5
    assert all('base_request_id' not in r for r in rows)
