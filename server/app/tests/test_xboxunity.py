from __future__ import annotations

from pathlib import Path

from xboxunity import TitleUpdateEntry, XboxUnityClient
from xboxunity import XboxUnityError
import pytest


def entry(media_id: str, version: int, update_id: str) -> TitleUpdateEntry:
    return TitleUpdateEntry(
        title_update_id=update_id,
        version=version,
        media_id=media_id,
        name="Example",
        size=0,
        upload_date="2026-01-01",
        base_version="00000001",
        sha1_hash="",
    )


def test_select_latest_title_update_requires_matching_media_id() -> None:
    updates = [
        entry("AAAABBBB", 9, "wrong-media"),
        entry("11112222", 3, "matching-old"),
        entry("11112222", 4, "matching-new"),
    ]

    selected = XboxUnityClient.select_latest_for_media_id(updates, "11112222")

    assert selected is not None
    assert selected.title_update_id == "matching-new"
    assert XboxUnityClient.select_latest_for_media_id(updates, "CCCCDDDD") is None


def test_title_update_download_replaces_target_atomically(tmp_path: Path) -> None:
    from package_fixtures import write_tu
    payload = write_tu(tmp_path / "original", media_id=0x11112222, base_version=1)
    class Response:
        status_code = 200
        headers = {
            "Content-Disposition": 'attachment; filename="tu00000001_00000000"',
            "Content-Length": str(len(payload)),
        }

        def iter_content(self, chunk_size: int):
            del chunk_size
            yield payload

        def close(self) -> None:
            pass

    class Session:
        def get(self, *args, **kwargs):
            del args, kwargs
            return Response()

    client = XboxUnityClient(timeout=1, retries=1)
    client.session = Session()
    path = client.download_title_update(entry("11112222", 1, "1"), tmp_path)

    assert path.read_bytes() == payload
    assert not path.with_name(path.name + ".partial").exists()


def test_selection_requires_matching_base_version():
    old = entry("11112222", 3, "compatible")
    incompatible = entry("11112222", 9, "wrong-base")
    incompatible.base_version = "00000002"
    chosen = XboxUnityClient.select_latest_for_media_id([incompatible, old], "11112222", "00000001")
    assert chosen.title_update_id == "compatible"
    assert XboxUnityClient.select_latest_for_media_id([incompatible], "11112222", "00000001") is None


def test_parse_flat_updates_and_kib_size():
    entries = XboxUnityClient()._parse_updates({"Type": 2, "Updates": [
        {"TitleUpdateID": "1", "MediaID": "12345678", "Size": "1272", "Version": "3", "BaseVersion": "00000002"}
    ]}, "415607E1")
    assert entries[0].size == 1302528
    assert entries[0].media_id == "12345678"
    assert entries[0].title_id == "415607E1"


@pytest.mark.parametrize("problem", ["content_id", "base_version", "length", "md5", "filename"])
def test_bad_download_does_not_replace_existing_update(tmp_path, problem):
    from package_fixtures import write_tu
    raw = write_tu(tmp_path / "fixture", media_id=0x11112222, base_version=1)
    update = entry("11112222", 1, "1")
    update.sha1_hash = raw[0x32C:0x340].hex()
    if problem == "content_id": update.sha1_hash = "00"*20
    if problem == "base_version": update.base_version = "00000002"
    name = "tu00000001_00000000"
    original = tmp_path / "out" / name
    original.parent.mkdir()
    original.write_bytes(b"keep original")
    class Response:
        status_code = 200
        headers = {"Content-Disposition": f'attachment; filename="{name}"', "Content-Length": str(len(raw))}
        def iter_content(self, chunk_size): yield raw
        def close(self): pass
    response = Response()
    if problem == "length": response.headers["Content-Length"] = str(len(raw)+1)
    if problem == "md5": response.headers["X-Content-Md5"] = "00"*16
    if problem == "filename": response.headers["Content-Disposition"] = 'attachment; filename="../outside"'
    class Session:
        def get(self, *args, **kwargs): return response
    client = XboxUnityClient(timeout=1, retries=1)
    client.session = Session()
    with pytest.raises(XboxUnityError):
        client.download_title_update(update, original.parent)
    assert original.read_bytes() == b"keep original"
    assert not list(original.parent.glob("*.partial"))
