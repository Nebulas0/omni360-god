from __future__ import annotations

import hmac
import os
import re
import shutil
import threading
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from flask import Flask, Response, jsonify, render_template, request, send_file, url_for

from config import Config
from bundle import install_guide
from db import Database
from formats import release_details, sanitize_component
from pipeline import JobRunner, Pipeline
from remote import (
    AUTH_MODE_API_KEY,
    AUTH_MODE_BASIC,
    RemoteClient,
    normalize_auth_mode,
)


VALID_KINDS = {"disc", "digital"}


def human_size(value: int | float | None) -> str:
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


def directory_size(root: Path) -> int:
    total = 0
    if not root.exists():
        return 0
    for base, directories, files in os.walk(root, followlinks=False):
        directories[:] = [
            name for name in directories
            if not (Path(base) / name).is_symlink()
        ]
        for name in files:
            path = Path(base) / name
            try:
                if not path.is_symlink():
                    total += path.stat().st_size
            except OSError:
                continue
    return total


class AppState:
    def __init__(self, config: Config, database: Database):
        self.config = config
        self.database = database
        self.sync_lock = threading.Lock()
        self.sync_thread: threading.Thread | None = None

    def start_catalog_sync(self, remote: RemoteClient, force: bool = False) -> bool:
        with self.sync_lock:
            if self.sync_thread and self.sync_thread.is_alive():
                return False
            self.sync_thread = threading.Thread(
                target=self._sync_all,
                args=(remote, force),
                name="xbox360-catalog-sync",
                daemon=True,
            )
            self.sync_thread.start()
            return True

    def _sync_all(self, remote: RemoteClient, force: bool) -> None:
        del force  # A refresh always asks the remote for the current directory listing.
        sources = (
            ("disc", self.config.disc_source_path),
            ("digital", self.config.digital_source_path),
        )
        for kind, source_path in sources:
            self.database.set_sync_state(kind, "syncing")
            try:
                records = remote.list_directory(source_path)
                count = self.database.upsert_catalog(kind, records)
                self.database.set_sync_state(kind, "ready", count)
            except Exception as exc:
                self.database.set_sync_state(kind, "error", 0, str(exc)[:1000])


def _serialize_item(item: dict[str, Any]) -> dict[str, Any]:
    release = release_details(item["name"])
    return {
        "id": item["id"],
        "name": item["name"],
        "source_kind": item["source_kind"],
        "source_path": item["source_path"],
        "size": item["remote_size"],
        "human_size": human_size(item["remote_size"]),
        "modified": item["remote_modified"],
        "type": item["remote_type"],
        "release": release,
        "status": item["status"],
        "last_error": item["last_error"],
        "latest_job_id": item.get("latest_job_id"),
        "latest_job_status": item.get("latest_job_status"),
        "latest_job_stage": item.get("latest_job_stage"),
        "latest_job_progress": item.get("latest_job_progress"),
        "downloaded_at": item.get("latest_download_confirmed_at"),
        "downloaded": bool(item.get("latest_download_confirmed_at")),
    }


