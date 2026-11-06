from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from formats import CONTENT_GOD, parse_stfs_header

BLOCK_SIZE = 0x1000
PART_SIZE = BLOCK_SIZE * 0xA290
PART_COUNT_OFFSET = 0x3A0
PARTS_SIZE_OFFSET = 0x3A4
BLOCK_COUNT_OFFSET = 0x392
MHT_HASH_OFFSET = 0x37D
HEADER_DIGEST_OFFSET = 0x32C
HEADER_DIGEST_START = 0x344
HEADER_DIGEST_LENGTH = 0xACBC


@dataclass(frozen=True)
class GodRepairResult:
    header: Path
    data_files: int
    blocks_before: int
    blocks_after: int
    repaired: bool


def _hash_count(hash_list: bytes) -> int:
    count = 0
    for offset in range(0, len(hash_list), 20):
        value = hash_list[offset:offset + 20]
        if len(value) < 20 or not any(value):
            break
        count += 1
    return count


def _part_layout(path: Path) -> tuple[int, int]:
    """Return (subpart count, data-block count) without reading payload data."""
    subparts = 0
    blocks = 0
    with path.open("rb") as source:
        if len(source.read(BLOCK_SIZE)) != BLOCK_SIZE:
            raise ValueError(f"GOD data part is missing its master hash list: {path}")
        while True:
            hash_list = source.read(BLOCK_SIZE)
            if not hash_list or not any(hash_list):
                break
            count = _hash_count(hash_list)
            if count <= 0:
                break
            payload = source.read(count * BLOCK_SIZE)
            if len(payload) != count * BLOCK_SIZE:
                raise ValueError(f"GOD data part has a truncated subpart: {path}")
            subparts += 1
            blocks += count
    return subparts, blocks


def _find_header(package_dir: Path) -> Path:
    headers = []
    for path in sorted(package_dir.iterdir(), key=lambda item: item.name):
        if not path.is_file():
            continue
        info = parse_stfs_header(path)
        if info and info.content_type == CONTENT_GOD:
            headers.append(path)
    if len(headers) != 1:
        raise ValueError(
            f"Expected one GOD header in {package_dir}, found {len(headers)}"
        )
    return headers[0]


def _write_last_part(
    source_path: Path,
    target_path: Path,
    blocks_to_keep: int,
) -> bytes:
    master_entries: list[bytes] = []
    remaining = blocks_to_keep
    with source_path.open("rb") as source, target_path.open("wb") as target:
        source_master = source.read(BLOCK_SIZE)
        if len(source_master) != BLOCK_SIZE:
            raise ValueError(f"GOD data part is missing its master hash list: {source_path}")
        target.write(b"\x00" * BLOCK_SIZE)

        while remaining:
            hash_list = source.read(BLOCK_SIZE)
            if len(hash_list) != BLOCK_SIZE:
                raise ValueError(f"GOD data part ended before its final blocks: {source_path}")
            count = _hash_count(hash_list)
            if count <= 0:
                raise ValueError(f"GOD data part has too few blocks: {source_path}")
            payload = source.read(count * BLOCK_SIZE)
            if len(payload) != count * BLOCK_SIZE:
                raise ValueError(f"GOD data part has a truncated subpart: {source_path}")

            take = min(remaining, count)
            kept_hashes = bytearray(BLOCK_SIZE)
            kept_hashes[:take * 20] = hash_list[:take * 20]
            target.write(kept_hashes)
            target.write(payload[:take * BLOCK_SIZE])
            master_entries.append(hashlib.sha1(kept_hashes).digest())
            remaining -= take

        new_master = bytearray(BLOCK_SIZE)
        for index, digest in enumerate(master_entries):
            new_master[index * 20:(index + 1) * 20] = digest
        target.seek(0)
        target.write(new_master)
        target.flush()
        os.fsync(target.fileno())
    return bytes(new_master)


