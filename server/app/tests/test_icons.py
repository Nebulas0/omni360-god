from __future__ import annotations

import hashlib
from pathlib import Path

import icons


def test_embed_game_icon_updates_both_header_thumbnails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    package = tmp_path / "Example Game" / "415607E1" / "00007000"
    package.mkdir(parents=True)
    header = bytearray(0xB000)
    header[:4] = b"LIVE"
    header[0x344:0x348] = (0x00007000).to_bytes(4, "big")
    header[0x354:0x358] = (0x12345678).to_bytes(4, "big")
    header[0x360:0x364] = (0x415607E1).to_bytes(4, "big")
    header_path = package / "12345678"
    header_path.write_bytes(header)
    icon = b"\x89PNG\r\n\x1a\nexample"
    monkeypatch.setattr(icons, "fetch_game_icon", lambda title_id: icon)

    assert icons.embed_game_icon(package.parent, "415607E1") is True

    result = header_path.read_bytes()
    assert int.from_bytes(result[0x1712:0x1716], "big") == len(icon)
    assert int.from_bytes(result[0x1716:0x171A], "big") == len(icon)
    assert result[0x171A:0x171A + len(icon)] == icon
    assert result[0x571A:0x571A + len(icon)] == icon
    assert result[0x32C:0x340] == hashlib.sha1(
        result[0x344:0x344 + 0xACBC]
    ).digest()
