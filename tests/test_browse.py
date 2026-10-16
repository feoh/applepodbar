"""Offline Textual journeys for browse, search, show detail, and playback."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.widgets import Input, ListView, Static

from applepodbar import app as app_module
from applepodbar.app import ApplepodbarApp, View
from applepodbar.local_library import LocalLibrary
from applepodbar.models import (
    BrowserItem,
    Episode,
    ItemKind,
    QueueEpisode,
    Show,
    Source,
)
from applepodbar.playback import PlaybackProgress
from applepodbar.settings import Tokens


class FakeCatalog:
    def __init__(self) -> None:
        self.closed = False
        self.daily = Show(
            id="1", title="The Daily", author="NYT", description="News every day"
        )
        self.episode_record = Episode(
            id="22",
            title="Today",
            show_id="1",
            show_title="The Daily",
            description="A story",
            asset_url="https://example.invalid/episode.mp3",
        )

    def charts(self) -> list[Show]:
        return [self.daily]

    def search(self, term: str) -> tuple[list[Show], list[Episode]]:
        assert term == "daily"
        return [self.daily], [self.episode_record]

    def show(self, show_id: str) -> tuple[Show, list[Episode]]:
        assert show_id == "1"
        return self.daily, [self.episode_record]

    def close(self) -> None:
        self.closed = True


class FakePlayer:
    def __init__(self) -> None:
        self.played: list[QueueEpisode] = []
        self.starts: list[float] = []
        self.speed = 1.0
        self.is_paused = False

    def play(self, episode: QueueEpisode, start_seconds: float = 0) -> None:
        self.played.append(episode)
        self.starts.append(start_seconds)

    def set_speed(self, speed: float) -> float:
        self.speed = speed
        return speed

    def pause_resume(self) -> None:
        self.is_paused = not self.is_paused

    def reap_finished(self) -> bool:
        return False

    def playback_progress(self) -> PlaybackProgress:
        return PlaybackProgress(30, 60)

    def close(self) -> None:
        pass


async def journey() -> None:
    catalog = FakeCatalog()
    app = ApplepodbarApp(catalog=catalog)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        assert app.state.view is View.HOME
        assert "The Daily" in [row.title for row in app.state.items]
        browser = app.query_one("#browser-list", ListView)
        browser.index = 2
        browser.focus()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        assert app.state.view is View.SHOW
        assert [row.title for row in app.state.items] == ["Today"]
        assert "News every day" in str(app.query_one("#description", Static).render())
        assert app.episodes["22"].asset_url is not None
        player = FakePlayer()
        app.player = player  # type: ignore[assignment]
        browser.index = 0
        browser.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert [entry.episode_id for entry in player.played] == ["22"]
        assert app.queue.current is not None
        app.display_progress(PlaybackProgress(30, 60))
        assert "0:30 / 1:00" in str(app.query_one("#now-playing", Static).render())
        app.action_speed_up()
        assert player.speed == 1.25
        app.action_focus_search()
        search = app.query_one("#search", Input)
        search.value = "daily"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        assert app.state.view is View.SEARCH
        assert [row.title for row in app.state.items] == ["The Daily", "Today"]
    assert catalog.closed


def test_browse_search_show_details() -> None:
    asyncio.run(journey())


def test_rss_local_follow_and_resume(tmp_path: Path) -> None:
    async def scenario() -> None:
        class RssCatalog(FakeCatalog):
            using_rss = True

            def charts(self) -> list[Show]:
                return [Show(id="1", title="The Daily", source=Source.RSS)]

        app = ApplepodbarApp(catalog=RssCatalog())  # type: ignore[arg-type]
        app.local_library = LocalLibrary(tmp_path / "local_library.json")
        async with app.run_test() as pilot:
            await pilot.pause()
            browser = app.query_one("#browser-list", ListView)
            browser.index = 0
            app.action_toggle_follow()
            assert app.local_library.followed_shows() == ["1"]
            app.show_view(View.LIBRARY)
            await pilot.pause()
            await pilot.pause()
            assert [item.title for item in app.state.items] == ["The Daily"]
            assert app.state.view is View.LIBRARY
            app.action_focus_search()
            assert app.query_one("#search", Input).display
            app.show_view(View.LIBRARY)
            assert not app.query_one("#search", Input).display
            app.query_one("#browser-list", ListView).focus()
            await pilot.press("l")
            await pilot.pause()
            assert app.state.view is View.LIBRARY
            player = FakePlayer()
            app.player = player  # type: ignore[assignment]
            episode = QueueEpisode(
                episode_id="episode",
                title="Today",
                show_title="The Daily",
                stream_url="https://example.invalid/audio.mp3",
                source=Source.RSS,
            )
            app.local_library.set_position("episode", 42_000)
            app.enqueue([episode])
            app.play_next()
            assert player.played[0].episode_id == "episode"
            assert player.starts == [42]
            app.record_position(force=True)
            assert app.local_library.position_ms("episode") == 30_000

    asyncio.run(scenario())


def test_signed_in_library_and_up_next(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        app_module, "load_tokens", lambda: Tokens(media_user_token="fixture")
    )

    class LibraryCatalog(FakeCatalog):
        using_rss = False

        def library_shows(self) -> list[Show]:
            return [self.daily]

        def library_section(self, section: str) -> list[Episode]:
            assert section == "recent"
            return [self.episode_record]

        def up_next(self) -> list[Episode]:
            return [self.episode_record]

        def account_storefront(self) -> str:
            return "us"

    async def scenario() -> None:
        app = ApplepodbarApp(catalog=LibraryCatalog())  # type: ignore[arg-type]
        async with app.run_test() as pilot:
            app.show_view(View.LIBRARY)
            await pilot.pause()
            await pilot.pause()
            assert "The Daily" in [item.title for item in app.state.items]
            assert "Recently played" in [item.title for item in app.state.items]
            browser = app.query_one("#browser-list", ListView)
            browser.index = 3
            browser.focus()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            assert app.state.view is View.LIBRARY
            assert [item.title for item in app.state.items] == ["Today"]
            app.show_view(View.UP_NEXT)
            await pilot.pause()
            await pilot.pause()
            assert app.state.view is View.UP_NEXT
            assert [item.title for item in app.state.items] == ["Today"]

    asyncio.run(scenario())


def test_signed_in_follow_save_and_mark_played_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        app_module, "load_tokens", lambda: Tokens(media_user_token="fixture")
    )

    class MutationCatalog(FakeCatalog):
        using_rss = False

        def __init__(self) -> None:
            super().__init__()
            self.actions: list[str] = []

        def account_storefront(self) -> str:
            return "us"

        def toggle_follow(self, show_id: str) -> bool:
            self.actions.append("follow")
            return True

        def toggle_saved(self, episode_id: str) -> bool:
            self.actions.append("save")
            return True

        def mark_played(self, episode: Episode) -> bool:
            self.actions.append("played")
            return True

    async def scenario() -> None:
        catalog = MutationCatalog()
        app = ApplepodbarApp(catalog=catalog)  # type: ignore[arg-type]
        async with app.run_test() as pilot:
            await pilot.pause()
            browser = app.query_one("#browser-list", ListView)
            browser.index = 2
            app.action_toggle_follow()
            await pilot.pause()
            assert "follow" in catalog.actions
            app.show_view(View.SHOW)
            app.apply_content(
                View.SHOW,
                [BrowserItem(ItemKind.EPISODE, "22", "Today")],
                [catalog.episode_record],
                catalog.daily,
                app._revision,
            )
            await pilot.pause()
            browser.index = 0
            await pilot.pause()
            app.action_toggle_save()
            await pilot.pause()
            app.action_mark_played()
            await pilot.pause()
            assert catalog.actions == ["follow", "save", "played"]

    asyncio.run(scenario())


def test_signed_in_episode_resumes_and_syncs_on_pause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        app_module, "load_tokens", lambda: Tokens(media_user_token="fixture")
    )

    class SyncCatalog(FakeCatalog):
        using_rss = False

        def __init__(self) -> None:
            super().__init__()
            self.positions: list[tuple[str, int, bool]] = []

        def account_storefront(self) -> str:
            return "us"

        def resume_position(self, episode_id: str, duration_ms: int) -> float:
            assert episode_id == "22"
            return 42.0

        def update_position(
            self,
            episode_id: str,
            position_ms: int,
            duration_ms: int,
            *,
            force: bool = False,
            completed: bool = False,
        ) -> bool:
            self.positions.append((episode_id, position_ms, force))
            return True

    async def scenario() -> None:
        catalog = SyncCatalog()
        app = ApplepodbarApp(catalog=catalog)  # type: ignore[arg-type]
        async with app.run_test() as pilot:
            player = FakePlayer()
            app.player = player  # type: ignore[assignment]
            app.enqueue(
                [
                    QueueEpisode(
                        episode_id="22",
                        title="Today",
                        show_title="The Daily",
                        stream_url="https://example.invalid/audio.mp3",
                        source=Source.AMP,
                    )
                ]
            )
            app.play_next()
            await pilot.pause()
            await pilot.pause()
            assert player.starts == [42]
            app.action_pause_resume()
            await pilot.pause()
            await pilot.pause()
            assert ("22", 30_000, True) in catalog.positions

    asyncio.run(scenario())
