"""Scoped console protocol and immutable package publication.

Browser administration intentionally uses the application's existing auth/CSRF.
The machine blueprint is authenticated here, never by a browser cookie.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import struct
import threading
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from flask import Blueprint, Response, g, jsonify, request, send_file

from db import Database, normalize_search_text, utc_now
from formats import CONTENT_ARCADE, CONTENT_DLC, CONTENT_GOD, CONTENT_TITLE_UPDATE, parse_stfs_header
from remote import RemoteClient
from validation import validate_god, validate_title_update
from unlock import is_unlocked, unlock_package

PREFIX = "/api/console/v1"
SCOPES = {"catalog:read", "install:create", "artifact:read", "install:report"}
log = logging.getLogger("xbox360.console")
MAX_FILE = 0xEFFFFFFF
MAX_JSON = 65536


class ConsoleError(ValueError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.status = code, status


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def json_bytes(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def safe_relative(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 200:
        raise ConsoleError("INVALID_PATH", "Invalid package path")
    if "\\" in value or value.startswith("/") or "//" in value:
        raise ConsoleError("INVALID_PATH", "Invalid package path")
    parts = value.split("/")
    for part in parts:
        if part in {"", ".", ".."} or len(part) > 42 or part.endswith((" ", ".")):
            raise ConsoleError("INVALID_PATH", "Path cannot be installed on FATX")
        if any(ord(c) < 32 or ord(c) > 126 or c in '<>:"\\|?*' for c in part):
            raise ConsoleError("INVALID_PATH", "Path cannot be installed on FATX")
    return str(PurePosixPath(*parts))


class ConsoleService:
    def __init__(self, config, database, runner, remote):
        self.config, self.database, self.runner, self.remote = config, database, runner, remote
        self.root = config.work_dir / "console"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.thread = None
        self.quota = int(os.getenv("CONSOLE_ARTIFACT_QUOTA_GIB", "256")) * 1024**3
        self.retention = max(1, int(os.getenv("CONSOLE_RETENTION_DAYS", "14"))) * 86400
        with self.connect() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS console_devices(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL,
                    scopes TEXT NOT NULL, created_at TEXT NOT NULL, last_seen TEXT,
                    revoked INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS console_pairings(
                    secret_hash TEXT PRIMARY KEY, code TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL, expires REAL NOT NULL, device_id TEXT,
                    last_poll REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS console_limits(
                    key TEXT PRIMARY KEY, window INTEGER NOT NULL, count INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS console_requests(
                    id TEXT PRIMARY KEY, device_id TEXT NOT NULL, idem TEXT NOT NULL,
                    payload TEXT NOT NULL, state TEXT NOT NULL, message TEXT NOT NULL,
                    job_id INTEGER, artifact_id TEXT, error_code TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(device_id,idem));
                CREATE TABLE IF NOT EXISTS console_artifacts(
                    id TEXT PRIMARY KEY, release_id TEXT NOT NULL, kind TEXT NOT NULL,
                    manifest TEXT NOT NULL, manifest_hash TEXT NOT NULL,
                    bytes INTEGER NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS console_events(
                    device_id TEXT NOT NULL, event_id TEXT NOT NULL, request_id TEXT NOT NULL,
                    payload TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(device_id,event_id));
            """)
            c.execute("UPDATE console_requests SET state='queued' WHERE state='preparing'")

    @contextmanager
    def connect(self):
        c = self.database.connect()
        try:
            yield c
        finally:
            c.close()

    def limit(self, key, maximum, window=60):
        period = int(time.time()) // window
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("INSERT INTO console_limits VALUES(?,?,1) ON CONFLICT(key) DO UPDATE SET "
                      "count=CASE WHEN window=excluded.window THEN count+1 ELSE 1 END,window=excluded.window",
                      (key, period))
            count = c.execute("SELECT count FROM console_limits WHERE key=?", (key,)).fetchone()[0]
            c.commit()
        if count > maximum:
            raise ConsoleError("RATE_LIMITED", "Please wait before trying again", 429)

    def authenticate(self):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or len(auth) > 160:
            raise ConsoleError("UNPAIRED", "Pair this console on the website", 401)
        token_hash = digest(auth[7:])
        with self.connect() as c:
            row = c.execute("SELECT * FROM console_devices WHERE token_hash=? AND revoked=0", (token_hash,)).fetchone()
            if not row or not hmac.compare_digest(row["token_hash"], token_hash):
                raise ConsoleError("TOKEN_REVOKED", "Pair this console again", 401)
            c.execute("UPDATE console_devices SET last_seen=? WHERE id=?", (utc_now(), row["id"]))
        g.console_device = dict(row)
        self.limit("device:" + row["id"], 240)

    def scope(self, value):
        if value not in json.loads(g.console_device["scopes"]):
            raise ConsoleError("FORBIDDEN", "Device does not have this permission", 403)

    def pair(self, name):
        self.limit("pair:" + (request.remote_addr or "unknown"), 10, 600)
        secret = secrets.token_urlsafe(32)
        code = "-".join(secrets.token_hex(2).upper() for _ in range(2))
        with self.connect() as c:
            c.execute("DELETE FROM console_pairings WHERE expires < ?", (time.time(),))
            c.execute("INSERT INTO console_pairings(secret_hash,code,name,expires) VALUES(?,?,?,?)",
                      (digest(secret), code, name, time.time() + 600))
        return {"device_code": secret, "user_code": code, "expires_in": 600, "interval": 5,
                "verification_uri": "https://" + self.config.public_host + "/consoles"}

    def poll_pair(self, secret):
        self.limit("poll:" + digest(secret), 20)
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM console_pairings WHERE secret_hash=?", (digest(secret),)).fetchone()
            if not row or row["expires"] < time.time():
                raise ConsoleError("PAIRING_EXPIRED", "Start pairing again", 410)
            if row["last_poll"] > time.time() - 4:
                raise ConsoleError("RATE_LIMITED", "Poll no more than once every five seconds", 429)
            c.execute("UPDATE console_pairings SET last_poll=? WHERE secret_hash=?", (time.time(), digest(secret)))
            if not row["device_id"]:
                c.commit()
                return {"status": "pending", "interval": 5}
            token = "fg_" + secrets.token_urlsafe(32)
            c.execute("UPDATE console_devices SET token_hash=? WHERE id=?", (digest(token), row["device_id"]))
            c.execute("DELETE FROM console_pairings WHERE secret_hash=?", (digest(secret),))
            c.commit()
        return {"status": "approved", "access_token": token, "token_type": "Bearer", "device_id": row["device_id"]}

    def approve(self, code, name):
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM console_pairings WHERE code=?", (code.upper().strip(),)).fetchone()
            if not row or row["expires"] < time.time() or row["device_id"]:
                raise ConsoleError("PAIRING_EXPIRED", "Code expired, invalid or already approved", 409)
            device = secrets.token_hex(16)
            c.execute("INSERT INTO console_devices(id,name,token_hash,scopes,created_at) VALUES(?,?,?,?,?)",
                      (device, name or row["name"], digest(secrets.token_urlsafe(32)), json.dumps(sorted(SCOPES)), utc_now()))
            c.execute("UPDATE console_pairings SET device_id=? WHERE code=?", (device, row["code"]))
            c.commit()
        return device

    def item(self, release):
        match = re.fullmatch(r"catalog-([1-9][0-9]{0,9})", str(release))
        item = self.database.get_item(int(match[1])) if match else None
        if not item:
            raise ConsoleError("NOT_FOUND", "Release not found", 404)
        return item

    def summary(self, item):
        return {"release_id": f"catalog-{item['id']}", "name": item["name"],
                "kind": "game" if item["source_kind"] == "disc" else "digital",
                "title_id": item.get("title_id"), "source_bytes": str(item["remote_size"]),
                "availability": "prepared" if item["status"] == "complete" else item["status"],
                "metadata_status": "known" if item.get("title_id") else "unknown"}

    def components(self, item):
        if not item.get("title_id"):
            return []
        title = self.database.get_title(item["title_id"])
        if not title:
            return []
        root = Path(title["output_dir"]).resolve()
        if not root.is_relative_to(self.config.output_dir.resolve()):
            raise ConsoleError("INVALID_PATH", "Title directory is outside the output root", 409)
        result = []
        for folder in ("00007000", "000D0000", "00000002", "000B0000", "Cache"):
            directory = root / folder
            for p in sorted(directory.glob("*")):
                if not p.is_file() or p.is_symlink() or p.name.endswith((".bak", ".partial", ".previous")):
                    continue
                info = parse_stfs_header(p)
                if info is None or info.title_id != item["title_id"]:
                    continue
                kinds = {CONTENT_GOD: "god", CONTENT_ARCADE: "xbla", CONTENT_DLC: "dlc", CONTENT_TITLE_UPDATE: "title_update"}
                if info.content_type not in kinds:
                    continue
                if folder != "Cache" and folder != info.content_folder:
                    continue
                with p.open("rb") as f:
                    head = f.read(0x368)
                result.append({"component_id": digest(str(p.relative_to(root)))[:32],
                               "kind": kinds[info.content_type], "name": info.display_name or p.name,
                               "title_id": info.title_id, "media_id": info.media_id,
                               "content_type": info.content_folder, "content_id": head[0x32C:0x340].hex().upper(),
                               "version": head[0x358:0x35C].hex().upper(),
                               "base_version": head[0x35C:0x360].hex().upper(),
                               "_path": p, "_root": root, "_cache": folder == "Cache"})
        return result

    @staticmethod
    def public_component(component):
        return {k: v for k, v in component.items() if not k.startswith("_")}

    def create_request(self, payload, idem):
        if set(payload) - {"release_id", "conversion", "component_ids", "unlock_packages"}:
            raise ConsoleError("INVALID_REQUEST", "Unknown installation option")
        item = self.item(payload.get("release_id"))
        mode = payload.get("conversion", "server")
        if mode not in {"server", "console"} or (mode == "console" and item["source_kind"] != "disc"):
            raise ConsoleError("UNSUPPORTED_SOURCE", "Xbox conversion is only available for disc archives")
        ids = payload.get("component_ids", [])
        if not isinstance(ids, list) or len(ids) > 100 or any(not isinstance(x, str) or not re.fullmatch(r"[0-9a-f]{32}", x) for x in ids):
            raise ConsoleError("INVALID_REQUEST", "Select at most 100 component IDs")
        unlock = payload.get("unlock_packages", self.config.effective_unlock_packages)
        if not isinstance(unlock, bool):
            raise ConsoleError("INVALID_REQUEST", "unlock_packages must be boolean")
        normalized = {"release_id": payload["release_id"], "conversion": mode,
                      "component_ids": sorted(set(ids)), "unlock_packages": unlock}
        encoded = json_bytes(normalized).decode()
        device = g.console_device["id"]
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            old = c.execute("SELECT * FROM console_requests WHERE device_id=? AND idem=?", (device, idem)).fetchone()
            if old:
                if old["payload"] != encoded:
                    raise ConsoleError("IDEMPOTENCY_CONFLICT", "Request key was already used with different options", 409)
                return dict(old)
            active = c.execute("SELECT count(*) FROM console_requests WHERE state IN ('queued','waiting','preparing') AND device_id=?", (device,)).fetchone()[0]
            total = c.execute("SELECT count(*) FROM console_requests WHERE state IN ('queued','waiting','preparing')").fetchone()[0]
            if active >= 2 or total >= 8:
                raise ConsoleError("QUEUE_FULL", "Wait for an active preparation to finish", 429)
            rid, now = secrets.token_hex(16), utc_now()
            c.execute("INSERT INTO console_requests(id,device_id,idem,payload,state,message,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                      (rid, device, idem, encoded, "queued", "Waiting for preparation", now, now))
            c.commit()
        self.wake.set()
        return self.get_request(rid, device)

    def get_request(self, rid, device=None):
        with self.connect() as c:
            row = c.execute("SELECT * FROM console_requests WHERE id=?" + (" AND device_id=?" if device else ""),
                            (rid, device) if device else (rid,)).fetchone()
        if not row:
            raise ConsoleError("NOT_FOUND", "Install request not found", 404)
        return dict(row)

    def request_view(self, row):
        result = {k: row[k] for k in ("id", "state", "message", "artifact_id", "error_code", "created_at", "updated_at")}
        result["conversion"] = json.loads(row["payload"])["conversion"]
        result["progress"] = 0
        with self.connect() as c:
            event = c.execute("SELECT payload FROM console_events WHERE request_id=? ORDER BY created_at DESC LIMIT 1", (row["id"],)).fetchone()
        result["installation_status"] = json.loads(event[0]).get("outcome") if event else None
        if row["job_id"]:
            job = self.database.get_job(row["job_id"])
            if job and job["status"] in {"queued", "working"}:
                result.update(stage=job["stage"], progress=job["progress"], message=job["message"])
        return result

    def update(self, rid, **values):
        values["updated_at"] = utc_now()
        with self.connect() as c:
            c.execute("UPDATE console_requests SET " + ",".join(k + "=?" for k in values) + " WHERE id=?", (*values.values(), rid))

    def start(self):
        self.thread = threading.Thread(target=self.run, name="console-artifact-worker", daemon=True)
        self.thread.start()

    def run(self):
        last_cleanup = 0
        while not self.stopping.is_set():
            if time.time() - last_cleanup > 3600:
                try:
                    self.cleanup()
                except Exception:
                    log.exception("Console artifact cleanup failed")
                last_cleanup = time.time()
            with self.connect() as c:
                rows = c.execute("SELECT * FROM console_requests WHERE state IN ('queued','waiting') ORDER BY created_at LIMIT 8").fetchall()
            for row in rows:
                try:
                    self.prepare(dict(row))
                except Exception as exc:
                    code = exc.code if isinstance(exc, ConsoleError) else "VALIDATION_FAILED"
                    # No upstream credentials or arbitrary exception representations in the API.
                    message = str(exc) if isinstance(exc, (ConsoleError, ValueError)) else "Preparation failed; check the server log"
                    self.update(row["id"], state="failed", error_code=code, message=message[:300])
                    log.exception("Console preparation failed: request=%s code=%s", row["id"], code)
            self.wake.wait(2)
            self.wake.clear()

    def prepare(self, row):
        payload = json.loads(row["payload"])
        item = self.item(payload["release_id"])
        if payload["conversion"] == "console":
            self.update(row["id"], state="preparing", message="Preparing original archive")
            self.publish_source(row, item)
            return
        job = self.database.get_job(row["job_id"]) if row["job_id"] else None
        if job is None:
            result = self.database.enqueue_items([item["id"]], options={"unlock_packages": payload["unlock_packages"]})[0]
            self.update(row["id"], state="waiting", job_id=result["job_id"], message="Waiting for server conversion")
            self.runner.wake()
            return
        if job["status"] in {"queued", "working"}:
            return
        if job["status"] != "complete":
            raise ConsoleError("CONVERSION_FAILED", "Server processing failed; inspect the job in Foundry", 409)
        self.update(row["id"], state="preparing", message="Copying and checking an immutable installation")
        components = self.components(item)
        selected = set(payload["component_ids"])
        if selected - {c["component_id"] for c in components}:
            raise ConsoleError("COMPONENT_CHANGED", "Selected components changed; refresh the release", 409)
        if selected:
            components = [c for c in components if c["component_id"] in selected]
        else:
            # No implicit DLC or title-update installation.
            allowed = {"god"} if item["source_kind"] == "disc" else {"xbla", "dlc"}
            produced = {str(Path(p).resolve()) for p in Database.decode_output_paths(job.get("artifact_paths"))}
            components = [c for c in components if c["kind"] in allowed and str(c["_path"].resolve()) in produced]
        if not components:
            raise ConsoleError("METADATA_UNKNOWN", "No supported, identified components are ready for this release", 409)
        self.publish_packages(row, payload, components)

    def check_quota(self, size):
        with self.connect() as c:
            used = c.execute("SELECT coalesce(sum(bytes),0) FROM console_artifacts").fetchone()[0]
        if used + size > self.quota or shutil.disk_usage(self.root).free < size + 1024**3:
            raise ConsoleError("NO_SPACE", "Console artifact storage is full; expire old artifacts or increase the quota", 507)

    @staticmethod
    def copy_hash(source, destination):
        if source.is_symlink():
            raise ConsoleError("INVALID_PATH", "Symlink package files are not supported")
        before = source.stat()
        destination.parent.mkdir(parents=True, exist_ok=True)
        sha, size = hashlib.sha256(), 0
        with source.open("rb") as inp, destination.open("xb") as out:
            while chunk := inp.read(1024**2):
                out.write(chunk)
                sha.update(chunk)
                size += len(chunk)
            out.flush()
            os.fsync(out.fileno())
        after = source.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino) or size != before.st_size:
            raise ConsoleError("ARTIFACT_CHANGED", "Package changed during preparation; retry when processing finishes", 409)
        return str(size), sha.hexdigest()

    def publish_packages(self, row, payload, components):
        rid = secrets.token_hex(16)
        staging = self.root / (rid + ".partial")
        staging.mkdir()
        manifest = {"schema_version": 1, "artifact_id": rid, "provider_id": "foundry",
                    "release_id": payload["release_id"], "conversion": "server", "components": [],
                    "created_at": utc_now(), "warnings": []}
        try:
            all_files, seen = [], set()
            for component in components:
                p = component["_path"]
                files = [p]
                if component["kind"] == "god":
                    files += sorted(p.with_name(p.name + ".data").glob("Data*"))
                all_files.extend(files)
                component["_files"] = files
            estimated = sum(p.stat().st_size for p in all_files)
            estimated += sum(c["_path"].stat().st_size for c in components if c["kind"] in {"dlc", "xbla"} and payload["unlock_packages"] and not is_unlocked(c["_path"]))
            self.check_quota(estimated)
            game_info = {}
            for component in components:
                entry = self.public_component(component)
                entry.update(files=[], unlock_applied=False)
                for p in component["_files"]:
                    if not p.resolve().is_relative_to(component["_root"]):
                        raise ConsoleError("INVALID_PATH", "Package file escaped its title directory")
                    rel = p.relative_to(component["_root"]).as_posix()
                    root = "cache" if component["_cache"] else "content"
                    relative = safe_relative(p.name if root == "cache" else component["title_id"] + "/" + rel)
                    key = root + "/" + relative
                    if key.casefold() in seen or p.stat().st_size > MAX_FILE:
                        raise ConsoleError("INVALID_PATH", "Duplicate path or oversized Xbox file")
                    seen.add(key.casefold())
                    file_id = digest(key)[:32]
                    size, sha = self.copy_hash(p, staging / key)
                    if component["kind"] in {"dlc", "xbla"}:
                        if payload["unlock_packages"]:
                            changed = unlock_package(staging / key, backup_dir=staging / "backups" / component["component_id"])
                            entry["unlock_applied"] = changed.unlocked or changed.already_unlocked
                            with (staging / key).open("rb") as package:
                                checksum = hashlib.sha256()
                                while block := package.read(1024**2):
                                    checksum.update(block)
                                sha = checksum.hexdigest()
                        elif is_unlocked(staging / key):
                            raise ConsoleError("POLICY_MISMATCH", "This source was already unlocked. Reprocess its original archive with unlocking disabled; no synthetic relock is performed", 409)
                    entry["files"].append({"file_id": file_id, "root": root, "relative_path": relative,
                        "role": "header" if component["kind"] == "god" and p == component["_path"] else "data" if component["kind"] == "god" else "package",
                        "bytes": size, "sha256": sha, "etag": sha,
                        "download_path": f"{PREFIX}/artifacts/{rid}/files/{file_id}"})
                staged = staging / ("cache" if component["_cache"] else "content") / (component["_path"].name if component["_cache"] else component["title_id"] + "/" + component["_path"].relative_to(component["_root"]).as_posix())
                if component["kind"] == "god":
                    checked = validate_god(staged, full_data=True)
                    entry.update(checked)
                    game_info[entry["media_id"]] = checked
                elif component["kind"] == "title_update":
                    entry.update(validate_title_update(staged, title_id=entry["title_id"]))
                else:
                    self.validate_content(staged)
                    entry["validation"] = "metadata_and_transport"
                manifest["components"].append(entry)
            for entry in manifest["components"]:
                if entry["kind"] == "title_update":
                    game = game_info.get(entry["media_id"])
                    if not game:
                        # TU-only installs may use a separately installed game.
                        # Still require an actual validated server-side execution
                        # ID, rather than trusting catalog names or client claims.
                        title = self.database.get_title(entry["title_id"])
                        if title:
                            game_root = Path(title["output_dir"]) / "00007000"
                            for header in game_root.glob("*"):
                                info = parse_stfs_header(header) if header.is_file() and not header.is_symlink() else None
                                if info and info.content_type == CONTENT_GOD and info.media_id == entry["media_id"]:
                                    checked = validate_god(header)
                                    if checked["base_version"] == entry["base_version"]:
                                        game = checked
                                        break
                    if not game or game["base_version"] != entry["base_version"]:
                        raise ConsoleError("INCOMPATIBLE_UPDATE", "Select the matching base game with this title update", 409)
                    entry["compatibility"] = {k: game[k] for k in ("title_id", "media_id", "base_version")}
            self.finish_artifact(row, rid, staging, manifest, "prepared")
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    @staticmethod
    def validate_content(path):
        info = parse_stfs_header(path)
        with path.open("rb") as f:
            h = f.read(0x344)
            header_size = int.from_bytes(h[0x340:0x344], "big")
            end = (header_size + 0xFFF) & ~0xFFF
            if not info or header_size < 0x971A or end > min(path.stat().st_size, 0x100000):
                raise ConsoleError("VALIDATION_FAILED", "Invalid content package header")
            f.seek(0x344)
            if hashlib.sha1(f.read(end - 0x344)).digest() != h[0x32C:0x340]:
                raise ConsoleError("VALIDATION_FAILED", "Content metadata checksum failed")

    def publish_source(self, row, item):
        rid = secrets.token_hex(16)
        staging = self.root / (rid + ".partial")
        staging.mkdir()
        try:
            size = int(item["remote_size"])
            if not size or size > 32 * 1024**3:
                raise ConsoleError("UNSUPPORTED_SOURCE", "Source size is unknown or exceeds the console limit")
            self.check_quota(size)
            source = self.config.work_dir / "source-cache" / f"{item['id']:08d}.zip"
            destination = staging / "source.zip"
            if source.is_file() and source.stat().st_size == size:
                length, sha = self.copy_hash(source, destination)
            else:
                r = self.remote
                remote = RemoteClient(r.base_url, r.api_key, r.timeout, r.retries,
                                      basic_user=r.basic_user, basic_password=r.basic_password, auth_mode=r.auth_mode)
                remote.download_file(item["source_path"], destination, expected_size=size)
                # Hash via the same bounded copy path; leave no mutable source inode.
                length, sha = self.copy_hash(destination, staging / "source.checked")
                destination.unlink()
                (staging / "source.checked").replace(destination)
            with zipfile.ZipFile(destination) as archive:
                members = archive.infolist()
                images = [m for m in members if m.filename.lower().endswith(".iso") and not m.is_dir()]
                if len(members) > 200000 or len(images) != 1:
                    raise ConsoleError("UNSUPPORTED_SOURCE", "Select a ZIP with exactly one ISO image")
                member = images[0]
                if member.flag_bits & 1 or len(member.filename) > 1024 or member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED} or member.file_size > 16 * 1024**3:
                    raise ConsoleError("UNSUPPORTED_SOURCE", "Unsupported ISO compression, encryption or size")
            with destination.open("rb") as f:
                f.seek(member.header_offset)
                local = f.read(30)
                if len(local) != 30 or local[:4] != b"PK\x03\x04":
                    raise ConsoleError("UNSUPPORTED_SOURCE", "Invalid ISO member header")
                name_len, extra_len = struct.unpack_from("<HH", local, 26)
                offset = member.header_offset + 30 + name_len + extra_len
                if offset + member.compress_size > int(length):
                    raise ConsoleError("UNSUPPORTED_SOURCE", "ISO member exceeds the source archive")
            manifest = {"schema_version": 1, "artifact_id": rid, "provider_id": "foundry",
                        "release_id": f"catalog-{item['id']}", "conversion": "console", "components": [],
                        "created_at": utc_now(), "warnings": [], "source": {
                            "format": "zip", "member": member.filename, "bytes": length, "sha256": sha,
                            "data_offset": str(offset), "compressed_bytes": str(member.compress_size),
                            "image_bytes": str(member.file_size), "method": member.compress_type,
                            "crc32": f"{member.CRC:08X}",
                            "etag": sha, "name": item["name"], "title_id": item.get("title_id"),
                            "download_path": f"{PREFIX}/source-artifacts/{rid}/file"}}
            self.finish_artifact(row, rid, staging, manifest, "source")
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def finish_artifact(self, row, rid, staging, manifest, kind):
        raw = json_bytes(manifest)
        if len(raw) > 1024**2:
            raise ConsoleError("UNSUPPORTED_SOURCE", "Package manifest exceeds the console memory limit")
        (staging / "manifest.json").write_bytes(raw)
        total = sum(p.stat().st_size for p in staging.rglob("*") if p.is_file())
        staging.rename(self.root / rid)
        with self.connect() as c:
            c.execute("INSERT INTO console_artifacts VALUES(?,?,?,?,?,?,?)",
                      (rid, manifest["release_id"], kind, raw.decode(), hashlib.sha256(raw).hexdigest(), total, time.time() + self.retention))
        self.update(row["id"], state="ready", artifact_id=rid, error_code=None, message="Ready to download and install")

    def artifact(self, aid):
        if not re.fullmatch(r"[0-9a-f]{32}", aid):
            raise ConsoleError("NOT_FOUND", "Artifact not found", 404)
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            # A device may download only artifacts attached to its own requests.
            row = c.execute("SELECT a.* FROM console_artifacts a WHERE a.id=? AND EXISTS "
                            "(SELECT 1 FROM console_requests r WHERE r.artifact_id=a.id AND r.device_id=?)",
                            (aid, g.console_device["id"])).fetchone()
            if not row or row["expires"] < time.time():
                raise ConsoleError("ARTIFACT_EXPIRED", "Prepare this installation again", 410)
            c.execute("UPDATE console_artifacts SET expires=? WHERE id=?", (time.time() + self.retention, aid))
            c.commit()
        return dict(row)

    def cleanup(self):
        with self.connect() as c:
            expired = c.execute("SELECT id FROM console_artifacts WHERE expires < ?", (time.time(),)).fetchall()
            for row in expired:
                aid = row[0]
                if re.fullmatch(r"[0-9a-f]{32}", aid):
                    shutil.rmtree(self.root / aid, ignore_errors=True)
                    c.execute("DELETE FROM console_artifacts WHERE id=?", (aid,))
                    c.execute("UPDATE console_requests SET state='expired' WHERE artifact_id=?", (aid,))


