from __future__ import annotations

import re
import shutil
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable


STFS_MAGICS = {b"CON ", b"LIVE", b"PIRS"}
CONTENT_DLC = 0x00000002
CONTENT_AVATAR = 0x00009000
CONTENT_TITLE_UPDATE = 0x000B0000
CONTENT_ARCADE = 0x000D0000
CONTENT_GOD = 0x00007000
CONTENT_XBOX_ORIGINAL = 0x00005000


@dataclass(frozen=True)
class STFSInfo:
    path: Path
    magic: str
    content_type: int
    title_id: str
    media_id: str
    display_name: str | None
    title_name: str | None

    @property
    def content_folder(self) -> str:
        return f"{self.content_type:08X}"

    @property
    def is_title_update(self) -> bool:
        return self.content_type == CONTENT_TITLE_UPDATE

    @property
    def is_god(self) -> bool:
        return self.content_type in {CONTENT_GOD, CONTENT_XBOX_ORIGINAL}


@dataclass(frozen=True)
class ConverterMetadata:
    title_id: str | None
    title_name: str | None
    content_type: str | None


def _decode_utf16be(data: bytes, offset: int, length: int = 0x100) -> str | None:
    chunk = data[offset:offset + length]
    if not chunk:
        return None
    try:
        value = chunk.decode("utf-16-be", errors="ignore").split("\x00", 1)[0]
    except UnicodeDecodeError:
        return None
    value = " ".join(value.split()).strip()
    return value or None


def parse_stfs_header(path: Path) -> STFSInfo | None:
    try:
        with path.open("rb") as source:
            header = source.read(0x1800)
    except (OSError, ValueError):
        return None
    if len(header) < 0x368 or header[:4] not in STFS_MAGICS:
        return None
    content_type = int.from_bytes(header[0x344:0x348], "big")
    title_id = int.from_bytes(header[0x360:0x364], "big")
    media_id = int.from_bytes(header[0x354:0x358], "big")
    return STFSInfo(
        path=path,
        magic=header[:4].decode("ascii", errors="replace"),
        content_type=content_type,
        title_id=f"{title_id:08X}",
        media_id=f"{media_id:08X}",
        display_name=_decode_utf16be(header, 0x411),
        title_name=_decode_utf16be(header, 0x1691),
    )


def parse_converter_output(output: str) -> ConverterMetadata:
    values: dict[str, str] = {}
    for line in output.splitlines():
        if ":" not in line:
            continue
        key, value = line.strip().split(":", 1)
        values[key.strip().lower()] = value.strip()
    title_id = values.get("title id")
    if title_id and not re.fullmatch(r"[0-9a-fA-F]{8}", title_id):
        title_id = None
    title_name = values.get("name")
    if title_name in {None, "", "(unknown)"}:
        title_name = None
    content_type = values.get("type")
    return ConverterMetadata(title_id, title_name, content_type)


def sanitize_component(value: str, fallback: str = "Xbox 360 title", limit: int = 120) -> str:
    value = unicodedata.normalize("NFC", value or "")
    value = value.replace("/", "_").replace("\\", "_")
    value = "".join(char for char in value if ord(char) >= 0x20 and char != "\x7f")
    value = re.sub(r"\s+", " ", value).strip(" .")
    value = value.replace("..", "_")
    if not value:
        value = fallback
    return value[:limit].rstrip(" .") or fallback


def source_title_fallback(name: str) -> str:
    value = Path(name).stem
    value = re.sub(
        r"\s*\((?:USA|Europe|World|Japan|En|Fr|Es|De|It|Pt|Addon|DLC|GOD|XBLA|Demo|Unknown|v\d+)\)",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"\s+-\s+(?:Addon|DLC|Patch Data|GOD|XBLA)\b.*$", "", value, flags=re.I)
    value = re.sub(r"\s+", " ", value).strip(" -")
    return sanitize_component(value)


def looks_like_title_id(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9A-Fa-f]{8}", value)) and value.upper() != "00000000"


def infer_title_id(parts: Iterable[str]) -> str | None:
    ignored = {
        "00000002", "00007000", "00005000", "000D0000", "000B0000",
        "00080000", "00001000", "00000000", "0000000000000000",
        "FFED2000", "FFFFFFFF",
    }
    for part in parts:
        if looks_like_title_id(part) and part.upper() not in ignored:
            return part.upper()
    return None


def is_cache_title_update(name: str) -> bool:
    return name.startswith("TU_")


def safe_extract_zip(
    archive: Path,
    destination: Path,
    max_members: int = 200_000,
) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    with zipfile.ZipFile(archive) as source:
        members = source.infolist()
        if len(members) > max_members:
            raise ValueError(
                f"Archive contains {len(members)} entries; limit is {max_members}"
            )
        root = destination.resolve()
        for member in members:
            raw_name = member.filename.replace("\\", "/")
            relative = PurePosixPath(raw_name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"Unsafe archive path: {member.filename}")
            if not relative.parts:
                continue
            target = (destination / Path(*relative.parts)).resolve()
            if not target.is_relative_to(root):
                raise ValueError(f"Unsafe archive path: {member.filename}")
            mode = (member.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise ValueError(f"Symlink entries are not allowed: {member.filename}")
            if member.is_dir() or raw_name.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open(member, "r") as input_file, target.open("wb") as output:
                shutil.copyfileobj(input_file, output, length=1024 * 1024)
            extracted.append(target)
    return extracted


def find_iso_files(root: Path) -> list[Path]:
    return sorted(
        path for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() == ".iso"
    )


def content_type_folder(content_type: int) -> str:
    return f"{content_type:08X}"


def package_kind(info: STFSInfo | None, filename: str = "") -> str:
    if info is None:
        if is_cache_title_update(filename):
            return "title-update-cache"
        return "unknown"
    if info.content_type == CONTENT_DLC:
        return "dlc"
    if info.content_type == CONTENT_AVATAR:
        return "avatar"
    if info.content_type == CONTENT_TITLE_UPDATE:
        return "title-update-cache" if is_cache_title_update(filename) else "title-update"
    if info.content_type == CONTENT_GOD:
        return "god"
    if info.content_type == CONTENT_ARCADE:
        return "xbla"
    if info.content_type == CONTENT_XBOX_ORIGINAL:
        return "original-xbox"
    return "content"

_REGION_TERMS = {
    "USA", "US", "Europe", "EU", "World", "Japan", "Asia", "Germany",
    "France", "Italy", "Spain", "Australia", "Korea", "Canada",
}
_LANGUAGE_CODES = {
    "En", "Fr", "Es", "De", "It", "Pt", "Nl", "Sv", "No", "Da",
    "Fi", "Pl", "Ru", "Ja", "Zh", "Ko",
}


def release_details(name: str) -> dict[str, str]:
    """Extract human-readable region/language hints without trusting them as IDs."""
    groups = re.findall(r"\(([^()]*)\)", Path(name).stem)
    regions: list[str] = []
    languages: list[str] = []
    for group in groups:
        values = [part.strip() for part in group.split(",") if part.strip()]
        if values and all(value in _REGION_TERMS for value in values):
            regions.extend(values)
        elif values and all(value in _LANGUAGE_CODES for value in values):
            languages.extend(values)
    parts = []
    if regions:
        parts.append(" / ".join(dict.fromkeys(regions)))
    if languages:
        parts.append("Languages: " + ", ".join(dict.fromkeys(languages)))
    return {
        "region": ", ".join(dict.fromkeys(regions)),
        "languages": ", ".join(dict.fromkeys(languages)),
        "label": " | ".join(parts),
    }
