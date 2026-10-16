"""Global offline-test guard: never read a real user's keyring session."""

from __future__ import annotations

import pytest

from applepodbar import settings


@pytest.fixture(autouse=True)
def no_real_user_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent personal media-user-tokens from entering tests or failure logs."""

    monkeypatch.setattr(settings, "load_session", lambda: None)
