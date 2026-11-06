from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

from config import Config
from bundle import add_bundle_documents, bundle_member
from compatibility import compatibility_rule
from db import Database, utc_now
from god import repair_god_package
from icons import embed_game_icon
from formats import (
    CONTENT_ARCADE,
    CONTENT_DLC,
    CONTENT_GOD,
    CONTENT_TITLE_UPDATE,
    CONTENT_XBOX_ORIGINAL,
    STFSInfo,
    content_type_folder,
    find_iso_files,
    infer_title_id,
    is_cache_title_update,
    package_kind,
    parse_converter_output,
    parse_stfs_header,
    safe_extract_zip,
    sanitize_component,
    source_title_fallback,
)
from remote import RemoteClient
from unlock import unlock_output_directory, unlock_package
from xboxunity import XboxUnityClient, XboxUnityError
from validation import ValidationError, validate_god, validate_title_update

logger = logging.getLogger("xbox360.pipeline")


class PipelineError(RuntimeError):
    pass


class Pipeline:
    def __init__(self, config: Config, database: Database, remote: RemoteClient):
        self.config = config
        self.database = database
        self.remote = remote
        self._artifact_paths: set[Path] = set()
        self._god_checks: dict[Path, dict] = {}
        self._tu_results: list[dict] = []
        self._warnings: list[str] = []
        self._report: dict | None = None

    def _job_dir(self, job_id: int) -> Path:
        path = self.config.work_dir / "jobs" / f"{job_id:08d}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _log(self, job_dir: Path, message: str) -> None:
        with (job_dir / "job.log").open("a", encoding="utf-8") as output:
            output.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {message}\n")

    def _source_archive_path(self, job: dict[str, Any], job_dir: Path) -> Path:
        """Reuse a completed source archive when a later retry is queued."""
        job_source = job_dir / "source.zip"
        if job_source.exists() or job_source.with_name(job_source.name + ".part").exists():
            return job_source

        source_path = job["source_path"]
        jobs_root = self.config.work_dir / "jobs"
        for candidate_dir in sorted(jobs_root.glob("*/"), reverse=True):
            candidate = candidate_dir / "source.zip"
            metadata = candidate_dir / "job.json"
            if not candidate.is_file() or not metadata.is_file():
                continue
            try:
                previous = json.loads(metadata.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if previous.get("source_path") == source_path:
                expected_size = int(job.get("remote_size") or 0)
                if expected_size <= 0 or candidate.stat().st_size == expected_size:
                    return candidate

        cache_dir = self.config.work_dir / "source-cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir / f"{int(job['item_id']):08d}.zip"

    def _update(
        self,
        job_id: int,
        stage: str,
        progress: float,
        message: str,
        bytes_done: int | None = None,
        bytes_total: int | None = None,
    ) -> None:
        fields: dict[str, Any] = {
            "stage": stage,
            "progress": max(0, min(99, round(progress, 2))),
            "message": message,
        }
        if bytes_done is not None:
            fields["bytes_done"] = bytes_done
        if bytes_total is not None:
            fields["bytes_total"] = bytes_total
        self.database.update_job(job_id, **fields)

    def _title_output(self, title_id: str, title_name: str | None) -> Path:
        title_id = title_id.upper()
        existing = self.database.get_title(title_id)
        if existing:
            output_dir = Path(existing["output_dir"])
            output_dir.mkdir(parents=True, exist_ok=True)
            return output_dir
        name = sanitize_component(title_name or f"Title {title_id}")
        output_dir = self.config.output_dir / name / title_id
        output_dir.mkdir(parents=True, exist_ok=True)
        self.database.put_title(title_id, name, str(output_dir))
        return output_dir

    def _copy_file(self, source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".partial")
        shutil.copy2(source, temporary)
        temporary.replace(destination)
        self._artifact_paths.add(destination.resolve())

    def _merge_tree(self, source: Path, destination: Path) -> None:
        if source.name.lower().endswith(".data"):
            # Replace complete fragment sets when reprocessing; merging can
            # leave stale Data#### files from a longer previous conversion.
            staging = destination.with_name(destination.name + ".replacing")
            previous = destination.with_name(destination.name + ".previous")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.rmtree(staging, ignore_errors=True)
            shutil.copytree(source, staging)
            if previous.exists():
                raise PipelineError(f"An interrupted data-folder replacement needs review: {previous}")
            if destination.exists():
                destination.rename(previous)
            try:
                staging.rename(destination)
            except Exception:
                if previous.exists():
                    previous.rename(destination)
                raise
            shutil.rmtree(previous, ignore_errors=True)
            self._artifact_paths.update(p.resolve() for p in destination.rglob("*") if p.is_file())
            return
        destination.mkdir(parents=True, exist_ok=True)
        for child in source.iterdir():
            target = destination / child.name
            if child.is_dir():
                self._merge_tree(child, target)
            else:
                self._copy_file(child, target)

    def _inspect_iso_layout(self, iso: Path) -> dict[str, bool]:
        if not self.config.extractor_path.is_file():
            raise PipelineError(
                f"XISO extractor is missing: {self.config.extractor_path}"
            )
        try:
            result = subprocess.run(
                [str(self.config.extractor_path), "-l", str(iso)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
        except OSError as exc:
            raise PipelineError(f"Could not start XISO extractor: {exc}") from exc
        if result.returncode != 0:
            raise PipelineError(
                f"Could not inspect {iso.name} as an Xbox ISO: "
                f"{(result.stdout or '').splitlines()[-1:] or ['unknown error']}"
            )
        listing = (result.stdout or "").lower().replace("\\", "/")
        has_content = "content/0000000000000000" in listing
        has_default_xex = "/default.xex" in listing or listing.endswith("default.xex")
        has_disc_content_placeholder = "ffed2000/ffffffff" in listing
        has_install_content_path = bool(re.search(
            r"/content/0000000000000000/[0-9a-f]{8}/(?:00000002|000b0000|000d0000|ffffffff)(?:/|$)",
            listing,
        ))
        return {
            "has_content": has_content,
            "has_default_xex": has_default_xex,
            "has_install_content_path": has_install_content_path,
            "is_content_disc": has_content and (
                not has_default_xex
                or has_disc_content_placeholder
                or has_install_content_path
            ),
        }

    def _extract_content_disc(
        self,
        job_id: int,
        iso: Path,
        destination: Path,
    ) -> Path:
        destination.mkdir(parents=True, exist_ok=True)
        self._update(
            job_id,
            "extracting",
            45,
            f"Extracting content disc {iso.name}",
        )
        try:
            result = subprocess.run(
                [str(self.config.extractor_path), str(iso), "-d", str(destination), "-s"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
        except OSError as exc:
            raise PipelineError(f"Could not extract content disc: {exc}") from exc
        if result.returncode != 0:
            tail = "\n".join((result.stdout or "").splitlines()[-8:])
            raise PipelineError(
                f"Content disc extraction failed for {iso.name}: {tail}"
            )
        return destination

    def _run_converter(self, job_id: int, iso: Path, convert_dir: Path) -> tuple[Path, str | None]:
        convert_dir.mkdir(parents=True, exist_ok=True)
        command = [
            str(self.config.converter_path),
            str(iso),
            str(convert_dir),
            "-j",
            str(self.config.converter_threads),
        ]
        if (
            self.config.download_game_icons
            and self.config.converter_path.name == "iso2god-next"
        ):
            command.append("--fetch-icon")
        self._update(
            job_id,
            "converting",
            45,
            f"Converting {iso.name} to Games on Demand",
        )
        try:
            completed = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
        except OSError as exc:
            raise PipelineError(f"Could not start iso2god: {exc}") from exc
        output = completed.stdout or ""
        title_metadata = parse_converter_output(output)
        if completed.returncode != 0:
            tail = "\n".join(output.splitlines()[-12:])
            raise PipelineError(
                f"iso2god failed for {iso.name} (exit {completed.returncode}): {tail}"
            )
        title_id = title_metadata.title_id
        candidates = []
        if title_id:
            candidates.append(convert_dir / title_id.upper())
        candidates.extend(
            path for path in convert_dir.rglob("*")
            if path.is_dir()
            and len(path.name) == 8
            and all(char in "0123456789abcdefABCDEF" for char in path.name)
            and (path / "00007000").exists()
        )
        for candidate in candidates:
            if candidate.exists() and (candidate / "00007000").exists():
                repair_god_package(candidate)
                if self.config.download_game_icons:
                    try:
                        embed_game_icon(candidate, candidate.name)
                    except Exception as exc:
                        logger.warning(
                            "game_icon_failed title_id=%s error=%s",
                            candidate.name,
                            str(exc)[:200],
                        )
                return candidate, title_metadata.title_name
        raise PipelineError(
            f"iso2god completed but no 00007000 package was found for {iso.name}"
        )

    def _process_disc_archive(
        self,
        job_id: int,
        item: dict[str, Any],
        input_root: Path,
    ) -> list[Path]:
        isos = find_iso_files(input_root)
        if not isos:
            raise PipelineError("The selected archive does not contain an ISO image")
        output_dirs: dict[str, Path] = {}
        compatibility = compatibility_rule(item["name"])
        for index, iso in enumerate(isos, start=1):
            layout = self._inspect_iso_layout(iso)
            if compatibility and compatibility.action == "skip":
                raise PipelineError(
                    f"Compatibility rules identify {iso.name} as a non-GOD "
                    "disc; extract its documented content separately"
                )
            if layout["is_content_disc"] or (
                compatibility and compatibility.action == "content"
            ):
                extracted_root = self._extract_content_disc(
                    job_id,
                    iso,
                    input_root.parent / f"content-disc-{index:02d}",
                )
                content_root = self._content_tree_root(extracted_root)
                content_item = dict(item)
                if compatibility and compatibility.target_title_id:
                    target_ids = Database.decode_output_paths(
                        content_item.get("target_title_ids")
                    )
                    if compatibility.target_title_id not in target_ids:
                        target_ids.append(compatibility.target_title_id)
                    content_item["target_title_ids"] = Database.encode_output_paths(
                        target_ids
                    )
                content_outputs = self._process_digital_archive(
                    job_id,
                    content_item,
                    content_root,
                )
                for output_dir in content_outputs:
                    output_dirs[output_dir.name] = output_dir
                self._update(
                    job_id,
                    "organizing",
                    45 + (45 * index / len(isos)),
                    f"Organized content disc {index} of {len(isos)}",
                )
                continue
            convert_dir = input_root.parent / f"god-output-{index:02d}"
            god_root, title_name = self._run_converter(job_id, iso, convert_dir)
            title_id = god_root.name.upper()
            title_name = title_name or source_title_fallback(item["name"])
            output_dir = self._title_output(title_id, title_name)
            self._merge_tree(god_root, output_dir)
            output_dirs[title_id] = output_dir
            self._update(
                job_id,
                "converting",
                45 + (45 * index / len(isos)),
                f"Converted disc {index} of {len(isos)}",
            )
        if not output_dirs:
            raise PipelineError(
                "The selected disc did not produce a GOD or content output"
            )
        return list(output_dirs.values())

    @staticmethod
    def _content_tree_root(root: Path) -> Path:
        """Return the disc's Content tree, excluding raw game/install files."""
        for candidate in root.rglob("*"):
            if not candidate.is_dir() or candidate.name.lower() != "content":
                continue
            if any(
                child.is_dir() and child.name.lower() == "0000000000000000"
                for child in candidate.iterdir()
            ):
                return candidate
        return root

    @staticmethod
    def _inside_god_data(path: Path) -> bool:
        return any(parent.name.lower().endswith(".data") for parent in path.parents)

    @staticmethod
    def _archive_title_id(path: Path, root: Path, info: STFSInfo | None) -> str | None:
        if info and info.title_id not in {"00000000", "FFED2000", "FFFFFFFF"}:
            return info.title_id
        try:
            relative = path.relative_to(root)
        except ValueError:
            relative = path
        return infer_title_id(relative.parts)

    @staticmethod
    def _archive_content_folder(path: Path, root: Path) -> str | None:
        try:
            parts = path.relative_to(root).parts
        except ValueError:
            parts = path.parts
        known = {
            "00000002", "00007000", "00005000", "000D0000", "000B0000",
            "00080000", "00001000",
        }
        for part in parts:
            upper = part.upper()
            if upper == "FFFFFFFF":
                return content_type_folder(CONTENT_DLC)
            if upper in known:
                return upper
        return None

    def _copy_digital_file(
        self,
        source: Path,
        game_root: Path,
        target_folder: str,
        relative_name: str | None = None,
    ) -> None:
        filename = relative_name or source.name
        destination = game_root / target_folder / filename
        self._copy_file(source, destination)

    def _process_digital_archive(
        self,
        job_id: int,
        item: dict[str, Any],
        input_root: Path,
    ) -> list[Path]:
        files = [
            path for path in input_root.rglob("*")
            if path.is_file() and not self._inside_god_data(path)
        ]
        if not files:
            raise PipelineError("The selected digital archive contains no files")

        metadata = {path: parse_stfs_header(path) for path in files}
        # Install a bundled base game before evaluating its title updates.
        files.sort(key=lambda path: 0 if metadata[path] and metadata[path].is_god else 1)

        fallback_name = source_title_fallback(item["name"])
        target_title_ids = Database.decode_output_paths(item.get("target_title_ids"))
        output_dirs: dict[str, Path] = {}
        consumed: set[Path] = set()
        unknown_index = 0

        for index, source in enumerate(files, start=1):
            if source in consumed:
                continue
            info = metadata[source]
            title_id = self._archive_title_id(source, input_root, info)
            relative = source.relative_to(input_root)
            if title_id:
                title_name = None
                if info:
                    title_name = info.title_name or info.display_name
                game_root = self._title_output(title_id, title_name or fallback_name)
                output_dirs[title_id] = game_root
                kind = package_kind(info, source.name)
                if info is not None:
                    if info.content_type == CONTENT_DLC:
                        target_folder = content_type_folder(CONTENT_DLC)
                    elif info.content_type == CONTENT_TITLE_UPDATE:
                        target_folder = "Cache" if is_cache_title_update(source.name) else content_type_folder(CONTENT_TITLE_UPDATE)
                    elif info.content_type in {
                        CONTENT_GOD, CONTENT_XBOX_ORIGINAL, CONTENT_ARCADE,
                    }:
                        target_folder = content_type_folder(info.content_type)
                    else:
                        target_folder = info.content_folder
                else:
                    target_folder = self._archive_content_folder(source, input_root) or "extras"
                    kind = "content" if target_folder != "extras" else "unknown"
                if (info and info.content_type == CONTENT_TITLE_UPDATE) or is_cache_title_update(source.name):
                    games = self._games(game_root)
                    matching = [g for g in games if info and g["media_id"] == info.media_id]
                    if games and not matching:
                        raise ValidationError("Imported title update does not match the bundled game's Media ID")
                    validate_title_update(source, title_id=title_id,
                        base_version=matching[0]["base_version"] if matching else None)
                self._copy_digital_file(source, game_root, target_folder)
                consumed.add(source)
                if info and info.is_god:
                    data_dir = source.parent / f"{source.name}.data"
                    if data_dir.is_dir():
                        self._merge_tree(data_dir, game_root / target_folder / data_dir.name)
                        consumed.update(
                            path for path in data_dir.rglob("*") if path.is_file()
                        )
                self._update(
                    job_id,
                    "organizing",
                    40 + (45 * index / max(1, len(files))),
                    f"Organized {kind}: {source.name}",
                )
                continue

            # Preserve files with no readable STFS header rather than silently dropping them.
            # If the operator supplied manual targets, duplicate the unclassified entry
            # into each selected title bundle so ambiguous DLC can be assigned explicitly.
            unknown_index += 1
            inferred_title_id = infer_title_id(relative.parts)
            target_specs: list[tuple[str, str | None]] = []
            if inferred_title_id:
                target_specs.append((inferred_title_id, None))
            elif target_title_ids:
                for target_id in target_title_ids:
                    target = self.database.get_title(target_id)
                    target_specs.append((
                        target_id,
                        target["title_name"] if target else fallback_name,
                    ))
            else:
                target_specs.append(("UNKNOWN", fallback_name))
            target_folder = self._archive_content_folder(source, input_root)
            if target_folder is None and info and info.content_type == CONTENT_DLC:
                target_folder = content_type_folder(CONTENT_DLC)
            if target_folder is None and info and info.content_type == CONTENT_TITLE_UPDATE:
                target_folder = content_type_folder(CONTENT_TITLE_UPDATE)
            if target_folder is None and is_cache_title_update(source.name):
                target_folder = "Cache"
            if target_folder is None:
                target_folder = "extras"
            try:
                relative_name = str(relative) if target_folder == "extras" else source.name
            except UnicodeError:
                relative_name = f"entry-{unknown_index:05d}-{source.name}"
            for target_id, target_name in target_specs:
                game_root = self._title_output(target_id, target_name or fallback_name)
                output_dirs[target_id] = game_root
                self._copy_digital_file(source, game_root, target_folder, relative_name)
            consumed.add(source)
            self._update(
                job_id,
                "organizing",
                40 + (45 * index / max(1, len(files))),
                f"Preserved unclassified content: {source.name}",
            )

        if not output_dirs:
            raise PipelineError("No installable content was found in the archive")
        return list(output_dirs.values())

    def _unlock_output_dirs(
        self,
        job_id: int,
        output_dirs: list[Path],
        job_dir: Path,
        enabled: bool | None = None,
        current_job_only: bool = False,
    ) -> dict[str, int]:
        """Process eligible digital licenses, preserving SVOD/GoD headers.

        Returns a summary dict with ``unlocked``, ``already_unlocked``, and
        ``skipped`` counts.  When unlocking is disabled in config this is a
        no-op.
        """
        summary = {"unlocked": 0, "already_unlocked": 0, "skipped": 0}
        if enabled is None:
            enabled = self.config.effective_unlock_packages
        if not enabled:
            self._log(job_dir, "Unlock stage disabled; package licenses preserved")
            return summary
        backup_dir = job_dir / "unlock-backups"
        for output_dir in output_dirs:
            if current_job_only:
                paths = sorted(p for p in self._artifact_paths
                    if p.is_file() and p.is_relative_to(output_dir.resolve()) and not self._inside_god_data(p))
                results = [unlock_package(p, backup_dir / output_dir.name) for p in paths]
            else:
                results = unlock_output_directory(output_dir, backup_dir)
            for result in results:
                if result.unlocked:
                    summary["unlocked"] += 1
                elif result.already_unlocked:
                    summary["already_unlocked"] += 1
                elif result.skipped:
                    summary["skipped"] += 1
        self._log(
            job_dir,
            f"Unlock stage: {summary['unlocked']} unlocked, "
            f"{summary['already_unlocked']} already unlocked, "
            f"{summary['skipped']} skipped",
        )
        if summary["unlocked"] or summary["already_unlocked"]:
            self._update(
                job_id,
                "unlocking",
                88,
                f"Unlocked {summary['unlocked']} package(s); "
                f"{summary['already_unlocked']} already unlocked",
            )
        return summary

    def _games(self, output_dir: Path) -> list[dict]:
        games = []
        for path in sorted((output_dir / "00007000").glob("*")):
            if not path.is_file():
                continue
            key = path.resolve()
            if key not in self._god_checks:
                self._god_checks[key] = validate_god(path, full_data=key in self._artifact_paths)
            games.append(self._god_checks[key])
        return games

    def _warn(self, message: str) -> None:
        if message not in self._warnings:
            self._warnings.append(message)

    def _download_title_updates(self, job_id: int, output_dirs: list[Path], job_dir: Path) -> dict[str, int]:
        summary = {"downloaded": 0, "skipped": 0, "failed": 0}
        if not self.config.xboxunity_tu_downloads:
            return summary
        client = XboxUnityClient(timeout=30, retries=2)
        for output_dir in output_dirs:
            title_id = output_dir.name.upper()
            games = self._games(output_dir)
            if not games:
                summary["skipped"] += 1
                continue
            try:
                entries = client.get_title_updates(title_id)
            except XboxUnityError as exc:
                message = f"{title_id}: title-update lookup failed: {exc}"
                self._warn(message)
                self._tu_results.append({"title_id": title_id, "status": "failed", "message": message})
                self._log(job_dir, message)
                summary["failed"] += 1
                continue
            for game in games:
                media_id, base = game["media_id"], game["base_version"]
                latest = client.select_latest_for_media_id(entries, media_id, base)
                if latest is None:
                    self._tu_results.append({"title_id": title_id, "media_id": media_id,
                        "status": "not_found", "message": f"No update matches Media ID {media_id} and base version {base}."})
                    summary["skipped"] += 1
                    continue
                if not re.fullmatch(r"[0-9A-Fa-f]{40}", latest.sha1_hash):
                    message = f"{title_id}/{media_id}: XboxUnity did not supply a valid update content ID."
                    self._warn(message)
                    self._tu_results.append({"title_id": title_id, "media_id": media_id, "status": "failed", "message": message})
                    summary["failed"] += 1
                    continue
                # A validated current copy avoids an unnecessary redownload.
                existing = None
                for folder in ("000B0000", "Cache"):
                    for candidate in (output_dir / folder).glob("*"):
                        if not candidate.is_file():
                            continue
                        try:
                            validate_title_update(candidate, title_id=title_id, media_id=media_id,
                                base_version=base, content_id=latest.sha1_hash, expected_size=latest.size)
                            existing = candidate
                            break
                        except ValidationError:
                            continue
                try:
                    if existing is None:
                        self._update(job_id, "downloading", 82, f"Downloading and checking TU {latest.version} for {title_id}")
                        path = client.download_title_update(latest, output_dir / "000B0000")
                        self._artifact_paths.add(path.resolve())
                        summary["downloaded"] += 1
                    else:
                        path = existing
                        summary["skipped"] += 1
                    details = validate_title_update(path, title_id=title_id, media_id=media_id,
                        base_version=base, content_id=latest.sha1_hash, expected_size=latest.size)
                    self._tu_results.append({**details, "status": "verified", "version": latest.version,
                        "message": f"TU {latest.version} verified and included (Media ID {media_id})."})
                    self._log(job_dir, f"Verified TU {latest.version} for {title_id}/{media_id}, base {base}")
                except (XboxUnityError, ValidationError) as exc:
                    message = f"{title_id}/{media_id}: title-update download or validation failed: {exc}"
                    self._warn(message)
                    self._tu_results.append({"title_id": title_id, "media_id": media_id,
                        "status": "failed", "message": message})
                    self._log(job_dir, message)
                    summary["failed"] += 1
        return summary

    @staticmethod
    def _god_media_ids(output_dir: Path) -> set[str]:
        god_dir = output_dir / content_type_folder(CONTENT_GOD)
        media_ids: set[str] = set()
        if not god_dir.is_dir():
            return media_ids
        for path in sorted(god_dir.iterdir(), key=lambda item: item.name):
            if not path.is_file():
                continue
            info = parse_stfs_header(path)
            if info and info.content_type == CONTENT_GOD:
                media_ids.add(info.media_id.upper())
        return media_ids

    @classmethod
    def _god_media_id(cls, output_dir: Path) -> str | None:
        return next(iter(sorted(cls._god_media_ids(output_dir))), None)

    def _normalize_title_updates(self, output_dirs: list[Path], job_dir: Path) -> None:
        """Quarantine invalid updates; keep matching content and cache layouts."""
        preferred = {(r["title_id"], r.get("media_id")): r.get("content_id")
                     for r in self._tu_results if r["status"] == "verified"}
        for output_dir in output_dirs:
            title_id = output_dir.name.upper()
            games = self._games(output_dir)
            bases = {g["media_id"]: g["base_version"] for g in games}
            for folder_name in ("000B0000", "Cache"):
                for source in sorted((output_dir / folder_name).glob("*")):
                    if not source.is_file():
                        continue
                    info = parse_stfs_header(source)
                    error = None
                    try:
                        if games and (info is None or info.media_id not in bases):
                            raise ValidationError("Media ID does not match a game in this bundle")
                        details = validate_title_update(source, title_id=title_id,
                            base_version=bases.get(info.media_id) if info else None)
                        wanted = preferred.get((title_id, details["media_id"]))
                        if wanted and details["content_id"] != wanted:
                            error = "replaced by the verified latest update"
                    except ValidationError as exc:
                        error = str(exc)
                        self._warn(f"{title_id}: excluded {source.name}: {error}")
                    if error:
                        backup = job_dir / "title-update-backups" / title_id / folder_name / source.name
                        backup.parent.mkdir(parents=True, exist_ok=True)
                        if not backup.exists():
                            shutil.copy2(source, backup)
                        source.unlink()
                        self._artifact_paths.discard(source.resolve())
                        self._log(job_dir, f"Backed up and excluded {source.name}: {error}")
                        continue
                    folder = "Cache" if is_cache_title_update(source.name) else "000B0000"
                    target = output_dir / folder / source.name
                    if target != source:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        if target.exists() and target.read_bytes() != source.read_bytes():
                            raise PipelineError(f"Conflicting title-update copies: {source.name}")
                        source.replace(target)
                        self._artifact_paths.discard(source.resolve())
                        self._artifact_paths.add(target.resolve())

    def build_report(self, output_dirs: list[Path], unlock_enabled: bool | None,
                     unlock_summary: dict | None = None) -> dict:
        titles, game_count, update_count = [], 0, 0
        full_count = 0
        for output_dir in output_dirs:
            games = self._games(output_dir)
            game_count += len(games)
            full_count += sum(g["level"] == "full_data" for g in games)
            updates = [dict(r) for r in self._tu_results if r["title_id"] == output_dir.name.upper()]
            known = {r.get("content_id") for r in updates}
            bases = {g["media_id"]: g["base_version"] for g in games}
            for folder in ("000B0000", "Cache"):
                for path in sorted((output_dir / folder).glob("*")):
                    if not path.is_file():
                        continue
                    info = parse_stfs_header(path)
                    try:
                        if games and (info is None or info.media_id not in bases):
                            raise ValidationError("Update does not match the bundled game")
                        details = validate_title_update(path, title_id=output_dir.name,
                            base_version=bases.get(info.media_id) if info else None)
                        update_count += 1
                        if details["content_id"] not in known:
                            status = "verified" if games else "unverified"
                            updates.append({**details, "status": status,
                                "message": ("Included update passed metadata and game compatibility checks."
                                            if games else "Update metadata passed; add its base game to check compatibility.")})
                            if not games:
                                self._warn(f"{output_dir.name}: add the base game to check title-update compatibility.")
                    except ValidationError as exc:
                        self._warn(f"{output_dir.name}: update {path.name} could not be verified: {exc}")
            titles.append({"title_id": output_dir.name, "name": output_dir.parent.name,
                           "games": games, "updates": updates})
        if game_count:
            god_detail = ("GoD header and hash tables verified." if game_count == 1
                          else f"Headers and hash tables verified for {game_count} GoD packages.")
            if full_count:
                god_detail += f" All data blocks checked for {full_count} {'game' if full_count == 1 else 'games'}."
        else:
            god_detail = "No Xbox 360 GoD package in this bundle; digital content only."
        failures = sum(r["status"] == "failed" for r in self._tu_results)
        unverified = any(u.get("status") == "unverified" for t in titles for u in t["updates"])
        if failures:
            update_state, update_detail = "warning", f"Update lookup or download failed. {update_count} previously verified updates included."
        elif unverified:
            update_state, update_detail = "warning", "Update metadata checked; the base game is needed to verify compatibility."
        elif update_count:
            update_state, update_detail = "passed", f"{update_count} compatible title {'update' if update_count == 1 else 'updates'} included."
        elif not self.config.xboxunity_tu_downloads:
            update_state, update_detail = "off", "Automatic update downloads are off; no update is included."
        elif game_count:
            update_state, update_detail = "not_found", "No compatible title update is included. A game may not need one."
        else:
            update_state, update_detail = "not_applicable", "Add the base game to match updates automatically."
        if unlock_enabled is None:
            unlock_state, unlock_detail = "unknown", "Unlock setting was not recorded for this older bundle. Existing files kept."
        else:
            unlock_state = "passed" if unlock_enabled else "off"
            unlock_detail = f"{'Enabled' if unlock_enabled else 'Disabled'} for this job. GoD converter licenses preserved."
            if unlock_summary and unlock_enabled:
                unlock_detail += f" {unlock_summary['unlocked']} newly unlocked; {unlock_summary['already_unlocked']} already unlocked."
        return {"schema": 1, "created_at": utc_now(), "unlock_enabled": unlock_enabled,
            "title_update_downloads": self.config.xboxunity_tu_downloads,
            "console_tested": False, "warnings": list(self._warnings), "titles": titles,
            "checks": [
                {"key": "god", "state": "passed" if game_count else "not_applicable", "label": "GoD integrity", "detail": god_detail},
                {"key": "updates", "state": update_state, "label": "Title updates", "detail": update_detail},
                {"key": "unlock", "state": unlock_state, "label": "XBLA / DLC unlock", "detail": unlock_detail},
                {"key": "console", "state": "unverified", "label": "Xbox launch", "detail": "Not tested on your console. File checks cannot confirm playback."},
            ]}

    def _make_bundle_zip(
        self,
        job_id: int,
        item: dict[str, Any],
        output_dirs: list[Path],
        job_dir: Path,
    ) -> Path:
        if len(output_dirs) == 1:
            title_dir = output_dirs[0]
            zip_name = f"{sanitize_component(title_dir.parent.name)} [{title_dir.name}].zip"
        else:
            zip_name = f"{source_title_fallback(item['name'])} [bundle-{job_id}].zip"
        target = self.config.output_dir / sanitize_component(zip_name, "xbox360-content.zip")
        # Keep the temporary ZIP beside the final file. The work and output
        # directories are separate bind mounts and may be different filesystems.
        partial = target.with_name(target.name + ".part")
        if partial.exists():
            partial.unlink()

        all_files = [
            path for title_dir in sorted(set(output_dirs), key=str)
            for path in sorted(title_dir.rglob("*")) if path.is_file()
        ]
        total = sum(path.stat().st_size for path in all_files)
        written = 0
        self._update(job_id, "packaging", 90, "Building final ZIP", 0, total)
        with zipfile.ZipFile(
            partial,
            "w",
            compression=zipfile.ZIP_STORED,
            allowZip64=True,
        ) as archive:
            members: set[str] = set()
            for path in all_files:
                member = bundle_member(path, self.config.output_dir)
                if member.casefold() in members:
                    raise PipelineError(f"Two output files map to the same Xbox path: {member}")
                members.add(member.casefold())
                archive.write(path, member)
                written += path.stat().st_size
                self._update(
                    job_id,
                    "packaging",
                    90 + (9 * written / max(1, total)),
                    f"Added {path.name} to final ZIP",
                    written,
                    total,
                )
            add_bundle_documents(archive, self._report)
        partial.replace(target)
        if self._report is not None:
            self.database.put_bundle_report(str(target), self._report)
        return target

    def _make_snapshot_zip(
        self,
        job_id: int,
        item: dict[str, Any],
        job_dir: Path,
    ) -> tuple[Path | None, str | None]:
        artifacts = sorted(
            {path for path in self._artifact_paths if path.is_file()},
            key=str,
        )
        if not artifacts:
            return None, None
        snapshot_name = (
            f"{sanitize_component(Path(item['name']).stem)} [job-{job_id}].zip"
        )
        snapshot_path = job_dir / "job-snapshot.zip"
        partial = job_dir / "job-snapshot.zip.part"
        partial.unlink(missing_ok=True)
        self._update(job_id, "packaging", 99, "Saving individual job snapshot")
        with zipfile.ZipFile(
            partial,
            "w",
            compression=zipfile.ZIP_STORED,
            allowZip64=True,
        ) as archive:
            for path in artifacts:
                archive.write(path, path.relative_to(self.config.output_dir))
        partial.replace(snapshot_path)
        return snapshot_path, snapshot_name

    def run(self, job: dict[str, Any]) -> None:
        job_id = int(job["id"])
        job_dir = self._job_dir(job_id)
        self._artifact_paths = set()
        self._god_checks = {}
        self._tu_results = []
        self._warnings = []
        self._report = None
        self._log(job_dir, f"Starting {job['source_path']}")
        logger.info("job_started job_id=%d item=%s source=%s", job_id, job.get("name", ""), job["source_path"])
        source_archive = self._source_archive_path(job, job_dir)
        extraction_dir = job_dir / "extracted"
        metadata_path = job_dir / "job.json"
        metadata_path.write_text(json.dumps(job, indent=2, default=str), encoding="utf-8")
        try:
            job_options = json.loads(job.get("options_json") or "{}")
            unlock_enabled = job_options.get("unlock_packages", self.config.effective_unlock_packages)
            if not isinstance(unlock_enabled, bool):
                raise PipelineError("Invalid captured unlock setting")
            self._log(
                job_dir,
                f"XBLA/DLC unlock {'enabled' if unlock_enabled else 'disabled'}; "
                "GoD license metadata is preserved",
            )
            self._update(job_id, "downloading", 1, f"Downloading {job['name']}")
            last_update = 0.0

            def download_progress(done: int, total: int) -> None:
                nonlocal last_update
                now = time.monotonic()
                if now - last_update < 1.0 and done != total:
                    return
                last_update = now
                ratio = done / total if total else 0
                self._update(
                    job_id,
                    "downloading",
                    2 + (28 * ratio),
                    f"Downloading {job['name']}",
                    done,
                    total,
                )

            self.remote.download_file(
                job["source_path"],
                source_archive,
                expected_size=int(job.get("remote_size") or 0),
                progress=download_progress,
            )
            self._log(job_dir, "Source download complete")
            self._update(job_id, "extracting", 31, "Extracting source archive")
            if extraction_dir.exists():
                shutil.rmtree(extraction_dir)
            extraction_dir.mkdir(parents=True, exist_ok=True)

            if zipfile.is_zipfile(source_archive):
                safe_extract_zip(
                    source_archive,
                    extraction_dir,
                    max_members=self.config.max_zip_members,
                )
                input_root = extraction_dir
            else:
                direct_name = Path(job["name"]).name
                if Path(direct_name).suffix.lower() != ".iso":
                    direct_name = f"{Path(direct_name).stem}.iso"
                direct_iso = extraction_dir / direct_name
                shutil.copy2(source_archive, direct_iso)
                input_root = extraction_dir

            isos = find_iso_files(input_root)
            if isos:
                output_dirs = self._process_disc_archive(job_id, job, input_root)
            else:
                output_dirs = self._process_digital_archive(job_id, job, input_root)
            self._log(job_dir, f"Organized {len(output_dirs)} output directory(s)")
            logger.info("job_organized job_id=%d output_dirs=%d", job_id, len(output_dirs))
            self._normalize_title_updates(output_dirs, job_dir)
            tu_summary = self._download_title_updates(job_id, output_dirs, job_dir)
            self._normalize_title_updates(output_dirs, job_dir)
            if tu_summary["downloaded"]:
                self._log(job_dir, f"XboxUnity TUs: {tu_summary['downloaded']} downloaded, {tu_summary['skipped']} skipped")
            unlock_summary = self._unlock_output_dirs(
                job_id, output_dirs, job_dir, enabled=unlock_enabled, current_job_only=True,
            )
            self._update(job_id, "checking", 89, "Recording game and title-update checks")
            self._report = self.build_report(output_dirs, unlock_enabled, unlock_summary)
            self.database.update_job(job_id, checks_json=json.dumps(self._report))
            final_zip = self._make_bundle_zip(job_id, job, output_dirs, job_dir)
            snapshot_path, snapshot_name = self._make_snapshot_zip(job_id, job, job_dir)
            self._log(job_dir, f"Final ZIP: {final_zip}")
            logger.info("job_complete job_id=%d zip=%s", job_id, final_zip.name)
            if not self.config.keep_source_archives:
                source_archive.unlink(missing_ok=True)
            shutil.rmtree(extraction_dir, ignore_errors=True)
            artifact_paths = self.database.encode_output_paths(
                [str(path) for path in sorted(self._artifact_paths, key=str)]
            )
            unlock_note = ""
            if unlock_enabled and (unlock_summary["unlocked"] or unlock_summary["already_unlocked"]):
                unlock_note = (
                    f" ({unlock_summary['unlocked']} unlocked, "
                    f"{unlock_summary['already_unlocked']} already unlocked)"
                )
            self.database.finish_job(
                job_id,
                int(job["item_id"]),
                True,
                f"Ready to download{' with warnings' if self._warnings else ''}: {final_zip.name}{unlock_note}",
                self.database.encode_output_paths([str(final_zip)]),
                artifact_paths=artifact_paths,
                snapshot_path=str(snapshot_path) if snapshot_path else None,
                snapshot_name=snapshot_name,
            )
        except Exception as exc:
            error = str(exc).strip() or exc.__class__.__name__
            self._log(job_dir, f"FAILED: {error}")
            logger.error("job_failed job_id=%d error=%s", job_id, error[:200])
            self.database.finish_job(
                job_id,
                int(job["item_id"]),
                False,
                "Failed",
                error=error[:2000],
            )


class JobRunner:
    def __init__(self, pipeline: Pipeline, database: Database):
        self.pipeline = pipeline
        self.database = database
        self.wake_event = threading.Event()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self._run,
            name="xbox360-job-worker",
            daemon=True,
        )

    def start(self) -> None:
        self.database.recover_running_jobs()
        self.thread.start()

    def wake(self) -> None:
        self.wake_event.set()

    def stop(self) -> None:
        self.stop_event.set()
        self.wake_event.set()

    def _run(self) -> None:
        while not self.stop_event.is_set():
            job = self.database.claim_next_job()
            if job is None:
                self.wake_event.wait(2)
                self.wake_event.clear()
                continue
            self.pipeline.run(job)
