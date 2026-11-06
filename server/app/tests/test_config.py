from __future__ import annotations

from pathlib import Path

import pytest

from config import Config


@pytest.mark.parametrize("default", [True, False])
def test_unlock_switch_persists_over_environment_default(tmp_path, monkeypatch, default):
    monkeypatch.setenv("WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("UNLOCK_PACKAGES", str(default).lower())
    config = Config.from_env()
    assert config.effective_unlock_packages is default
    config.save_unlock_packages(not default)
    assert config.effective_unlock_packages is not default
    assert Config.from_env().effective_unlock_packages is not default
    assert (config.work_dir / "unlock-packages").stat().st_mode & 0o777 == 0o600


def test_unlock_switch_rejects_non_boolean_without_overwriting(tmp_path, monkeypatch):
    monkeypatch.setenv("WORK_DIR", str(tmp_path / "work"))
    config = Config.from_env()
    config.save_unlock_packages(False)
    with pytest.raises(ValueError, match="boolean"):
        config.save_unlock_packages("false")
    assert config.effective_unlock_packages is False


def test_remote_auth_mode_is_persisted_and_loaded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_dir = tmp_path / "work"
    monkeypatch.setenv("WORK_DIR", str(work_dir))
    monkeypatch.setenv("REMOTE_API_KEY", "api-key")
    monkeypatch.setenv("REMOTE_BASIC_USER", "games")
    monkeypatch.setenv("REMOTE_BASIC_PASSWORD", "password")
    monkeypatch.setenv("REMOTE_AUTH_MODE", "basic")

    config = Config.from_env()
    assert config.remote_auth_mode == "basic"
    config.ensure_directories()
    config.save_remote_auth_mode("api-key")

    reloaded = Config.from_env()
    assert reloaded.remote_auth_mode == "api-key"
    assert reloaded.remote_auth_mode_file.stat().st_mode & 0o777 == 0o600


def test_remote_auth_mode_rejects_unknown_values(tmp_path: Path) -> None:
    config = Config(
        remote_base_url="https://example.invalid",
        remote_api_key="api-key",
        remote_api_key_file=tmp_path / "remote-api-key",
        remote_auth_mode="api-key",
        remote_auth_mode_file=tmp_path / "remote-auth-mode",
        remote_basic_user="",
        remote_basic_password="",
        remote_basic_file=tmp_path / "remote-basic",
        disc_source_path="roms/xbox360",
        digital_source_path="roms/xbox360-digital",
        output_dir=tmp_path / "output",
        work_dir=tmp_path / "work",
        db_path=tmp_path / "state.sqlite3",
        extractor_path=tmp_path / "extract-xiso",
        converter_path=tmp_path / "iso2god",
        converter_threads=1,
        remote_timeout=10,
        remote_retries=1,
        keep_source_archives=True,
        unlock_packages=True,
        xboxunity_tu_downloads=False,
        download_game_icons=False,
        max_zip_members=100,
        app_auth_user="",
        app_auth_password="",
        public_host="test.local",
    )

    with pytest.raises(ValueError, match="auth_mode"):
        config.save_remote_auth_mode("digest")
