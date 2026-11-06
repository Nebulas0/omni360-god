"""Read-only integrity and compatibility checks for Xbox packages."""
from __future__ import annotations

import hashlib
import re
import struct
from pathlib import Path

from formats import CONTENT_GOD, CONTENT_TITLE_UPDATE, parse_stfs_header

BLOCK = 0x1000
BLOCKS_PER_SUBPART = 0xCC
SUBPARTS_PER_PART = 0xCB
BLOCKS_PER_PART = BLOCKS_PER_SUBPART * SUBPARTS_PER_PART


class ValidationError(ValueError):
    pass


def normalized_version(value: str | int) -> str:
    if isinstance(value, int):
        return f"{value:08X}"
    text = str(value).strip().removeprefix("0x").removeprefix("0X")
    if not re.fullmatch(r"[0-9A-Fa-f]{1,8}", text):
        raise ValidationError("Invalid title-update base version")
    return f"{int(text, 16):08X}"


def validate_title_update(
    path: Path, *, title_id: str | None = None, media_id: str | None = None,
    base_version: str | None = None, content_id: str | None = None,
    expected_size: int = 0,
) -> dict:
    info = parse_stfs_header(path)
    if info is None or info.content_type != CONTENT_TITLE_UPDATE:
        raise ValidationError("The download is not a title-update package")
    if title_id and info.title_id != title_id.upper():
        raise ValidationError("Title-update Title ID does not match the game")
    if media_id and info.media_id != media_id.upper():
        raise ValidationError("Title-update Media ID does not match the game")
    size = path.stat().st_size
    if expected_size and size != expected_size:
        raise ValidationError("Title-update size differs from XboxUnity's listing")
    with path.open("rb") as source:
        prefix = source.read(0x368)
        header_size = int.from_bytes(prefix[0x340:0x344], "big")
        end = (header_size + BLOCK - 1) // BLOCK * BLOCK
        if header_size < 0x971A or end > size or end > 0x100000:
            raise ValidationError("Title-update metadata header is truncated or invalid")
        source.seek(0x344)
        digest = hashlib.sha1(source.read(end - 0x344)).digest()
    stored = prefix[0x32C:0x340]
    if digest != stored:
        raise ValidationError("Title-update metadata checksum failed")
    if content_id:
        if not re.fullmatch(r"[0-9a-fA-F]{40}", content_id):
            raise ValidationError("XboxUnity supplied an invalid content ID")
        if stored.hex().upper() != content_id.upper():
            raise ValidationError("Title-update content ID differs from XboxUnity's listing")
    actual_base = prefix[0x35C:0x360].hex().upper()
    if base_version and actual_base != normalized_version(base_version):
        raise ValidationError("Title-update base version does not match the game executable")
    return {
        "title_id": info.title_id, "media_id": info.media_id,
        "base_version": actual_base, "content_id": stored.hex().upper(),
        "filename": path.name, "bytes": size,
    }


class GodVolume:
    def __init__(self, header: Path):
        self.header = header
        self.parts = sorted(header.with_name(header.name + ".data").glob("Data[0-9][0-9][0-9][0-9]"))
        h = header.read_bytes()
        self.size = int.from_bytes(h[0x392:0x395], "big") * BLOCK

    def read(self, offset: int, size: int) -> bytes:
        if offset < 0 or size < 0 or offset + size > self.size:
            raise ValidationError("Game file extends outside the GoD data volume")
        result = bytearray()
        while size:
            block, within = divmod(offset, BLOCK)
            part, local = divmod(block, BLOCKS_PER_PART)
            subpart, position = divmod(local, BLOCKS_PER_SUBPART)
            physical = (2 + subpart * (BLOCKS_PER_SUBPART + 1) + position) * BLOCK + within
            take = min(size, BLOCK - within)
            if part >= len(self.parts):
                raise ValidationError("GoD data fragment is missing")
            with self.parts[part].open("rb") as source:
                source.seek(physical)
                data = source.read(take)
            if len(data) != take:
                raise ValidationError("GoD data fragment is truncated")
            result.extend(data)
            offset += take
            size -= take
        return bytes(result)


