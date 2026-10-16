"""Zero-auth iTunes Search + publisher RSS fallback for free audio."""

from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import urlsplit

import feedparser  # type: ignore[import-untyped]
import httpx

from applepodbar.models import Episode, Show, Source

ITUNES_BASE = "https://itunes.apple.com"


class RssClientError(RuntimeError):
    """The iTunes directory or publisher feed is unavailable."""


class RssClient:
    """Resolve Apple show IDs to publisher RSS without any Apple credentials."""

    def __init__(
        self, client: httpx.Client | None = None, *, storefront: str = "us"
    ) -> None:
        self._owns_client = client is None
        self.http = client or httpx.Client(timeout=15, follow_redirects=True)
        self.storefront = storefront

    def close(self) -> None:
        if self._owns_client:
            self.http.close()

    def _get(
        self, url: str, params: dict[str, str | int] | None = None
    ) -> httpx.Response:
        try:
            response = self.http.get(url, params=params, follow_redirects=True)
            response.raise_for_status()
            return response
        except httpx.HTTPError as exc:
            raise RssClientError(
                "Could not reach the iTunes directory or podcast feed."
            ) from exc

    def search(self, term: str, *, limit: int = 25) -> list[Show]:
        """Find shows through iTunes Search, preserving the shared AMP show ID."""

        result = self._get(
            f"{ITUNES_BASE}/search",
            {
                "term": term,
                "entity": "podcast",
                "limit": limit,
                "country": self.storefront,
            },
        ).json()
        return [
            self._show_from_itunes(row)
            for row in result.get("results", [])
            if row.get("collectionId")
        ]

    def show(self, show_id: str) -> tuple[Show, list[Episode]]:
        """Look up a feed URL, then parse its audio enclosures."""

        results = (
            self._get(f"{ITUNES_BASE}/lookup", {"id": show_id})
            .json()
            .get("results", [])
        )
        if not results or not results[0].get("feedUrl"):
            raise RssClientError("No publisher RSS feed is available for this show.")
        show = self._show_from_itunes(results[0])
        assert show.feed_url is not None
        parsed_url = urlsplit(show.feed_url)
        if parsed_url.scheme not in ("http", "https") or not parsed_url.hostname:
            raise RssClientError("The publisher feed has an unsupported URL.")
        feed = feedparser.parse(self._get(show.feed_url).content)
        episodes: list[Episode] = []
        for item in feed.entries:
            audio = next(
                (
                    enclosure
                    for enclosure in item.get("enclosures", [])
                    if enclosure.get("href")
                    and (
                        enclosure.get("type", "").startswith("audio/")
                        or enclosure.get("href", "")
                        .lower()
                        .split("?")[0]
                        .endswith((".mp3", ".m4a", ".aac"))
                    )
                ),
                None,
            )
            if audio is None:
                continue
            guid = str(item.get("id") or item.get("link") or audio["href"])
            stable_id = hashlib.sha256(f"{show_id}:{guid}".encode()).hexdigest()[:24]
            duration_ms = _duration_ms(item.get("itunes_duration"))
            episodes.append(
                Episode(
                    id=stable_id,
                    title=str(item.get("title", "Untitled")),
                    show_id=show_id,
                    show_title=show.title,
                    description=str(item.get("summary", "")),
                    asset_url=str(audio["href"]),
                    duration_ms=duration_ms,
                    guid=guid,
                    release_date=str(item.get("published", "")),
                    artwork_url=show.artwork_url,
                    source=Source.RSS,
                )
            )
        return show, episodes

    @staticmethod
    def _show_from_itunes(record: dict[str, Any]) -> Show:
        return Show(
            id=str(record["collectionId"]),
            title=str(
                record.get("collectionName") or record.get("trackName") or "Untitled"
            ),
            author=str(record.get("artistName") or ""),
            artwork_url=record.get("artworkUrl600") or record.get("artworkUrl100"),
            feed_url=record.get("feedUrl"),
            source=Source.RSS,
        )


def _duration_ms(value: Any) -> int:
    """Parse iTunes RSS duration in seconds or HH:MM:SS format."""

    if not value:
        return 0
    try:
        parts = [int(part) for part in str(value).split(":")]
        if len(parts) > 3 or any(part < 0 for part in parts):
            return 0
        total = 0
        for part in parts:
            total = total * 60 + part
        return total * 1000
    except ValueError:
        return 0
