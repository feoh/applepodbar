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
        self.stops = 0

    def play(self, episode: QueueEpisode, start_seconds: float = 0) -> None:
        self.played.append(episode)
        self.starts.append(start_seconds)

    def set_speed(self, speed: float) -> float:
        self.speed = speed
        return speed

    def pause_resume(self) -> None:
        self.is_paused = not self.is_paused

    def stop(self) -> None:
        self.stops += 1

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


def test_letter_bindings_work_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Startup must not leave focus in the hidden search Input."""

    monkeypatch.setattr(
        app_module, "load_tokens", lambda: Tokens(media_user_token="fixture")
    )
    monkeypatch.setattr(app_module, "load_config", app_module.ApplepodbarConfig)

    class LibraryCatalog(FakeCatalog):
        using_rss = False

        def library_shows(self) -> list[Show]:
            return [self.daily]

        def account_storefront(self) -> str:
            return "us"

    async def scenario() -> None:
        app = ApplepodbarApp(catalog=LibraryCatalog())  # type: ignore[arg-type]
        async with app.run_test() as pilot:
            await pilot.pause()
            assert "Signed in" in str(app.query_one("#status", Static).render())
            await pilot.press("l")
            await pilot.pause()
            await pilot.pause()
            assert app.state.view is View.LIBRARY
            assert "The Daily" in [item.title for item in app.state.items]
            await pilot.press("slash")
            assert app.focused is app.query_one("#search", Input)
            await pilot.press("escape")
            assert not app.query_one("#search", Input).display
            assert app.focused is app.query_one("#browser-list", ListView)

    asyncio.run(scenario())


class TwoEpisodeCatalog(FakeCatalog):
    using_rss = False

    def __init__(self) -> None:
        super().__init__()
        self.other = Show(id="2", title="Darknet Diaries", author="Jack")
        self.second = Episode(
            id="33",
            title="Tomorrow",
            show_id="1",
            show_title="The Daily",
            asset_url="https://example.invalid/tomorrow.mp3",
        )
        self.positions: list[tuple[str, int]] = []

    def charts(self) -> list[Show]:
        return [self.daily, self.other]

    def show(self, show_id: str) -> tuple[Show, list[Episode]]:
        if show_id == "2":
            return self.other, []
        return self.daily, [self.episode_record, self.second]

    def account_storefront(self) -> str:
        return "us"

    def resume_position(self, episode_id: str, duration_ms: int) -> float:
        return 0.0

    def update_position(
        self,
        episode_id: str,
        position_ms: int,
        duration_ms: int,
        *,
        force: bool = False,
        completed: bool = False,
    ) -> bool:
        self.positions.append((episode_id, position_ms))
        return True


def test_enter_switches_episodes_and_back_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enter plays now, e queues, b steps back, and the queue pane jumps."""

    monkeypatch.setattr(
        app_module, "load_tokens", lambda: Tokens(media_user_token="fixture")
    )
    monkeypatch.setattr(app_module, "load_config", app_module.ApplepodbarConfig)

    async def settle(pilot: object) -> None:
        for _ in range(3):
            await pilot.pause()  # type: ignore[attr-defined]

    async def scenario() -> None:
        catalog = TwoEpisodeCatalog()
        app = ApplepodbarApp(catalog=catalog)  # type: ignore[arg-type]
        async with app.run_test() as pilot:
            player = FakePlayer()
            app.player = player  # type: ignore[assignment]
            await settle(pilot)
            browser = app.query_one("#browser-list", ListView)
            browser.index = 2  # The Daily, after the two category rows
            await pilot.press("enter")
            await settle(pilot)
            assert app.state.view is View.SHOW

            browser.index = 0
            await pilot.press("enter")
            await settle(pilot)
            browser.index = 1
            await pilot.press("enter")
            await settle(pilot)
            # The second Enter switched immediately and saved the first position.
            assert [e.episode_id for e in player.played] == ["22", "33"]
            assert ("22", 30_000) in catalog.positions
            assert "Enter plays another" in str(
                app.query_one("#status", Static).render()
            )

            browser.index = 0
            await pilot.press("e")
            assert [e.episode_id for e in app.queue.episodes][-1] == "22"
            assert [e.episode_id for e in player.played] == ["22", "33"]

            queue_list = app.query_one("#queue-list", ListView)
            queue_list.focus()
            queue_list.index = 0
            await pilot.press("enter")
            await settle(pilot)
            assert player.played[-1].episode_id == "22"

            browser.focus()
            await pilot.press("b")
            await settle(pilot)
            assert app.state.view is View.HOME
            assert "Darknet Diaries" in [item.title for item in app.state.items]

    asyncio.run(scenario())


