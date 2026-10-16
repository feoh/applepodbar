"""Configuration and token persistence.

Layout is specified in docs/SPEC.md §8:

    <config_dir>/applepodbar/
      config.toml   preferences (non-secret)
      tokens.json   developer token + expiry, media-user-token  (mode 0600)

Tokens live in their own file so the preferences file stays safe to read,
diff, and paste into a bug report.
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from platformdirs import user_config_dir

APP_NAME = "applepodbar"
CONFIG_DIR = Path(user_config_dir(APP_NAME))
CONFIG_PATH = CONFIG_DIR / "config.toml"
TOKENS_PATH = CONFIG_DIR / "tokens.json"

DEFAULT_STOREFRONT = "us"
DEFAULT_SEEK_FORWARD_SECONDS = 30
DEFAULT_SEEK_BACK_SECONDS = 15
DEFAULT_PLAYBACK_SPEED = 1.0


@dataclass(frozen=True)
class ApplepodbarConfig:
    """Persisted, non-secret preferences."""

    storefront: str = DEFAULT_STOREFRONT
    seek_forward_seconds: int = DEFAULT_SEEK_FORWARD_SECONDS
    seek_back_seconds: int = DEFAULT_SEEK_BACK_SECONDS
    playback_speed: float = DEFAULT_PLAYBACK_SPEED


@dataclass(frozen=True)
class Tokens:
    """Credentials for the AMP API.

    ``developer_token`` is Apple's own web-player token, scraped at runtime and
    cached until ``developer_token_expires_at`` (a POSIX timestamp). It is never
    vendored into the repository -- see docs/SPEC.md §4.1.

    ``media_user_token`` identifies the signed-in user and is captured once by
    the Playwright login flow (§4.2).
    """

    developer_token: str | None = None
    developer_token_expires_at: int | None = None
    media_user_token: str | None = None

    @property
    def is_signed_in(self) -> bool:
        """Return whether a user token is available for ``/v1/me/`` routes."""

        return bool(self.media_user_token)


def config_exists(path: Path = CONFIG_PATH) -> bool:
    """Return whether a config file exists."""

    return path.exists()


def load_config(path: Path = CONFIG_PATH) -> ApplepodbarConfig:
    """Load preferences, falling back to defaults for anything missing.

    A missing config file is normal on first run and yields defaults.
    """

    if not path.exists():
        return ApplepodbarConfig()

    with path.open("rb") as config_file:
        data = tomllib.load(config_file)

    defaults = ApplepodbarConfig()
    general = data.get("general", {})
    playback = data.get("playback", {})

    storefront = str(general.get("storefront", defaults.storefront)).strip().lower()

    return ApplepodbarConfig(
        storefront=storefront or defaults.storefront,
        seek_forward_seconds=_positive_int(
            playback.get("seek_forward_seconds"), defaults.seek_forward_seconds
        ),
        seek_back_seconds=_positive_int(
            playback.get("seek_back_seconds"), defaults.seek_back_seconds
        ),
        playback_speed=_positive_float(
            playback.get("playback_speed"), defaults.playback_speed
        ),
    )


def save_config(config: ApplepodbarConfig, path: Path = CONFIG_PATH) -> None:
    """Write preferences as TOML."""

    path.parent.mkdir(parents=True, exist_ok=True)
    contents = "\n".join(
        [
            "[general]",
            f'storefront = "{_toml_escape(config.storefront)}"',
            "",
            "[playback]",
            f"seek_forward_seconds = {config.seek_forward_seconds}",
            f"seek_back_seconds = {config.seek_back_seconds}",
            f"playback_speed = {config.playback_speed}",
            "",
        ]
    )
    path.write_text(contents, encoding="utf-8")


def load_tokens(path: Path = TOKENS_PATH) -> Tokens:
    """Load cached tokens, treating an unreadable or corrupt file as empty.

    A corrupt token cache is recoverable by re-scraping and re-logging in, so it
    must never be fatal.
    """

    if not path.exists():
        return Tokens()

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Tokens()

    if not isinstance(data, dict):
        return Tokens()

    return Tokens(
        developer_token=_optional_str(data.get("developer_token")),
        developer_token_expires_at=_optional_int(
            data.get("developer_token_expires_at")
        ),
        media_user_token=_optional_str(data.get("media_user_token")),
    )


def save_tokens(tokens: Tokens, path: Path = TOKENS_PATH) -> None:
    """Write tokens with user-only permissions."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "developer_token": tokens.developer_token,
        "developer_token_expires_at": tokens.developer_token_expires_at,
        "media_user_token": tokens.media_user_token,
    }
    contents = json.dumps(payload, indent=2) + "\n"

    old_umask = os.umask(0o177)
    try:
        path.write_text(contents, encoding="utf-8")
    finally:
        os.umask(old_umask)
    path.chmod(0o600)


def clear_media_user_token(path: Path = TOKENS_PATH) -> Tokens:
    """Forget the user token, keeping the cached developer token.

    Called when a ``/v1/me/`` route answers 403/40300, which means the user
    token expired or was revoked.
    """

    tokens = replace(load_tokens(path), media_user_token=None)
    save_tokens(tokens, path)
    return tokens


def _toml_escape(value: str) -> str:
    """Escape a value for a basic TOML string."""

    return value.replace("\\", "\\\\").replace('"', '\\"')


def _optional_str(value: object) -> str | None:
    """Return a non-empty string, or None."""

    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _optional_int(value: object) -> int | None:
    """Return an int, or None."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _positive_int(value: object, fallback: int) -> int:
    """Return a positive int, or the fallback."""

    if isinstance(value, bool) or not isinstance(value, int):
        return fallback
    return value if value > 0 else fallback


def _positive_float(value: object, fallback: float) -> float:
    """Return a positive float, or the fallback."""

    if isinstance(value, bool) or not isinstance(value, int | float):
        return fallback
    return float(value) if value > 0 else fallback
