import hashlib
import json
import os
import shutil
import time
import zipfile
from pathlib import Path

import pytest

from app import create_app
from config import Config
from console_api import ConsoleError, PREFIX, digest, safe_relative
from package_fixtures import synthetic_iso, write_god, write_tu
from xboxunity import TitleUpdateEntry, XboxUnityClient

CSRF = {"X-Requested-With": "XMLHttpRequest"}


def export_fixture(name, value):
    destination = os.getenv("CONSOLE_CONTRACT_FIXTURES")
    if destination:
        Path(destination).mkdir(parents=True, exist_ok=True)
        (Path(destination) / name).write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setenv("APP_AUTH_USER", "")
    monkeypatch.setenv("UNLOCK_PACKAGES", "true")
    app = create_app(Config.from_env(), start_background=False)
    app.testing = True
    s = app.config["CONSOLE_SERVICE"]
    return app, app.test_client(), s


def pair(app, client, service, name="Test Xbox"):
    result = client.post(PREFIX + "/pairings", json={"name": name})
    assert result.status_code == 200
    result = result.json
    assert client.post("/api/consoles/pair", json={"code": result["user_code"]}).status_code == 403
    assert client.post("/api/consoles/pair", json={"code": result["user_code"]}, headers=CSRF).status_code == 200
    token = client.post(PREFIX + "/pairings/token", json={"device_code": result["device_code"]})
    assert token.status_code == 200
    return {"Authorization": "Bearer " + token.json["access_token"]}, token.json["device_id"]


def seed_game(service):
    db = service.database
    db.upsert_catalog("disc", [{"name": "Synthetic game.zip", "source_path": "roms/game.zip", "size": 1}])
    item = db.catalog("disc", "", 1, 10)[0][0]
    job = db.enqueue_items([item["id"]])[0]
    root = service.config.output_dir / "Synthetic game" / "415607E1"
    header = write_god(root)
    db.put_title("415607E1", "Synthetic game", str(root))
    with service.connect() as c:
        c.execute("UPDATE catalog_items SET status='complete',title_id='415607E1' WHERE id=?", (item["id"],))
        files = [str(p) for p in root.rglob("*") if p.is_file()]
        c.execute("UPDATE jobs SET status='complete',artifact_paths=?,checks_json=? WHERE id=?",
                  (json.dumps(files), json.dumps({"unlock_enabled": True}), job["job_id"]))
    return f"catalog-{item['id']}", header


def test_approved_pairing_can_retry_a_lost_token_response(setup):
    app, client, service = setup
    start = client.post(PREFIX + "/pairings", json={"name": "Retry Xbox"}).json
    assert client.post("/api/consoles/pair", json={"code": start["user_code"]}, headers=CSRF).status_code == 200
    first = client.post(PREFIX + "/pairings/token", json={"device_code": start["device_code"]})
    assert first.status_code == 200
    # Its response is lost on the wire. Retry after the advertised poll interval.
    with service.connect() as c:
        c.execute("UPDATE console_pairings SET last_poll=0")
    retry = client.post(PREFIX + "/pairings/token", json={"device_code": start["device_code"]})
    assert retry.status_code == 200 and retry.json == first.json
    assert client.get(PREFIX + "/catalog", headers={"Authorization": "Bearer " + first.json["access_token"]}).status_code == 200
    assert client.post("/api/consoles/pair", json={"code": start["user_code"]}, headers=CSRF).status_code == 409
    with service.connect() as c:
        devices = c.execute("SELECT * FROM console_devices").fetchall()
        assert len(devices) == 1 and devices[0]["token_hash"] == digest(first.json["access_token"])
        assert first.json["access_token"] not in json.dumps(dict(devices[0]))
        assert c.execute("SELECT secret_hash FROM console_pairings").fetchone()[0] == digest(start["device_code"])


