"""XboxUnity Title Update client.

Queries the XboxUnity community database (https://xboxunity.net) for
available title updates by Title ID, and downloads the latest matching
TU into the proper Xbox 360 content layout.

The XboxUnity API is a public AJAX-style API that requires an
``X-Requested-With: XMLHttpRequest`` header for the query endpoint.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests

from validation import ValidationError, normalized_version, validate_title_update

logger = logging.getLogger("xbox360.xboxunity")

BASE_URL = "https://xboxunity.net/Resources/Lib"
DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 GODFoundry/1.0"
DOWNLOAD_LIMIT = 1024 * 1024 * 1024  # 1 GiB safety cap

ProgressCallback = Callable[[int, int], None]


@dataclass
class TitleUpdateEntry:
    title_update_id: str
    version: int
    media_id: str
    name: str
    size: int
    upload_date: str
    base_version: str
    sha1_hash: str
    title_id: str = ""


class XboxUnityError(RuntimeError):
    pass


class XboxUnityClient:
    def __init__(self, timeout: int = 30, retries: int = 3):
        self.timeout = timeout
        self.retries = retries
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": DEFAULT_UA,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://xboxunity.net/",
        })

    def _retry_delay(self, attempt: int) -> None:
        import time
        time.sleep(min(2 ** attempt, 10))

    def get_title_updates(self, title_id: str) -> list[TitleUpdateEntry]:
        """Return all title updates for the given Title ID across all media IDs."""
        url = f"{BASE_URL}/TitleUpdateInfo.php"
        params = {"titleid": title_id.upper()}
        last_error: Exception | None = None
        for attempt in range(self.retries):
            response = None
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
                if response.status_code == 200:
                    return self._parse_updates(response.json(), title_id.upper())
                last_error = XboxUnityError(f"XboxUnity query HTTP {response.status_code}")
            except (requests.RequestException, ValueError, XboxUnityError) as exc:
                last_error = exc
            finally:
                if response is not None:
                    response.close()
            if attempt + 1 < self.retries:
                self._retry_delay(attempt)
        raise XboxUnityError(str(last_error or "XboxUnity query failed"))

    def _parse_updates(self, data: dict[str, Any], title_id: str = "") -> list[TitleUpdateEntry]:
        if not isinstance(data, dict):
            raise XboxUnityError("XboxUnity returned an invalid update list")
        entries: list[TitleUpdateEntry] = []
        groups = data.get("MediaIDS", [])
        if not isinstance(groups, list) or not isinstance(data.get("Updates", []), list):
            raise XboxUnityError("XboxUnity returned an invalid update list")
        # XboxUnity also returns Type 2 records with a flat Updates list.
        if "Updates" in data:
            groups = [*groups, *({"MediaID": u.get("MediaID", ""), "Updates": [u]}
                                    for u in data.get("Updates", []) if isinstance(u, dict))]
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("Updates", []), list):
                continue
            media_id = group.get("MediaID", "")
            for update in group.get("Updates", []):
                if not isinstance(update, dict):
                    continue
                try:
                    entries.append(TitleUpdateEntry(
                        title_update_id=str(update["TitleUpdateID"]),
                        version=int(update.get("Version", 0)),
                        media_id=media_id,
                        name=str(update.get("Name", "")),
                        size=int(update.get("Size", 0)) * 1024,
                        upload_date=str(update.get("UploadDate", "")),
                        base_version=str(update.get("BaseVersion", "")),
                        sha1_hash=str(update.get("hash", "")),
                        title_id=title_id,
                    ))
                except (KeyError, ValueError, TypeError):
                    continue
        return entries

    def latest_for_media_id(
        self,
        title_id: str,
        media_id: str,
    ) -> TitleUpdateEntry | None:
        """Return the highest-version TU matching the given media ID."""
        return self.select_latest_for_media_id(
            self.get_title_updates(title_id),
            media_id,
        )

    @staticmethod
    def select_latest_for_media_id(
        entries: list[TitleUpdateEntry],
        media_id: str,
        base_version: str | None = None,
    ) -> TitleUpdateEntry | None:
        """Select a TU only when XboxUnity reports the exact Media ID."""
        matching = []
        for entry in entries:
            if entry.media_id.upper() != media_id.upper():
                continue
            if base_version is not None:
                try:
                    if normalized_version(entry.base_version) != normalized_version(base_version):
                        continue
                except ValidationError:
                    continue
            matching.append(entry)
        if not matching:
            return None
        matching.sort(key=lambda e: e.version, reverse=True)
        return matching[0]

    def download_title_update(
        self,
        tu: TitleUpdateEntry,
        destination_dir: Path,
        progress: ProgressCallback | None = None,
    ) -> Path:
        """Download a title update file. Returns the local path."""
        url = f"{BASE_URL}/TitleUpdate.php"
        params = {"tuid": tu.title_update_id}
        headers = {"X-Requested-With": ""}  # download endpoint doesn't need AJAX header
        destination_dir.mkdir(parents=True, exist_ok=True)
        last_error: Exception | None = None
        for attempt in range(self.retries):
            response = None
            partial = None
            try:
                response = self.session.get(
                    url, params=params, headers=headers,
                    timeout=self.timeout, stream=True,
                )
                if response.status_code != 200:
                    raise XboxUnityError(f"Download HTTP {response.status_code}")

                disposition = response.headers.get("Content-Disposition", "")
                filename = _parse_filename(disposition) or f"tu_{tu.title_update_id}"
                if filename in {".", ".."} or any(c in filename for c in ("/", "\\", "\x00", "\r", "\n")):
                    raise XboxUnityError("XboxUnity returned an unsafe filename")

                target = destination_dir / filename
                partial = target.with_name(target.name + ".partial")
                total = int(response.headers.get("Content-Length", 0))
                downloaded = 0
                md5 = hashlib.md5()
                try:
                    with partial.open("wb") as output:
                        for chunk in response.iter_content(chunk_size=64 * 1024):
                            if not chunk:
                                continue
                            downloaded += len(chunk)
                            if downloaded > DOWNLOAD_LIMIT:
                                raise XboxUnityError("download exceeded safety limit")
                            output.write(chunk)
                            md5.update(chunk)
                            if progress and total:
                                progress(downloaded, total)
                        output.flush()
                    if total and total != downloaded:
                        raise XboxUnityError("Title-update download is incomplete")
                    transport_hash = response.headers.get("X-Content-Md5") or response.headers.get("Content-MD5")
                    if transport_hash:
                        transport_hash = transport_hash.strip().strip('"')
                        expected_md5 = (bytes.fromhex(transport_hash) if len(transport_hash) == 32
                                        else base64.b64decode(transport_hash, validate=True))
                        if md5.digest() != expected_md5:
                            raise XboxUnityError("Title-update download checksum failed")
                    # XboxUnity's 40-digit hash is the STFS content ID at 0x32C,
                    # not a SHA-1 digest of the entire downloaded file.
                    validate_title_update(
                        partial, title_id=tu.title_id or None,
                        media_id=tu.media_id or None, base_version=tu.base_version or None,
                        content_id=tu.sha1_hash or None, expected_size=tu.size,
                    )
                    partial.replace(target)
                except Exception:
                    partial.unlink(missing_ok=True)
                    raise
                logger.info("tu_downloaded title_id=%s file=%s size=%d",
                            tu.media_id, filename, downloaded)
                return target
            except (requests.RequestException, XboxUnityError, ValidationError, ValueError) as exc:
                last_error = exc
                if attempt + 1 < self.retries:
                    self._retry_delay(attempt)
            finally:
                if response is not None:
                    response.close()
                if partial is not None:
                    partial.unlink(missing_ok=True)
        raise XboxUnityError(str(last_error or "download failed"))


def _parse_filename(disposition: str) -> str:
    """Extract filename from a Content-Disposition header."""
    import re
    match = re.search(r'filename="?([^";]+)"?', disposition)
    return match.group(1) if match else ""
