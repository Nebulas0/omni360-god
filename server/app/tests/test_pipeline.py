from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from config import Config
from db import Database
from pipeline import Pipeline


def write_stfs(path: Path, content_type: int, title_id: int, title: str) -> None:
    header = bytearray(0x1800)
    header[:4] = b"LIVE"
    header[0x344:0x348] = content_type.to_bytes(4, "big")
    header[0x354:0x358] = (0x12345678).to_bytes(4, "big")
    header[0x360:0x364] = title_id.to_bytes(4, "big")
    encoded = title.encode("utf-16-be")
    header[0x1691:0x1691 + len(encoded)] = encoded
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + b"payload")


def make_pipeline(tmp_path: Path) -> Pipeline:
    work = tmp_path / "work"
    config = Config(
        remote_base_url="https://example.invalid",
        remote_api_key="unused",
        remote_api_key_file=work / "remote-api-key",
        remote_auth_mode="api-key",
        remote_auth_mode_file=work / "remote-auth-mode",
        remote_basic_user="",
        remote_basic_password="",
        remote_basic_file=work / "remote-basic",
        disc_source_path="roms/xbox360",
        digital_source_path="roms/xbox360-digital",
        output_dir=tmp_path / "output",
        work_dir=work,
        db_path=work / "state.sqlite3",
        extractor_path=tmp_path / "extract-xiso",
        converter_path=tmp_path / "iso2god",
        converter_threads=1,
        remote_timeout=10,
        remote_retries=1,
        keep_source_archives=True,
        unlock_packages=True,
        xboxunity_tu_downloads=False,
        download_game_icons=False,
        max_zip_members=100,
        app_auth_user="",
        app_auth_password="",
        public_host="test.local",
    )
    config.ensure_directories()
    database = Database(config.db_path)
    database.initialize()
    remote = MagicMock()
    return Pipeline(config, database, remote)


def test_digital_package_layout_and_zip(tmp_path: Path) -> None:
    pipeline = make_pipeline(tmp_path)
    extracted = tmp_path / "extracted"
    package = extracted / "Content" / "415607E1" / "00000002" / "dlc-package"
    write_stfs(package, 0x00000002, 0x415607E1, "Example Game")
    god = extracted / "Content" / "415607E1" / "00007000" / "media-id"
    write_stfs(god, 0x00007000, 0x415607E1, "Example Game")
    data_dir = god.parent / "media-id.data"
    data_dir.mkdir()
    (data_dir / "Data0000").write_bytes(b"data")

    job = {"id": 1, "item_id": 1, "name": "Example Game (DLC).zip"}
    outputs = pipeline._process_digital_archive(1, job, extracted)
    assert len(outputs) == 1
    game_root = outputs[0]
    assert game_root == tmp_path / "output" / "Example Game" / "415607E1"
    assert (game_root / "00000002" / "dlc-package").is_file()
    assert (game_root / "00007000" / "media-id").is_file()
    assert (game_root / "00007000" / "media-id.data" / "Data0000").is_file()

    job_dir = pipeline._job_dir(1)
    archive = pipeline._make_bundle_zip(1, job, outputs, job_dir)
    assert archive.name == "Example Game [415607E1].zip"
    assert archive.is_file()


def test_god_media_id_is_used_for_title_update_matching(tmp_path: Path) -> None:
    pipeline = make_pipeline(tmp_path)
    output_dir = tmp_path / "output" / "Example Game" / "415607E1"
    god = output_dir / "00007000" / "media-id"
    write_stfs(god, 0x00007000, 0x415607E1, "Example Game")

    assert pipeline._god_media_id(output_dir) == "12345678"


def test_normalize_title_updates_removes_mismatches_and_duplicates(
    tmp_path: Path,
) -> None:
    pipeline = make_pipeline(tmp_path)
    output_dir = tmp_path / "output" / "Example Game" / "415607E1"
    from package_fixtures import write_god, write_tu
    write_god(output_dir)
    old_tu = output_dir / "000B0000" / "tu00000001_00000000"
    new_tu = output_dir / "000B0000" / "tu00000004_00000000"
    wrong_tu = output_dir / "000B0000" / "tu00000009_00000000"
    cache_tu = output_dir / "000B0000" / "TU_16TEST_0000000000000.0000000000001"
    for path, content_type, title_id in (
        (old_tu, 0x000B0000, 0x415607E1),
        (new_tu, 0x000B0000, 0x415607E1),
        (wrong_tu, 0x000B0000, 0x415607E1),
        (cache_tu, 0x000B0000, 0x415607E1),
    ):
        write_tu(path, title_id=title_id, version=4 if path == new_tu else 1)
    with wrong_tu.open("r+b") as source:
        source.seek(0x354)
        source.write((0x87654321).to_bytes(4, "big"))

    job_dir = pipeline._job_dir(2)
    import hashlib
    pipeline._tu_results = [{"title_id": "415607E1", "media_id": "12345678", "status": "verified",
        "content_id": new_tu.read_bytes()[0x32C:0x340].hex().upper()}]
    pipeline._normalize_title_updates([output_dir], job_dir)

    assert not old_tu.exists()
    assert new_tu.exists()
    assert not wrong_tu.exists()
    assert not cache_tu.exists()
    assert not (output_dir / "Cache" / cache_tu.name).exists()
    assert (job_dir / "title-update-backups" / "415607E1" / "000B0000" / wrong_tu.name).exists()


