"""User credential persistence, separate from the on-disk developer JWT cache.

``keyring`` is optional: anonymous catalog browsing never imports it eagerly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

SERVICE = "applepodbar"
ACCOUNT = "apple-podcasts-session"


@dataclass(frozen=True)
class UserSession:
    """Captured MusicKit user credential and browser cookies."""

    token: str
    cookies: tuple[dict[str, Any], ...] = ()
    dsid: str | None = None


class CredentialStoreError(RuntimeError):
    """The OS keyring is missing or cannot persist a user session."""


def _keyring() -> Any:
    try:
        import keyring
    except ImportError as exc:
        raise CredentialStoreError(
            "Install the login extra with `uv sync --extra login` for keyring support."
        ) from exc
    return keyring


def load_session() -> UserSession | None:
    """Return the stored session, or None for an anonymous install."""

    try:
        value = _keyring().get_password(SERVICE, ACCOUNT)
        if not value:
            return None
        data = json.loads(value)
        token = data.get("token")
        if not isinstance(token, str) or not token:
            return None
        return UserSession(
            token=token,
            cookies=tuple(data.get("cookies", [])),
            dsid=data.get("dsid"),
        )
    except (CredentialStoreError, ValueError, TypeError, AttributeError):
        return None
    except Exception:  # noqa: BLE001 - optional backend failures mean anonymous mode
        # A locked or missing keyring must not prevent anonymous browsing.
        return None


def save_session(session: UserSession) -> None:
    """Persist the user credential only in the OS keyring."""

    try:
        _keyring().set_password(
            SERVICE,
            ACCOUNT,
            json.dumps(
                {
                    "token": session.token,
                    "cookies": session.cookies,
                    "dsid": session.dsid,
                }
            ),
        )
    except CredentialStoreError:
        raise
    except Exception as exc:
        raise CredentialStoreError(
            "Cannot save to the OS keyring; install or unlock a supported keyring backend."
        ) from exc


def clear_session() -> None:
    """Forget an expired user credential without touching the developer JWT."""

    try:
        backend = _keyring()
    except CredentialStoreError:
        return
    try:
        backend.delete_password(SERVICE, ACCOUNT)
    except backend.errors.PasswordDeleteError:
        # A missing account is harmless; the next login will replace it.
        return
    except Exception as exc:
        raise CredentialStoreError("Cannot clear the OS keyring session.") from exc