def _serialize_job(job: dict[str, Any], database: Database | None = None) -> dict[str, Any]:
    combined_paths = Database.decode_output_paths(job.get("output_path"))
    snapshot_path = Path(job["snapshot_path"]) if job.get("snapshot_path") else None
    downloads: list[dict[str, Any]] = []
    if combined_paths and Path(combined_paths[0]).is_file():
        downloads.append({
            "url": url_for("download_job_combined", job_id=job["id"]),
            "name": Path(combined_paths[0]).name,
            "label": "Download latest game bundle",
            "kind": "combined",
        })
    if snapshot_path and snapshot_path.is_file():
        downloads.append({
            "url": url_for("download_job_snapshot", job_id=job["id"]),
            "name": job.get("snapshot_name") or snapshot_path.name,
            "label": "Files from this job",
            "kind": "snapshot",
        })
    release = release_details(job["name"])
    shared_output_count = 0
    if database is not None and combined_paths:
        shared_output_count = database.output_reference_count(combined_paths[0])
    job_report = Database.decode_report(job.get("checks_json"))
    bundle_report = database.get_bundle_report(combined_paths[0]) if database and combined_paths else None
    return {
        "id": job["id"],
        "item_id": job["item_id"],
        "name": job["name"],
        "source_kind": job["source_kind"],
        "source_path": job["source_path"],
        "release": release,
        "target_title_ids": Database.decode_output_paths(job.get("target_title_ids")),
        "status": job["status"],
        "stage": job["stage"],
        "progress": job["progress"],
        "bytes_done": job["bytes_done"],
        "bytes_total": job["bytes_total"],
        "message": job["message"],
        "error": job["error"],
        "created_at": job["created_at"],
        "started_at": job["started_at"],
        "completed_at": job["completed_at"],
        "download_confirmed_at": job.get("download_confirmed_at"),
        "download_confirmed": bool(job.get("download_confirmed_at")),
        "downloads": downloads,
        "has_snapshot": bool(snapshot_path and snapshot_path.is_file()),
        "shared_output_count": shared_output_count,
        "report": bundle_report or job_report,
        "job_report": job_report,
        "reprocess_of": job.get("reprocess_of"),
        "can_reprocess": job["status"] in {"complete", "failed"},
        "install_guide_url": url_for("api_job_install_guide", job_id=job["id"]),
    }


