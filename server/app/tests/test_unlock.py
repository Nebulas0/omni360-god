from __future__ import annotations

from pathlib import Path

from formats import (
    CONTENT_ARCADE,
    CONTENT_DLC,
    CONTENT_GOD,
    CONTENT_XBOX_ORIGINAL,
    CONTENT_TITLE_UPDATE,
)
from unlock import (
    LICENSE_BLOCK_OFFSET,
    LICENSE_BLOCK_SIZE,
    is_unlocked,
    should_unlock,
    unlock_output_directory,
    unlock_package,
)


def write_stfs(path: Path, content_type: int, title_id: int, locked: bool = True) -> None:
    header = bytearray(0x1800)
    header[:4] = b"LIVE"
    header[0x344:0x348] = content_type.to_bytes(4, "big")
    header[0x354:0x358] = (0x12345678).to_bytes(4, "big")
    header[0x360:0x364] = title_id.to_bytes(4, "big")
    if locked:
        header[0x22C:0x22C + 0x20] = b"\x01" * 0x20
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + b"payload")


def test_should_unlock_dlc() -> None:
    from formats import parse_stfs_header
    path = Path("/tmp/test-dlc")
    write_stfs(path, CONTENT_DLC, 0x415607E1)
    info = parse_stfs_header(path)
    assert should_unlock(info, path.name) is True
    path.unlink(missing_ok=True)


def test_should_unlock_xbla() -> None:
    from formats import parse_stfs_header
    path = Path("/tmp/test-xbla")
    write_stfs(path, CONTENT_ARCADE, 0x584114BB)
    info = parse_stfs_header(path)
    assert should_unlock(info, path.name) is True
    path.unlink(missing_ok=True)


def test_should_not_unlock_god() -> None:
    from formats import parse_stfs_header
    path = Path("/tmp/test-god")
    write_stfs(path, CONTENT_GOD, 0x57520802)
    info = parse_stfs_header(path)
    assert should_unlock(info, path.name) is False
    path.unlink(missing_ok=True)


def test_should_not_unlock_title_update() -> None:
    from formats import parse_stfs_header
    path = Path("/tmp/test-tu")
    write_stfs(path, CONTENT_TITLE_UPDATE, 0x57520802)
    info = parse_stfs_header(path)
    assert should_unlock(info, path.name) is False
    path.unlink(missing_ok=True)


def test_should_not_unlock_cache_title_update() -> None:
    from formats import parse_stfs_header
    path = Path("/tmp/TU_12345678_00000000")
    write_stfs(path, CONTENT_TITLE_UPDATE, 0x57520802)
    info = parse_stfs_header(path)
    assert should_unlock(info, path.name) is False
    path.unlink(missing_ok=True)


def test_unlock_package_zeros_license_block(tmp_path: Path) -> None:
    package = tmp_path / "dlc-pkg"
    write_stfs(package, CONTENT_DLC, 0x415607E1, locked=True)
    assert not is_unlocked(package)
    result = unlock_package(package)
    assert result.unlocked is True
    assert result.skipped is False
    assert is_unlocked(package)
    with package.open("rb") as f:
        f.seek(LICENSE_BLOCK_OFFSET)
        block = f.read(LICENSE_BLOCK_SIZE)
    assert block == b"\x00" * LICENSE_BLOCK_SIZE


def test_unlock_package_skips_already_unlocked(tmp_path: Path) -> None:
    package = tmp_path / "dlc-pkg"
    write_stfs(package, CONTENT_DLC, 0x415607E1, locked=False)
    assert is_unlocked(package)
    result = unlock_package(package)
    assert result.unlocked is False
    assert result.already_unlocked is True


def test_unlock_package_skips_title_update(tmp_path: Path) -> None:
    package = tmp_path / "tu-pkg"
    write_stfs(package, CONTENT_TITLE_UPDATE, 0x57520802, locked=True)
    result = unlock_package(package)
    assert result.skipped is True
    assert result.unlocked is False
    assert not is_unlocked(package)


