from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ACTIVE_ITEM_STATUSES = {"queued", "working"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def normalize_search_text(value: str) -> str:
    """Make catalog searches insensitive to punctuation, case, and accents."""
    decomposed = unicodedata.normalize("NFKD", value or "")
    without_marks = "".join(
        char for char in decomposed if not unicodedata.combining(char)
    )
    return re.sub(r"[^0-9a-z]+", " ", without_marks.casefold()).strip()


class Database:
    def __init__(self, path: Path):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def initialize(self) -> None:
        connection = self.connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS catalog_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_path TEXT NOT NULL UNIQUE,
                    source_kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    search_text TEXT NOT NULL DEFAULT '',
                    remote_size INTEGER NOT NULL DEFAULT 0,
                    remote_modified TEXT,
                    remote_type TEXT,
                    status TEXT NOT NULL DEFAULT 'available',
                    title_id TEXT,
                    title_name TEXT,
                    last_job_id INTEGER,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_catalog_kind_name
                    ON catalog_items(source_kind, name COLLATE NOCASE);
                CREATE INDEX IF NOT EXISTS idx_catalog_status
                    ON catalog_items(status);

                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES catalog_items(id),
                    status TEXT NOT NULL DEFAULT 'queued',
                    stage TEXT NOT NULL DEFAULT 'queued',
                    progress REAL NOT NULL DEFAULT 0,
                    bytes_done INTEGER NOT NULL DEFAULT 0,
                    bytes_total INTEGER NOT NULL DEFAULT 0,
                    message TEXT,
                    output_path TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    artifact_paths TEXT,
                    snapshot_path TEXT,
                    snapshot_name TEXT,
                    target_title_ids TEXT,
                    download_confirmed_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_status_created
                    ON jobs(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_jobs_item
                    ON jobs(item_id, id DESC);

                CREATE TABLE IF NOT EXISTS titles (
                    title_id TEXT PRIMARY KEY,
                    title_name TEXT NOT NULL,
                    output_dir TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sync_state (
                    source_kind TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    item_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS bundle_reports (
                    archive_path TEXT PRIMARY KEY,
                    report_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            self._ensure_catalog_columns(connection)
            self._ensure_job_columns(connection)
        finally:
            connection.close()

    @staticmethod
    def _ensure_catalog_columns(connection: sqlite3.Connection) -> None:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(catalog_items)").fetchall()
        }
        if "search_text" not in columns:
            connection.execute(
                "ALTER TABLE catalog_items ADD COLUMN search_text TEXT NOT NULL DEFAULT ''"
            )
        connection.execute(
            "UPDATE catalog_items SET search_text = ? WHERE search_text = '' OR search_text IS NULL",
            ("",),
        )
        rows = connection.execute(
            "SELECT id, name, source_path FROM catalog_items WHERE search_text = ''"
        ).fetchall()
        for row in rows:
            connection.execute(
                "UPDATE catalog_items SET search_text = ? WHERE id = ?",
                (normalize_search_text(f"{row['name']} {row['source_path']}"), row["id"]),
            )

    @staticmethod
    def _ensure_job_columns(connection: sqlite3.Connection) -> None:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
        }
        migrations = {
            "artifact_paths": "TEXT",
            "snapshot_path": "TEXT",
            "snapshot_name": "TEXT",
            "target_title_ids": "TEXT",
            "download_confirmed_at": "TEXT",
            "checks_json": "TEXT",
            "reprocess_of": "INTEGER",
            "options_json": "TEXT",
        }
        for name, definition in migrations.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")

    def recover_running_jobs(self) -> int:
        connection = self.connect()
        try:
            now = utc_now()
            cursor = connection.execute(
                """
                UPDATE jobs
                   SET status = 'queued',
                       stage = 'queued',
                       message = 'Recovered after container restart',
                       updated_at = ?
                 WHERE status = 'working'
                """,
                (now,),
            )
            connection.execute(
                """
                UPDATE catalog_items
                   SET status = 'queued', updated_at = ?
                 WHERE status = 'working'
                """,
                (now,),
            )
            return cursor.rowcount
        finally:
            connection.close()

    def upsert_catalog(self, source_kind: str, records: Iterable[dict[str, Any]]) -> int:
        connection = self.connect()
        count = 0
        try:
            connection.execute("BEGIN")
            now = utc_now()
            for record in records:
                connection.execute(
                    """
                    INSERT INTO catalog_items (
                        source_path, source_kind, name, search_text, remote_size,
                        remote_modified, remote_type, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_path) DO UPDATE SET
                        source_kind = excluded.source_kind,
                        name = excluded.name,
                        search_text = excluded.search_text,
                        remote_size = excluded.remote_size,
                        remote_modified = excluded.remote_modified,
                        remote_type = excluded.remote_type,
                        updated_at = excluded.updated_at
                    """,
                    (
                        record["source_path"],
                        source_kind,
                        record["name"],
                        normalize_search_text(f"{record['name']} {record['source_path']}"),
                        int(record.get("size") or 0),
                        record.get("modified"),
                        record.get("type"),
                        now,
                        now,
                    ),
                )
                count += 1
            connection.execute("COMMIT")
            return count
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def set_sync_state(
        self,
        source_kind: str,
        status: str,
        item_count: int = 0,
        error: str | None = None,
    ) -> None:
        connection = self.connect()
        try:
            connection.execute(
                """
                INSERT INTO sync_state(source_kind, status, item_count, error, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source_kind) DO UPDATE SET
                    status = excluded.status,
                    item_count = excluded.item_count,
                    error = excluded.error,
                    updated_at = excluded.updated_at
                """,
                (source_kind, status, item_count, error, utc_now()),
            )
        finally:
            connection.close()

    def sync_states(self) -> list[dict[str, Any]]:
        connection = self.connect()
        try:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM sync_state ORDER BY source_kind"
            ).fetchall()]
        finally:
            connection.close()

    def catalog(
        self,
        source_kind: str,
        query: str = "",
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        page = max(1, page)
        page_size = min(200, max(1, page_size))
        offset = (page - 1) * page_size
        connection = self.connect()
        try:
            where = "source_kind = ?"
            params: list[Any] = [source_kind]
            tokens = normalize_search_text(query).split()
            for token in tokens:
                where += " AND search_text LIKE ?"
                params.append(f"%{token}%")
            total = connection.execute(
                f"SELECT COUNT(*) FROM catalog_items WHERE {where}", params
            ).fetchone()[0]
            rows = connection.execute(
                f"""
                SELECT c.*, j.status AS latest_job_status,
                       j.stage AS latest_job_stage, j.progress AS latest_job_progress,
                       j.id AS latest_job_id,
                       j.download_confirmed_at AS latest_download_confirmed_at
                  FROM catalog_items c
                  LEFT JOIN jobs j ON j.id = (
                      SELECT id FROM jobs j2
                       WHERE j2.item_id = c.id ORDER BY j2.id DESC LIMIT 1
                  )
                 WHERE {where}
                 ORDER BY c.name COLLATE NOCASE, c.id
                 LIMIT ? OFFSET ?
                """,
                params + [page_size, offset],
            ).fetchall()
            return [dict(row) for row in rows], total
        finally:
            connection.close()

    def catalog_counts(self) -> list[dict[str, Any]]:
        connection = self.connect()
        try:
            rows = connection.execute(
                """
                SELECT source_kind, status, COUNT(*) AS count
                  FROM catalog_items
                 GROUP BY source_kind, status
                 ORDER BY source_kind, status
                """
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def get_item(self, item_id: int) -> dict[str, Any] | None:
        connection = self.connect()
        try:
            return row_dict(connection.execute(
                "SELECT * FROM catalog_items WHERE id = ?", (item_id,)
            ).fetchone())
        finally:
            connection.close()

    def enqueue_items(
        self,
        item_ids: Iterable[int],
        target_title_ids: dict[int, list[str]] | None = None,
        reprocess_of: int | None = None,
        options: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        connection = self.connect()
        result: list[dict[str, Any]] = []
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = utc_now()
            for raw_id in item_ids:
                item_id = int(raw_id)
                item = connection.execute(
                    "SELECT * FROM catalog_items WHERE id = ?", (item_id,)
                ).fetchone()
                if item is None:
                    result.append({"item_id": item_id, "status": "missing"})
                    continue
                if item["status"] in ACTIVE_ITEM_STATUSES:
                    if options is not None:
                        active = connection.execute("SELECT options_json FROM jobs WHERE id=?", (item["last_job_id"],)).fetchone()
                        if active is None or json.loads(active[0] or "{}") != options:
                            raise ValueError("An active job uses different settings; wait for it to finish")
                    result.append({
                        "item_id": item_id,
                        "status": item["status"],
                        "job_id": item["last_job_id"],
                    })
                    continue
                if item["status"] == "complete" and reprocess_of is None:
                    result.append({
                        "item_id": item_id,
                        "status": "complete",
                        "job_id": item["last_job_id"],
                    })
                    continue
                selected_targets = (target_title_ids or {}).get(item_id, [])
                selected_targets = [str(value).upper() for value in selected_targets]
                cursor = connection.execute(
                    """
                    INSERT INTO jobs(
                        item_id, status, stage, message, target_title_ids,
                        created_at, updated_at, reprocess_of, options_json
                    ) VALUES (?, 'queued', 'queued', 'Waiting for worker', ?, ?, ?, ?, ?)
                    """,
                    (item_id, json.dumps(selected_targets), now, now, reprocess_of,
                     json.dumps(options) if options is not None else None),
                )
                job_id = cursor.lastrowid
                connection.execute(
                    """
                    UPDATE catalog_items
                       SET status = 'queued', last_job_id = ?, last_error = NULL,
                           updated_at = ?
                     WHERE id = ?
                    """,
                    (job_id, now, item_id),
                )
                result.append({
                    "item_id": item_id,
                    "status": "queued",
                    "job_id": job_id,
                })
            connection.execute("COMMIT")
            return result
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def reprocess_job(self, job_id: int) -> dict[str, Any]:
        previous = self.get_job(job_id)
        if previous is None:
            raise ValueError("Job not found")
        if previous["status"] not in {"complete", "failed"}:
            raise ValueError("Only finished or failed jobs can be reprocessed")
        return self.enqueue_items(
            [previous["item_id"]],
            {previous["item_id"]: self.decode_output_paths(previous.get("target_title_ids"))},
            reprocess_of=job_id,
        )[0]

    @staticmethod
    def decode_report(value: str | None) -> dict[str, Any] | None:
        try:
            result = json.loads(value or "null")
            return result if isinstance(result, dict) else None
        except (ValueError, TypeError):
            return None

    def get_bundle_report(self, archive_path: str) -> dict[str, Any] | None:
        connection = self.connect()
        try:
            row = connection.execute(
                "SELECT report_json FROM bundle_reports WHERE archive_path = ?", (archive_path,),
            ).fetchone()
            return self.decode_report(row[0]) if row else None
        finally:
            connection.close()

    def put_bundle_report(self, archive_path: str, report: dict[str, Any]) -> None:
        connection = self.connect()
        try:
            connection.execute(
                "INSERT INTO bundle_reports(archive_path, report_json, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(archive_path) DO UPDATE SET report_json=excluded.report_json, updated_at=excluded.updated_at",
                (archive_path, json.dumps(report), utc_now()),
            )
        finally:
            connection.close()

    def claim_next_job(self) -> dict[str, Any] | None:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT j.*, c.source_path, c.source_kind, c.name, c.remote_size,
                       c.remote_modified, c.remote_type, c.title_id, c.title_name
                  FROM jobs j
                  JOIN catalog_items c ON c.id = j.item_id
                 WHERE j.status = 'queued'
                 ORDER BY j.id
                 LIMIT 1
                """
            ).fetchone()
            if row is None:
                connection.execute("COMMIT")
                return None
            now = utc_now()
            connection.execute(
                """
                UPDATE jobs
                   SET status = 'working', stage = 'starting',
                       message = 'Starting job', started_at = ?, updated_at = ?
                 WHERE id = ?
                """,
                (now, now, row["id"]),
            )
            connection.execute(
                """
                UPDATE catalog_items SET status = 'working', updated_at = ?
                 WHERE id = ?
                """,
                (now, row["item_id"]),
            )
            connection.execute("COMMIT")
            claimed = dict(row)
            claimed.update(status="working", stage="starting", started_at=now)
            return claimed
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def update_job(self, job_id: int, **fields: Any) -> None:
        allowed = {
            "status", "stage", "progress", "bytes_done", "bytes_total",
            "message", "output_path", "error", "updated_at", "completed_at",
            "artifact_paths", "snapshot_path", "snapshot_name", "target_title_ids",
            "download_confirmed_at",
            "checks_json",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        updates.setdefault("updated_at", utc_now())
        assignments = ", ".join(f"{key} = ?" for key in updates)
        connection = self.connect()
        try:
            connection.execute(
                f"UPDATE jobs SET {assignments} WHERE id = ?",
                list(updates.values()) + [job_id],
            )
        finally:
            connection.close()

    def finish_job(
        self,
        job_id: int,
        item_id: int,
        success: bool,
        message: str,
        output_path: str | None = None,
        error: str | None = None,
        artifact_paths: str | None = None,
        snapshot_path: str | None = None,
        snapshot_name: str | None = None,
    ) -> None:
        connection = self.connect()
        try:
            now = utc_now()
            status = "complete" if success else "failed"
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE jobs
                   SET status = ?, stage = ?, progress = ?, message = ?,
                       output_path = ?, error = ?, artifact_paths = ?,
                       snapshot_path = ?, snapshot_name = ?, completed_at = ?, updated_at = ?
                 WHERE id = ?
                """,
                (
                    status,
                    "complete" if success else "failed",
                    100 if success else 0,
                    message,
                    output_path,
                    error,
                    artifact_paths,
                    snapshot_path,
                    snapshot_name,
                    now,
                    now,
                    job_id,
                ),
            )
            connection.execute(
                """
                UPDATE catalog_items
                   SET status = ?, last_error = ?, updated_at = ?
                 WHERE id = ?
                """,
                (status, error, now, item_id),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        connection = self.connect()
        try:
            row = connection.execute(
                """
                SELECT j.*, c.source_path, c.source_kind, c.name,
                       c.remote_size, c.title_id, c.title_name
                  FROM jobs j JOIN catalog_items c ON c.id = j.item_id
                 WHERE j.id = ?
                """,
                (job_id,),
            ).fetchone()
            return row_dict(row)
        finally:
            connection.close()

    def recent_jobs(
        self,
        limit: int = 50,
        sort: str = "newest",
        filter: str = "active",
    ) -> list[dict[str, Any]]:
        _SORT_OPTIONS = {
            "newest": "j.created_at DESC, j.id DESC",
            "oldest": "j.created_at ASC, j.id ASC",
            "updated": "j.updated_at DESC, j.id DESC",
            "name": "c.name COLLATE NOCASE ASC, j.id DESC",
        }
        order_by = _SORT_OPTIONS.get(sort, _SORT_OPTIONS["newest"])
        _FILTER_OPTIONS = {
            "active": "j.status != 'deleted' AND j.download_confirmed_at IS NULL",
            "downloaded": "j.status != 'deleted' AND j.download_confirmed_at IS NOT NULL",
            "all": "j.status != 'deleted'",
        }
        where_clause = _FILTER_OPTIONS.get(filter, _FILTER_OPTIONS["active"])
        connection = self.connect()
        try:
            rows = connection.execute(
                f"""
                SELECT j.*, c.source_path, c.source_kind, c.name,
                       c.remote_size, c.title_id, c.title_name
                  FROM jobs j JOIN catalog_items c ON c.id = j.item_id
                 WHERE {where_clause}
                 ORDER BY {order_by} LIMIT ?
                """,
                (min(200, max(1, limit)),),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def mark_downloaded(self, job_id: int, downloaded: bool = True) -> list[int]:
        job = self.get_job(job_id)
        if job is None or job["status"] != "complete":
            return []
        output_paths = self.decode_output_paths(job.get("output_path"))
        affected = self.jobs_for_output(output_paths[0]) if output_paths else [job_id]
        if job_id not in affected:
            affected.append(job_id)
        connection = self.connect()
        try:
            now = utc_now() if downloaded else None
            placeholders = ",".join("?" for _ in affected)
            connection.execute(
                f"UPDATE jobs SET download_confirmed_at = ?, updated_at = ? WHERE id IN ({placeholders})",
                [now, utc_now()] + affected,
            )
            return affected
        finally:
            connection.close()

    def titles(self) -> list[dict[str, Any]]:
        connection = self.connect()
        try:
            rows = connection.execute(
                "SELECT * FROM titles ORDER BY title_name COLLATE NOCASE, title_id"
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def output_reference_count(self, output_path: str) -> int:
        connection = self.connect()
        try:
            return int(connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE status = 'complete' AND output_path LIKE ?",
                (f'%"{output_path}"%',),
            ).fetchone()[0])
        finally:
            connection.close()

    def jobs_for_output(self, output_path: str) -> list[int]:
        connection = self.connect()
        try:
            rows = connection.execute(
                "SELECT id FROM jobs WHERE status = 'complete' AND output_path LIKE ?",
                (f'%"{output_path}"%',),
            ).fetchall()
            return [int(row[0]) for row in rows]
        finally:
            connection.close()

    def jobs_for_title_dirs(self, output_dirs: Iterable[Path]) -> list[int]:
        output_dirs = [Path(path).resolve() for path in output_dirs]
        connection = self.connect()
        try:
            rows = connection.execute(
                "SELECT id, artifact_paths FROM jobs WHERE status = 'complete'"
            ).fetchall()
            matches: list[int] = []
            for row in rows:
                artifacts = self.decode_output_paths(row["artifact_paths"])
                if any(
                    any(Path(artifact).resolve().is_relative_to(directory) for directory in output_dirs)
                    for artifact in artifacts
                ):
                    matches.append(int(row["id"]))
            return matches
        finally:
            connection.close()

    def mark_jobs_deleted(self, job_ids: Iterable[int], message: str) -> None:
        job_ids = [int(job_id) for job_id in job_ids]
        if not job_ids:
            return
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = utc_now()
            placeholders = ",".join("?" for _ in job_ids)
            rows = connection.execute(
                f"SELECT id, item_id FROM jobs WHERE id IN ({placeholders})",
                job_ids,
            ).fetchall()
            connection.execute(
                f"""
                UPDATE jobs
                   SET status = 'deleted', stage = 'deleted', progress = 0,
                       message = ?, output_path = NULL, artifact_paths = NULL,
                       snapshot_path = NULL, snapshot_name = NULL,
                       download_confirmed_at = NULL, updated_at = ?, completed_at = ?
                 WHERE id IN ({placeholders})
                """,
                [message, now, now] + job_ids,
            )
            for row in rows:
                connection.execute(
                    """
                    UPDATE catalog_items
                       SET status = 'available', last_error = ?, updated_at = ?
                     WHERE id = ? AND last_job_id = ?
                    """,
                    (message, now, row["item_id"], row["id"]),
                )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def mark_job_work_deleted(self, job_id: int, message: str) -> None:
        self.update_job(job_id, message=message)

    def deleted_job_ids(self) -> list[int]:
        connection = self.connect()
        try:
            return [int(row[0]) for row in connection.execute(
                "SELECT id FROM jobs WHERE status = 'deleted' ORDER BY id"
            ).fetchall()]
        finally:
            connection.close()

    def orphaned_deleted_item_ids(self) -> list[int]:
        connection = self.connect()
        try:
            rows = connection.execute(
                """
                SELECT DISTINCT j.item_id
                  FROM jobs j
                 WHERE j.status = 'deleted'
                   AND NOT EXISTS (
                       SELECT 1 FROM jobs active
                        WHERE active.item_id = j.item_id
                          AND active.status != 'deleted'
                   )
                """
            ).fetchall()
            return [int(row[0]) for row in rows]
        finally:
            connection.close()

    def delete_title(self, title_id: str) -> None:
        connection = self.connect()
        try:
            connection.execute("DELETE FROM titles WHERE title_id = ?", (title_id.upper(),))
        finally:
            connection.close()

    def get_title(self, title_id: str) -> dict[str, Any] | None:
        connection = self.connect()
        try:
            return row_dict(connection.execute(
                "SELECT * FROM titles WHERE title_id = ?", (title_id,)
            ).fetchone())
        finally:
            connection.close()

    def put_title(self, title_id: str, title_name: str, output_dir: str) -> None:
        connection = self.connect()
        try:
            now = utc_now()
            connection.execute(
                """
                INSERT INTO titles(title_id, title_name, output_dir, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(title_id) DO UPDATE SET
                    title_name = excluded.title_name,
                    output_dir = excluded.output_dir,
                    updated_at = excluded.updated_at
                """,
                (title_id, title_name, output_dir, now, now),
            )
        finally:
            connection.close()

    @staticmethod
    def encode_output_paths(paths: list[str]) -> str:
        return json.dumps(paths, separators=(",", ":"))

    @staticmethod
    def decode_output_paths(value: str | None) -> list[str]:
        if not value:
            return []
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return [value]
        return decoded if isinstance(decoded, list) else [str(decoded)]