def god_execution_info(header: Path) -> dict:
    """Read the actual XEX execution ID; converter header versions are zero."""
    volume = GodVolume(header)
    descriptor = volume.read(0x10000, 0x800)
    if descriptor[:20] != b"MICROSOFT*XBOX*MEDIA":
        raise ValidationError("GoD volume descriptor is missing")
    sector, size = struct.unpack_from("<II", descriptor, 20)
    if size > 8 * 1024 * 1024:
        raise ValidationError("Game root directory is too large")
    table = volume.read(sector * 0x800, size)
    pending, visited = [0], set()
    while pending:
        offset = pending.pop()
        if offset in visited or offset + 14 > len(table):
            raise ValidationError("Invalid game directory tree")
        visited.add(offset)
        left, right, sector, size, _, name_length = struct.unpack_from("<HHIIBB", table, offset)
        if left == right == 0xFFFF:
            continue
        if offset + 14 + name_length > len(table):
            raise ValidationError("Game directory entry is truncated")
        name = table[offset + 14:offset + 14 + name_length].lower()
        if name == b"default.xex":
            start = sector * 0x800
            xex = volume.read(start, 24)
            if xex[:4] != b"XEX2":
                raise ValidationError("Game executable is not an XEX2 file")
            fields = int.from_bytes(xex[20:24], "big")
            if fields > 1024 or 24 + fields * 8 > size:
                raise ValidationError("Invalid game executable metadata")
            entries = volume.read(start + 24, fields * 8)
            for key, value in struct.iter_unpack(">II", entries):
                if key == 0x40006:
                    if value + 24 > size:
                        raise ValidationError("Game execution metadata extends outside its executable")
                    raw = volume.read(start + value, 24)
                    media, version, base, title = struct.unpack_from(">IIII", raw)
                    return {"title_id": f"{title:08X}", "media_id": f"{media:08X}",
                            "version": f"{version:08X}", "base_version": f"{base:08X}"}
            raise ValidationError("Game executable has no execution ID")
        pending.extend(link * 4 for link in (left, right) if link not in (0, 0xFFFF))
    raise ValidationError("Game volume has no default.xex")


def validate_god(header: Path, *, full_data: bool = False) -> dict:
    info = parse_stfs_header(header)
    if info is None or info.content_type != CONTENT_GOD:
        raise ValidationError("Invalid GoD header")
    h = header.read_bytes()
    if len(h) >= 0x32C and not any(h[0x22C:0x32C]):
        raise ValidationError("GoD license table is empty; reprocess the original disc image")
    if len(h) != 0xB000 or hashlib.sha1(h[0x344:]).digest() != h[0x32C:0x340]:
        raise ValidationError("GoD header checksum failed")
    parts = GodVolume(header).parts
    count = int.from_bytes(h[0x3A0:0x3A4], "little")
    if count == 0 or len(parts) != count or [p.name for p in parts] != [f"Data{i:04d}" for i in range(count)]:
        raise ValidationError("GoD fragment count or sequence is incorrect")
    if sum(p.stat().st_size for p in parts) != int.from_bytes(h[0x3A4:0x3A8], "big") * 0x100:
        raise ValidationError("GoD fragment sizes do not match the header")
    expected_master = h[0x37D:0x391]
    total_blocks = 0
    for part_index, path in enumerate(parts):
        with path.open("rb") as source:
            master = source.read(BLOCK)
            if len(master) != BLOCK or hashlib.sha1(master).digest() != expected_master:
                raise ValidationError(f"GoD master hash chain failed in {path.name}")
            subpart = 0
            part_blocks = 0
            length = path.stat().st_size
            while source.tell() < length:
                if subpart >= SUBPARTS_PER_PART:
                    raise ValidationError("GoD fragment contains too many hash tables")
                hashes = source.read(BLOCK)
                if len(hashes) != BLOCK or hashlib.sha1(hashes).digest() != master[subpart*20:subpart*20+20]:
                    raise ValidationError(f"GoD subtable hash failed in {path.name}")
                n = next((i for i in range(204) if hashes[i*20:i*20+20] == bytes(20)), 204)
                if n == 0 or any(hashes[n*20:]):
                    raise ValidationError("GoD subtable has invalid hash entries")
                if source.tell() + n * BLOCK > length:
                    raise ValidationError("GoD payload is truncated")
                if full_data:
                    for i in range(n):
                        if hashlib.sha1(source.read(BLOCK)).digest() != hashes[i*20:i*20+20]:
                            raise ValidationError(f"GoD data checksum failed in {path.name}")
                else:
                    source.seek(n * BLOCK, 1)
                part_blocks += n
                subpart += 1
            if part_index < count - 1 and part_blocks != BLOCKS_PER_PART:
                raise ValidationError("A non-final GoD fragment is incomplete")
            expected_master = master[subpart*20:subpart*20+20]
            if part_index == count - 1 and any(master[subpart*20:]):
                raise ValidationError("Final GoD fragment has an unexpected next-fragment hash")
            total_blocks += part_blocks
    if total_blocks != int.from_bytes(h[0x392:0x395], "big"):
        raise ValidationError("GoD block count does not match its data")
    execution = god_execution_info(header)
    if execution["title_id"] != info.title_id or execution["media_id"] != info.media_id:
        raise ValidationError("GoD header identifiers differ from the game executable")
    return {**execution, "header": header.name, "fragments": count, "blocks": total_blocks,
            "status": "passed", "level": "full_data" if full_data else "structure"}