def test_pairing_retry_cannot_reactivate_revoked_or_expired_device(setup):
    app, client, service = setup
    start = client.post(PREFIX + "/pairings", json={"name": "Revoked Xbox"}).json
    assert client.post("/api/consoles/pair", json={"code": start["user_code"]}, headers=CSRF).status_code == 200
    token = client.post(PREFIX + "/pairings/token", json={"device_code": start["device_code"]}).json
    assert client.post("/api/consoles/" + token["device_id"] + "/revoke", json={}, headers=CSRF).status_code == 200
    with service.connect() as c:
        c.execute("UPDATE console_pairings SET last_poll=0")
    assert client.post(PREFIX + "/pairings/token", json={"device_code": start["device_code"]}).status_code == 410
    assert client.get(PREFIX + "/catalog", headers={"Authorization": "Bearer " + token["access_token"]}).status_code == 401
    with service.connect() as c:
        c.execute("UPDATE console_pairings SET expires=0")
    assert client.post(PREFIX + "/pairings/token", json={"device_code": start["device_code"]}).status_code == 410


def prepare(client, s, headers, release, **extra):
    result = client.post(PREFIX + "/install-requests", json={"release_id": release, **extra},
                         headers={**headers, "Idempotency-Key": "test-request-key-12345"})
    assert result.status_code == 200, result.json
    row = s.get_request(result.json["id"])
    s.prepare(row)
    row = s.get_request(row["id"])
    if row["state"] == "waiting":
        s.prepare(row)
    return s.get_request(row["id"])


def test_pair_auth_scope_revoke_and_isolation(setup):
    app, client, s = setup
    assert client.get(PREFIX + "/capabilities").status_code == 200
    assert client.get(PREFIX + "/catalog").status_code == 401
    h, device = pair(app, client, s)
    assert client.get(PREFIX + "/catalog", headers=h).status_code == 200
    with s.connect() as c:
        row = c.execute("SELECT * FROM console_devices WHERE id=?", (device,)).fetchone()
        assert row["token_hash"] == digest(h["Authorization"][7:])
        assert h["Authorization"][7:] not in json.dumps(dict(row))
        c.execute("UPDATE console_devices SET scopes='[]' WHERE id=?", (device,))
    assert client.get(PREFIX + "/catalog", headers=h).status_code == 403
    assert client.post(f"/api/consoles/{device}/revoke", headers=CSRF).status_code == 200
    assert client.get(PREFIX + "/catalog", headers=h).status_code == 401


