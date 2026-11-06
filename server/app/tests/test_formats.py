from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from formats import (
    CONTENT_DLC,
    CONTENT_TITLE_UPDATE,
    is_cache_title_update,
    parse_stfs_header,
    safe_extract_zip,
    sanitize_component,
    source_title_fallback,
)


def write_stfs_header(path: Path, content_type: int, title_id: int) -> None:
    header = bytearray(0x1800)
    header[:4] = b"LIVE"
    header[0x344:0x348] = content_type.to_bytes(4, "big")
    header[0x354:0x358] = (0x12345678).to_bytes(4, "big")
    header[0x360:0x364] = title_id.to_bytes(4, "big")
    header[0x411:0x411 + 16] = "Addon".encode("utf-16-be")
    header[0x1691:0x1691 + 22] = "Example Game".encode("utf-16-be")
    path.write_bytes(header)


def test_parse_stfs_header(tmp_path: Path) -> None:
    package = tmp_path / "dlc"
    write_stfs_header(package, CONTENT_DLC, 0x415607E1)
    info = parse_stfs_header(package)
    assert info is not None
    assert info.title_id == "415607E1"
    assert info.content_type == CONTENT_DLC
    assert info.title_name == "Example Game"


def test_title_update_filename_rules() -> None:
    assert is_cache_title_update("TU_1A58231_0000018000000.00000000000GA")
    assert not is_cache_title_update("tu00000001_00000000")


def test_safe_extract_rejects_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("../../outside.txt", "no")
    with pytest.raises(ValueError, match="Unsafe archive path"):
        safe_extract_zip(archive, tmp_path / "out")


def test_safe_extract_accepts_normal_archive(tmp_path: Path) -> None:
    archive = tmp_path / "safe.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("Content/415607E1/00000002/addon", "yes")
    files = safe_extract_zip(archive, tmp_path / "out")
    assert len(files) == 1
    assert files[0].read_text() == "yes"


def test_names_are_safe_and_useful() -> None:
    assert sanitize_component("../Call\u0020of\u0020Duty/3") == "_Call of Duty_3"
    assert source_title_fallback("BioShock 2 (USA, Europe) (En,Fr,Es).zip") == "BioShock 2 (USA, Europe) (En,Fr,Es)"


def test_release_details_preserves_language_and_region_hints() -> None:
    from formats import release_details
    details = release_details("Scene It Box Office Smash! (USA, Europe) (En,Fr,Es).zip")
    assert details["region"] == "USA, Europe"
    assert details["languages"] == "En, Fr, Es"
    assert "Languages: En, Fr, Es" in details["label"]
