from __future__ import annotations

import hashlib
from pathlib import Path

from god import repair_god_package


def test_repair_god_package_trims_final_part_and_rebuilds_metadata(
    tmp_path: Path,
) -> None:
    package = tmp_path / "Example Game" / "415607E1"
    god_dir = package / "00007000"
    data_dir = god_dir / "12345678.data"
    god_dir.mkdir(parents=True)
    data_dir.mkdir()

    blocks = [b"A" * 0x1000, b"B" * 0x1000, b"C" * 0x1000]
    sub_hashes = bytearray(0x1000)
    for index, block in enumerate(blocks):
        sub_hashes[index * 20:(index + 1) * 20] = hashlib.sha1(block).digest()
    master = bytearray(0x1000)
    master[:20] = hashlib.sha1(sub_hashes).digest()
    data_path = data_dir / "Data0000"
    data_path.write_bytes(master + sub_hashes + b"".join(blocks))

    header = bytearray(0xB000)
    header[:4] = b"LIVE"
    header[0x344:0x348] = (0x00007000).to_bytes(4, "big")
    header[0x354:0x358] = (0x12345678).to_bytes(4, "big")
    header[0x360:0x364] = (0x415607E1).to_bytes(4, "big")
    header[0x392:0x395] = (2).to_bytes(3, "big")
    header[0x3A0:0x3A4] = (1).to_bytes(4, "little")
    header[0x3A4:0x3A8] = (data_path.stat().st_size // 0x100).to_bytes(4, "big")
    header_path = god_dir / "12345678"
    header_path.write_bytes(header)

    result = repair_god_package(package)

    assert result.repaired is True
    assert result.blocks_before == 3
    assert result.blocks_after == 2
    repaired = data_path.read_bytes()
    assert len(repaired) == 0x1000 + 0x1000 + (2 * 0x1000)
    assert repaired[0x1000:0x1000 + 40] == sub_hashes[:40]

    final_header = header_path.read_bytes()
    assert int.from_bytes(final_header[0x3A4:0x3A8], "big") == len(repaired) // 0x100
    assert final_header[0x37D:0x391] == hashlib.sha1(repaired[:0x1000]).digest()
    assert final_header[0x32C:0x340] == hashlib.sha1(
        final_header[0x344:0x344 + 0xACBC]
    ).digest()
