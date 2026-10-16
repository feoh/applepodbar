"""Shared boundary types between catalog backends, playback, and the UI.

Both the AMP client and the RSS fallback produce these same objects, which is
what lets the UI stay unaware of which backend answered. See docs/SPEC.md §5.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ItemKind(Enum):
    """What a browser row represents."""

    SHOW = "show"
    EPISODE = "episode"
    CATEGORY = "category"


class Source(Enum):
    """Which backend produced a record.

    Position sync is only possible for ``AMP`` records; ``RSS`` records come
    from a publisher feed and Apple knows nothing about them.
    """

    AMP = "amp"
    RSS = "rss"


@dataclass(frozen=True)
class Show:
    """A podcast series."""

    id: str
    title: str
    author: str = ""
    description: str = ""
    artwork_url: str | None = None
    feed_url: str | None = None
    source: Source = Source.AMP

    @property
    def label(self) -> str:
        """Return a one-line display label."""

        return f"{self.title} — {self.author}" if self.author else self.title


@dataclass(frozen=True)
class Episode:
    """A single episode of a show."""

    id: str
    title: str
    show_id: str = ""
    show_title: str = ""
    description: str = ""
    asset_url: str | None = None
    duration_ms: int = 0
    guid: str = ""
    release_date: str = ""
    artwork_url: str | None = None
    source: Source = Source.AMP

    @property
    def label(self) -> str:
        """Return a one-line display label."""

        if self.duration_ms > 0:
            return f"{self.title} ({format_duration(self.duration_ms)})"
        return self.title

    @property
    def is_playable(self) -> bool:
        """Return whether this episode exposes an audio asset we can play."""

        return bool(self.asset_url)

    @property
    def supports_position_sync(self) -> bool:
        """Return whether Apple can store a playback position for this episode."""

        return self.source is Source.AMP


@dataclass(frozen=True)
class BrowserItem:
    """A row in the browser pane."""

    kind: ItemKind
    id: str
    title: str
    subtitle: str = ""

    @property
    def label(self) -> str:
        """Return a one-line display label."""

        return f"{self.title} — {self.subtitle}" if self.subtitle else self.title


@dataclass(frozen=True)
class QueueEpisode:
    """An episode queued for playback.

    ``stream_url`` is resolved before queueing so the player never has to call
    back into a catalog backend.
    """

    episode_id: str
    title: str
    show_title: str
    stream_url: str
    duration_ms: int = 0
    source: Source = Source.AMP

    @property
    def label(self) -> str:
        """Return a one-line display label."""

        return f"{self.title} — {self.show_title}" if self.show_title else self.title

    @classmethod
    def from_episode(cls, episode: Episode) -> QueueEpisode:
        """Build a queue entry from a playable episode.

        Raises:
            ValueError: If the episode has no audio asset.
        """

        if not episode.asset_url:
            msg = f"Episode {episode.id!r} has no playable asset URL."
            raise ValueError(msg)
        return cls(
            episode_id=episode.id,
            title=episode.title,
            show_title=episode.show_title,
            stream_url=episode.asset_url,
            duration_ms=episode.duration_ms,
            source=episode.source,
        )


def format_duration(milliseconds: int) -> str:
    """Format a duration in milliseconds as ``H:MM:SS`` or ``M:SS``."""

    if milliseconds <= 0:
        return "0:00"
    total_seconds = milliseconds // 1000
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"
