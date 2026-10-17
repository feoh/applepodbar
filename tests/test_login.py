"""Offline sign-in tests: no browser, Apple account, or real keyring required."""

from __future__ import annotations

import json
import sys
from typing import Any

import pytest

from applepodbar import credentials
from applepodbar.login import LoginError, capture_user_session


class FakeKeyring:
    def __init__(self) -> None:
        self.value: str | None = None

    def get_password(self, service: str, account: str) -> str | None:
        assert (service, account) == (credentials.SERVICE, credentials.ACCOUNT)
        return self.value

    def set_password(self, service: str, account: str, value: str) -> None:
        self.value = value

    def delete_password(self, service: str, account: str) -> None:
        self.value = None


def test_user_session_stays_in_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    keyring = FakeKeyring()
    monkeypatch.setattr(credentials, "_keyring", lambda: keyring)
    session = credentials.UserSession(
        "secret", ({"name": "session", "value": "cookie"},), "123"
    )
    credentials.save_session(session)
    assert credentials.load_session() == session
    assert json.loads(keyring.value or "{}")["token"] == "secret"
    credentials.clear_session()
    assert credentials.load_session() is None


class FakeRequest:
    url = "https://amp-api.podcasts.apple.com/v1/me/library/podcasts"

    @property
    def headers(self) -> dict[str, str]:
        return {"media-user-token": "user-secret", "x-dsid": "123"}

    def all_headers(self) -> dict[str, str]:
        raise AssertionError(
            "All-headers protocol round-trip is unsafe during shutdown"
        )


class FakeContext:
    def __init__(self) -> None:
        self.listener: Any = None
        self.removed = False

    def on(self, event: str, listener: Any) -> None:
        assert event == "request"
        self.listener = listener

    def remove_listener(self, event: str, listener: Any) -> None:
        self.removed = True

    def cookies(self) -> list[dict[str, str]]:
        return [{"name": "podcasts", "value": "cookie"}]


class FakePage:
    def __init__(self, context: FakeContext, *, emit_request: bool = True) -> None:
        self.context = context
        self.emit_request = emit_request
        self.opened = False

    def goto(self, url: str, *, wait_until: str) -> None:
        assert url == "https://podcasts.apple.com"
        self.opened = True

    def get_by_role(self, role: str, *, name: str) -> Any:
        raise LookupError("button missing on this deploy")

    def wait_for_timeout(self, milliseconds: int) -> None:
        assert milliseconds == 500
        if self.emit_request:
            self.context.listener(FakeRequest())


def test_capture_observes_request_and_cookies() -> None:
    context = FakeContext()
    session = capture_user_session(context, FakePage(context))
    assert session.token == "user-secret"
    assert session.dsid == "123"
    assert session.cookies[0]["value"] == "cookie"
    assert context.removed


def test_capture_times_out_without_a_token() -> None:
    context = FakeContext()
    with pytest.raises(LoginError, match="No Apple Podcasts user token"):
        capture_user_session(
            context, FakePage(context, emit_request=False), timeout_seconds=0
        )
    assert context.removed


def test_missing_login_extra_points_at_uv_tool_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from applepodbar import login

    # A None entry makes the import raise ImportError, as with no extra installed.
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)

    with pytest.raises(LoginError, match=r"uv tool install 'applepodbar\[login\]'"):
        login.login()
