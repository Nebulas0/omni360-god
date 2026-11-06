import hashlib
from pathlib import Path

import pytest

from package_fixtures import write_god, write_tu
from validation import ValidationError, god_execution_info, validate_god, validate_title_update


def test_god_checks_data_and_reads_base_version_from_xex(tmp_path):
    header = write_god(tmp_path / "415607E1", base_version=7)
    assert header.read_bytes()[0x35C:0x360] == bytes(4)
    result = validate_god(header, full_data=True)
    assert result["base_version"] == "00000007"
    assert result["level"] == "full_data"
    data = header.with_name(header.name + ".data") / "Data0000"
    with data.open("r+b") as f:
        f.seek(0x2000)
        f.write(b"damaged")
    with pytest.raises(ValidationError, match="data checksum"):
        validate_god(header, full_data=True)


def test_god_rejects_stale_or_missing_fragments(tmp_path):
    header = write_god(tmp_path / "415607E1")
    extra = header.with_name(header.name + ".data") / "Data0001"
    extra.write_bytes(b"stale")
    with pytest.raises(ValidationError, match="fragment count"):
        validate_god(header)


def test_title_update_matches_content_id_not_full_file_sha1(tmp_path):
    path = tmp_path / "tu"
    raw = write_tu(path)
    content_id = raw[0x32C:0x340].hex()
    assert content_id != hashlib.sha1(raw).hexdigest()
    result = validate_title_update(path, title_id="415607E1", media_id="12345678",
        base_version="00000002", content_id=content_id, expected_size=len(raw))
    assert result["content_id"] == content_id.upper()
    with pytest.raises(ValidationError, match="base version"):
        validate_title_update(path, base_version="00000003")
    with pytest.raises(ValidationError, match="content ID"):
        validate_title_update(path, content_id="00"*20)
    with path.open("r+b") as f:
        f.seek(0x400)
        f.write(b"tampered")
    with pytest.raises(ValidationError, match="checksum"):
        validate_title_update(path)
