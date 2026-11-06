from __future__ import annotations

import hashlib
import logging
import os
import shutil
from pathlib import Path

import requests

from formats import CONTENT_GOD, parse_stfs_header

logger = logging.getLogger("xbox360.icons")

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_ICON_BYTES = 0x4000
ICON_SIZE_OFFSET = 0x1712
ICON_OFFSET = 0x171A
TITLE_ICON_SIZE_OFFSET = 0x1716
TITLE_ICON_OFFSET = 0x571A
HEADER_DIGEST_OFFSET = 0x32C
HEADER_DIGEST_START = 0x344
HEADER_DIGEST_LENGTH = 0xACBC


def fetch_game_icon(title_id: str, timeout: int = 10) -> bytes | None:
    """Fetch the small dashboard icon used by iso2god-next."""
    url = f"http://image.xboxlive.com/global/t.{title_id.upper()}/icon/0/8000"
    response = requests.get(
        url,
        headers={"User-Agent": "xbox360-iso2god/1.0"},
        timeout=(5, timeout),
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    icon = response.content
    if not icon.startswith(PNG_SIGNATURE):
        raise ValueError("Xbox Live returned a non-PNG game icon")
    if len(icon) > MAX_ICON_BYTES:
        raise ValueError(
            f"Xbox Live icon is {len(icon)} bytes; maximum is {MAX_ICON_BYTES}"
        )
    return icon


def _god_header(package_dir: Path) -> Path:
    god_dir = package_dir / f"{CONTENT_GOD:08X}"
    headers = []
    for path in sorted(god_dir.iterdir(), key=lambda item: item.name):
        if not path.is_file():
            continue
        info = parse_stfs_header(path)
        if info and info.content_type == CONTENT_GOD:
            headers.append(path)
    if len(headers) != 1:
        raise ValueError(f"Expected one GOD header in {god_dir}")
    return headers[0]


def embed_game_icon(package_dir: Path, title_id: str) -> bool:
    """Embed a validated 64x64-or-smaller PNG into a GOD header."""
    header_path = _god_header(package_dir)
    header = bytearray(header_path.read_bytes())
    if len(header) < TITLE_ICON_OFFSET + MAX_ICON_BYTES:
        raise ValueError(f"GOD header is too small for an icon: {header_path}")
    existing_size = int.from_bytes(
        header[ICON_SIZE_OFFSET:ICON_SIZE_OFFSET + 4],
        "big",
    )
    existing = header[ICON_OFFSET:ICON_OFFSET + existing_size]
    if (
        existing_size > 0
        and existing_size <= MAX_ICON_BYTES
        and existing.startswith(PNG_SIGNATURE)
    ):
        return False

    icon = fetch_game_icon(title_id)
    if icon is None:
        return False

    icon_size = len(icon).to_bytes(4, "big")
    header[ICON_SIZE_OFFSET:ICON_SIZE_OFFSET + 4] = icon_size
    header[TITLE_ICON_SIZE_OFFSET:TITLE_ICON_SIZE_OFFSET + 4] = icon_size
    header[ICON_OFFSET:ICON_OFFSET + MAX_ICON_BYTES] = b"\x00" * MAX_ICON_BYTES
    header[TITLE_ICON_OFFSET:TITLE_ICON_OFFSET + MAX_ICON_BYTES] = b"\x00" * MAX_ICON_BYTES
    header[ICON_OFFSET:ICON_OFFSET + len(icon)] = icon
    header[TITLE_ICON_OFFSET:TITLE_ICON_OFFSET + len(icon)] = icon
    header[HEADER_DIGEST_OFFSET:HEADER_DIGEST_OFFSET + 20] = hashlib.sha1(
        header[HEADER_DIGEST_START:HEADER_DIGEST_START + HEADER_DIGEST_LENGTH]
    ).digest()

    temporary = header_path.with_name(header_path.name + ".icon")
    temporary.write_bytes(header)
    try:
        shutil.copystat(header_path, temporary)
    except OSError:
        pass
    os.replace(temporary, header_path)
    logger.info("icon_embedded title_id=%s file=%s bytes=%d", title_id, header_path.name, len(icon))
    return True
