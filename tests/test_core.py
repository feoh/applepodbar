"""Tests for the applepodbar shell.

No test here may require network access, a real ``mpv``, or a real browser.
"""

from __future__ import annotations

import asyncio
import json
import stat
from collections.abc import Callable, Coroutine
from pathlib import Path

import pytest

from applepodbar import app as app_module
from applepodbar import playback as playback_module
from applepodbar.app import SIGN_IN_PROMPT, ApplepodbarApp, View, resolve_view
from applepodbar.models import (
    Episode,
    ItemKind,
    QueueEpisode,
    Show,
    Source,
    format_duration,
)
from applepodbar.playback import PlaybackProgress, PlaybackQueue
from applepodbar.settings import (
    ApplepodbarConfig,
    Tokens,
    clear_media_user_token,
    load_config,
    load_tokens,
    save_config,
    save_tokens,
)

# -- models ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("milliseconds", "expected"),
    [
        (0, "0:00"),
        (-1, "0:00"),
        (1000, "0:01"),
        (61_000, "1:01"),
        (1_662_000, "27:42"),
        (3_600_000, "1:00:00"),
        (3_661_000, "1:01:01"),
    ],
)
def test_format_duration(milliseconds: int, expected: str) -> None:
    """Durations render as M:SS, and grow an hours field only when needed."""

    assert format_duration(milliseconds) == expected


def test_episode_playability_and_sync_support() -> None:
    """An episode is playable with an asset, and syncable only when from AMP."""

    amp = Episode(id="1", title="Ep", asset_url="https://example.invalid/a.mp3")
    assert amp.is_playable
    assert amp.supports_position_sync

    rss = Episode(
        id="2",
        title="Ep",
        asset_url="https://example.invalid/b.mp3",
        source=Source.RSS,
    )
    assert rss.is_playable
    # Apple cannot store a position for an episode it never served.
    assert not rss.supports_position_sync

    assert not Episode(id="3", title="No asset").is_playable


def test_queue_episode_from_episode_requires_an_asset() -> None:
    """Queueing resolves the stream URL up front, or refuses."""

    episode = Episode(
        id="42",
        title="MAGA's Midterm Defectors",
        show_title="The Daily",
        asset_url="https://example.invalid/ep.mp3",
        duration_ms=1_662_000,
    )
    queued = QueueEpisode.from_episode(episode)
    assert queued.episode_id == "42"
    assert queued.stream_url == "https://example.invalid/ep.mp3"
    assert queued.label == "MAGA's Midterm Defectors — The Daily"

    with pytest.raises(ValueError, match="no playable asset URL"):
        QueueEpisode.from_episode(Episode(id="43", title="Unplayable"))


def test_labels_fall_back_when_metadata_is_missing() -> None:
    """Labels degrade to the bare title rather than rendering a dangling dash."""

    assert Show(id="1", title="The Daily", author="NYT").label == "The Daily — NYT"
    assert Show(id="1", title="The Daily").label == "The Daily"
    assert Episode(id="1", title="Ep", duration_ms=61_000).label == "Ep (1:01)"
    assert Episode(id="1", title="Ep").label == "Ep"


# -- playback queue -------------------------------------------------------


def _queued(name: str) -> QueueEpisode:
    return QueueEpisode(
        episode_id=name,
        title=name,
        show_title="Show",
        stream_url=f"https://example.invalid/{name}.mp3",
    )


def test_appending_does_not_start_playback() -> None:
    """Appending leaves current empty until next() is called."""

    queue = PlaybackQueue()
    queue.append([_queued("a"), _queued("b")])

    assert queue.current is None

    assert queue.next() is not None
    assert queue.current is not None
    assert queue.current.episode_id == "a"

    assert queue.next() is not None
    assert queue.current is not None
    assert queue.current.episode_id == "b"

    # Exhausted: no current episode, and no crash.
    assert queue.next() is None
    assert queue.current is None


def test_replace_resets_playback_state() -> None:
    """Replacing the queue clears what was playing."""

    queue = PlaybackQueue()
    queue.append([_queued("a")])
    queue.next()
    queue.replace([_queued("c")])

    assert queue.current is None
    assert queue.next_index == 0
    assert queue.next() is not None
    assert queue.current is not None
    assert queue.current.episode_id == "c"


def test_queue_labels_mark_the_current_episode() -> None:
    """Only the playing row gets the marker."""

    queue = PlaybackQueue()
    queue.append([_queued("a"), _queued("b")])
    queue.next()

    labels = queue.labels()
    assert labels[0].startswith("▶ ")
    assert labels[1].startswith("  ")


# -- playback progress ----------------------------------------------------


def test_progress_ratio_is_clamped_and_safe_at_zero_duration() -> None:
    """A live or unknown-duration stream must not divide by zero."""

    assert PlaybackProgress().progress_ratio() == 0.0
    assert PlaybackProgress(position_seconds=30).progress_ratio() == 0.0

    halfway = PlaybackProgress(position_seconds=50, duration_seconds=100)
    assert halfway.progress_ratio() == 0.5
    assert halfway.position_ms == 50_000

    overrun = PlaybackProgress(position_seconds=150, duration_seconds=100)
    assert overrun.progress_ratio() == 1.0


