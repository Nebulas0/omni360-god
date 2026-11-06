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
