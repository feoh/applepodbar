"""Backend-agnostic content boundary for the Textual UI.

The UI never imports a transport. If Apple's developer JWT becomes unavailable,
subsequent browse/search/show requests use iTunes Search and publisher RSS.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from applepodbar.amp_client import AmpClient, parse_episode, parse_show
from applepodbar.models import BrowserItem, Episode, ItemKind, Show
from applepodbar.positions import PositionSync
from applepodbar.rss_client import RssClient, RssClientError
from applepodbar.tokens import DeveloperTokenError


class Catalog:
    """Resolve browse and search content into shared boundary objects."""

    def __init__(self, *, storefront: str = "us", backend: str = "amp") -> None:
        self.amp = AmpClient(storefront=storefront)
        self.positions = PositionSync(self.amp)
        self._rss: RssClient | None = None
        self._use_rss = backend == "rss"
        self.storefront = storefront

    @property
    def using_rss(self) -> bool:
        return self._use_rss

    def _fallback(self) -> RssClient:
        self._use_rss = True
        if self._rss is None:
            self._rss = RssClient(storefront=self.storefront)
        return self._rss

    def close(self) -> None:
        self.amp.close()
        if self._rss is not None:
            self._rss.close()

    def charts(self) -> list[Show]:
        """Return top shows from AMP or a broad iTunes directory search."""

        if self._use_rss:
            return self._fallback().search("podcast")
        try:
            payload = self.amp.charts()
            buckets = payload.get("results", {}).get("podcasts", [])
            data = buckets[0].get("data", []) if buckets else payload.get("data", [])
            return [
                parse_show(record)
                for record in data
                if record.get("type") == "podcasts"
            ]
        except DeveloperTokenError:
            return self._fallback().search("podcast")

    def genres(self) -> list[BrowserItem]:
        """List AMP podcast genres (unavailable in RSS-only mode)."""

        if self._use_rss:
            return []
        try:
            return [
                BrowserItem(
                    ItemKind.CATEGORY,
                    f"genre:{row['id']}",
                    row.get("attributes", {}).get("name", "Genre"),
                )
                for row in self.amp.genres().get("data", [])
            ]
        except DeveloperTokenError:
            self._fallback()
            return []

    def editorial(self) -> list[BrowserItem]:
        """List curated sections in the nested web editorial tree."""

        if self._use_rss:
            return []
        try:
            return [
                BrowserItem(
                    ItemKind.CATEGORY, str(row["id"]), row["attributes"]["name"]
                )
                for group in self.amp.editorial_groupings().get("data", [])
                for row in _editorial_nodes(group)
                if row.get("type") == "editorial-elements"
                and row.get("attributes", {}).get("name")
                and row.get("relationships", {}).get("children")
            ]
        except DeveloperTokenError:
            self._fallback()
            return []

    def genre_shows(self, genre_id: str) -> list[Show]:
        """Browse charts filtered by genre ID."""

        if self._use_rss:
            return []
        try:
            payload = self.amp.get(
                self.amp._catalog("charts"),
                {"types": "podcasts", "genre": genre_id, "limit": 25},
            )
            buckets = payload.get("results", {}).get("podcasts", [])
            return (
                [parse_show(row) for row in buckets[0].get("data", [])]
                if buckets
                else []
            )
        except DeveloperTokenError:
            self._fallback()
            return []

    def grouping_shows(self, grouping_id: str) -> list[Show]:
        """Resolve an editorial section's embedded podcast contents."""

        if self._use_rss:
            return []
        try:
            for group in self.amp.editorial_groupings().get("data", []):
                for node in _editorial_nodes(group):
                    if str(node.get("id")) == grouping_id:
                        return [
                            parse_show(item)
                            for child in _editorial_nodes(node)
                            for item in child.get("relationships", {})
                            .get("contents", {})
                            .get("data", [])
                            if item.get("type") == "podcasts"
                        ]
        except DeveloperTokenError:
            self._fallback()
        return []

    def search(self, term: str) -> tuple[list[Show], list[Episode]]:
        """Search shows and episodes (RSS mode returns shows only)."""

        if self._use_rss:
            return self._fallback().search(term), []
        try:
            results = self.amp.search(term).get("results", {})
            shows = [
                parse_show(row) for row in results.get("podcasts", {}).get("data", [])
            ]
            episodes = [
                parse_episode(row)
                for row in results.get("podcast-episodes", {}).get("data", [])
            ]
            return shows, episodes
        except DeveloperTokenError:
            return self._fallback().search(term), []

    def show(self, show_id: str) -> tuple[Show, list[Episode]]:
        """Fetch a show and its episodes from AMP or publisher RSS."""

        if self._use_rss:
            return self._fallback().show(show_id)
        try:
            return self.amp.show(show_id), self.amp.episodes(show_id)
        except DeveloperTokenError:
            return self._fallback().show(show_id)

    def episode(self, episode_id: str) -> Episode:
        """Fetch an AMP episode when it is not already in the current view."""

        if self._use_rss:
            raise RssClientError("RSS episodes must be opened through a show's feed.")
        return self.amp.episode(episode_id)

    def library_shows(self) -> list[Show]:
        """Return followed shows from the signed-in Apple Library."""

        return self.amp.library_shows()

    def library_section(self, section: str) -> list[Episode]:
        """Resolve a named signed-in episode collection."""

        sections = {
            "saved": self.amp.saved_episodes,
            "latest": self.amp.latest_episodes,
            "updated": self.amp.updated_episodes,
            "recent": self.amp.recently_played,
        }
        if section not in sections:
            raise ValueError(f"Unknown Library section: {section}")
        return sections[section]()

    def up_next(self) -> list[Episode]:
        return self.amp.up_next()

    def toggle_follow(self, show_id: str) -> bool:
        """Follow/unfollow an Apple show; return the new followed state."""

        is_followed = any(show.id == show_id for show in self.amp.library_shows())
        if is_followed:
            self.amp.unfollow_show(show_id)
        else:
            self.amp.follow_show(show_id)
        return not is_followed

    def toggle_saved(self, episode_id: str) -> bool:
        """Save/unsave an episode; return the new saved state."""

        is_saved = any(
            episode.id == episode_id for episode in self.amp.saved_episodes()
        )
        if is_saved:
            self.amp.unsave_episode(episode_id)
        else:
            self.amp.save_episode(episode_id)
        return not is_saved

    def mark_played(self, episode: Episode) -> bool:
        """Mark played using the web player's completed position mutation."""

        return self.update_position(
            episode.id,
            episode.duration_ms,
            episode.duration_ms,
            force=True,
            completed=True,
        )

    def library_channels(self) -> list[dict[str, Any]]:
        return self.amp.library_channels()

    def library_stations(self) -> list[dict[str, Any]]:
        return self.amp.library_stations()

    def account_storefront(self) -> str:
        """Refresh catalog locale from the authenticated account."""

        storefront = self.amp.account_storefront()
        self.amp.storefront = storefront
        return storefront

    def resume_position(self, episode_id: str, duration_ms: int) -> float:
        """Fetch a cross-device position, or return zero for a fresh episode."""

        return self.positions.read(episode_id).resume_seconds(duration_ms)

    def update_position(
        self,
        episode_id: str,
        position_ms: int,
        duration_ms: int,
        *,
        force: bool = False,
        completed: bool = False,
    ) -> bool:
        """Coalesced periodic or lifecycle write to the Apple account."""

        return self.positions.write(
            episode_id, position_ms, duration_ms, force=force, completed=completed
        )


def _editorial_nodes(record: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Walk the nested editorial tree embedded in the groupings response."""

    yield record
    for relation in ("tabs", "children"):
        for child in record.get("relationships", {}).get(relation, {}).get("data", []):
            yield from _editorial_nodes(child)
