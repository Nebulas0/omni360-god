import json
import shutil
import zipfile
import pytest
from dataclasses import replace

from package_fixtures import write_god, write_tu
from test_pipeline import make_pipeline, write_stfs
from xboxunity import XboxUnityError
from validation import ValidationError


def queued_job(pipeline, name="Example Game.zip"):
    pipeline.database.upsert_catalog("digital", [{"source_path": "roms/xbox360-digital/"+name,
        "name": name, "size": 0}])
    item = pipeline.database.catalog("digital")[0][0]
    pipeline.database.enqueue_items([item["id"]])
    return pipeline.database.claim_next_job()


def test_reprocessing_keeps_saved_files_and_uses_new_unlock_setting(tmp_path):
    pipeline = make_pipeline(tmp_path)
    source = tmp_path / "source.zip"
    dlc = tmp_path / "input" / "415607E1" / "00000002" / "addon"
    write_stfs(dlc, 2, 0x415607E1, "Example Game")
    with dlc.open("r+b") as f:
        f.seek(0x22C)
        f.write(b"\x01"*256)
    with zipfile.ZipFile(source, "w") as z:
        z.write(dlc, "415607E1/00000002/addon")
    pipeline.remote.download_file.side_effect = lambda _, destination, **kw: shutil.copy2(source, destination)
    # The worker creates source-cache before calling the downloader.
    first = queued_job(pipeline)
    pipeline.run(first)
    saved = pipeline.database.get_job(first["id"])
    assert saved["status"] == "complete", saved["error"]
    snapshot_before = open(saved["snapshot_path"], "rb").read()
    report = json.loads(saved["checks_json"])
    assert report["unlock_enabled"] is True

    pipeline.config.save_unlock_packages(False)
    pipeline.database.reprocess_job(first["id"])
    second = pipeline.database.claim_next_job()
    pipeline.run(second)
    current = pipeline.database.get_job(second["id"])
    assert current["status"] == "complete", current["error"]
    assert open(saved["snapshot_path"], "rb").read() == snapshot_before
    report = json.loads(current["checks_json"])
    assert report["unlock_enabled"] is False
    archive_path = pipeline.database.decode_output_paths(current["output_path"])[0]
    with zipfile.ZipFile(archive_path) as z:
        assert z.read("Content/0000000000000000/415607E1/00000002/addon")[0x22C:0x32C] == b"\x01"*256
        assert b"Hdd1:/Cache/" in z.read("INSTALL-ON-XBOX.txt")
        assert json.loads(z.read("BUNDLE-CHECKS.json"))["unlock_enabled"] is False
    assert pipeline.database.get_bundle_report(archive_path)["unlock_enabled"] is False


def test_update_failure_produces_ready_bundle_with_visible_warning(tmp_path, monkeypatch):
    pipeline = make_pipeline(tmp_path)
    pipeline.config = replace(pipeline.config, xboxunity_tu_downloads=True)
    source = tmp_path / "source.zip"
    title = tmp_path / "input" / "415607E1"
    header = write_god(title)
    with zipfile.ZipFile(source, "w") as z:
        for path in title.rglob("*"):
            if path.is_file():
                z.write(path, path.relative_to(title.parent))
    pipeline.remote.download_file.side_effect = lambda _, destination, **kw: shutil.copy2(source, destination)
    class Client:
        def __init__(self, **kw): pass
        def get_title_updates(self, title_id): raise XboxUnityError("Test service unavailable")
    monkeypatch.setattr("pipeline.XboxUnityClient", Client)
    job = queued_job(pipeline)
    pipeline.run(job)
    result = pipeline.database.get_job(job["id"])
    assert result["status"] == "complete", result["error"]
    assert "with warnings" in result["message"]
    report = json.loads(result["checks_json"])
    assert report["warnings"]
    assert report["checks"][1]["state"] == "warning"
    assert report["titles"][0]["games"][0]["level"] == "full_data"


def test_reprocessing_replaces_entire_fragment_set(tmp_path):
    pipeline = make_pipeline(tmp_path)
    source = tmp_path / "new.data"
    target = tmp_path / "output.data"
    source.mkdir(); target.mkdir()
    (source / "Data0000").write_bytes(b"new")
    (target / "Data0000").write_bytes(b"old")
    (target / "Data0001").write_bytes(b"stale")
    pipeline._merge_tree(source, target)
    assert [p.name for p in target.iterdir()] == ["Data0000"]
    assert (target / "Data0000").read_bytes() == b"new"


def test_imported_incompatible_update_cannot_overwrite_existing_copy(tmp_path):
    pipeline = make_pipeline(tmp_path)
    title = pipeline.config.output_dir / "Example Game" / "415607E1"
    write_god(title)
    pipeline.database.put_title("415607E1", "Example Game", str(title))
    existing = title / "000B0000" / "tu00000001_00000000"
    original = write_tu(existing, base_version=2)
    source = tmp_path / "incoming"
    write_tu(source / "415607E1/000B0000/tu00000001_00000000", base_version=3)
    with pytest.raises(ValidationError, match="base version"):
        pipeline._process_digital_archive(1, {"name":"Example Game.zip"}, source)
    assert existing.read_bytes() == original


def test_job_unlock_setting_does_not_modify_unrelated_addons(tmp_path):
    pipeline = make_pipeline(tmp_path)
    output = pipeline.config.output_dir / "Example Game" / "415607E1"
    old = output / "00000002" / "existing-addon"
    fresh = output / "00000002" / "new-addon"
    for path in (old, fresh):
        write_stfs(path, 2, 0x415607E1, "Example Game")
        with path.open("r+b") as f:
            f.seek(0x22C); f.write(b"\x01"*256)
    before = old.read_bytes()
    pipeline._artifact_paths = {fresh.resolve()}
    pipeline._unlock_output_dirs(1, [output], pipeline._job_dir(1), enabled=True, current_job_only=True)
    assert old.read_bytes() == before
    assert fresh.read_bytes()[0x22C:0x32C] == bytes(256)
    assert (pipeline._job_dir(1) / "unlock-backups/415607E1/00000002/new-addon.x360lock.bak").exists()