def register_console_api(app, config, database, runner, remote, start_background):
    service = ConsoleService(config, database, runner, remote)
    app.config["CONSOLE_SERVICE"] = service
    api = Blueprint("console", __name__, url_prefix=PREFIX)

    def payload():
        if request.content_length is None or request.content_length > MAX_JSON:
            raise ConsoleError("INVALID_REQUEST", "JSON body is missing or too large", 413)
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise ConsoleError("INVALID_REQUEST", "Expected a JSON object")
        return value

    @api.errorhandler(ConsoleError)
    def error(exc):
        response = jsonify(error=str(exc), code=exc.code)
        response.status_code = exc.status
        if exc.status == 429:
            response.headers["Retry-After"] = "60"
        return response

    @api.before_request
    def authorize():
        if request.endpoint not in {"console.capabilities", "console.pair", "console.token"}:
            service.authenticate()

    @api.after_request
    def private_response(response):
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @api.get("/capabilities")
    def capabilities():
        return jsonify(protocol_version=1, min_client_version="1.0.0", provider_id="foundry",
                       modes=["server", "console"], source_formats=["zip-stored", "zip-deflate"],
                       manifest_limit_bytes="1048576", max_page_size=50, max_components=100,
                       max_file_bytes=str(MAX_FILE), checksums=["sha256"], pairing=True)

    @api.post("/pairings")
    def pair():
        name = payload().get("name", "Xbox 360")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60:
            raise ConsoleError("INVALID_REQUEST", "Choose a device name of 1 to 60 characters")
        return jsonify(service.pair(name.strip()))

    @api.post("/pairings/token")
    def token():
        secret = payload().get("device_code", "")
        if not isinstance(secret, str) or not 32 <= len(secret) <= 100:
            raise ConsoleError("INVALID_REQUEST", "Invalid device code")
        return jsonify(service.poll_pair(secret))

    @api.get("/catalog")
    def catalog():
        service.scope("catalog:read")
        kind = request.args.get("kind", "game")
        if kind not in {"game", "digital"}:
            raise ConsoleError("INVALID_REQUEST", "kind must be game or digital")
        try:
            cursor = max(0, int(request.args.get("cursor", "0")))
            limit = min(50, max(1, int(request.args.get("limit", "30"))))
        except ValueError:
            raise ConsoleError("INVALID_REQUEST", "Invalid catalog cursor or page size")
        q = normalize_search_text(request.args.get("q", "")[:120])
        with service.connect() as c:
            rows = c.execute("SELECT * FROM catalog_items WHERE id>? AND source_kind=? AND search_text LIKE ? ORDER BY id LIMIT ?",
                             (cursor, "disc" if kind == "game" else "digital", "%" + q + "%", limit + 1)).fetchall()
        return jsonify(items=[service.summary(dict(r)) for r in rows[:limit]],
                       next_cursor=str(rows[limit-1]["id"]) if len(rows) > limit else None)

    @api.get("/releases/<release>")
    def release(release):
        service.scope("catalog:read")
        item = service.item(release)
        return jsonify(**service.summary(item), components=[service.public_component(c) for c in service.components(item)])

    @api.post("/compatibility/resolve")
    def compatibility():
        service.scope("catalog:read")
        value = payload()
        title = str(value.get("title_id", "")).upper()
        media = str(value.get("media_id", "")).upper()
        base = str(value.get("base_version", "")).upper()
        if not all(re.fullmatch(r"[0-9A-F]{8}", x) for x in (title, media, base)):
            raise ConsoleError("METADATA_UNKNOWN", "Actual Title ID, Media ID and base version are required")
        components = service.components({"title_id": title})
        return jsonify(components=[service.public_component(c) for c in components if c["kind"] == "dlc" or
                                  (c["kind"] == "title_update" and c["media_id"] == media and c["base_version"] == base)],
                       update_activation="Enable the matching title update in Aurora after installation")

    @api.post("/install-requests")
    def install():
        service.scope("install:create")
        idem = request.headers.get("Idempotency-Key", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,96}", idem):
            raise ConsoleError("INVALID_REQUEST", "A unique Idempotency-Key header is required")
        service.limit("installs:" + g.console_device["id"], 30, 3600)
        return jsonify(service.request_view(service.create_request(payload(), idem)))

    @api.get("/install-requests/<rid>")
    def install_status(rid):
        service.scope("install:create")
        return jsonify(service.request_view(service.get_request(rid, g.console_device["id"])))

    @api.post("/install-requests/<rid>/events")
    def event(rid):
        service.scope("install:report")
        service.get_request(rid, g.console_device["id"])
        value = payload()
        event_id = value.get("event_id", "")
        if not isinstance(event_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{16,96}", event_id) or value.get("outcome") not in {"installed", "failed", "cancelled"}:
            raise ConsoleError("INVALID_REQUEST", "Invalid installation event")
        with service.connect() as c:
            c.execute("INSERT OR IGNORE INTO console_events VALUES(?,?,?,?,?)",
                      (g.console_device["id"], event_id, rid, json_bytes(value).decode(), utc_now()))
        return jsonify(accepted=True)

    @api.get("/artifacts/<aid>/manifest")
    def manifest(aid):
        service.scope("artifact:read")
        artifact = service.artifact(aid)
        response = Response(artifact["manifest"], mimetype="application/json")
        response.set_etag(artifact["manifest_hash"])
        return response.make_conditional(request)

    def send_artifact(aid, file_id=None):
        service.scope("artifact:read")
        artifact = service.artifact(aid)
        manifest = json.loads(artifact["manifest"])
        if file_id is None:
            if artifact["kind"] != "source":
                raise ConsoleError("NOT_FOUND", "Source artifact not found", 404)
            entry, relative = manifest["source"], "source.zip"
        else:
            files = [f for c in manifest["components"] for f in c["files"]]
            entry = next((f for f in files if f["file_id"] == file_id), None)
            if not entry:
                raise ConsoleError("NOT_FOUND", "File not found", 404)
            relative = entry["root"] + "/" + safe_relative(entry["relative_path"])
        root = (service.root / aid).resolve()
        path = root / relative
        if not path.resolve().is_relative_to(root) or path.is_symlink() or not path.is_file() or path.stat().st_size != int(entry["bytes"]):
            raise ConsoleError("ARTIFACT_CHANGED", "Artifact is unavailable; prepare it again", 409)
        response = send_file(path, mimetype="application/octet-stream", conditional=True, etag=entry["sha256"], max_age=0)
        response.headers["Accept-Ranges"] = "bytes"
        return response

    @api.get("/artifacts/<aid>/files/<fid>")
    def file(aid, fid):
        return send_artifact(aid, fid)

    @api.get("/source-artifacts/<aid>/file")
    def source(aid):
        return send_artifact(aid)

    app.register_blueprint(api)
    app.register_error_handler(ConsoleError, error)

    @app.get("/consoles")
    def consoles_page():
        from flask import render_template
        return render_template("consoles.html")

    @app.get("/api/consoles")
    def consoles_list():
        with service.connect() as c:
            devices = [dict(r) for r in c.execute("SELECT id,name,scopes,created_at,last_seen,revoked FROM console_devices ORDER BY created_at DESC")]
            installs = [service.request_view(dict(r)) for r in c.execute("SELECT * FROM console_requests ORDER BY created_at DESC LIMIT 50")]
        return jsonify(devices=devices, installs=installs)

    @app.post("/api/consoles/pair")
    def approve():
        value = payload()
        code, name = value.get("code", ""), value.get("name", "")
        if not isinstance(code, str) or not isinstance(name, str) or len(name) > 60:
            return jsonify(error="Invalid code or device name"), 400
        try:
            return jsonify(device_id=service.approve(code, name.strip()))
        except ConsoleError as exc:
            return error(exc)

    @app.post("/api/consoles/<device>/revoke")
    def revoke(device):
        with service.connect() as c:
            c.execute("UPDATE console_devices SET revoked=1 WHERE id=?", (device,))
        return jsonify(revoked=True)

    if start_background:
        service.start()
    return service
