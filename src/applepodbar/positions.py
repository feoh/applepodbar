"""Playback-position wire format and sync policy, separate from mpv/UI."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from applepodbar.amp_client import AmpClient

POSITION_PATH = "me/playback/positions/podcast-episodes"
SYNC_INTERVAL_SECONDS = 30


@dataclass(frozen=True)
class PlaybackPosition:
    """A cross-device position returned by Apple."""

    position_ms: int = 0
    completed: bool = False

    def resume_seconds(self, duration_ms: int = 0) -> float:
        """Resume only beyond 30 seconds and before an episode's end."""

        if self.completed or self.position_ms <= 30_000:
            return 0.0
        if duration_ms > 0 and self.position_ms >= duration_ms:
            return 0.0
        return self.position_ms / 1000


@dataclass
class PositionSync:
    """Read remote positions and coalesce periodic/pause/stop writes."""

    client: AmpClient
    last_write: dict[str, tuple[float, int, bool]] = field(default_factory=dict)

    def read(self, episode_id: str) -> PlaybackPosition:
        """The GET query is `ids=ID`; writes target an episode resource."""

        payload = self.client.get(POSITION_PATH, {"ids": episode_id})
        for row in payload.get("data", []):
            if str(row.get("id")) == episode_id:
                attrs = row.get("attributes") or {}
                position = attrs.get("positionInMilliseconds")
                return PlaybackPosition(
                    position_ms=position
                    if isinstance(position, int) and not isinstance(position, bool)
                    else 0,
                    completed=attrs.get("completed") is True,
                )
        return PlaybackPosition()

    def write(
        self,
        episode_id: str,
        position_ms: int,
        duration_ms: int,
        *,
        force: bool = False,
        completed: bool = False,
    ) -> bool:
        """Write after 30s, or on lifecycle events; suppress duplicate flushes.

        A successful write alone advances the timer, so a network failure can
        be retried on the next periodic tick.
        """

        now = time.monotonic()
        last = self.last_write.get(episode_id)
        if last is not None:
            elapsed = now - last[0]
            if not force and elapsed < SYNC_INTERVAL_SECONDS:
                return False
            if (
                force
                and elapsed < 2
                and abs(position_ms - last[1]) < 1000
                and completed == last[2]
            ):
                return False
        is_completed = completed or (duration_ms > 0 and position_ms >= duration_ms)
        recorded = (
            datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        )
        self.client.put(
            f"{POSITION_PATH}/{episode_id}",
            {},
            {
                "type": "playback-positions",
                "attributes": {
                    "positionInMilliseconds": max(0, position_ms),
                    "recordedAtTimestamp": recorded,
                    "completed": is_completed,
                },
            },
        )
        self.last_write[episode_id] = (now, position_ms, is_completed)
        return True