def test_switching_before_mpv_opens_stream_keeps_resume_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A not-yet-loaded stream reports 0/0; that must not be synced as 0."""

    monkeypatch.setattr(
        app_module, "load_tokens", lambda: Tokens(media_user_token="fixture")
    )
    monkeypatch.setattr(app_module, "load_config", app_module.ApplepodbarConfig)

    class LoadingPlayer(FakePlayer):
        def playback_progress(self) -> PlaybackProgress:
            return PlaybackProgress()

    async def scenario() -> None:
        catalog = TwoEpisodeCatalog()
        app = ApplepodbarApp(catalog=catalog)  # type: ignore[arg-type]
        async with app.run_test() as pilot:
            player = LoadingPlayer()
            app.player = player  # type: ignore[assignment]
            app.episodes = {"22": catalog.episode_record, "33": catalog.second}
            app.play_episode_now("22")
            await pilot.pause()
            await pilot.pause()
            app.play_episode_now("33")
            await pilot.pause()
            await pilot.pause()
            assert [e.episode_id for e in player.played] == ["22", "33"]
            assert catalog.positions == []

    asyncio.run(scenario())


def test_cover_art_keeps_its_aspect_ratio(monkeypatch: pytest.MonkeyPatch) -> None:
    """Square artwork must render square, not stretched to fill the pane."""

    from io import BytesIO

    from PIL import Image
    from textual_image._terminal import get_cell_size
    from textual_image.widget import AutoImage

    monkeypatch.setattr(app_module, "load_tokens", Tokens)
    monkeypatch.setattr(app_module, "load_config", app_module.ApplepodbarConfig)

    async def scenario() -> None:
        app = ApplepodbarApp(catalog=FakeCatalog())  # type: ignore[arg-type]
        async with app.run_test(size=(140, 40)) as pilot:
            data = BytesIO()
            Image.new("RGB", (600, 600)).save(data, "PNG")
            art = app.query_one("#cover-art", AutoImage)
            art.image = BytesIO(data.getvalue())
            art.display = True
            await pilot.pause()
            cell = get_cell_size()
            pixel_width = art.size.width * cell.width
            pixel_height = art.size.height * cell.height
            assert abs(pixel_width / pixel_height - 1) < 0.1

    asyncio.run(scenario())


def test_f_toggles_full_screen_now_playing(monkeypatch: pytest.MonkeyPatch) -> None:
    """f hides the browser and queue around artwork; f, b, or navigation exits."""

    from io import BytesIO

    from PIL import Image
    from textual_image._terminal import get_cell_size
    from textual_image.widget import AutoImage

    monkeypatch.setattr(app_module, "load_tokens", Tokens)
    monkeypatch.setattr(app_module, "load_config", app_module.ApplepodbarConfig)

    async def scenario() -> None:
        catalog = FakeCatalog()
        app = ApplepodbarApp(catalog=catalog)  # type: ignore[arg-type]
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            browser = app.query_one("#browser-list", ListView)
            side = app.query_one("#side")
            browser_pane = app.query_one("#browser")
            queue_list = app.query_one("#queue-list", ListView)
            browser.focus()
            await pilot.press("f")
            assert "Nothing playing" in str(app.query_one("#status", Static).render())
            assert browser_pane.display

            app.player = FakePlayer()  # type: ignore[assignment]
            app.episodes = {"22": catalog.episode_record}
            app.play_episode_now("22")
            await pilot.pause()
            # Browsing elsewhere replaces the description with the show's.
            app.query_one("#description", Static).update("News every day")
            art = app.query_one("#cover-art", AutoImage)
            data = BytesIO()
            Image.new("RGB", (600, 600)).save(data, "PNG")
            art.image = BytesIO(data.getvalue())
            art.display = True
            await pilot.pause()
            small_height = art.size.height

            await pilot.press("f")
            await pilot.pause()
            assert not browser_pane.display
            assert not queue_list.display
            assert side.size.width > 100
            assert app.focused is None
            assert "A story" in str(app.query_one("#description", Static).render())
            assert art.size.height > small_height
            cell = get_cell_size()
            ratio = (art.size.width * cell.width) / (art.size.height * cell.height)
            assert abs(ratio - 1) < 0.1

            await pilot.press("f")
            await pilot.pause()
            assert browser_pane.display
            assert queue_list.display
            assert app.focused is browser

            await pilot.press("f")
            await pilot.press("b")
            await pilot.pause()
            assert browser_pane.display
            assert app.state.view is View.HOME

            await pilot.press("f")
            await pilot.press("s")
            await pilot.pause()
            assert browser_pane.display

    asyncio.run(scenario())
