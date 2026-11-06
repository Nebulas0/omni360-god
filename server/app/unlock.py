"""License-block postprocessing for eligible STFS content packages.

Retains the existing license-block handling for eligible digital content.
SVOD / GoD and original-Xbox packages are excluded: the converter emits
their license metadata, including an all-ones first licensee ID, and this
stage must preserve it. Clearing it does not preserve converter output.

License entries are in the base header, outside the metadata digest and
payload hash tree. A valid metadata digest therefore does not detect an
accidentally erased license entry.

Only the generated copies inside the output directory are modified.  Source
archives are never touched.  Optional ``.x360lock.bak`` sidecars are stored
in a separate backup directory so they do not pollute the final ZIP archives.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from formats import (
    CONTENT_ARCADE,
    CONTENT_DLC,
    CONTENT_GOD,
    CONTENT_TITLE_UPDATE,
    CONTENT_XBOX_ORIGINAL,
    STFS_MAGICS,
    STFSInfo,
    is_cache_title_update,
    parse_stfs_header,
)

LICENSE_BLOCK_OFFSET = 0x22C
LICENSE_BLOCK_SIZE = 0x100
BACKUP_SUFFIX = ".x360lock.bak"

# Content types that represent licensed content and should be unlocked.
#   0x00000002 – DLC / Marketplace content
#   0x000D0000 – Xbox Live Arcade
#   0x00009000 – Avatar items / game add-on bundles
_UNLOCKABLE_TYPES = {
    CONTENT_DLC,
    CONTENT_ARCADE,
    0x00009000,
}


@dataclass
class UnlockResult:
    path: Path
    content_type: int
    title_id: str
    already_unlocked: bool
    unlocked: bool
    skipped: bool
    reason: str


def is_unlocked(path: Path) -> bool:
    """Return True if the entire 0x100-byte license block is already zeroed."""
    try:
        with path.open("rb") as source:
            source.seek(LICENSE_BLOCK_OFFSET)
            block = source.read(LICENSE_BLOCK_SIZE)
        return len(block) == LICENSE_BLOCK_SIZE and not any(block)
    except OSError:
        return False


def should_unlock(info: STFSInfo | None, filename: str) -> bool:
    """Determine whether a package should be unlocked based on STFS metadata.

    Title updates (0x000B0000) and cache title updates (``TU_*``) are never
    unlocked — they are patches, not licensed content.
    SVOD packages keep the license metadata supplied by the converter.
    """
    if info is None:
        return False
    if info.content_type in {CONTENT_GOD, CONTENT_XBOX_ORIGINAL}:
        return False
    if info.content_type == CONTENT_TITLE_UPDATE:
        return False
    if is_cache_title_update(filename):
        return False
    return info.content_type in _UNLOCKABLE_TYPES


def unlock_package(
    path: Path,
    backup_dir: Path | None = None,
) -> UnlockResult:
    """Unlock a single STFS package by zeroing the license block at 0x22C.

    If *backup_dir* is provided, a copy of the original file is stored there
    before modification.  If the file is already unlocked no backup is
    created and no modification is performed.

    The file is copied to a temporary sidecar (streamed, constant memory)
    and only the 0x100-byte license block is rewritten via seek+write, then
    the temp atomically replaces the original.  This avoids loading
    multi-hundred-megabyte DLC packages entirely into memory.
    """
    info = parse_stfs_header(path)
    if not should_unlock(info, path.name):
        return UnlockResult(
            path=path,
            content_type=info.content_type if info else 0,
            title_id=info.title_id if info else "00000000",
            already_unlocked=False,
            unlocked=False,
            skipped=True,
            reason=(
                "preserve SVOD converter license metadata"
                if info and info.is_god else "not eligible for unlock"
            ),
        )

    file_size = path.stat().st_size
    if file_size < LICENSE_BLOCK_OFFSET + LICENSE_BLOCK_SIZE:
        return UnlockResult(
            path=path,
            content_type=info.content_type,
            title_id=info.title_id,
            already_unlocked=False,
            unlocked=False,
            skipped=True,
            reason="file too small for license block",
        )

    if is_unlocked(path):
        return UnlockResult(
            path=path,
            content_type=info.content_type,
            title_id=info.title_id,
            already_unlocked=True,
            unlocked=False,
            skipped=False,
            reason="already unlocked",
        )

    if backup_dir is not None:
        backup_path = backup_dir / path.parent.name / (path.name + BACKUP_SUFFIX)
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        if not backup_path.exists():
            shutil.copy2(path, backup_path)

    temporary = path.with_name(path.name + ".unlocking")
    shutil.copy2(path, temporary)
    with temporary.open("r+b") as output:
        output.seek(LICENSE_BLOCK_OFFSET)
        output.write(b"\x00" * LICENSE_BLOCK_SIZE)
        output.flush()
        try:
            os.fsync(output.fileno())
        except OSError:
            pass
    temporary.replace(path)

    return UnlockResult(
        path=path,
        content_type=info.content_type,
        title_id=info.title_id,
        already_unlocked=False,
        unlocked=True,
        skipped=False,
        reason="unlocked",
    )


def unlock_output_directory(
    output_dir: Path,
    backup_dir: Path | None = None,
) -> list[UnlockResult]:
    """Walk an output directory and unlock all eligible STFS packages.

    Files inside ``.data`` directories (SVOD data fragments), backup
    sidecars, and temporary files are skipped.
    """
    results: list[UnlockResult] = []
    for path in sorted(output_dir.rglob("*")):
        if not path.is_file():
            continue
        name = path.name
        if name.endswith(BACKUP_SUFFIX) or name.endswith(".partial") or name.endswith(".unlocking"):
            continue
        if any(parent.name.lower().endswith(".data") for parent in path.parents):
            continue
        try:
            with path.open("rb") as source:
                magic = source.read(4)
        except OSError:
            continue
        if magic not in STFS_MAGICS:
            continue
        results.append(unlock_package(path, backup_dir))
    return results