# -- settings -------------------------------------------------------------


def test_config_round_trips(tmp_path: Path) -> None:
    """Saved preferences load back unchanged."""

    path = tmp_path / "config.toml"
    config = ApplepodbarConfig(
        storefront="gb",
        seek_forward_seconds=45,
        seek_back_seconds=10,
        playback_speed=1.5,
    )
    save_config(config, path)

    assert load_config(path) == config


def test_missing_config_yields_defaults(tmp_path: Path) -> None:
    """First run is not an error."""

    assert load_config(tmp_path / "absent.toml") == ApplepodbarConfig()


def test_config_rejects_nonsense_values(tmp_path: Path) -> None:
    """Bad values fall back to defaults instead of breaking playback."""

    path = tmp_path / "config.toml"
    path.write_text(
        "[general]\n"
        'storefront = ""\n'
        "\n"
        "[playback]\n"
        "seek_forward_seconds = 0\n"
        'seek_back_seconds = "ten"\n'
        "playback_speed = -1\n",
        encoding="utf-8",
    )

    defaults = ApplepodbarConfig()
    assert load_config(path) == defaults


def test_tokens_round_trip_and_are_not_world_readable(tmp_path: Path) -> None:
    """Token storage is user-only, per docs/SPEC.md §4.2."""

    path = tmp_path / "tokens.json"
    tokens = Tokens(
        developer_token="header.payload.signature",
        developer_token_expires_at=1_798_132_873,
        media_user_token="user-token",
    )
    save_tokens(tokens, path)

    assert load_tokens(path) == Tokens(
        developer_token=tokens.developer_token,
        developer_token_expires_at=tokens.developer_token_expires_at,
    )
    assert "user-token" not in path.read_text(encoding="utf-8")

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, f"tokens.json is mode {mode:o}, expected 600"


def test_corrupt_token_cache_is_recoverable(tmp_path: Path) -> None:
    """A corrupt cache reads as empty rather than crashing the app."""

    path = tmp_path / "tokens.json"
    path.write_text("{not json", encoding="utf-8")
    assert load_tokens(path) == Tokens()

    path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    assert load_tokens(path) == Tokens()

    assert load_tokens(tmp_path / "absent.json") == Tokens()


def test_clearing_the_user_token_keeps_the_developer_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 403 on /v1/me/ invalidates the user token only, not Apple's."""

    from applepodbar import settings

    session = ["user"]
    monkeypatch.setattr(
        settings,
        "load_session",
        lambda: type("Session", (), {"token": session[0]})() if session else None,
    )
    monkeypatch.setattr(settings, "clear_session", lambda: session.clear())
    path = tmp_path / "tokens.json"
    save_tokens(
        Tokens(
            developer_token="dev",
            developer_token_expires_at=1_798_132_873,
            media_user_token="user",
        ),
        path,
    )

    cleared = clear_media_user_token(path)

    assert cleared.media_user_token is None
    assert not cleared.is_signed_in
    assert cleared.developer_token == "dev"
    assert cleared.developer_token_expires_at == 1_798_132_873
    assert load_tokens(path) == cleared


# -- view resolution ------------------------------------------------------


@pytest.mark.parametrize("view", [View.LIBRARY, View.UP_NEXT])
def test_signed_out_views_prompt_for_sign_in(view: View) -> None:
    """Library and Up Next need a user token (docs/SPEC.md §3.4)."""

    state = resolve_view(view, Tokens())

    assert state.message == SIGN_IN_PROMPT
    assert state.items == ()


@pytest.mark.parametrize("view", [View.LIBRARY, View.UP_NEXT])
def test_signed_in_views_do_not_prompt(view: View) -> None:
    """With a user token those views stop asking the user to sign in."""

    state = resolve_view(view, Tokens(media_user_token="user"))

    assert state.message != SIGN_IN_PROMPT


def test_home_is_available_without_signing_in() -> None:
    """Catalog browsing works with the developer token alone."""

    state = resolve_view(View.HOME, Tokens())

    assert state.message == ""
    assert [item.title for item in state.items] == [
        "Browse genres",
        "Editorial collections",
    ]
    assert all(item.kind is ItemKind.CATEGORY for item in state.items)


# -- app shell ------------------------------------------------------------


@pytest.fixture(autouse=True)
def offline_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every UI test stays offline, including startup's Home load."""

    class FakeCatalog:
        using_rss = False

        def __init__(self, *, storefront: str = "us", backend: str = "amp") -> None:
            pass

        def charts(self) -> list[Show]:
            return []

        def close(self) -> None:
            pass

    monkeypatch.setattr(app_module, "Catalog", FakeCatalog)