def create_app(
    config: Config | None = None,
    start_background: bool = True,
) -> Flask:
    config = config or Config.from_env()
    config.ensure_directories()
    database = Database(config.db_path)
    database.initialize()
    remote = RemoteClient(
        config.remote_base_url,
        config.remote_api_key,
        timeout=config.remote_timeout,
        retries=config.remote_retries,
        basic_user=config.remote_basic_user,
        basic_password=config.remote_basic_password,
        auth_mode=config.remote_auth_mode,
    )
    pipeline = Pipeline(config, database, remote)
    runner = JobRunner(pipeline, database)
    state = AppState(config, database)

    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.update(
        XBOX_CONFIG=config,
        XBOX_DATABASE=database,
        XBOX_REMOTE=remote,
        XBOX_RUNNER=runner,
        XBOX_STATE=state,
    )

    def json_error(message: str, status: int = 400):
        return jsonify({"error": message}), status

    @app.before_request
    def require_basic_auth() -> Any:
        # Machine routes enforce per-device bearer authorization in their blueprint.
        # Do not exempt browser administration or similarly prefixed paths.
        if request.blueprint == "console":
            return None
        if not config.app_auth_user:
            return None
        auth = request.authorization
        if (
            auth is None
            or not hmac.compare_digest(auth.username or "", config.app_auth_user)
            or not hmac.compare_digest(auth.password or "", config.app_auth_password)
        ):
            return (
                "Authentication required",
                401,
                {"WWW-Authenticate": 'Basic realm="Xbox 360 GOD Foundry"'},
            )
        return None

    @app.before_request
    def require_csrf_header() -> Any:
        """Block state-changing requests without a custom header.

        Cross-origin forms and <img>/<script> tags cannot set custom request
        headers without a CORS preflight, so this blocks CSRF attacks while
        allowing the SPA's own fetch calls (which always include the header).
        GET requests and the health-check endpoint are exempt.
        """
        if request.blueprint == "console":
            return None
        if request.method in {"GET", "HEAD", "OPTIONS"}:
            return None
        if request.endpoint == "healthz":
            return None
        if request.headers.get("X-Requested-With") != "XMLHttpRequest":
            return json_error("missing or invalid X-Requested-With header", 403)
        return None

    @app.get("/")
    def index():
        return render_template("index.html", public_host=config.public_host)

    @app.get("/healthz")
    def healthz():
        ready = config.converter_path.is_file() and config.db_path.exists()
        payload = {
            "ok": ready,
            "converter": str(config.converter_path),
            "remote_configured": remote.configured,
            "remote_auth_mode": remote.auth_mode,
        }
        return jsonify(payload), 200 if ready else 503

    @app.get("/api/state")
    def api_state():
        sync_states = database.sync_states()
        return jsonify({
            "sync": sync_states,
            "counts": database.catalog_counts(),
            "remote_configured": remote.configured,
            "remote_auth_mode": remote.auth_mode,
            "converter_available": config.converter_path.is_file(),
            "output_dir": str(config.output_dir),
            "public_host": config.public_host,
        })

    @app.get("/api/settings")
    def api_settings():
        return jsonify({
            "api_key_configured": bool(remote.api_key),
            "persistent_api_key": config.remote_api_key_file.is_file(),
            "auth_mode": remote.auth_mode,
            "persistent_auth_mode": config.remote_auth_mode_file.is_file(),
            "basic_configured": remote.basic_configured,
            "basic_username": remote.basic_user,
            "basic_persistent": config.remote_basic_file.is_file(),
            "remote_base_url": config.remote_base_url,
            "unlock_packages": config.effective_unlock_packages,
        })

    @app.post("/api/settings/unlock")
    def api_settings_unlock_update():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("unlock_packages"), bool):
            return json_error("unlock_packages must be a boolean")
        enabled = payload["unlock_packages"]
        try:
            config.save_unlock_packages(enabled)
        except (OSError, ValueError) as exc:
            return json_error(f"Could not save unlock setting: {exc}", 500)
        return jsonify({
            "saved": True,
            "unlock_packages": enabled,
            "message": f"XBLA/DLC unlock {'enabled' if enabled else 'disabled'} for new jobs. GoD licenses are always preserved.",
        })

    @app.post("/api/settings")
    def api_settings_update():
        payload = request.get_json(silent=True) or {}
        requested_mode = payload.get("auth_mode")
        if not isinstance(requested_mode, str):
            return json_error("auth_mode must be api-key or basic")
        auth_mode = normalize_auth_mode(requested_mode, default="")
        if auth_mode not in {AUTH_MODE_API_KEY, AUTH_MODE_BASIC}:
            return json_error("auth_mode must be api-key or basic")
        api_key = payload.get("api_key")
        basic_user = payload.get("basic_user")
        basic_password = payload.get("basic_password")
        try:
            if isinstance(api_key, str):
                config.save_remote_api_key(api_key)
                effective_key = api_key.strip() or os.getenv("REMOTE_API_KEY", "").strip()
                remote.set_api_key(effective_key)
            elif api_key is not None:
                return json_error("api_key must be a string")

            if basic_user is not None and not isinstance(basic_user, str):
                return json_error("basic_user must be a string")
            if basic_password is not None and not isinstance(basic_password, str):
                return json_error("basic_password must be a string")
            if basic_user is not None or basic_password is not None:
                effective_user = (
                    basic_user.strip()
                    if isinstance(basic_user, str)
                    else remote.basic_user
                )
                effective_password = (
                    basic_password
                    if isinstance(basic_password, str)
                    else remote.basic_password
                )
                config.save_remote_basic_credentials(
                    effective_user,
                    effective_password,
                )
                remote.set_basic_credentials(effective_user, effective_password)

            effective_key = remote.api_key
            effective_user = remote.basic_user
            effective_password = remote.basic_password
            if auth_mode == AUTH_MODE_API_KEY and not effective_key:
                return json_error("An API key is required for API-key mode")
            if auth_mode == AUTH_MODE_BASIC and not (
                effective_user and effective_password
            ):
                return json_error(
                    "A Basic Auth username and password are required for Basic Auth mode"
                )
            config.save_remote_auth_mode(auth_mode)
            remote.set_auth_mode(auth_mode)
            started = state.start_catalog_sync(remote, force=True)
            runner.wake()
        except (OSError, ValueError) as exc:
            return json_error(f"Could not save credentials: {exc}", 500)
        return jsonify({
            "saved": True,
            "started": started,
            "auth_mode": remote.auth_mode,
            "basic_configured": remote.basic_configured,
            "api_key_configured": bool(remote.api_key),
            "message": f"{'Basic Auth' if remote.auth_mode == AUTH_MODE_BASIC else 'X-API-Key'} selected; catalog refresh started",
        }), 202

    @app.post("/api/settings/test")
    def api_settings_test():
        payload = request.get_json(silent=True) or {}
        requested_mode = payload.get("auth_mode", remote.auth_mode)
        if not isinstance(requested_mode, str):
            return json_error("auth_mode must be api-key or basic")
        auth_mode = normalize_auth_mode(requested_mode, default="")
        if auth_mode not in {AUTH_MODE_API_KEY, AUTH_MODE_BASIC}:
            return json_error("auth_mode must be api-key or basic")
        api_key = payload.get("api_key")
        basic_user = payload.get("basic_user")
        basic_password = payload.get("basic_password")
        if api_key is not None and not isinstance(api_key, str):
            return json_error("api_key must be a string")
        if basic_user is not None and not isinstance(basic_user, str):
            return json_error("basic_user must be a string")
        if basic_password is not None and not isinstance(basic_password, str):
            return json_error("basic_password must be a string")
        tester = RemoteClient(
            config.remote_base_url,
            api_key.strip() if isinstance(api_key, str) and api_key.strip() else remote.api_key,
            timeout=15,
            retries=1,
            basic_user=(
                basic_user.strip()
                if isinstance(basic_user, str) and basic_user.strip()
                else remote.basic_user
            ),
            basic_password=(
                basic_password
                if isinstance(basic_password, str) and basic_password
                else remote.basic_password
            ),
            auth_mode=auth_mode,
        )
        return jsonify(tester.test_connection(config.disc_source_path))

    @app.get("/api/targets")
    def api_targets():
        return jsonify({
            "targets": [
                {
                    "title_id": title["title_id"],
                    "title_name": title["title_name"],
                    "output_exists": True,
                }
                for title in database.titles()
                if Path(title["output_dir"]).is_dir()
            ]
        })

    @app.get("/api/storage")
    def api_storage():
        filesystem = shutil.disk_usage(config.output_dir)
        output_bytes = directory_size(config.output_dir)
        work_bytes = directory_size(config.work_dir)
        return jsonify({
            "filesystem": {
                "total": filesystem.total,
                "used": filesystem.used,
                "free": filesystem.free,
                "human_total": human_size(filesystem.total),
                "human_used": human_size(filesystem.used),
                "human_free": human_size(filesystem.free),
            },
            "application": {
                "output": output_bytes,
                "work": work_bytes,
                "total": output_bytes + work_bytes,
                "human_output": human_size(output_bytes),
                "human_work": human_size(work_bytes),
                "human_total": human_size(output_bytes + work_bytes),
            },
        })

    @app.get("/api/catalog")
    def api_catalog():
        kind = request.args.get("kind", "disc").strip().lower()
        if kind not in VALID_KINDS:
            return json_error("kind must be disc or digital")
        query = request.args.get("q", "")
        try:
            page = int(request.args.get("page", "1"))
            page_size = int(request.args.get("page_size", "50"))
        except ValueError:
            return json_error("page and page_size must be integers")
        items, total = database.catalog(kind, query, page, page_size)
        return jsonify({
            "items": [_serialize_item(item) for item in items],
            "total": total,
            "page": max(1, page),
            "page_size": min(200, max(1, page_size)),
        })

    @app.post("/api/catalog/refresh")
    def api_catalog_refresh():
        started = state.start_catalog_sync(remote, force=True)
        return jsonify({"started": started}), 202 if started else 200

    @app.get("/api/jobs")
    def api_jobs():
        try:
            limit = int(request.args.get("limit", "50"))
        except ValueError:
            return json_error("limit must be an integer")
        sort = request.args.get("sort", "newest").strip().lower()
        if sort not in {"newest", "oldest", "updated", "name"}:
            return json_error("sort must be newest, oldest, updated, or name")
        filter = request.args.get("filter", "active").strip().lower()
        if filter not in {"active", "downloaded", "all"}:
            return json_error("filter must be active, downloaded, or all")
        return jsonify({
            "jobs": [_serialize_job(job, database) for job in database.recent_jobs(limit, sort, filter)]
        })

    @app.get("/api/jobs/<int:job_id>")
    def api_job(job_id: int):
        job = database.get_job(job_id)
        if job is None:
            return json_error("job not found", 404)
        return jsonify(_serialize_job(job, database))

    @app.post("/api/jobs")
    def api_enqueue_jobs():
        payload = request.get_json(silent=True) or {}
        raw_ids = payload.get("item_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            return json_error("item_ids must be a non-empty list")
        if len(raw_ids) > 100:
            return json_error("Select at most 100 items at a time")
        try:
            item_ids = [int(value) for value in raw_ids]
        except (TypeError, ValueError):
            return json_error("item_ids must contain integers")
        raw_targets = payload.get("target_title_ids", [])
        if not isinstance(raw_targets, list):
            return json_error("target_title_ids must be a list")
        target_title_ids = {
            item_id: [str(target).upper() for target in raw_targets]
            for item_id in item_ids
        }
        try:
            result = database.enqueue_items(item_ids, target_title_ids)
        except (TypeError, ValueError) as exc:
            return json_error(str(exc))
        runner.wake()
        return jsonify({"jobs": result}), 202

    @app.post("/api/jobs/<int:job_id>/reprocess")
    def api_job_reprocess(job_id: int):
        if database.get_job(job_id) is None:
            return json_error("job not found", 404)
        try:
            result = database.reprocess_job(job_id)
        except ValueError as exc:
            return json_error(str(exc), 409)
        runner.wake()
        return jsonify({"job": result, "message": "Queued with current settings; previous job files are kept."}), 202

    @app.get("/api/jobs/<int:job_id>/install-guide")
    def api_job_install_guide(job_id: int):
        job = database.get_job(job_id)
        if job is None:
            return json_error("job not found", 404)
        paths = Database.decode_output_paths(job.get("output_path"))
        report = database.get_bundle_report(paths[0]) if paths else None
        return Response(install_guide(report or Database.decode_report(job.get("checks_json"))),
            mimetype="text/plain", headers={"Content-Disposition": 'attachment; filename="INSTALL-ON-XBOX.txt"'})

    @app.post("/api/jobs/<int:job_id>/download-confirmed")
    def api_download_confirmed(job_id: int):
        job = database.get_job(job_id)
        if job is None:
            return json_error("job not found", 404)
        if job["status"] != "complete":
            return json_error("only completed jobs can be marked as downloaded")
        payload = request.get_json(silent=True) or {}
        downloaded = payload.get("downloaded", True) is not False
        affected = database.mark_downloaded(job_id, downloaded)
        return jsonify({
            "downloaded": downloaded,
            "affected_jobs": affected,
        })

    def _send_job_archive(job_id: int, kind: str):
        job = database.get_job(job_id)
        if job is None or job["status"] != "complete":
            return json_error("completed download not found", 404)
        if kind == "snapshot":
            raw_path = job.get("snapshot_path")
            download_name = job.get("snapshot_name") or "job-snapshot.zip"
            allowed_root = config.work_dir.resolve()
        else:
            paths = Database.decode_output_paths(job.get("output_path"))
            if len(paths) != 1:
                return json_error("job has no current combined archive", 404)
            raw_path = paths[0]
            download_name = Path(raw_path).name
            allowed_root = config.output_dir.resolve()
        if not raw_path:
            return json_error("requested archive is not available", 404)
        archive_path = Path(raw_path).resolve()
        if not archive_path.is_relative_to(allowed_root) or not archive_path.is_file():
            return json_error("requested archive is no longer available", 404)
        return send_file(
            archive_path,
            as_attachment=True,
            download_name=download_name,
            max_age=0,
        )

    @app.get("/download/<int:job_id>")
    def download_job(job_id: int):
        return _send_job_archive(job_id, "combined")

    @app.get("/download/<int:job_id>/combined")
    def download_job_combined(job_id: int):
        return _send_job_archive(job_id, "combined")

    @app.get("/download/<int:job_id>/snapshot")
    def download_job_snapshot(job_id: int):
        return _send_job_archive(job_id, "snapshot")

    def _resolve_title_dirs(
        job: dict[str, Any],
        output_root: Path,
    ) -> tuple[list[Path], set[str]]:
        """Resolve the generated title directories for a job.

        Returns (title_dirs, title_ids).  Uses the artifact manifest first,
        then falls back to the titles table and the output_path filename.
        """
        title_dirs: list[Path] = []
        title_ids: set[str] = set()

        artifact_paths = Database.decode_output_paths(job.get("artifact_paths"))
        for raw_artifact in artifact_paths:
            artifact = Path(raw_artifact).resolve()
            if not artifact.is_relative_to(output_root):
                continue
            relative = artifact.relative_to(output_root)
            if len(relative.parts) >= 2:
                candidate = output_root / relative.parts[0] / relative.parts[1]
                if candidate not in title_dirs:
                    title_dirs.append(candidate)
                if re.fullmatch(r"[0-9A-Fa-f]{8}", relative.parts[1]):
                    title_ids.add(relative.parts[1].upper())

        for title in database.titles():
            title_dir = Path(title["output_dir"]).resolve()
            if any(
                Path(artifact).resolve().is_relative_to(title_dir)
                for artifact in artifact_paths
            ):
                if title_dir not in title_dirs:
                    title_dirs.append(title_dir)
                title_ids.add(title["title_id"])

        if not title_dirs:
            output_paths = Database.decode_output_paths(job.get("output_path"))
            for raw_path in output_paths:
                match = re.search(r"\[([0-9A-Fa-f]{8})\](?:\.zip)?$", Path(raw_path).name)
                if match:
                    title_id = match.group(1).upper()
                    title = database.get_title(title_id)
                    if title:
                        title_dir = Path(title["output_dir"]).resolve()
                        if title_dir not in title_dirs:
                            title_dirs.append(title_dir)
                        title_ids.add(title_id)
        return title_dirs, title_ids

    @app.post("/api/jobs/<int:job_id>/delete")
    def api_delete_job(job_id: int):
        job = database.get_job(job_id)
        if job is None:
            return json_error("job not found", 404)
        if job["status"] in {"queued", "working"}:
            return json_error("active jobs cannot be deleted; wait for them to finish")
        payload = request.get_json(silent=True) or {}
        if payload.get("confirm") is not True:
            return json_error("explicit confirmation is required")
        delete_output = bool(payload.get("delete_output", True))
        delete_work = bool(payload.get("delete_work", True))
        delete_title_dir = bool(payload.get("delete_title_dir", False))
        output_root = config.output_dir.resolve()
        work_root = config.work_dir.resolve()
        output_paths = set(Database.decode_output_paths(job.get("output_path")))
        affected_jobs: set[int] = {job_id}
        title_dirs: list[Path] = []
        title_ids_to_delete: set[str] = set()

        if delete_title_dir:
            title_dirs, title_ids_to_delete = _resolve_title_dirs(job, output_root)
            if not title_dirs:
                return json_error(
                    "could not determine the generated title directory; no files were deleted",
                    400,
                )
            affected_jobs.update(database.jobs_for_title_dirs(title_dirs))
            for affected_id in list(affected_jobs):
                affected = database.get_job(affected_id)
                if affected:
                    output_paths.update(Database.decode_output_paths(affected.get("output_path")))

        for raw_path in list(output_paths):
            output_path = Path(raw_path).resolve()
            if not output_path.is_relative_to(output_root):
                return json_error("refusing to delete a path outside games-GoD", 400)
            affected_jobs.update(database.jobs_for_output(raw_path))

        for title_dir in title_dirs:
            if not title_dir.is_relative_to(output_root):
                return json_error("refusing to delete a title outside games-GoD", 400)

        deleted_paths: list[str] = []
        if delete_output:
            for raw_path in output_paths:
                output_path = Path(raw_path).resolve()
                if output_path.is_file():
                    output_path.unlink()
                    deleted_paths.append(str(output_path))
        if delete_title_dir:
            for title_dir in title_dirs:
                if title_dir.is_dir():
                    shutil.rmtree(title_dir)
                    deleted_paths.append(str(title_dir))
            for title_id in title_ids_to_delete:
                database.delete_title(title_id)

        work_job_ids = affected_jobs if (delete_work and delete_title_dir) else {job_id}
        if delete_work:
            for work_job_id in work_job_ids:
                work_job = database.get_job(work_job_id)
                if work_job and work_job.get("snapshot_path"):
                    snapshot_path = Path(work_job["snapshot_path"]).resolve()
                    if not snapshot_path.is_relative_to(work_root):
                        return json_error("refusing to delete a snapshot outside work storage", 400)
                job_dir = (config.work_dir / "jobs" / f"{work_job_id:08d}").resolve()
                if not job_dir.is_relative_to(work_root):
                    return json_error("refusing to delete work outside downloaded-worked-on", 400)
                if job_dir.is_dir():
                    shutil.rmtree(job_dir)
                    deleted_paths.append(str(job_dir))

        if delete_output or delete_title_dir:
            database.mark_jobs_deleted(
                affected_jobs,
                "Generated output deleted from the website",
            )
        elif delete_work:
            database.mark_job_work_deleted(job_id, "Job work files deleted; combined archive retained")
        return jsonify({
            "deleted": deleted_paths,
            "affected_jobs": sorted(affected_jobs),
            "message": "Requested generated files were deleted",
        })

    @app.post("/api/cleanup/deleted-work")
    def api_cleanup_deleted_work():
        payload = request.get_json(silent=True) or {}
        if payload.get("confirm") is not True:
            return json_error("explicit confirmation is required")
        work_root = config.work_dir.resolve()
        deleted_paths: list[str] = []
        deleted_job_ids = database.deleted_job_ids()
        for job_id in deleted_job_ids:
            job_dir = (work_root / "jobs" / f"{job_id:08d}").resolve()
            if job_dir.is_relative_to(work_root) and job_dir.is_dir():
                shutil.rmtree(job_dir)
                deleted_paths.append(str(job_dir))
        source_cache = work_root / "source-cache"
        for item_id in database.orphaned_deleted_item_ids():
            for suffix in (".zip", ".zip.part"):
                candidate = (source_cache / f"{item_id:08d}{suffix}").resolve()
                if candidate.is_relative_to(work_root) and candidate.is_file():
                    candidate.unlink()
                    deleted_paths.append(str(candidate))
        return jsonify({
            "deleted": deleted_paths,
            "message": "Deleted job work and orphaned source-cache files were removed",
        })

    @app.errorhandler(413)
    def request_too_large(_error):
        return json_error("request too large", 413)

    from console_api import register_console_api
    register_console_api(app, config, database, runner, remote, start_background)

    if start_background:
        runner.start()
        state.start_catalog_sync(remote)

    return app


start_background = os.getenv("START_BACKGROUND", "false").strip().lower() in {"1", "true", "yes", "on"}
app = create_app(start_background=start_background)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