def test_unlock_package_creates_backup(tmp_path: Path) -> None:
    package = tmp_path / "output" / "415607E1" / "00000002" / "dlc-pkg"
    write_stfs(package, CONTENT_DLC, 0x415607E1, locked=True)
    backup_dir = tmp_path / "backups"
    original_bytes = package.read_bytes()
    result = unlock_package(package, backup_dir=backup_dir)
    assert result.unlocked is True
    backup_path = backup_dir / package.parent.name / (package.name + ".x360lock.bak")
    assert backup_path.is_file()
    assert backup_path.read_bytes() == original_bytes


def test_unlock_output_directory_unlocks_dlc_and_skips_tu(tmp_path: Path) -> None:
    title_dir = tmp_path / "Game" / "57520802"
    dlc = title_dir / "00000002" / "addon"
    tu = title_dir / "000B0000" / "tu00000008_00000000"
    god = title_dir / "00007000" / "5D278B66"
    write_stfs(dlc, CONTENT_DLC, 0x57520802, locked=True)
    write_stfs(tu, CONTENT_TITLE_UPDATE, 0x57520802, locked=True)
    write_stfs(god, CONTENT_GOD, 0x57520802, locked=True)

    results = unlock_output_directory(title_dir)
    result_by_name = {r.path.name: r for r in results}
    assert result_by_name["addon"].unlocked is True
    assert result_by_name["5D278B66"].skipped is True
    assert result_by_name["tu00000008_00000000"].skipped is True
    assert is_unlocked(dlc)
    assert not is_unlocked(god)
    assert not is_unlocked(tu)


def test_converter_svod_license_is_preserved_byte_for_byte(tmp_path: Path) -> None:
    """Both upstream empty_live.bin templates start with this licensee ID.

    Regress the actual failure: clearing these eight bytes leaves the header
    checksum valid but changes a working converter's licensing metadata.
    """
    import hashlib

    for content_type in (CONTENT_GOD, CONTENT_XBOX_ORIGINAL):
        package = tmp_path / f"{content_type:08X}" / "11223344"
        header = bytearray(0xB000)
        header[:4] = b"LIVE"
        header[0x22C:0x234] = b"\xff" * 8
        header[0x344:0x348] = content_type.to_bytes(4, "big")
        header[0x360:0x364] = (0x415607E1).to_bytes(4, "big")
        header[0x32C:0x340] = hashlib.sha1(header[0x344:]).digest()
        package.parent.mkdir()
        package.write_bytes(header)

        result = unlock_package(package, backup_dir=tmp_path / "backups")
        assert result.skipped is True
        assert result.unlocked is False
        assert package.read_bytes() == header
        assert not (tmp_path / "backups").exists()


def test_zeroed_god_header_is_not_classified_as_already_unlocked(tmp_path: Path) -> None:
    package = tmp_path / "00007000" / "11223344"
    write_stfs(package, CONTENT_GOD, 0x415607E1, locked=False)
    result = unlock_package(package)
    assert result.skipped is True
    assert result.already_unlocked is False


def test_unlock_output_directory_skips_data_fragments(tmp_path: Path) -> None:
    title_dir = tmp_path / "Game" / "57520802"
    god_header = title_dir / "00007000" / "5D278B66"
    data_dir = title_dir / "00007000" / "5D278B66.data"
    data_dir.mkdir(parents=True)
    god_header.write_bytes(b"NOTSTFS" + b"\x00" * 200)
    data_fragment = data_dir / "Data0000"
    data_fragment.write_bytes(b"NOTSTFS" + b"\x00" * 200)
    results = unlock_output_directory(title_dir)
    assert results == []


def test_unlock_output_directory_skips_non_stfs_files(tmp_path: Path) -> None:
    title_dir = tmp_path / "Game" / "57520802"
    readme = title_dir / "readme.txt"
    readme.parent.mkdir(parents=True)
    readme.write_text("hello")
    results = unlock_output_directory(title_dir)
    assert results == []