class FakePlayer:
    """Stand-in for MpvPlayer that records calls instead of running mpv."""

    def __init__(self) -> None:
        self.played: list[QueueEpisode] = []
        self.seeks: list[float] = []
        self.speed = 1.0
        self.is_paused = False
        self.stopped = False
        self.closed = False

    def play(self, episode: QueueEpisode, start_seconds: float = 0.0) -> None:
        self.played.append(episode)
        self.is_paused = False

    def pause_resume(self) -> None:
        self.is_paused = not self.is_paused

    def playback_progress(self) -> PlaybackProgress:
        return PlaybackProgress(30, 60)

    def seek(self, offset_seconds: float) -> None:
        self.seeks.append(offset_seconds)

    def set_speed(self, speed: float) -> float:
        self.speed = speed
        return speed

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True


def _run_app(scenario: Callable[[], Coroutine[object, object, None]]) -> None:
    """Run an async Textual scenario."""

    asyncio.run(scenario())


def test_app_starts_on_home_without_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh install opens on Home and says it is signed out."""

    monkeypatch.setattr(app_module, "load_config", lambda: ApplepodbarConfig())
    monkeypatch.setattr(app_module, "load_tokens", lambda: Tokens())

    async def scenario() -> None:
        pilot_app = ApplepodbarApp()
        async with pilot_app.run_test() as pilot:
            await pilot.pause()

            assert pilot_app.state.view is View.HOME
            assert pilot_app.sub_title == "Home"

            browser = pilot_app.query_one("#browser-list", app_module.ListView)
            assert len(browser.children) >= 2

            status = pilot_app.query_one("#status", app_module.Static)
            assert "Not signed in" in str(status.render())

    _run_app(scenario)


def test_switching_to_library_signed_out_shows_the_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pressing `l` while signed out explains how to sign in."""

    monkeypatch.setattr(app_module, "load_config", lambda: ApplepodbarConfig())
    monkeypatch.setattr(app_module, "load_tokens", lambda: Tokens())

    async def scenario() -> None:
        pilot_app = ApplepodbarApp()
        async with pilot_app.run_test() as pilot:
            await pilot.pause()
            # The search Input would otherwise swallow the letter key.
            pilot_app.query_one("#search", app_module.Input).display = False
            pilot_app.query_one("#browser-list", app_module.ListView).focus()

            await pilot.press("l")
            await pilot.pause()

            assert pilot_app.state.view is View.LIBRARY
            message = pilot_app.query_one("#message", app_module.Static)
            assert str(message.render()) == SIGN_IN_PROMPT

    _run_app(scenario)


def test_playing_and_stopping_updates_now_playing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The now-playing line tracks the queue, and stop clears it."""

    monkeypatch.setattr(app_module, "load_config", lambda: ApplepodbarConfig())
    monkeypatch.setattr(app_module, "load_tokens", lambda: Tokens())

    async def scenario() -> None:
        pilot_app = ApplepodbarApp()
        async with pilot_app.run_test() as pilot:
            await pilot.pause()
            fake = FakePlayer()
            pilot_app.player = fake  # type: ignore[assignment]

            now_playing = pilot_app.query_one("#now-playing", app_module.Static)
            assert str(now_playing.render()) == "Nothing playing"

            pilot_app.enqueue([_queued("a")])
            pilot_app.play_next()
            await pilot.pause()

            assert [e.episode_id for e in fake.played] == ["a"]
            assert "a — Show" in str(now_playing.render())

            pilot_app.action_stop()
            await pilot.pause()

            assert fake.stopped
            assert str(now_playing.render()) == "Nothing playing"

    _run_app(scenario)


def test_seek_actions_use_the_configured_increments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seek honours config rather than hardcoded values."""

    config = ApplepodbarConfig(seek_forward_seconds=45, seek_back_seconds=10)
    monkeypatch.setattr(app_module, "load_config", lambda: config)
    monkeypatch.setattr(app_module, "load_tokens", lambda: Tokens())

    async def scenario() -> None:
        pilot_app = ApplepodbarApp()
        async with pilot_app.run_test() as pilot:
            await pilot.pause()
            fake = FakePlayer()
            pilot_app.player = fake  # type: ignore[assignment]

            pilot_app.action_seek_forward()
            pilot_app.action_seek_back()

            assert fake.seeks == [45, -10]

    _run_app(scenario)


def test_missing_mpv_is_reported_without_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Browsing still works on a machine without mpv installed."""

    monkeypatch.setattr(app_module, "load_config", lambda: ApplepodbarConfig())
    monkeypatch.setattr(app_module, "load_tokens", lambda: Tokens())
    monkeypatch.setattr(playback_module.shutil, "which", lambda _name: None)

    async def scenario() -> None:
        pilot_app = ApplepodbarApp()
        async with pilot_app.run_test() as pilot:
            await pilot.pause()

            pilot_app.enqueue([_queued("a")])
            pilot_app.play_next()
            await pilot.pause()

            assert pilot_app.player is None
            status = pilot_app.query_one("#status", app_module.Static)
            assert "mpv was not found" in str(status.render())

    _run_app(scenario)