def test_retry_reuses_previous_source_archive(tmp_path: Path) -> None:
    pipeline = make_pipeline(tmp_path)
    previous_dir = pipeline.config.work_dir / "jobs" / "00000001"
    previous_dir.mkdir(parents=True)
    source = previous_dir / "source.zip"
    source.write_bytes(b"archive")
    (previous_dir / "job.json").write_text(
        '{"source_path": "roms/xbox360/retry.zip"}', encoding="utf-8"
    )
    current_dir = pipeline.config.work_dir / "jobs" / "00000002"
    current_dir.mkdir(parents=True)
    selected = {
        "item_id": 2,
        "source_path": "roms/xbox360/retry.zip",
        "remote_size": 0,
    }
    assert pipeline._source_archive_path(selected, current_dir) == source


def test_job_snapshot_contains_only_this_job_artifacts(tmp_path: Path) -> None:
    pipeline = make_pipeline(tmp_path)
    output = tmp_path / "output" / "Example Game" / "415607E1" / "00000002"
    output.mkdir(parents=True)
    package = output / "addon-package"
    package.write_bytes(b"addon")
    pipeline._artifact_paths = {package}
    job_dir = pipeline._job_dir(9)
    snapshot, name = pipeline._make_snapshot_zip(
        9,
        {"name": "Example Game (USA) (En,Fr).zip"},
        job_dir,
    )
    assert snapshot is not None
    assert name == "Example Game (USA) (En,Fr) [job-9].zip"
    import zipfile
    with zipfile.ZipFile(snapshot) as archive:
        assert archive.namelist() == ["Example Game/415607E1/00000002/addon-package"]


def test_install_disc_with_content_folder_is_not_treated_as_god(tmp_path: Path) -> None:
    pipeline = make_pipeline(tmp_path)
    extractor = tmp_path / "extract-xiso"
    extractor.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' '/Content/0000000000000000/57520802/00000002/pkg' '/Default.xex'\n",
        encoding="utf-8",
    )
    extractor.chmod(0o755)
    pipeline.config = pipeline.config.__class__(
        **{**pipeline.config.__dict__, "extractor_path": extractor}
    )
    result = pipeline._inspect_iso_layout(tmp_path / "disc.iso")
    assert result["has_default_xex"] is True
    assert result["has_install_content_path"] is True
    assert result["is_content_disc"] is True


def test_content_tree_root_excludes_raw_disc_files(tmp_path: Path) -> None:
    extracted = tmp_path / "disc"
    extracted.mkdir()
    (extracted / "Default.xex").write_bytes(b"x")
    content = extracted / "Content" / "0000000000000000"
    content.mkdir(parents=True)
    assert Pipeline._content_tree_root(extracted) == extracted / "Content"


def test_unlock_switch_only_changes_digital_content_and_keeps_backups(tmp_path):
    pipeline = make_pipeline(tmp_path)
    output = pipeline.config.output_dir / "Game" / "415607E1"
    dlc = output / "00000002" / "addon"
    xbla = output / "000D0000" / "arcade"
    god = output / "00007000" / "12345678"
    for path, kind in ((dlc, 2), (xbla, 0xD0000), (god, 0x7000)):
        write_stfs(path, kind, 0x415607E1, "Game")
        with path.open("r+b") as file:
            file.seek(0x22C)
            file.write(b"\xff" * 8)
    before = {p: p.read_bytes() for p in (dlc, xbla, god)}
    job_dir = pipeline._job_dir(1)

    pipeline.config.save_unlock_packages(False)
    assert pipeline._unlock_output_dirs(1, [output], job_dir)["unlocked"] == 0
    assert all(p.read_bytes() == before[p] for p in before)
    assert not (job_dir / "unlock-backups").exists()

    pipeline.config.save_unlock_packages(True)
    result = pipeline._unlock_output_dirs(1, [output], job_dir)
    assert result["unlocked"] == 2
    assert god.read_bytes() == before[god]
    for path in (dlc, xbla):
        assert path.read_bytes()[0x22C:0x32C] == bytes(256)
        backup = job_dir / "unlock-backups" / path.parent.name / (path.name + ".x360lock.bak")
        assert backup.read_bytes() == before[path]
    assert not (job_dir / "unlock-backups" / "00007000").exists()


def test_unlock_step_uses_job_setting_even_after_switch_changes(tmp_path):
    pipeline = make_pipeline(tmp_path)
    output = pipeline.config.output_dir / "Game" / "415607E1"
    dlc = output / "00000002" / "addon"
    write_stfs(dlc, 2, 0x415607E1, "Game")
    with dlc.open("r+b") as file:
        file.seek(0x22C)
        file.write(b"\xff" * 8)
    pipeline.config.save_unlock_packages(False)
    result = pipeline._unlock_output_dirs(1, [output], pipeline._job_dir(1), enabled=True)
    assert result["unlocked"] == 1