def test_basic_auth_exemption_is_only_real_machine_blueprint(tmp_path, monkeypatch):
    monkeypatch.setenv("WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("APP_AUTH_USER", "admin")
    monkeypatch.setenv("APP_AUTH_PASSWORD", "password")
    client = create_app(Config.from_env(), start_background=False).test_client()
    assert client.get(PREFIX + "/capabilities").status_code == 200
    assert client.get(PREFIX + "/catalog").status_code == 401
    assert client.get("/api/settings").status_code == 401
    assert client.get("/api/consoles").status_code == 401
    assert client.get("/api/console/v1-extra/settings").status_code == 401
    assert client.get("/api/consoles", auth=("admin", "password")).status_code == 200


def test_pair_expiry_replay_and_pending(setup):
    app, client, s = setup
    value = client.post(PREFIX + "/pairings", json={"name": "Xbox"}).json
    assert client.post(PREFIX + "/pairings/token", json={"device_code": value["device_code"]}).json["status"] == "pending"
    assert client.post(PREFIX + "/pairings/token", json={"device_code": value["device_code"]}).status_code == 429
    with s.connect() as c:
        c.execute("UPDATE console_pairings SET expires=0")
    assert client.post("/api/consoles/pair", json={"code": value["user_code"]}, headers=CSRF).status_code == 409
    assert client.post(PREFIX + "/pairings/token", json={"device_code": value["device_code"]}).status_code == 410


def test_prepared_manifest_hashes_range_and_immutable_snapshot(setup):
    app, client, s = setup
    h, _ = pair(app, client, s)
    release, original = seed_game(s)
    row = prepare(client, s, h, release)
    assert row["state"] == "ready"
    response = client.get(f"{PREFIX}/artifacts/{row['artifact_id']}/manifest", headers=h)
    assert response.status_code == 200
    manifest = response.json
    export_fixture("prepared.json", manifest)
    assert manifest["components"][0]["level"] == "full_data"
    assert manifest["components"][0]["unlock_applied"] is False
    entry = next(f for f in manifest["components"][0]["files"] if f["role"] == "header")
    path = entry["download_path"]
    raw = client.get(path, headers=h).data
    assert hashlib.sha256(raw).hexdigest() == entry["sha256"]
    assert client.head(path, headers=h).headers["Content-Length"] == entry["bytes"]
    partial = client.get(path, headers={**h, "Range": "bytes=8-31"})
    assert partial.status_code == 206 and partial.data == raw[8:32]
    assert partial.headers["Content-Range"] == f"bytes 8-31/{len(raw)}"
    assert client.get(path, headers={**h, "Range": "bytes=9999999-"}).status_code == 416
    assert client.get(path, headers={**h, "Range": "bytes=8-31", "If-Range": '"different"'}).status_code == 200
    original.write_bytes(b"changed")
    assert client.get(path, headers=h).data == raw
    h2, _ = pair(app, client, s, "Other Xbox")
    assert client.get(path, headers=h2).status_code == 410


def test_legacy_completed_job_resolves_identity_without_catalog_title_id(setup):
    app, client, s = setup
    h, _ = pair(app, client, s)
    release, _ = seed_game(s)
    with s.connect() as c:
        c.execute("UPDATE catalog_items SET title_id=NULL")
    resolved = client.get(PREFIX + "/releases/" + release, headers=h).json
    assert resolved["title_id"] == "415607E1"
    assert resolved["metadata_status"] == "known"
    assert resolved["components"][0]["kind"] == "god"
    assert prepare(client, s, h, release)["state"] == "ready"


def test_digital_release_contains_only_its_own_dlc_without_redownloading_base_game(setup):
    app, client, s = setup
    h, _ = pair(app, client, s)
    _, god = seed_game(s)
    dlc = god.parent.parent / "00000002" / "one-addon"
    raw = bytearray(write_tu(dlc))
    raw[0x344:0x348] = (2).to_bytes(4, "big")
    raw[0x32C:0x340] = hashlib.sha1(raw[0x344:0xB000]).digest()
    dlc.write_bytes(raw)
    db = s.database
    db.upsert_catalog("digital", [{"name": "One DLC.zip", "source_path": "roms/dlc.zip", "size": 1}])
    job = db.enqueue_items([2])[0]
    with s.connect() as c:
        c.execute("UPDATE catalog_items SET status='complete' WHERE id=2")
        c.execute("UPDATE jobs SET status='complete',artifact_paths=? WHERE id=?", (json.dumps([str(dlc)]), job["job_id"]))
    resolved = client.get(PREFIX + "/releases/catalog-2", headers=h).json
    assert resolved["title_id"] == "415607E1"
    assert [c["kind"] for c in resolved["components"]] == ["dlc"]


def test_idempotency_and_private_requests(setup):
    app, client, s = setup
    h, _ = pair(app, client, s)
    release, _ = seed_game(s)
    args = {"json": {"release_id": release}, "headers": {**h, "Idempotency-Key": "same-key-123456789"}}
    a = client.post(PREFIX + "/install-requests", **args)
    b = client.post(PREFIX + "/install-requests", **args)
    assert a.json["id"] == b.json["id"]
    args["json"]["conversion"] = "console"
    assert client.post(PREFIX + "/install-requests", **args).status_code == 409
    other, _ = pair(app, client, s, "Other")
    assert client.get(PREFIX + "/install-requests/" + a.json["id"], headers=other).status_code == 404


@pytest.mark.parametrize("path", ["../bad", "/absolute", "a//b", "a\\b", "a/b:", "a/" + "a"*43, "a/trailing.", "a/../b"])
def test_reject_unsafe_fatx_paths(path):
    with pytest.raises(ConsoleError):
        safe_relative(path)


@pytest.mark.parametrize("method", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_original_zip_source_metadata_and_range(setup, method):
    app, client, s = setup
    h, _ = pair(app, client, s)
    release, _ = seed_game(s)
    cache = s.config.work_dir / "source-cache" / "00000001.zip"
    cache.parent.mkdir()
    image = synthetic_iso()
    with zipfile.ZipFile(cache, "w", compression=method, allowZip64=True) as archive:
        archive.writestr("game.iso", image)
    with s.connect() as c:
        c.execute("UPDATE catalog_items SET remote_size=? WHERE id=1", (cache.stat().st_size,))
    row = prepare(client, s, h, release, conversion="console")
    manifest = client.get(f"{PREFIX}/artifacts/{row['artifact_id']}/manifest", headers=h).json
    source = manifest["source"]
    export_fixture("source-stored.json" if method == zipfile.ZIP_STORED else "source-deflate.json", manifest)
    assert source["method"] == method and source["image_bytes"] == str(len(image))
    raw = client.get(source["download_path"], headers=h).data
    assert hashlib.sha256(raw).hexdigest() == source["sha256"]
    if method == zipfile.ZIP_STORED:
        assert raw[int(source["data_offset"]):int(source["data_offset"]) + len(image)] == image


def test_mismatched_tu_is_rejected(setup):
    app, client, s = setup
    h, _ = pair(app, client, s)
    release, header = seed_game(s)
    tu = header.parent.parent / "000B0000" / "tu-test"
    write_tu(tu, base_version=999)
    components = client.get(PREFIX + "/releases/" + release, headers=h).json["components"]
    with pytest.raises(ConsoleError, match="matching base game"):
        prepare(client, s, h, release, component_ids=[c["component_id"] for c in components])


def test_tu_only_request_is_validated_against_actual_existing_game(setup):
    app, client, s = setup
    h, _ = pair(app, client, s)
    release, header = seed_game(s)
    write_tu(header.parent.parent / "000B0000" / "tu-test", base_version=2)
    components = client.get(PREFIX + "/releases/" + release, headers=h).json["components"]
    update = next(c for c in components if c["kind"] == "title_update")
    row = prepare(client, s, h, release, component_ids=[update["component_id"]])
    manifest = client.get(f"{PREFIX}/artifacts/{row['artifact_id']}/manifest", headers=h).json
    assert len(manifest["components"]) == 1
    assert manifest["components"][0]["compatibility"]["base_version"] == "00000002"


def test_corrupt_god_does_not_publish_partial_snapshot(setup):
    app, client, s = setup
    h, _ = pair(app, client, s)
    release, header = seed_game(s)
    with (header.with_name(header.name + ".data") / "Data0000").open("r+b") as out:
        out.seek(0x2000)
        out.write(b"corrupt")
    with pytest.raises(ValueError, match="checksum"):
        prepare(client, s, h, release)
    with s.connect() as c:
        assert c.execute("SELECT count(*) FROM console_artifacts").fetchone()[0] == 0
    assert list(s.root.glob("*.partial")) == []


@pytest.mark.parametrize("enabled", [False, True])
def test_dlc_unlock_variant_is_private_and_does_not_modify_live_files(setup, enabled):
    app, client, s = setup
    h, _ = pair(app, client, s)
    release, header = seed_game(s)
    dlc = header.parent.parent / "00000002" / "example-dlc"
    raw = bytearray(write_tu(dlc))
    raw[0x344:0x348] = (2).to_bytes(4, "big")
    raw[0x32C:0x340] = hashlib.sha1(raw[0x344:0xB000]).digest()
    dlc.write_bytes(raw)
    components = client.get(PREFIX + "/releases/" + release, headers=h).json["components"]
    selected = next(c for c in components if c["kind"] == "dlc")
    row = prepare(client, s, h, release, component_ids=[selected["component_id"]], unlock_packages=enabled)
    manifest = client.get(f"{PREFIX}/artifacts/{row['artifact_id']}/manifest", headers=h).json
    component = manifest["components"][0]
    file = component["files"][0]
    received = client.get(file["download_path"], headers=h).data
    assert hashlib.sha256(received).hexdigest() == file["sha256"]
    assert component["unlock_applied"] is enabled
    assert bool(any(received[0x22C:0x32C])) is not enabled
    assert dlc.read_bytes() == raw
    assert not any("backup" in f["relative_path"] for f in component["files"])


def test_expired_artifact_lease_and_quota(setup):
    app, client, s = setup
    h, _ = pair(app, client, s)
    release, _ = seed_game(s)
    row = prepare(client, s, h, release)
    with s.connect() as c:
        c.execute("UPDATE console_artifacts SET expires=0")
    assert client.get(f"{PREFIX}/artifacts/{row['artifact_id']}/manifest", headers=h).status_code == 410
    s.cleanup()
    assert not (s.root / row["artifact_id"]).exists()
    s.quota = 1
    with pytest.raises(ConsoleError, match="storage is full"):
        s.check_quota(2)


def test_captured_unlock_option_survives_setting_change(setup):
    app, client, s = setup
    db = s.database
    db.upsert_catalog("disc", [{"name": "New game.zip", "source_path": "roms/new.zip", "size": 100}])
    queued = db.enqueue_items([1], options={"unlock_packages": False})[0]
    s.config.save_unlock_packages(True)
    assert json.loads(db.get_job(queued["job_id"])["options_json"])["unlock_packages"] is False
    with pytest.raises(ValueError, match="different settings"):
        db.enqueue_items([1], options={"unlock_packages": True})


@pytest.mark.parametrize("cache", [False, True])
def test_online_update_for_console_converted_game_needs_no_server_game_or_iso(setup, tmp_path, monkeypatch, cache):
    app, client, s = setup
    h, _ = pair(app, client, s)
    fixture = tmp_path / "fixture-tu"
    raw = write_tu(fixture, base_version=2)
    entry = TitleUpdateEntry("test-123", 3, "12345678", "Test update", len(raw), "", "00000002", raw[0x32C:0x340].hex(), "415607E1")
    monkeypatch.setattr(XboxUnityClient, "get_title_updates", lambda self, title: [entry])

    def download(self, update, target):
        target.mkdir(parents=True, exist_ok=True)
        result = target / ("TU_test_update" if cache else "tu_test_update")
        shutil.copy2(fixture, result)
        return result

    monkeypatch.setattr(XboxUnityClient, "download_title_update", download)
    value = {"kind": "title_update", "game_identity": {"title_id": "415607E1", "media_id": "12345678", "base_version": "00000002"}}
    response = client.post(PREFIX + "/install-requests", json=value, headers={**h, "Idempotency-Key": "online-tu-test-123456"})
    assert response.status_code == 200
    s.prepare(s.get_request(response.json["id"]))
    row = s.get_request(response.json["id"])
    assert row["state"] == "ready" and row["job_id"] is None
    manifest = client.get(f"{PREFIX}/artifacts/{row['artifact_id']}/manifest", headers=h).json
    update = manifest["components"][0]
    assert update["unlock_applied"] is False
    assert update["compatibility"] == value["game_identity"]
    assert update["files"][0]["root"] == ("cache" if cache else "content")
    assert client.get(update["files"][0]["download_path"], headers=h).data == raw


def test_online_update_unknown_identity_and_absent_match_are_explicit(setup, monkeypatch):
    app, client, s = setup
    h, _ = pair(app, client, s)
    headers = {**h, "Idempotency-Key": "online-tu-missing-123"}
    assert client.post(PREFIX + "/install-requests", json={"kind": "title_update", "game_identity": {}}, headers=headers).status_code == 400
    monkeypatch.setattr(XboxUnityClient, "get_title_updates", lambda self, title: [])
    value = {"kind": "title_update", "game_identity": {"title_id": "415607E1", "media_id": "12345678", "base_version": "00000002"}}
    response = client.post(PREFIX + "/install-requests", json=value, headers=headers)
    with pytest.raises(ConsoleError, match="no update for this exact"):
        s.prepare(s.get_request(response.json["id"]))


def test_online_update_wrong_content_id_is_not_published(setup, tmp_path, monkeypatch):
    app, client, s = setup
    h, _ = pair(app, client, s)
    raw_path = tmp_path / "update"
    raw = write_tu(raw_path)
    entry = TitleUpdateEntry("test-bad", 3, "12345678", "Wrong ID", len(raw), "", "00000002", "AB"*20, "415607E1")
    monkeypatch.setattr(XboxUnityClient, "get_title_updates", lambda self, title: [entry])
    monkeypatch.setattr(XboxUnityClient, "download_title_update", lambda self, entry, target: raw_path)
    value = {"kind": "title_update", "game_identity": {"title_id": "415607E1", "media_id": "12345678", "base_version": "00000002"}}
    response = client.post(PREFIX + "/install-requests", json=value, headers={**h, "Idempotency-Key": "online-tu-wrong-12345"})
    with pytest.raises(ValueError, match="content ID"):
        s.prepare(s.get_request(response.json["id"]))
    with s.connect() as c:
        assert c.execute("SELECT count(*) FROM console_artifacts").fetchone()[0] == 0


def test_retried_install_keeps_original_default_after_global_toggle_changes(setup):
    app, client, s = setup
    h, _ = pair(app, client, s)
    release, _ = seed_game(s)
    headers = {**h, "Idempotency-Key": "lost-response-toggle-123"}
    first = client.post(PREFIX + "/install-requests", json={"release_id": release}, headers=headers)
    s.config.save_unlock_packages(False)
    retry = client.post(PREFIX + "/install-requests", json={"release_id": release}, headers=headers)
    assert retry.status_code == 200 and retry.json["id"] == first.json["id"]
    assert retry.json["selection"]["unlock_packages"] is True
    assert client.post(PREFIX + "/install-requests", json={"release_id": release, "unlock_packages": False}, headers=headers).status_code == 409


def test_expired_request_exposes_saved_selection_before_periodic_cleanup(setup):
    app, client, s = setup
    h, _ = pair(app, client, s)
    release, _ = seed_game(s)
    row = prepare(client, s, h, release)
    with s.connect() as c:
        c.execute("UPDATE console_artifacts SET expires=0")
    status = client.get(PREFIX + "/install-requests/" + row["id"], headers=h).json
    assert status["state"] == "expired"
    assert status["selection"]["release_id"] == release


def test_game_release_does_not_offer_another_disc_or_edition_as_its_base(setup):
    app, client, s = setup
    h, _ = pair(app, client, s)
    release, header = seed_game(s)
    other = header.with_name("87654321")
    raw = bytearray(header.read_bytes())
    raw[0x354:0x358] = (0x87654321).to_bytes(4, "big")
    raw[0x32C:0x340] = hashlib.sha1(raw[0x344:]).digest()
    other.write_bytes(raw)
    components = client.get(PREFIX + "/releases/" + release, headers=h).json["components"]
    assert [c["media_id"] for c in components if c["kind"] == "god"] == ["12345678"]


def test_source_ingestion_resumes_the_same_partial_after_server_restart(setup, tmp_path, monkeypatch):
    from console_api import ConsoleService, RemoteClient
    app, client, s = setup
    h, _ = pair(app, client, s)
    release, _ = seed_game(s)
    archive = tmp_path / "source.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as out:
        out.writestr("game.iso", synthetic_iso())
    raw = archive.read_bytes()
    with s.connect() as c:
        c.execute("UPDATE catalog_items SET remote_size=? WHERE id=1", (len(raw),))
    seen = []

    def interrupted(self, source_path, destination, expected_size=0, progress=None):
        seen.append(destination)
        partial = destination.with_name(destination.name + ".part")
        if len(seen) == 1:
            partial.write_bytes(raw[:50])
            raise RuntimeError("simulated server exit during source download")
        assert destination == seen[0] and partial.read_bytes() == raw[:50]
        destination.write_bytes(raw)
        partial.unlink()
        return len(raw)

    monkeypatch.setattr(RemoteClient, "download_file", interrupted)
    response = client.post(PREFIX + "/install-requests", json={"release_id": release, "conversion": "console"},
                           headers={**h, "Idempotency-Key": "source-restart-123456"})
    row = s.get_request(response.json["id"])
    with pytest.raises(RuntimeError, match="simulated server exit"):
        s.prepare(row)
    restarted = ConsoleService(s.config, s.database, s.runner, s.remote)
    # Merely constructing a read-only API must not reset a live worker.
    assert restarted.get_request(row["id"])["state"] == "preparing"
    restarted.recover_requests()
    recovered = restarted.get_request(row["id"])
    assert recovered["state"] == "queued"
    restarted.prepare(recovered)
    assert restarted.get_request(row["id"])["state"] == "ready"
    assert len(seen) == 2


def test_failed_publication_is_not_registered_and_old_orphans_are_cleaned(setup):
    import sqlite3
    app, client, s = setup
    h, _ = pair(app, client, s)
    release, _ = seed_game(s)
    with s.connect() as c:
        c.executescript("CREATE TRIGGER reject_ready BEFORE UPDATE OF state ON console_requests WHEN NEW.state='ready' BEGIN SELECT RAISE(ABORT,'publication interrupted'); END;")
    with pytest.raises(sqlite3.IntegrityError, match="publication interrupted"):
        prepare(client, s, h, release)
    with s.connect() as c:
        assert c.execute("SELECT count(*) FROM console_artifacts").fetchone()[0] == 0
    orphans = [p for p in s.root.iterdir() if p.is_dir() and len(p.name) == 32]
    assert len(orphans) == 1
    s.cleanup()
    assert orphans[0].exists()
    old = time.time() - s.retention - 10
    os.utime(orphans[0], (old, old))
    s.cleanup()
    assert not orphans[0].exists()


@pytest.mark.parametrize('source_kind', ['disc', 'digital'])
@pytest.mark.parametrize('target', ['415607E1', '12345678'])
def test_addon_source_is_scoped_to_actual_game_and_never_reinstalls_god(setup, source_kind, target):
    app, client, s = setup
    h, _ = pair(app, client, s)
    _, god = seed_game(s)
    dlc = god.parent.parent / '00000002' / 'standalone-addon'
    raw = bytearray(write_tu(dlc)); raw[0x344:0x348] = (2).to_bytes(4, 'big')
    raw[0x32C:0x340] = hashlib.sha1(raw[0x344:0xB000]).digest(); dlc.write_bytes(raw)
    s.database.upsert_catalog(source_kind, [{'name': 'Synthetic game - Bonus (World) (Addon).zip', 'source_path': 'addon.zip', 'size': 1}])
    job = s.database.enqueue_items([2])[0]
    with s.connect() as c:
        c.execute("UPDATE catalog_items SET status='complete' WHERE id=2")
        c.execute("UPDATE jobs SET status='complete',artifact_paths=? WHERE id=?", (json.dumps([str(dlc)]), job['job_id']))
    release = client.get(PREFIX + '/releases/catalog-2', headers=h).json
    assert release['kind'] == 'digital' and [c['kind'] for c in release['components']] == ['dlc']
    assert [r['release_id'] for r in client.get(PREFIX + '/catalog?kind=digital', headers=h).json['items']] == ['catalog-2']
    assert [r['release_id'] for r in client.get(PREFIX + '/catalog?kind=game', headers=h).json['items']] == ['catalog-1']
    r = client.post(PREFIX + '/install-requests', json={'release_id': 'catalog-2', 'conversion': 'console'}, headers={**h, 'Idempotency-Key': 'no-addon-conversion'})
    assert r.status_code == 400
    if target != '415607E1':
        with pytest.raises(ConsoleError, match='does not belong'):
            prepare(client, s, h, 'catalog-2', for_title_id=target)
    else:
        row = prepare(client, s, h, 'catalog-2', for_title_id=target, unlock_packages=False)
        manifest = client.get(f"{PREFIX}/artifacts/{row['artifact_id']}/manifest", headers=h).json
        assert [c['kind'] for c in manifest['components']] == ['dlc']
        assert client.get(manifest['components'][0]['files'][0]['download_path'], headers=h).data == raw
        assert god.exists()


def test_leave_licenses_unchanged_keeps_already_unlocked_dlc(setup):
    app, client, s = setup
    h, _ = pair(app, client, s)
    release, god = seed_game(s)
    dlc = god.parent.parent / '00000002' / 'already-unlocked'
    raw = bytearray(write_tu(dlc)); raw[0x344:0x348] = (2).to_bytes(4, 'big'); raw[0x22C:0x32C] = bytes(256)
    raw[0x32C:0x340] = hashlib.sha1(raw[0x344:0xB000]).digest(); dlc.write_bytes(raw)
    components = client.get(PREFIX + '/releases/' + release, headers=h).json['components']
    selected = next(c for c in components if c['kind'] == 'dlc')
    row = prepare(client, s, h, release, component_ids=[selected['component_id']], unlock_packages=False)
    manifest = client.get(f"{PREFIX}/artifacts/{row['artifact_id']}/manifest", headers=h).json
    component = manifest['components'][0]
    assert not component['unlock_applied'] and component['license_state'] == 'unlocked'
    assert client.get(component['files'][0]['download_path'], headers=h).data == raw


def test_store_api_requires_device_and_validates_filters(setup):
    app, client, s = setup
    assert client.get(PREFIX + '/store/home').status_code == 401
    h, _ = pair(app, client, s)
    seed_game(s)
    assert client.get(PREFIX + '/store/home', headers=h).status_code == 200
    result = client.get(PREFIX + '/store/titles', headers=h).json
    assert result['total'] == 1 and result['items'][0]['release_count'] == 1
    gid = result['items'][0]['group_id']
    assert client.get(PREFIX + '/store/titles/' + gid, headers=h).json['releases'][0]['release_id'] == 'catalog-1'
    for query in ['kind=unknown', 'letter=AB', 'cursor=-1', 'title_id=not-hex']:
        assert client.get(PREFIX + '/store/titles?' + query, headers=h).status_code == 400
