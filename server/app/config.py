from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from remote import AUTH_MODE_API_KEY, AUTH_MODE_BASIC, normalize_auth_mode


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


@dataclass(frozen=True)
class Config:
    remote_base_url: str
    remote_api_key: str
    remote_api_key_file: Path
    remote_auth_mode: str
    remote_auth_mode_file: Path
    remote_basic_user: str
    remote_basic_password: str
    remote_basic_file: Path
    disc_source_path: str
    digital_source_path: str
    output_dir: Path
    work_dir: Path
    db_path: Path
    extractor_path: Path
    converter_path: Path
    converter_threads: int
    remote_timeout: int
    remote_retries: int
    keep_source_archives: bool
    unlock_packages: bool
    xboxunity_tu_downloads: bool
    download_game_icons: bool
    max_zip_members: int
    app_auth_user: str
    app_auth_password: str
    public_host: str

    @classmethod
    def from_env(cls) -> "Config":
        work_dir = Path(
            os.getenv("WORK_DIR", "/data/downloaded-worked-on")
        ).expanduser()
        db_path = Path(
            os.getenv("DB_PATH", str(work_dir / "xbox360.sqlite3"))
        ).expanduser()

        api_key_file = Path(
            os.getenv("REMOTE_API_KEY_FILE", str(work_dir / "remote-api-key"))
        ).expanduser()
        env_api_key = os.getenv("REMOTE_API_KEY", "").strip()
        persisted_api_key = ""
        try:
            if api_key_file.is_file():
                persisted_api_key = api_key_file.read_text(encoding="utf-8").strip()
        except OSError:
            persisted_api_key = ""

        auth_mode_file = Path(
            os.getenv("REMOTE_AUTH_MODE_FILE", str(work_dir / "remote-auth-mode"))
        ).expanduser()
        persisted_auth_mode = ""
        try:
            if auth_mode_file.is_file():
                persisted_auth_mode = auth_mode_file.read_text(
                    encoding="utf-8"
                ).strip()
        except OSError:
            persisted_auth_mode = ""

        basic_file = Path(
            os.getenv("REMOTE_BASIC_FILE", str(work_dir / "remote-basic"))
        ).expanduser()
        env_basic_user = os.getenv("REMOTE_BASIC_USER", "").strip()
        env_basic_password = os.getenv("REMOTE_BASIC_PASSWORD", "")
        persisted_user = ""
        persisted_password = ""
        try:
            if basic_file.is_file():
                content = basic_file.read_text(encoding="utf-8")
                lines = content.splitlines()
                if len(lines) >= 1:
                    persisted_user = lines[0].strip()
                if len(lines) >= 2:
                    persisted_password = lines[1]
        except OSError:
            pass

        default_auth_mode = (
            AUTH_MODE_BASIC
            if (persisted_user or env_basic_user) and not (persisted_api_key or env_api_key)
            else AUTH_MODE_API_KEY
        )

        return cls(
            remote_base_url=os.getenv(
                "REMOTE_BASE_URL", "https://games.mediahubs.org"
            ).rstrip("/"),
            remote_api_key=persisted_api_key or env_api_key,
            remote_api_key_file=api_key_file,
            remote_auth_mode=normalize_auth_mode(
                persisted_auth_mode or os.getenv("REMOTE_AUTH_MODE"),
                default=default_auth_mode,
            ),
            remote_auth_mode_file=auth_mode_file,
            remote_basic_user=persisted_user or env_basic_user,
            remote_basic_password=persisted_password or env_basic_password,
            remote_basic_file=basic_file,
            disc_source_path=os.getenv(
                "DISC_SOURCE_PATH", "roms/xbox360"
            ).strip("/"),
            digital_source_path=os.getenv(
                "DIGITAL_SOURCE_PATH", "roms/xbox360-digital"
            ).strip("/"),
            output_dir=Path(
                os.getenv("OUTPUT_DIR", "/data/games-GoD")
            ).expanduser(),
            work_dir=work_dir,
            db_path=db_path,
            extractor_path=Path(
                os.getenv("EXTRACTOR_PATH", "/usr/local/bin/extract-xiso")
            ).expanduser(),
            converter_path=Path(
                os.getenv("CONVERTER_PATH", "/usr/local/bin/iso2god-next")
            ).expanduser(),
            converter_threads=max(1, _int_env("CONVERTER_THREADS", 2)),
            remote_timeout=max(10, _int_env("REMOTE_TIMEOUT", 120)),
            remote_retries=max(1, _int_env("REMOTE_RETRIES", 5)),
            keep_source_archives=_bool_env("KEEP_SOURCE_ARCHIVES", True),
            unlock_packages=_bool_env("UNLOCK_PACKAGES", True),
            xboxunity_tu_downloads=_bool_env("XBOXUNITY_TU_DOWNLOADS", True),
            download_game_icons=_bool_env("DOWNLOAD_GAME_ICONS", True),
            max_zip_members=max(
                1, _int_env("MAX_ZIP_MEMBERS", 200_000)
            ),
            app_auth_user=os.getenv("APP_AUTH_USER", "").strip(),
            app_auth_password=os.getenv("APP_AUTH_PASSWORD", ""),
            public_host=os.getenv(
                "PUBLIC_HOST", "iso2god.mediahub.stream"
            ).strip(),
        )

    def ensure_directories(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def effective_unlock_packages(self) -> bool:
        """Use the saved switch when present, otherwise UNLOCK_PACKAGES."""
        try:
            value = (self.work_dir / "unlock-packages").read_text().strip()
        except FileNotFoundError:
            return self.unlock_packages
        if value not in {"true", "false"}:
            raise ValueError("Saved unlock setting must be true or false")
        return value == "true"

    def save_unlock_packages(self, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise ValueError("unlock_packages must be a boolean")
        self.work_dir.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", dir=self.work_dir, prefix=".unlock-packages-", delete=False,
            ) as output:
                temporary = Path(output.name)
                output.write("true\n" if enabled else "false\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.work_dir / "unlock-packages")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def save_remote_api_key(self, value: str) -> None:
        value = value.strip()
        self.remote_api_key_file.parent.mkdir(parents=True, exist_ok=True)
        if not value:
            self.remote_api_key_file.unlink(missing_ok=True)
            return
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        descriptor = os.open(self.remote_api_key_file, flags, 0o600)
        try:
            os.write(descriptor, (value + "\n").encode("utf-8"))
        finally:
            os.close(descriptor)
        os.chmod(self.remote_api_key_file, 0o600)

    def save_remote_auth_mode(self, value: str) -> None:
        value = normalize_auth_mode(value, default="")
        if value not in {AUTH_MODE_API_KEY, AUTH_MODE_BASIC}:
            raise ValueError("auth_mode must be api-key or basic")
        self.remote_auth_mode_file.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        descriptor = os.open(self.remote_auth_mode_file, flags, 0o600)
        try:
            os.write(descriptor, (value + "\n").encode("utf-8"))
        finally:
            os.close(descriptor)
        os.chmod(self.remote_auth_mode_file, 0o600)

    def save_remote_basic_credentials(self, user: str, password: str) -> None:
        user = user.strip()
        self.remote_basic_file.parent.mkdir(parents=True, exist_ok=True)
        if not user and not password:
            self.remote_basic_file.unlink(missing_ok=True)
            return
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        descriptor = os.open(self.remote_basic_file, flags, 0o600)
        try:
            os.write(descriptor, f"{user}\n{password}".encode("utf-8"))
        finally:
            os.close(descriptor)
        os.chmod(self.remote_basic_file, 0o600)
