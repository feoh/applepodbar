"""Discover and cache the Apple Podcasts web player's developer JWT.

The token is Apple's, not ours to distribute. The bundle URL is discovered from
HTML on every refresh, and the JWT is decoded only to inspect its expiry.
"""

from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from applepodbar.settings import TOKENS_PATH, load_tokens, save_tokens

WEB_ORIGIN = "https://podcasts.apple.com"
BUNDLE_RE = re.compile(
    r'<script\b[^>]*\bsrc=["\']([^"\']*/assets/index~[^"\']+\.js)["\']', re.IGNORECASE
)
# Apple may include more header fields after alg, or end the header there.
JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
REFRESH_MARGIN_SECONDS = 24 * 60 * 60


class DeveloperTokenError(RuntimeError):
    """The web player changed or its token could not be refreshed."""


def _expiry(token: str) -> int:
    try:
        payload = token.split(".")[1]
        decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        exp = json.loads(decoded)["exp"]
        if isinstance(exp, bool) or not isinstance(exp, int):
            raise TypeError("invalid exp")
        return exp
    except (IndexError, ValueError, KeyError, TypeError) as exc:
        raise DeveloperTokenError(
            "Apple's web player token has no valid expiry."
        ) from exc


class DeveloperTokenProvider:
    """Retrieve a cached token, scraping the current web bundle as needed."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        storefront: str = "us",
        tokens_path: Path = TOKENS_PATH,
    ) -> None:
        self.client = client
        self.storefront = storefront
        self.tokens_path = tokens_path

    def get(self, *, force: bool = False) -> str:
        """Return a valid token, refreshing within 24h of expiry or on demand."""

        cached = load_tokens(self.tokens_path)
        if (
            not force
            and cached.developer_token
            and cached.developer_token_expires_at
            and cached.developer_token_expires_at > time.time() + REFRESH_MARGIN_SECONDS
        ):
            return cached.developer_token

        try:
            page = self.client.get(
                f"{WEB_ORIGIN}/{self.storefront}/home",
                headers={"User-Agent": "Mozilla/5.0 (compatible; applepodbar/0.1)"},
                follow_redirects=True,
            )
            page.raise_for_status()
            match = BUNDLE_RE.search(page.text)
            if not match:
                raise DeveloperTokenError("No main script found in Apple's web player.")
            bundle_url = urljoin(str(page.url), match.group(1))
            parsed = urlsplit(bundle_url)
            if parsed.scheme != "https" or parsed.netloc != "podcasts.apple.com":
                raise DeveloperTokenError("Unexpected web player bundle host.")
            bundle = self.client.get(bundle_url, follow_redirects=True)
            bundle.raise_for_status()
            token = ""
            expires = 0
            for match in JWT_RE.finditer(bundle.text):
                candidate = match.group(0)
                try:
                    header = candidate.split(".")[0]
                    decoded = base64.urlsafe_b64decode(
                        header + "=" * (-len(header) % 4)
                    )
                    if json.loads(decoded).get("alg") != "ES256":
                        continue
                    expires = _expiry(candidate)
                except (ValueError, TypeError, AttributeError, DeveloperTokenError):
                    continue
                token = candidate
                break
            if not token:
                raise DeveloperTokenError(
                    "No developer JWT found in Apple's web player bundle."
                )
            if expires <= time.time() + REFRESH_MARGIN_SECONDS:
                raise DeveloperTokenError(
                    "Apple's web player token is already expiring."
                )
        except (httpx.HTTPError, DeveloperTokenError) as exc:
            raise DeveloperTokenError(
                "Could not extract Apple's web player token — the web player may have changed."
            ) from exc

        save_tokens(
            replace(
                load_tokens(self.tokens_path),
                developer_token=token,
                developer_token_expires_at=expires,
            ),
            self.tokens_path,
        )
        return token
