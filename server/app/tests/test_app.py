from __future__ import annotations

from pathlib import Path

from app import create_app
from config import Config


def test_health_and_index_without_background_threads(tmp_path: Path) -> None:
    config = Config(
        remote_base_url="https://example.invalid",
        remote_api_key="test-key",
        remote_api_key_file=tmp_path / "work" / "remote-api-key",
        remote_auth_mode="api-key",
        remote_auth_mode_file=tmp_path / "work" / "remote-auth-mode",
        remote_basic_user="",
        remote_basic_password="",
        remote_basic_file=tmp_path / "work" / "remote-basic",
        disc_source_path="roms/xbox360",
        digital_source_path="roms/xbox360-digital",
        output_dir=tmp_path / "output",
        work_dir=tmp_path / "work",
        db_path=tmp_path / "work" / "state.sqlite3",
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
    config.output_dir.mkdir()
    config.converter_path.write_bytes(b"test")
    app = create_app(config, start_background=False)
    client = app.test_client()
    response = client.get("/")
    assert response.status_code == 200
    assert b"GOD Foundry" in response.data
    assert client.get("/healthz").status_code == 200


def test_frontend_api_helper_preserves_csrf_header() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "static" / "app.js"
    ).read_text(encoding="utf-8")
    assert "const { headers: optionHeaders = {}, ...requestOptions } = options;" in source
    assert "...requestOptions,\n    headers:" in source
    assert '"X-Requested-With": "XMLHttpRequest"' in source


def test_unlock_setting_saves_without_credentials_or_catalog_refresh(tmp_path, monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setenv("WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setenv("UNLOCK_PACKAGES", "true")
    monkeypatch.setenv("APP_AUTH_USER", "")
    config = Config.from_env()
    app = create_app(config, start_background=False)
    state = app.config["XBOX_STATE"]
    state.start_catalog_sync = Mock()
    client = app.test_client()
    assert client.get("/api/settings").json["unlock_packages"] is True
    assert b'role="switch"' in client.get("/").data

    assert client.post("/api/settings/unlock", json={"unlock_packages": False}).status_code == 403
    headers = {"X-Requested-With": "XMLHttpRequest"}
    for invalid in ({}, {"unlock_packages": "false"}, {"unlock_packages": 0}, []):
        assert client.post("/api/settings/unlock", json=invalid, headers=headers).status_code == 400
    for enabled in (False, True, False):
        response = client.post("/api/settings/unlock", json={"unlock_packages": enabled}, headers=headers)
        assert response.status_code == 200
        assert response.json["unlock_packages"] is enabled
        assert client.get("/api/settings").json["unlock_packages"] is enabled
    state.start_catalog_sync.assert_not_called()

    reloaded = create_app(Config.from_env(), start_background=False)
    assert reloaded.test_client().get("/api/settings").json["unlock_packages"] is False


def test_reprocess_api_and_latest_bundle_report(tmp_path, monkeypatch):
    import json
    from unittest.mock import Mock
    monkeypatch.setenv("WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setenv("APP_AUTH_USER", "")
    app = create_app(Config.from_env(), start_background=False)
    db = app.config["XBOX_DATABASE"]
    app.config["XBOX_RUNNER"].wake = Mock()
    db.upsert_catalog("digital", [{"source_path": "game.zip", "name": "Game.zip", "size": 1}])
    item = db.catalog("digital")[0][0]
    job_id = db.enqueue_items([item["id"]])[0]["job_id"]
    db.claim_next_job()
    archive = tmp_path / "output" / "Game.zip"
    archive.write_bytes(b"bundle")
    db.update_job(job_id, checks_json=json.dumps({"unlock_enabled": True}))
    db.finish_job(job_id, item["id"], True, "Ready", db.encode_output_paths([str(archive)]))
    db.put_bundle_report(str(archive), {"unlock_enabled": False, "checks": []})
    client = app.test_client()
    data = client.get(f"/api/jobs/{job_id}").json
    assert data["report"]["unlock_enabled"] is False
    assert data["job_report"]["unlock_enabled"] is True
    assert data["downloads"][0]["label"] == "Download latest game bundle"
    guide = client.get(data["install_guide_url"])
    assert guide.status_code == 200 and b"Hdd1:/Cache/" in guide.data
    assert client.post(f"/api/jobs/{job_id}/reprocess").status_code == 403
    response = client.post(f"/api/jobs/{job_id}/reprocess", headers={"X-Requested-With": "XMLHttpRequest"})
    assert response.status_code == 202
    assert response.json["job"]["job_id"] != job_id
    assert db.get_job(job_id)["status"] == "complete"
