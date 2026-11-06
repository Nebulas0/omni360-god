from __future__ import annotations

import pytest

from remote import AUTH_MODE_BASIC, AUTH_MODE_API_KEY, RemoteClient, RemoteError


class FakeResponse:
    def __init__(self, status: int, payload: dict):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload

    def close(self):
        pass


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(kwargs)
        headers = kwargs.get("headers", {})
        if headers.get("X-API-Key") == "valid-key":
            return FakeResponse(200, {"items": []})
        if kwargs.get("auth") == ("games", "password"):
            return FakeResponse(200, {"items": []})
        return FakeResponse(401, {})


def test_api_key_mode_sends_api_key_and_games_api_header() -> None:
    client = RemoteClient(
        "https://example.invalid",
        "valid-key",
        retries=1,
        basic_user="games",
        basic_password="password",
        auth_mode=AUTH_MODE_API_KEY,
    )
    fake = FakeSession()
    client.session = fake
    payload = client.get_json("https://example.invalid/api/browse")
    assert payload == {"items": []}
    assert client.auth_mode == AUTH_MODE_API_KEY
    assert fake.calls[0]["headers"]["X-API-Key"] == "valid-key"
    assert fake.calls[0]["headers"]["X-Requested-With"] == "XMLHttpRequest"
    assert fake.calls[0]["auth"] is None


def test_basic_auth_mode_does_not_send_api_key() -> None:
    client = RemoteClient(
        "https://example.invalid",
        "valid-key",
        retries=1,
        basic_user="games",
        basic_password="password",
        auth_mode=AUTH_MODE_BASIC,
    )
    fake = FakeSession()
    client.session = fake
    payload = client.get_json("https://example.invalid/api/browse")
    assert payload == {"items": []}
    assert client.auth_mode == AUTH_MODE_BASIC
    assert "X-API-Key" not in fake.calls[0]["headers"]
    assert fake.calls[0]["headers"]["X-Requested-With"] == "XMLHttpRequest"
    assert fake.calls[0]["auth"] == ("games", "password")


def test_api_key_failure_does_not_implicitly_switch_to_basic() -> None:
    client = RemoteClient(
        "https://example.invalid",
        "invalid-key",
        retries=2,
        basic_user="games",
        basic_password="password",
        auth_mode=AUTH_MODE_API_KEY,
    )
    fake = FakeSession()
    client.session = fake
    with pytest.raises(RemoteError, match="using api-key"):
        client.get_json("https://example.invalid/api/browse")
    assert len(fake.calls) == 2
    assert all(call["auth"] is None for call in fake.calls)
