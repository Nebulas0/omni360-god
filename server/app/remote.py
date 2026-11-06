from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import requests


ProgressCallback = Callable[[int, int], None]
AUTH_MODE_API_KEY = "api-key"
AUTH_MODE_BASIC = "basic"
VALID_AUTH_MODES = {AUTH_MODE_API_KEY, AUTH_MODE_BASIC}


class RemoteError(RuntimeError):
    pass


def normalize_auth_mode(value: str | None, default: str = AUTH_MODE_API_KEY) -> str:
    normalized = (value or "").strip().lower().replace("_", "-")
    aliases = {
        "api-key": AUTH_MODE_API_KEY,
        "apikey": AUTH_MODE_API_KEY,
        "x-api-key": AUTH_MODE_API_KEY,
        "basic": AUTH_MODE_BASIC,
        "basic-auth": AUTH_MODE_BASIC,
    }
    return aliases.get(normalized, default)


class RemoteClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: int = 120,
        retries: int = 5,
        basic_user: str = "",
        basic_password: str = "",
        auth_mode: str = AUTH_MODE_API_KEY,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.basic_user = basic_user.strip()
        self.basic_password = basic_password
        self.timeout = timeout
        self.retries = retries
        self._auth_mode = normalize_auth_mode(auth_mode)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "xbox360-iso2god/1.0",
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        })

    @property
    def basic_configured(self) -> bool:
        return bool(self.basic_user and self.basic_password)

    @property
    def auth_mode(self) -> str:
        return self._auth_mode

    @property
    def configured(self) -> bool:
        if self.auth_mode == AUTH_MODE_BASIC:
            return self.basic_configured
        return bool(self.api_key)

    def set_api_key(self, api_key: str) -> None:
        self.api_key = api_key.strip()

    def set_basic_credentials(self, user: str, password: str) -> None:
        self.basic_user = user.strip()
        self.basic_password = password

    def set_auth_mode(self, auth_mode: str) -> None:
        normalized = normalize_auth_mode(auth_mode, default="")
        if normalized not in VALID_AUTH_MODES:
            raise ValueError("auth_mode must be api-key or basic")
        self._auth_mode = normalized

    def _ensure_auth(self) -> None:
        if self.auth_mode == AUTH_MODE_BASIC:
            if not self.basic_configured:
                raise RemoteError("Basic Auth username and password are not configured")
            return
        if not self.api_key:
            raise RemoteError("Remote API key is not configured")

    def _request(self, url: str, headers: dict[str, str] | None = None, stream: bool = False):
        request_headers = {
            "X-Requested-With": "XMLHttpRequest",
            **(headers or {}),
        }
        if self.auth_mode == AUTH_MODE_BASIC:
            auth = (self.basic_user, self.basic_password)
        else:
            auth = None
            if self.api_key:
                request_headers["X-API-Key"] = self.api_key
        return self.session.get(
            url,
            headers=request_headers,
            auth=auth,
            stream=stream,
            timeout=(15, self.timeout),
            allow_redirects=False,
        )

    def _retry_delay(self, attempt: int) -> None:
        time.sleep(min(2 ** attempt, 15))

    def _handle_unauthorized(self, status_code: int, operation: str) -> RemoteError:
        return RemoteError(
            f"{operation} authentication failed using {self.auth_mode} "
            f"(HTTP {status_code})"
        )

    def get_json(self, url: str) -> dict[str, Any]:
        self._ensure_auth()
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                response = self._request(url)
                if response.status_code == 200:
                    try:
                        payload = response.json()
                    except json.JSONDecodeError as exc:
                        raise RemoteError("Remote API returned invalid JSON") from exc
                    if not isinstance(payload, dict):
                        raise RemoteError("Remote API returned an unexpected JSON shape")
                    return payload
                if response.status_code in {401, 403}:
                    last_error = self._handle_unauthorized(
                        response.status_code, "Remote API"
                    )
                elif response.status_code == 429 or response.status_code >= 500:
                    last_error = RemoteError(
                        f"Remote API temporarily unavailable (HTTP {response.status_code})"
                    )
                else:
                    raise RemoteError(
                        f"Remote API request failed (HTTP {response.status_code})"
                    )
            except (requests.RequestException, RemoteError) as exc:
                last_error = exc
            if attempt + 1 < self.retries:
                self._retry_delay(attempt)
        raise RemoteError(str(last_error or "Remote API request failed"))

    def list_directory(self, relative_path: str) -> list[dict[str, Any]]:
        encoded_path = quote(relative_path.strip("/"), safe="/")
        payload = self.get_json(f"{self.base_url}/api/browse/{encoded_path}")
        items = payload.get("items")
        if not isinstance(items, list):
            raise RemoteError("Remote browse response did not contain items")
        records: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict) or item.get("isDirectory"):
                continue
            source_path = item.get("relativePath")
            name = item.get("name")
            if not isinstance(source_path, str) or not isinstance(name, str):
                continue
            records.append({
                "source_path": source_path.strip("/"),
                "name": name,
                "size": int(item.get("size") or 0),
                "modified": item.get("modified"),
                "type": item.get("type"),
            })
        return records

    def file_download_url(self, relative_path: str) -> str:
        encoded_path = quote(relative_path.strip("/"), safe="/")
        return f"{self.base_url}/api/files/{encoded_path}/download"

    def test_connection(self, relative_path: str = "roms/xbox360") -> dict[str, Any]:
        """Try a lightweight request using the currently configured credentials.

        Returns a dict with ``ok`` (bool), ``status`` (int), and ``auth_mode``.
        Does not raise; callers can display the result directly.
        """
        result: dict[str, Any] = {"ok": False, "status": 0, "auth_mode": self.auth_mode}
        try:
            self._ensure_auth()
        except RemoteError as exc:
            result["error"] = str(exc)
            return result
        try:
            encoded_path = quote(relative_path.strip("/"), safe="/")
            response = self._request(
                f"{self.base_url}/api/browse/{encoded_path}"
            )
            result["status"] = response.status_code
            if response.status_code == 200:
                result["ok"] = True
                result["message"] = "Connection successful"
            elif response.status_code in {401, 403}:
                result["error"] = f"Authentication rejected (HTTP {response.status_code})"
            else:
                result["error"] = f"Unexpected response (HTTP {response.status_code})"
            response.close()
        except requests.RequestException as exc:
            result["error"] = f"Network error: {exc}"
        return result

    def download_file(
        self,
        relative_path: str,
        destination: Path,
        expected_size: int = 0,
        progress: ProgressCallback | None = None,
    ) -> int:
        self._ensure_auth()
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + ".part")
        if destination.exists() and (
            expected_size <= 0 or destination.stat().st_size == expected_size
        ):
            size = destination.stat().st_size
            if progress:
                progress(size, expected_size or size)
            return size

        url = self.file_download_url(relative_path)
        last_error: Exception | None = None
        for attempt in range(self.retries):
            offset = partial.stat().st_size if partial.exists() else 0
            headers: dict[str, str] = {}
            if offset:
                headers["Range"] = f"bytes={offset}-"
            try:
                response = self._request(url, headers=headers, stream=True)
                if response.status_code == 416 and expected_size == offset:
                    partial.replace(destination)
                    if progress:
                        progress(offset, expected_size or offset)
                    return offset
                if response.status_code in {401, 403}:
                    last_error = self._handle_unauthorized(
                        response.status_code, "Remote download"
                    )
                    response.close()
                    if attempt + 1 < self.retries:
                        self._retry_delay(attempt)
                    continue
                if response.status_code not in {200, 206}:
                    if response.status_code == 429 or response.status_code >= 500:
                        raise RemoteError(
                            f"Remote download temporarily unavailable (HTTP {response.status_code})"
                        )
                    raise RemoteError(
                        f"Remote download failed (HTTP {response.status_code})"
                    )

                append = offset > 0 and response.status_code == 206
                if not append:
                    offset = 0
                content_length = int(response.headers.get("Content-Length") or 0)
                total = expected_size
                if response.status_code == 206:
                    content_range = response.headers.get("Content-Range", "")
                    match = re.search(r"bytes\s+\d+-\d+/(\d+)", content_range)
                    if match:
                        total = int(match.group(1))
                if total <= 0 and content_length > 0:
                    total = offset + content_length if append else content_length

                mode = "ab" if append else "wb"
                completed = offset
                with partial.open(mode) as output:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        output.write(chunk)
                        completed += len(chunk)
                        if progress:
                            progress(completed, total)
                    output.flush()
                response.close()

                actual_size = partial.stat().st_size
                if expected_size > 0 and actual_size != expected_size:
                    raise RemoteError(
                        f"Downloaded size {actual_size} does not match expected size {expected_size}"
                    )
                partial.replace(destination)
                if progress:
                    progress(actual_size, expected_size or total or actual_size)
                return actual_size
            except (requests.RequestException, RemoteError) as exc:
                last_error = exc
                if attempt + 1 < self.retries:
                    self._retry_delay(attempt)
        raise RemoteError(str(last_error or "Remote download failed"))
