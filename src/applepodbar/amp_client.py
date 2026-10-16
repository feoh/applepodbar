"""Read the Apple Podcasts catalog through the web player's AMP API.

No credentials are embedded in the package. The developer JWT comes from the
current web bundle; user credentials, when available, are read from settings.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, Self
from urllib.parse import urlsplit

import httpx

from applepodbar.models import Episode, Show
from applepodbar.settings import TOKENS_PATH, clear_media_user_token, load_tokens
from applepodbar.tokens import WEB_ORIGIN, DeveloperTokenProvider

API_BASE = "https://amp-api.podcasts.apple.com/v1/"


class AmpClientError(RuntimeError):
    """An API outage, malformed response, or invalid endpoint (not a login issue)."""


class AuthRequired(AmpClientError):
    """The signed-in session is missing or expired; run applepodbar login."""


def _artwork(attributes: Mapping[str, Any], size: int = 300) -> str | None:
    art = attributes.get("artwork")
    if not isinstance(art, dict) or not isinstance(art.get("url"), str):
        return None
    return (
        art["url"]
        .replace("{w}", str(size))
        .replace("{h}", str(size))
        .replace("{f}", "jpg")
    )


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""


def parse_show(record: Mapping[str, Any]) -> Show:
    """Map an AMP show resource into the shared UI boundary."""

    attributes = record.get("attributes") or {}
    return Show(
        id=str(record["id"]),
        title=_string(attributes.get("name")),
        author=_string(attributes.get("artistName")),
        description=_string(attributes.get("description")),
        artwork_url=_artwork(attributes),
    )


def parse_episode(record: Mapping[str, Any]) -> Episode:
    """Map an AMP episode; paid/FairPlay HLS is never passed to mpv."""

    attributes = record.get("attributes") or {}
    offers = attributes.get("offers") or []
    paid = any(
        offer.get("kind") == "get" and offer.get("type") not in (None, "STDQ")
        for offer in offers
        if isinstance(offer, dict)
    )
    asset = attributes.get("assetUrl")
    if not isinstance(asset, str) or not asset.startswith(("https://", "http://")):
        asset = None
    # The paid stream is FairPlay; even an exposed HLS URL cannot be played.
    if paid or (asset and "/hls/playlist.m3u8" in asset):
        asset = None
    duration = attributes.get("durationInMilliseconds")
    if isinstance(duration, bool) or not isinstance(duration, int):
        duration = 0
    relationships = record.get("relationships") or {}
    podcast = (relationships.get("podcast") or {}).get("data") or []
    parent = podcast[0] if podcast else {}
    return Episode(
        id=str(record["id"]),
        title=_string(attributes.get("name")),
        show_id=str(parent.get("id", "")),
        show_title=_string((parent.get("attributes") or {}).get("name")),
        description=_string(attributes.get("description")),
        asset_url=asset,
        duration_ms=max(0, duration),
        guid=_string(attributes.get("guid")),
        release_date=_string(attributes.get("releaseDateTime")),
        artwork_url=_artwork(attributes),
        is_paid=paid,
        subscription_url=_string(attributes.get("subscriptionUrl")) or None,
    )


class AmpClient:
    """Synchronous catalog transport; construct in a worker, not the UI thread."""

    def __init__(
        self,
        *,
        storefront: str = "us",
        client: httpx.Client | None = None,
        tokens_path: Path = TOKENS_PATH,
    ) -> None:
        self.storefront = storefront
        self._owns_client = client is None
        self.http = client or httpx.Client(timeout=15)
        self.tokens_path = tokens_path
        self.provider = DeveloperTokenProvider(
            self.http, storefront=storefront, tokens_path=tokens_path
        )

    def close(self) -> None:
        """Release an owned connection pool."""

        if self._owns_client:
            self.http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def get(
        self, path: str, params: Mapping[str, str | int] | None = None
    ) -> dict[str, Any]:
        """GET an AMP path, refreshing the developer token once on a 401."""

        return self._request("GET", path, params)

    def put(
        self,
        path: str,
        params: Mapping[str, str | int],
        body: Mapping[str, Any],
    ) -> dict[str, Any]:
        """PUT an authenticated AMP resource with the same error handling."""

        return self._request("PUT", path, params, body)

    def post(self, path: str) -> dict[str, Any]:
        """POST an empty-body Library mutation."""

        return self._request("POST", path)

    def delete(self, path: str) -> dict[str, Any]:
        """DELETE a Library resource."""

        return self._request("DELETE", path)

    def _request(
        self,
        method: str,
        path: str,
        params: Mapping[str, str | int] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Validate destination before forwarding either credential to AMP."""

        if path.startswith("https://"):
            url = path
        elif path.startswith("/v1/"):
            url = "https://amp-api.podcasts.apple.com" + path
        else:
            url = API_BASE + path.lstrip("/")
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "amp-api.podcasts.apple.com"
            or not parsed.path.startswith("/v1/")
        ):
            raise AmpClientError("AMP pagination URL has an unexpected host or path.")
        user_token = load_tokens(self.tokens_path).media_user_token
        if parsed.path.startswith("/v1/me/") and not user_token:
            raise AuthRequired("Sign in with `applepodbar login` to use your Library.")

        for attempt in range(2):
            token = self.provider.get(force=attempt > 0)
            headers = {"Authorization": f"Bearer {token}", "Origin": WEB_ORIGIN}
            if user_token:
                headers["media-user-token"] = user_token
            try:
                response = self.http.request(
                    method, url, params=params, headers=headers, json=body
                )
            except httpx.HTTPError as exc:
                raise AmpClientError("Apple Podcasts API is unavailable.") from exc
            if response.status_code == 401 and attempt == 0:
                continue
            if response.status_code >= 400:
                code = ""
                try:
                    code = str(response.json().get("errors", [{}])[0].get("code", ""))
                except (ValueError, TypeError, AttributeError, IndexError, KeyError):
                    pass
                if parsed.path.startswith("/v1/me/") and (
                    response.status_code == 401
                    or (response.status_code == 403 and code == "40300")
                ):
                    clear_media_user_token(self.tokens_path)
                    raise AuthRequired(
                        "Apple Podcasts sign-in expired; run `applepodbar login` again."
                    )
                raise AmpClientError(
                    f"Apple Podcasts API returned {response.status_code} (code {code or 'unknown'})."
                )
            if response.status_code == 204 or not response.content:
                return {}
            try:
                result = response.json()
            except ValueError as exc:
                raise AmpClientError("Apple Podcasts returned invalid JSON.") from exc
            if not isinstance(result, dict):
                raise AmpClientError("Apple Podcasts returned an unexpected response.")
            return result
        raise AssertionError("unreachable")

    def _catalog(self, suffix: str) -> str:
        return f"catalog/{self.storefront}/{suffix}"

    def _me_items(
        self, path: str, *, limit: int = 25, max_pages: int = 4
    ) -> list[dict[str, Any]]:
        """Read signed-in resources, capping each interactive fetch at 100 items.

        Most /me routes reject a limit above 25 with HTTP 400/40005.
        """

        next_path = f"me/{path}"
        params: Mapping[str, str | int] | None = {"limit": limit}
        seen: set[str] = set()
        result: list[dict[str, Any]] = []
        for _ in range(max_pages):
            if next_path in seen:
                raise AmpClientError("Apple Podcasts returned a pagination loop.")
            seen.add(next_path)
            payload = self.get(next_path, params=params)
            data = payload.get("data", [])
            if not isinstance(data, list):
                raise AmpClientError("Apple Podcasts returned invalid library data.")
            result.extend(data)
            link = payload.get("next")
            if not link:
                break
            if not isinstance(link, str):
                raise AmpClientError("Apple Podcasts returned an invalid next link.")
            next_path = link
            params = None
        return result

    def follow_show(self, show_id: str) -> None:
        """Follow a show in the signed-in Apple Library."""

        self.post(f"me/library/podcasts/{show_id}")

    def unfollow_show(self, show_id: str) -> None:
        """Remove a show from the signed-in Apple Library."""

        self.delete(f"me/library/podcasts/{show_id}")

    def save_episode(self, episode_id: str) -> None:
        """Save an episode in the signed-in Apple Library."""

        self.post(f"me/library/podcast-episodes/{episode_id}")

    def unsave_episode(self, episode_id: str) -> None:
        """Remove a saved episode from the signed-in Apple Library."""

        self.delete(f"me/library/podcast-episodes/{episode_id}")

    def library_shows(self) -> list[Show]:
        """Return followed podcasts."""

        return [parse_show(item) for item in self._me_items("library/podcasts")]

    def saved_episodes(self) -> list[Episode]:
        """Return episodes explicitly saved to Library."""

        return [
            parse_episode(item) for item in self._me_items("library/podcast-episodes")
        ]

    def up_next(self) -> list[Episode]:
        """Return the user's Apple Up Next queue."""

        return [parse_episode(item) for item in self._me_items("library/next-episodes")]

    def latest_episodes(self) -> list[Episode]:
        """Return the latest episodes from followed podcasts."""

        return [
            parse_episode(item) for item in self._me_items("library/latest-episodes")
        ]

    def updated_episodes(self) -> list[Episode]:
        """Return Library's recently updated episodes."""

        return [
            parse_episode(item)
            for item in self._me_items("library/recent/updated/podcast-episodes")
        ]

    def recently_played(self) -> list[Episode]:
        """Return episodes played recently on the user's devices."""

        return [
            parse_episode(item)
            for item in self._me_items("recent/played/podcast-episodes")
        ]

    def library_channels(self) -> list[dict[str, Any]]:
        """Return followed podcast-channel resources."""

        return self._me_items("library/podcast-channels")

    def library_stations(self) -> list[dict[str, Any]]:
        """Return Library station resources."""

        return self._me_items("library/podcast-stations")

    def account_storefront(self) -> str:
        """Return the logged-in user's country code from /me/storefront."""

        payload = self.get("me/storefront")
        data = payload.get("data", [])
        if not isinstance(data, list) or not data:
            raise AmpClientError("Apple Podcasts returned no user storefront.")
        return str(data[0]["id"]).lower()

    def _pages(
        self, path: str, params: Mapping[str, str | int] | None = None
    ) -> Iterator[dict[str, Any]]:
        """Yield resources, following AMP's `next` links until exhausted."""

        seen: set[str] = set()
        while path:
            if path in seen:
                raise AmpClientError("Apple Podcasts returned a pagination loop.")
            seen.add(path)
            payload = self.get(path, params=params)
            data = payload.get("data", [])
            if not isinstance(data, list):
                raise AmpClientError("Apple Podcasts returned invalid catalog data.")
            yield from data
            next_url = payload.get("next")
            if next_url is not None and not isinstance(next_url, str):
                raise AmpClientError("Apple Podcasts returned an invalid next link.")
            path = next_url or ""
            params = None

    def search(
        self,
        term: str,
        *,
        types: str = "podcasts,podcast-episodes,podcast-channels",
        limit: int = 25,
    ) -> dict[str, Any]:
        """Search all requested AMP resource types (raw grouped results)."""

        return self.get(
            self._catalog("search"),
            {
                "term": term,
                "types": types,
                "limit": limit,
                "with": "entitlements,hlsVideo",
            },
        )

    def search_shows(self, term: str, *, limit: int = 25) -> list[Show]:
        """Search podcast shows and normalize their records."""

        payload = self.search(term, types="podcasts", limit=limit)
        return [
            parse_show(item)
            for item in payload.get("results", {}).get("podcasts", {}).get("data", [])
        ]

    def show(self, show_id: str) -> Show:
        """Fetch one podcast show."""

        data = self.get(
            self._catalog(f"podcasts/{show_id}"), {"with": "entitlements,hlsVideo"}
        )["data"]
        return parse_show(data[0])

    def episodes(self, show_id: str, *, limit: int = 100) -> list[Episode]:
        """Fetch all episodes of a show, following `next` pagination."""

        return [
            parse_episode(item)
            for item in self._pages(
                self._catalog(f"podcasts/{show_id}/episodes"),
                {"limit": limit, "with": "entitlements,hlsVideo"},
            )
        ]

    def episode(self, episode_id: str) -> Episode:
        """Fetch one episode."""

        data = self.get(
            self._catalog(f"podcast-episodes/{episode_id}"),
            {"with": "entitlements,hlsVideo"},
        )["data"]
        return parse_episode(data[0])

    def charts(self, *, types: str = "podcasts", limit: int = 25) -> dict[str, Any]:
        """Fetch catalog charts."""

        return self.get(
            self._catalog("charts"),
            {"types": types, "limit": limit, "with": "entitlements,hlsVideo"},
        )

    def genres(self) -> dict[str, Any]:
        """Fetch catalog genres."""

        return self.get(self._catalog("genres"))

    def editorial_groupings(self) -> dict[str, Any]:
        """Fetch the web player's editorial groupings."""

        return self.get(
            f"editorial/{self.storefront}/groupings",
            {"platform": "web", "name": "podcasts"},
        )