def _replace_master_hash(path: Path, digest: bytes, subparts: int) -> bytes:
    with path.open("r+b") as target:
        master = bytearray(target.read(BLOCK_SIZE))
        if len(master) != BLOCK_SIZE:
            raise ValueError(f"GOD data part is missing its master hash list: {path}")
        offset = subparts * 20
        if offset + 20 > BLOCK_SIZE:
            raise ValueError(f"GOD master hash list is full: {path}")
        master[offset:offset + 20] = digest
        target.seek(0)
        target.write(master)
        target.flush()
        os.fsync(target.fileno())
    return bytes(master)


def repair_god_package(package_dir: Path) -> GodRepairResult:
    """Trim converter padding that lies beyond the GOD header block count.

    iso2god-rs calculates a trimmed block count but its part writer can copy
    the remaining ISO tail into the final part. The console uses the header's
    block count, so remove that unallocated tail and rebuild the MHT chain.
    """
    header_path = _find_header(package_dir / "00007000")
    info = parse_stfs_header(header_path)
    if info is None or info.content_type != CONTENT_GOD:
        raise ValueError(f"Invalid GOD header: {header_path}")

    data_dir = header_path.parent / f"{header_path.name}.data"
    data_files = sorted(
        path for path in data_dir.glob("Data*")
        if path.is_file() and re.fullmatch(r"Data\d{4}", path.name)
    )
    if not data_files:
        raise ValueError(f"GOD data directory is empty: {data_dir}")

    header = bytearray(header_path.read_bytes())
    block_count = int.from_bytes(
        header[BLOCK_COUNT_OFFSET:BLOCK_COUNT_OFFSET + 3],
        "big",
    )
    part_count = int.from_bytes(
        header[PART_COUNT_OFFSET:PART_COUNT_OFFSET + 4],
        "little",
    )
    if part_count != len(data_files):
        raise ValueError(
            f"GOD header expects {part_count} data files, found {len(data_files)}"
        )

    layouts = [_part_layout(path) for path in data_files]
    blocks_before = sum(blocks for _, blocks in layouts)
    if blocks_before < block_count:
        raise ValueError(
            f"GOD data contains {blocks_before} blocks but header requires {block_count}"
        )
    if blocks_before == block_count:
        return GodRepairResult(
            header=header_path,
            data_files=len(data_files),
            blocks_before=blocks_before,
            blocks_after=blocks_before,
            repaired=False,
        )

    blocks_before_last = sum(blocks for _, blocks in layouts[:-1])
    blocks_in_last = block_count - blocks_before_last
    if blocks_in_last <= 0 or blocks_in_last > layouts[-1][1]:
        raise ValueError("GOD block count cannot be represented by its final part")

    last_path = data_files[-1]
    temporary = last_path.with_name(last_path.name + ".repairing")
    new_last_master = _write_last_part(
        last_path,
        temporary,
        blocks_in_last,
    )
    temporary.replace(last_path)

    current_master_digest = hashlib.sha1(new_last_master).digest()
    for index in range(len(data_files) - 2, -1, -1):
        master = _replace_master_hash(
            data_files[index],
            current_master_digest,
            layouts[index][0],
        )
        current_master_digest = hashlib.sha1(master).digest()

    parts_total_size = (
        last_path.stat().st_size
        + (len(data_files) - 1) * PART_SIZE
    )
    header[PARTS_SIZE_OFFSET:PARTS_SIZE_OFFSET + 4] = (
        parts_total_size // 0x100
    ).to_bytes(4, "big")
    header[MHT_HASH_OFFSET:MHT_HASH_OFFSET + 20] = current_master_digest
    header[HEADER_DIGEST_OFFSET:HEADER_DIGEST_OFFSET + 20] = hashlib.sha1(
        header[HEADER_DIGEST_START:HEADER_DIGEST_START + HEADER_DIGEST_LENGTH]
    ).digest()

    header_temporary = header_path.with_name(header_path.name + ".repairing")
    header_temporary.write_bytes(header)
    header_temporary.replace(header_path)

    return GodRepairResult(
        header=header_path,
        data_files=len(data_files),
        blocks_before=blocks_before,
        blocks_after=block_count,
        repaired=True,
    )
