from db import Database
from store_browser import StoreBrowser, is_addon_source


def test_store_groups_sources_and_matches_only_the_parent_dlcs(tmp_path):
    db = Database(tmp_path / 'catalog.sqlite3'); db.initialize()
    names = ['Fruit Ninja Kinect (Europe).zip', 'Fruit Ninja Kinect (USA).zip',
             'Fruit Ninja Kinect - High Tech Vault (World) (Addon).zip',
             'Fruit Ninja Kinect 2 - Extras (World) (Addon).zip', 'Unmapped Game.zip']
    db.upsert_catalog('disc', [{'name': n, 'source_path': n, 'size': 1} for n in names])
    metadata = [{'name': 'Fruit Ninja Kinect', 'kind': 'digital', 'title_id': '58410A8E'},
                {'name': 'Fruit Ninja Kinect 2', 'kind': 'game', 'title_id': '12345678'}]
    store = StoreBrowser(db, metadata)
    games = store.browse('game')['items']
    assert len(games) == 2 and next(g for g in games if g['name'] == 'Fruit Ninja Kinect')['release_count'] == 2
    unknown = next(g for g in games if g['name'] == 'Unmapped Game')
    assert unknown['art_title_id'] is None
    dlcs = store.browse('dlc', title_id='58410A8E', parent_name='Fruit Ninja Kinect')['items']
    assert len(dlcs) == 1 and 'High Tech Vault' in dlcs[0]['name']
    assert dlcs[0]['art_title_id'] == '58410A8E' and dlcs[0]['kind'] == 'digital'
    assert len(store.releases(dlcs[0]['group_id'])['releases']) == 1
    assert dlcs[0]['default_release_id'] == store.releases(dlcs[0]['group_id'])['releases'][0]['release_id']
    assert '(Addon)' in dlcs[0]['default_source_name']
    assert not store.browse('dlc', title_id='FFFFFFFF', parent_name='Fruit Ninja Kinect')['items']
    assert is_addon_source('Game (addon).ZIP') and not is_addon_source('Game Addon Edition.zip')
    assert store.home()['game_count'] == 2


def test_store_pagination_and_ambiguous_artwork(tmp_path):
    db = Database(tmp_path / 'catalog.sqlite3'); db.initialize()
    db.upsert_catalog('disc', [{'name': f'Game {i:03d}.zip', 'source_path': f'{i}.zip', 'size': 1} for i in range(55)])
    store = StoreBrowser(db, [{'name': 'Game 000', 'kind': 'game', 'title_id': t} for t in ['12345678', '87654321']])
    first = store.browse(); second = store.browse(offset=48)
    assert first['total'] == 55 and len(first['items']) == 48 and first['next_cursor'] == '48'
    assert len(second['items']) == 7 and second['previous_cursor'] == '0' and second['next_cursor'] is None
    # Exact same-name collisions must not choose another game's cover.
    assert first['items'][0]['name'] == 'Game 000'
    assert first['items'][0]['art_title_id'] is None
