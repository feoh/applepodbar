"""Keyboard-driven Textual UI for catalog browsing, playback, and Library."""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from io import BytesIO
from typing import ClassVar
from urllib.request import urlopen

from cyclopts import App as CliApp
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    ProgressBar,
    Static,
)
from textual_image.widget import AutoImage

from applepodbar.amp_client import AmpClientError, AuthRequired
from applepodbar.catalog import Catalog
from applepodbar.local_library import LocalLibrary
from applepodbar.models import (
    BrowserItem,
    Episode,
    ItemKind,
    QueueEpisode,
    Show,
    Source,
    format_duration,
)
from applepodbar.playback import (
    MpvNotFoundError,
    MpvPlayer,
    PlaybackProgress,
    PlaybackQueue,
)
from applepodbar.settings import ApplepodbarConfig, Tokens, load_config, load_tokens


class View(Enum):
    """Top-level views."""

    HOME = "Home"
    SEARCH = "Search"
    LIBRARY = "Library"
    UP_NEXT = "Up Next"
    SHOW = "Show"


#: Views that need a signed-in user, per docs/SPEC.md §3.4.
SIGNED_IN_VIEWS = frozenset({View.LIBRARY, View.UP_NEXT})

SIGN_IN_PROMPT = "Sign in with `applepodbar login` to use this view."

#: A browser location: (load_content kind, value, view). Top-level views use
#: VIEW_LOCATION as the kind and the View name as the value.
type Location = tuple[str, str, View]
VIEW_LOCATION = "view"
MAX_HISTORY = 50


@dataclass(frozen=True)
class ViewState:
    """What the browser pane should currently show."""

    view: View
    items: tuple[BrowserItem, ...] = ()
    message: str = ""


class BrowserRow(ListItem):
    """List row carrying a BrowserItem payload."""

    def __init__(self, item: BrowserItem) -> None:
        super().__init__(Label(item.label))
        self.item = item


def placeholder_items(view: View) -> tuple[BrowserItem, ...]:
    """Return the placeholder rows for a view.

    Replaced per-view as the catalog backends land.
    """

    if view is View.HOME:
        return (
            BrowserItem(ItemKind.CATEGORY, "genres", "Browse genres"),
            BrowserItem(ItemKind.CATEGORY, "editorial", "Editorial collections"),
        )
    return ()


def resolve_view(view: View, tokens: Tokens) -> ViewState:
    """Build the state for a view, accounting for whether the user is signed in.

    Keeping this a pure function means the signed-out behaviour can be tested
    without constructing an app or touching the network.
    """

    if view in SIGNED_IN_VIEWS and not tokens.is_signed_in:
        return ViewState(view=view, message=SIGN_IN_PROMPT)

    items = placeholder_items(view)
    message = "" if items else f"{view.value} is not implemented yet."
    return ViewState(view=view, items=items, message=message)


class ApplepodbarApp(App[None]):
    """Keyboard-driven Apple Podcasts terminal player."""

    TITLE = "applepodbar"

    CSS = """
    #body {
        height: 1fr;
    }

    #browser {
        width: 2fr;
        border: round $accent;
    }

    #side {
        width: 1fr;
        border: round $panel;
    }

    #cover-art {
        /* width: auto keeps the artwork's aspect ratio within the height. */
        width: auto;
        height: 12;
        display: none;
    }

    #queue-title {
        padding: 0 1;
        color: $text-muted;
    }

    #description {
        height: auto;
        max-height: 5;
        padding: 0 1;
    }

    #progress {
        height: 1;
        padding: 0 1;
    }

    #message {
        padding: 1 2;
        color: $text-muted;
    }

    #now-playing {
        height: auto;
        padding: 0 1;
        border-top: solid $panel;
    }

    #status {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }

    #search {
        display: none;
    }
    """

    BINDINGS: ClassVar = [
        Binding("q", "quit", "Quit"),
        Binding("slash", "focus_search", "Search"),
        Binding("escape", "go_back", "Back", show=False),
        Binding("h", "show_view('HOME')", "Home"),
        Binding("l", "show_view('LIBRARY')", "Library"),
        Binding("u", "show_view('UP_NEXT')", "Up Next"),
        Binding("f", "focus_now_playing", "Now playing"),
        Binding("space", "pause_resume", "Play/pause"),
        Binding("s", "stop", "Stop"),
        Binding("right", "seek_forward", "Seek +"),
        Binding("left", "seek_back", "Seek -"),
        Binding("plus", "speed_up", "Faster"),
        Binding("minus", "speed_down", "Slower"),
        Binding("e", "enqueue", "Queue"),
        Binding("n", "next_episode", "Next"),
        Binding("b", "go_back", "Back"),
        Binding("a", "toggle_follow", "Follow"),
        Binding("v", "toggle_save", "Save"),
        Binding("m", "mark_played", "Played"),
    ]

    def __init__(self, catalog: Catalog | None = None) -> None:
        super().__init__()
        self.config: ApplepodbarConfig = ApplepodbarConfig()
        self.tokens: Tokens = Tokens()
        self.catalog = catalog
        self.queue = PlaybackQueue()
        self.player: MpvPlayer | None = None
        self.state = ViewState(view=View.HOME)
        self.episodes: dict[str, Episode] = {}
        self.current_show: Show | None = None
        self._revision = 0
        self.local_library = LocalLibrary()
        self._last_local_save = time.monotonic()
        self._last_progress = PlaybackProgress()
        self._playing_id: str | None = None
        self._location: Location | None = None
        self._history: list[Location] = []

    def compose(self) -> ComposeResult:
        """Build the widget tree."""

        yield Header()
        yield Input(placeholder="Search Apple Podcasts…", id="search")
        with Horizontal(id="body"):
            with Vertical(id="browser"):
                yield ListView(id="browser-list")
                yield Static("", id="message")
            with Vertical(id="side"):
                yield AutoImage(id="cover-art")
                yield Static("", id="description")
                yield Static("Queue — Enter jumps to an episode", id="queue-title")
                yield ListView(id="queue-list")
        yield Static("Nothing playing", id="now-playing")
        yield ProgressBar(
            total=100, show_eta=False, show_percentage=False, id="progress"
        )
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        """Load config and tokens, then render the initial view."""

        self.config = load_config()
        self.tokens = load_tokens()
        if self.catalog is None:
            self.catalog = Catalog(
                storefront=self.config.storefront, backend=self.config.catalog_backend
            )
        self.show_view(View.HOME)
        if self.tokens.is_signed_in and not getattr(self.catalog, "using_rss", False):
            self.load_account_storefront()
        self.set_interval(1, self.refresh_progress)
        if self.tokens.is_signed_in:
            self.set_status("Signed in — press l for Library, u for Up Next.")
        else:
            self.set_status(
                "Not signed in — catalog browsing only. Quit and run `applepodbar login`."
            )

    # -- view management ---------------------------------------------------

    def remember(self, location: Location) -> None:
        """Record a navigation so ``b`` can return to where the user was."""

        if location == self._location:
            return
        if self._location is not None:
            self._history = [*self._history, self._location][-MAX_HISTORY:]
        self._location = location

    def open_location(
        self, location: Location, message: str, *, remember: bool = True
    ) -> None:
        """Load content into the browser, e.g. a show's episodes."""

        if remember:
            self.remember(location)
        kind, value, view = location
        self._revision += 1
        self.state = ViewState(view, message=message)
        self.render_state()
        self.load_content(kind, value, self._revision)

    def action_go_back(self) -> None:
        """Close search, or return to the previous browser location."""

        search = self.query_one("#search", Input)
        if search.has_focus and self._location is not None:
            # Abandoning a search returns to whatever was shown before `/`.
            location = self._location
        elif self._history:
            location = self._history.pop()
            self._location = location
        else:
            self.show_view(View.HOME)
            return
        kind, _, view = location
        if kind == VIEW_LOCATION:
            self.show_view(view, remember=False)
        else:
            self.open_location(location, "Loading…", remember=False)

    def show_view(self, view: View, *, remember: bool = True) -> None:
        """Switch the browser pane to a view."""

        if remember and view is not View.SEARCH:
            self.remember((VIEW_LOCATION, view.name, view))
        self._revision += 1
        search = self.query_one("#search", Input)
        search.display = view is View.SEARCH
        if not search.display and (self.focused is None or self.focused is search):
            # A hidden Input that keeps focus swallows every letter binding,
            # which made Library and Up Next unreachable from the keyboard.
            self.query_one("#browser-list", ListView).focus()
        if view is View.LIBRARY and getattr(self.catalog, "using_rss", False):
            self.state = ViewState(view, message="Loading local follows…")
            self.render_state()
            self.load_content("local_library", "", self._revision)
            return
        self.state = resolve_view(view, self.tokens)
        self.render_state()
        if view is View.HOME and self.catalog is not None:
            self.load_content("home", "", self._revision)
        elif view is View.LIBRARY and self.tokens.is_signed_in:
            self.load_content("library", "", self._revision)
        elif view is View.UP_NEXT and self.tokens.is_signed_in:
            self.load_content("up_next", "", self._revision)
        elif view is View.SEARCH and self.catalog is not None:
            self.query_one("#search", Input).display = True
            self.query_one("#search", Input).focus()

    def render_state(self) -> None:
        """Render the current view state into the browser pane."""

        browser = self.query_one("#browser-list", ListView)
        browser.clear()
        for item in self.state.items:
            browser.append(BrowserRow(item))
        self.query_one("#message", Static).update(self.state.message)
        self.sub_title = self.state.view.value

    def render_queue(self) -> None:
        """Render the queue pane."""

        queue_list = self.query_one("#queue-list", ListView)
        queue_list.clear()
        for label in self.queue.labels():
            queue_list.append(ListItem(Label(label)))

    def set_status(self, message: str) -> None:
        """Write a line to the status area."""

        self.query_one("#status", Static).update(message)

    def update_now_playing(self, progress: PlaybackProgress | None = None) -> None:
        """Refresh the now-playing line."""

        current = self.queue.current
        if current is None:
            self.query_one("#now-playing", Static).update("Nothing playing")
            return

        line = current.label
        if progress is not None and progress.duration_seconds > 0:
            elapsed = format_duration(int(progress.position_seconds * 1000))
            total = format_duration(int(progress.duration_seconds * 1000))
            line = f"{line}  [{elapsed} / {total}]"
        self.query_one("#now-playing", Static).update(line)

    # -- playback ----------------------------------------------------------

    def ensure_player(self) -> MpvPlayer | None:
        """Return the mpv player, creating it on first use.

        mpv is only required once something is actually played, so a user
        without mpv can still browse.
        """

        if self.player is not None:
            return self.player
        try:
            self.player = MpvPlayer()
        except MpvNotFoundError as exc:
            self.set_status(str(exc))
            return None
        self.player.set_speed(self.config.playback_speed)
        return self.player

    def play_next(self) -> None:
        """Play the next queued episode."""

        if self.player is not None and self._playing_id is not None:
            self.player.stop()
        self._playing_id = None
        episode = self.queue.next()
        if episode is None:
            self.set_status("Queue is empty.")
            self.update_now_playing()
            return

        self.render_queue()
        if episode.source is Source.AMP and self.tokens.is_signed_in and self.catalog:
            self.set_status("Checking playback position…")
            self.resolve_resume(episode)
        else:
            start = (
                self.local_library.position_ms(episode.episode_id) / 1000
                if episode.source is Source.RSS
                else 0
            )
            self.start_playback(episode, start)

    @work(thread=True, exclusive=True, group="resume")
    def resolve_resume(self, episode: QueueEpisode) -> None:
        """Fetch Apple's remote position without blocking Textual's event loop."""

        start = 0.0
        try:
            if self.catalog is not None:
                start = self.catalog.resume_position(
                    episode.episode_id, episode.duration_ms
                )
        except AmpClientError:
            pass  # Offline playback still works; a later sync can retry.
        self.call_from_thread(self.start_playback, episode, start)

    def start_playback(self, episode: QueueEpisode, start_seconds: float = 0) -> None:
        """Begin playback only if this is still the selected queue item."""

        if self.queue.current != episode:
            return
        player = self.ensure_player()
        if player is None:
            return
        player.play(episode, start_seconds=start_seconds)
        self._playing_id = episode.episode_id
        self._last_progress = PlaybackProgress()
        self.update_now_playing()
        self.render_queue()
        self.show_cover_art(episode.artwork_url)
        self.set_status(
            f"Playing {episode.title}. Enter plays another episode, e queues it, n skips."
        )

    def enqueue(self, episodes: list[QueueEpisode]) -> None:
        """Append episodes to the queue."""

        self.queue.append(episodes)
        self.render_queue()

    # -- actions -----------------------------------------------------------

    def action_show_view(self, view_name: str) -> None:
        """Switch views by name, as used from a binding."""

        self.show_view(View[view_name])

    def action_focus_search(self) -> None:
        """Reveal and focus the search input."""

        search = self.query_one("#search", Input)
        search.display = True
        search.focus()
        self._revision += 1
        self.state = ViewState(view=View.SEARCH, message="Type a search term.")
        self.render_state()

    def action_focus_now_playing(self) -> None:
        """Move focus to the now-playing area."""

        self.query_one("#now-playing", Static).focus()

    def action_pause_resume(self) -> None:
        """Toggle pause, or start the queue if nothing is loaded."""

        if self.player is None:
            self.play_next()
            return
        self.player.pause_resume()
        if self.player.is_paused:
            self.record_position(force=True)
        self.set_status("Paused." if self.player.is_paused else "Playing.")

    def action_stop(self) -> None:
        """Stop playback after flushing the current position."""

        if self.player is not None:
            self.record_position(force=True, blocking=True)
            self.player.stop()
        self._playing_id = None
        self.queue.clear_current()
        self.update_now_playing()
        self.render_queue()
        self.set_status("Stopped.")

    def action_seek_forward(self) -> None:
        """Seek forward by the configured increment."""

        if self.player is not None:
            self.player.seek(self.config.seek_forward_seconds)

    def action_seek_back(self) -> None:
        """Seek back by the configured increment."""

        if self.player is not None:
            self.player.seek(-self.config.seek_back_seconds)

    @work(thread=True, exclusive=True, group="content")
    def load_content(self, kind: str, value: str, revision: int) -> None:
        """Fetch content off the Textual event loop and discard stale results."""

        catalog = self.catalog
        if catalog is None:
            return
        try:
            if kind == "home":
                shows = catalog.charts()
                items = (
                    []
                    if getattr(catalog, "using_rss", False)
                    else [BrowserItem(ItemKind.CATEGORY, "genres", "Browse genres")]
                )
                items += (
                    []
                    if getattr(catalog, "using_rss", False)
                    else [
                        BrowserItem(
                            ItemKind.CATEGORY, "editorial", "Editorial collections"
                        )
                    ]
                )
                items += [
                    BrowserItem(ItemKind.SHOW, show.id, show.title, show.author)
                    for show in shows
                ]
                episodes: list[Episode] = []
                show = None
                view = View.HOME
            elif kind == "library":
                shows = catalog.library_shows()
                items = [
                    BrowserItem(ItemKind.CATEGORY, "library:saved", "Saved episodes"),
                    BrowserItem(ItemKind.CATEGORY, "library:latest", "Latest episodes"),
                    BrowserItem(
                        ItemKind.CATEGORY, "library:updated", "Recently updated"
                    ),
                    BrowserItem(ItemKind.CATEGORY, "library:recent", "Recently played"),
                    BrowserItem(
                        ItemKind.CATEGORY, "library:channels", "Podcast channels"
                    ),
                    BrowserItem(ItemKind.CATEGORY, "library:stations", "Stations"),
                ]
                items += [
                    BrowserItem(ItemKind.SHOW, s.id, s.title, s.author) for s in shows
                ]
                episodes = []
                show = None
                view = View.LIBRARY
            elif kind == "library_section":
                if value in {"channels", "stations"}:
                    records = (
                        catalog.library_channels()
                        if value == "channels"
                        else catalog.library_stations()
                    )
                    items = [
                        BrowserItem(
                            ItemKind.CATEGORY,
                            f"info:{row['id']}",
                            row.get("attributes", {}).get("name", "Untitled"),
                        )
                        for row in records
                    ]
                    episodes = []
                else:
                    episodes = catalog.library_section(value)
                    items = [
                        BrowserItem(ItemKind.EPISODE, e.id, e.title, e.show_title)
                        for e in episodes
                    ]
                show = None
                view = View.LIBRARY
            elif kind == "up_next":
                episodes = catalog.up_next()
                items = [
                    BrowserItem(ItemKind.EPISODE, e.id, e.title, e.show_title)
                    for e in episodes
                ]
                show = None
                view = View.UP_NEXT
            elif kind == "local_library":
                shows = [
                    catalog.show(show_id)[0]
                    for show_id in self.local_library.followed_shows()
                ]
                items = [
                    BrowserItem(ItemKind.SHOW, show.id, show.title, show.author)
                    for show in shows
                ]
                episodes = []
                show = None
                view = View.LIBRARY
            elif kind == "genres":
                items = catalog.genres()
                episodes = []
                show = None
                view = View.HOME
            elif kind == "editorial":
                items = catalog.editorial()
                episodes = []
                show = None
                view = View.HOME
            elif kind == "genre":
                shows = catalog.genre_shows(value)
                items = [
                    BrowserItem(ItemKind.SHOW, item.id, item.title, item.author)
                    for item in shows
                ]
                episodes = []
                show = None
                view = View.HOME
            elif kind == "grouping":
                shows = catalog.grouping_shows(value)
                items = [
                    BrowserItem(ItemKind.SHOW, item.id, item.title, item.author)
                    for item in shows
                ]
                episodes = []
                show = None
                view = View.HOME
            elif kind == "search":
                shows, episodes = catalog.search(value)
                items = [
                    BrowserItem(ItemKind.SHOW, s.id, s.title, s.author) for s in shows
                ]
                items += [
                    BrowserItem(ItemKind.EPISODE, e.id, e.title, e.show_title)
                    for e in episodes
                ]
                show = None
                view = View.SEARCH
            else:
                show, episodes = catalog.show(value)
                items = [
                    BrowserItem(
                        ItemKind.EPISODE,
                        e.id,
                        f"🔒 {e.title}" if e.is_paid else e.title,
                        format_duration(e.duration_ms),
                    )
                    for e in episodes
                ]
                view = View.SHOW
            self.call_from_thread(
                self.apply_content, view, items, episodes, show, revision
            )
        except AuthRequired:
            self.call_from_thread(self.handle_auth_required, revision)
        except Exception as exc:  # noqa: BLE001 - surface API outages to the user
            self.call_from_thread(self.show_content_error, str(exc), revision)

    def apply_content(
        self,
        view: View,
        items: list[BrowserItem],
        episodes: list[Episode],
        show: Show | None,
        revision: int,
    ) -> None:
        """Apply only the newest worker response to the browser."""

        if revision != self._revision:
            return
        self.current_show = show
        self.episodes = {episode.id: episode for episode in episodes}
        self.state = ViewState(view, tuple(items), "" if items else "Nothing found.")
        self.render_state()
        self.query_one("#description", Static).update(show.description if show else "")

    def show_content_error(self, message: str, revision: int) -> None:
        if revision == self._revision:
            self.set_status(f"Catalog unavailable: {message}")
            self.query_one("#message", Static).update("Could not load this view.")

    def handle_auth_required(self, revision: int) -> None:
        if revision == self._revision:
            self.tokens = load_tokens()
            self.state = resolve_view(self.state.view, Tokens())
            self.render_state()
            self.set_status(
                "Apple Podcasts session expired; run `applepodbar login` again."
            )

    @work(thread=True)
    def load_account_storefront(self) -> None:
        catalog = self.catalog
        if catalog is None:
            return
        try:
            storefront = catalog.account_storefront()
        except Exception:  # noqa: BLE001 - locale refresh is best effort
            return
        self.call_from_thread(self.apply_account_storefront, storefront)

    def apply_account_storefront(self, storefront: str) -> None:
        if storefront != self.config.storefront:
            self.config = ApplepodbarConfig(
                storefront=storefront,
                catalog_backend=self.config.catalog_backend,
                seek_forward_seconds=self.config.seek_forward_seconds,
                seek_back_seconds=self.config.seek_back_seconds,
                playback_speed=self.config.playback_speed,
            )
            if self.state.view is View.HOME:
                self.show_view(View.HOME)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search":
            term = event.value.strip()
            if term:
                self.query_one("#search", Input).display = False
                self.query_one("#browser-list", ListView).focus()
                self.open_location(
                    ("search", term, View.SEARCH), f"Searching for {term}…"
                )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id == "queue-list":
            self.play_queue_index(event.list_view.index)
            return
        if event.list_view.id != "browser-list" or not isinstance(
            event.item, BrowserRow
        ):
            return
        item = event.item.item
        if item.kind is ItemKind.CATEGORY:
            if item.id.startswith("info:"):
                self.set_status(
                    "Channel and station details are not available in this view."
                )
                return
            if item.id.startswith("library:"):
                self.open_location(
                    (
                        "library_section",
                        item.id.removeprefix("library:"),
                        View.LIBRARY,
                    ),
                    "Loading Library section…",
                )
                return
            if item.id in {"genres", "editorial"}:
                kind = item.id
            elif item.id.startswith("genre:"):
                kind = "genre"
            else:
                kind = "grouping"
            self.open_location(
                (kind, item.id.removeprefix("genre:"), View.HOME), "Loading…"
            )
        elif item.kind is ItemKind.SHOW:
            self.open_location(("show", item.id, View.SHOW), "Loading episodes…")
        else:
            self.play_episode_now(item.id)

    def playable_episode(self, episode_id: str) -> Episode | None:
        """Return a listed episode if mpv can play it, explaining if not."""

        episode = self.episodes.get(episode_id)
        if episode is None:
            return None
        if not episode.is_playable:
            self.set_status(
                "This episode is not playable in mpv. Paid Apple audio uses FairPlay; listen in Apple Podcasts."
            )
            return None
        return episode

    def play_episode_now(self, episode_id: str) -> None:
        """Play an episode immediately, saving the position of the current one."""

        episode = self.playable_episode(episode_id)
        if episode is None:
            return
        self.record_position(force=True, blocking=True)
        self.queue.insert_next([QueueEpisode.from_episode(episode)])
        self.query_one("#description", Static).update(episode.description)
        self.play_next()

    def action_enqueue(self) -> None:
        """Add the highlighted episode to the end of the queue."""

        row = self.query_one("#browser-list", ListView).highlighted_child
        if not isinstance(row, BrowserRow) or row.item.kind is not ItemKind.EPISODE:
            self.set_status("Highlight an episode to add it to the queue.")
            return
        episode = self.playable_episode(row.item.id)
        if episode is None:
            return
        self.enqueue([QueueEpisode.from_episode(episode)])
        self.set_status(f"Queued {episode.title}. Press n to skip to it.")

    def play_queue_index(self, index: int | None) -> None:
        """Jump to a queue row selected in the queue pane."""

        if index is None or index == self.queue.current_index:
            return
        self.record_position(force=True, blocking=True)
        if self.queue.jump_to(index) is not None:
            self.play_next()

    def action_toggle_follow(self) -> None:
        """Follow/unfollow the selected show locally or in Apple Library."""

        row = self.query_one("#browser-list", ListView).highlighted_child
        show_id = (
            row.item.id
            if isinstance(row, BrowserRow) and row.item.kind is ItemKind.SHOW
            else (self.current_show.id if self.current_show else None)
        )
        if not show_id:
            self.set_status("Select a show to follow.")
            return
        if not getattr(self.catalog, "using_rss", False):
            if not self.tokens.is_signed_in:
                self.set_status("Sign in with `applepodbar login` to follow shows.")
            else:
                self.mutate_library("follow", show_id)
            return
        if show_id in self.local_library.followed_shows():
            self.local_library.unfollow(show_id)
            self.set_status("Removed from local follows.")
        else:
            self.local_library.follow(show_id)
            self.set_status("Added to local follows.")

    def selected_episode(self) -> Episode | None:
        """Resolve the highlighted episode or currently playing AMP episode."""

        row = self.query_one("#browser-list", ListView).highlighted_child
        if isinstance(row, BrowserRow) and row.item.kind is ItemKind.EPISODE:
            return self.episodes.get(row.item.id)
        current = self.queue.current
        if current is not None:
            return Episode(
                id=current.episode_id,
                title=current.title,
                duration_ms=current.duration_ms,
                source=current.source,
            )
        return None

    def action_toggle_save(self) -> None:
        episode = self.selected_episode()
        if episode is None:
            self.set_status("Select an episode to save.")
        elif episode.source is not Source.AMP or not self.tokens.is_signed_in:
            self.set_status("Sign in with `applepodbar login` to save episodes.")
        else:
            self.mutate_library("save", episode.id)

    def action_mark_played(self) -> None:
        """Use the web player's completed-position mutation (AMP only)."""

        episode = self.selected_episode()
        if episode is None:
            self.set_status("Select an episode to mark played.")
        elif episode.source is not Source.AMP or not self.tokens.is_signed_in:
            self.set_status("Sign in to mark Apple episodes played.")
        else:
            current = self.queue.current
            if current is not None and current.episode_id == episode.id and self.player:
                self.player.stop()
                self._playing_id = None
                self.queue.clear_current()
                self.update_now_playing()
            self.mutate_library("played", episode.id, episode)

    @work(thread=True, exclusive=True, group="mutation")
    def mutate_library(
        self, action: str, resource_id: str, episode: Episode | None = None
    ) -> None:
        """Apply a signed-in Library mutation without blocking the TUI."""

        catalog = self.catalog
        if catalog is None:
            return
        try:
            if action == "follow":
                state = catalog.toggle_follow(resource_id)
                message = "Following show." if state else "Unfollowed show."
            elif action == "save":
                state = catalog.toggle_saved(resource_id)
                message = "Episode saved." if state else "Episode removed from Library."
            elif action == "played" and episode is not None:
                catalog.mark_played(episode)
                message = "Episode marked played."
            else:
                return
            self.call_from_thread(self.set_status, message)
            if action == "follow" and self.state.view is View.LIBRARY:
                self.call_from_thread(self.show_view, View.LIBRARY)
        except AuthRequired:
            self.call_from_thread(
                self.set_status, "Sign-in expired; run `applepodbar login`."
            )
        except AmpClientError:
            self.call_from_thread(
                self.set_status, "Apple Library is unavailable; try again later."
            )

    def record_position(
        self,
        *,
        force: bool = False,
        completed: bool = False,
        progress: PlaybackProgress | None = None,
        blocking: bool = False,
    ) -> None:
        """Persist RSS locally or send a coalesced signed-in AMP update."""

        current = self.queue.current
        if (
            current is None
            or self.player is None
            or current.episode_id != self._playing_id
        ):
            return
        if progress is None:
            try:
                progress = self.player.playback_progress()
            except (OSError, RuntimeError):
                progress = self._last_progress
        if progress.duration_seconds <= 0 and not completed:
            # mpv has not opened the stream yet, so 0 is not a real position.
            # Writing it would clobber the resume point we just started from.
            return
        if current.source is Source.RSS:
            if force or time.monotonic() - self._last_local_save >= 30:
                self.local_library.set_position(
                    current.episode_id, progress.position_ms
                )
                self._last_local_save = time.monotonic()
        elif self.tokens.is_signed_in and self.catalog is not None:
            position_ms = (
                max(progress.position_ms, current.duration_ms)
                if completed
                else progress.position_ms
            )
            duration_ms = current.duration_ms or int(progress.duration_seconds * 1000)
            if blocking:
                try:
                    self.catalog.update_position(
                        current.episode_id,
                        position_ms,
                        duration_ms,
                        force=force,
                        completed=completed,
                    )
                except AmpClientError:
                    pass  # Exit/stop must remain possible while offline.
            else:
                self.sync_position(
                    current.episode_id, position_ms, duration_ms, force, completed
                )

    @work(thread=True, exclusive=True, group="positions")
    def sync_position(
        self,
        episode_id: str,
        position_ms: int,
        duration_ms: int,
        force: bool,
        completed: bool,
    ) -> None:
        try:
            if self.catalog is not None:
                self.catalog.update_position(
                    episode_id,
                    position_ms,
                    duration_ms,
                    force=force,
                    completed=completed,
                )
        except AmpClientError:
            pass  # Connection failures must not interrupt playback.

    def action_next_episode(self) -> None:
        self.record_position(force=True, blocking=True)
        self.play_next()

    def action_speed_up(self) -> None:
        self.change_speed(0.25)

    def action_speed_down(self) -> None:
        self.change_speed(-0.25)

    def change_speed(self, increment: float) -> None:
        player = self.player
        if player is not None:
            self.set_status(
                f"Playback speed: {player.set_speed(player.speed + increment):.2f}×"
            )

    @work(thread=True, exclusive=True, group="progress")
    def refresh_progress(self) -> None:
        player = self.player
        if (
            player is None
            or self.queue.current is None
            or self._playing_id != self.queue.current.episode_id
        ):
            return
        try:
            if player.reap_finished():
                self.call_from_thread(self.on_episode_finished)
                return
            progress = player.playback_progress()
            self.call_from_thread(self.display_progress, progress)
        except (OSError, RuntimeError):
            return

    def on_episode_finished(self) -> None:
        self.record_position(
            force=True, completed=True, progress=self._last_progress, blocking=True
        )
        self.play_next()

    def display_progress(self, progress: PlaybackProgress) -> None:
        self._last_progress = progress
        self.record_position(progress=progress)
        self.update_now_playing(progress)
        self.query_one("#progress", ProgressBar).update(
            progress=progress.progress_ratio() * 100
        )

    def show_cover_art(self, artwork_url: str | None) -> None:
        art = self.query_one("#cover-art", AutoImage)
        art.image = None
        art.display = False
        if artwork_url and artwork_url.startswith("https://"):
            self.load_cover_art(artwork_url)

    @work(thread=True, exclusive=True, group="artwork")
    def load_cover_art(self, artwork_url: str) -> None:
        try:
            with urlopen(artwork_url, timeout=10) as response:
                data = response.read(4_000_000)
        except (OSError, ValueError):
            return
        self.call_from_thread(self.display_cover_art, artwork_url, data)

    def display_cover_art(self, artwork_url: str, data: bytes) -> None:
        current = self.queue.current
        if current is None or current.artwork_url != artwork_url:
            return
        art = self.query_one("#cover-art", AutoImage)
        art.image = BytesIO(data)
        art.display = True

    def on_resize(self, event: object) -> None:
        """Keep the browser usable in narrow terminals."""

        self.query_one("#side", Vertical).display = self.size.width >= 80

    def on_unmount(self) -> None:
        """Shut mpv and the catalog connection pool down with the app."""

        if self.player is not None:
            self.record_position(force=True, blocking=True)
            self.player.close()
            self.player = None
        if self.catalog is not None:
            self.catalog.close()


cli = CliApp(name="applepodbar")


@cli.default
def run() -> None:
    """Start the Apple Podcasts terminal player."""

    ApplepodbarApp().run()


@cli.command(name="login")
def login_command() -> None:
    """Sign in through Chromium and save the user session in the OS keyring."""

    from applepodbar.credentials import CredentialStoreError
    from applepodbar.login import LoginError, login

    try:
        login()
    except (LoginError, CredentialStoreError) as exc:
        raise SystemExit(str(exc)) from None


def main() -> None:
    """Dispatch the player or the optional login command."""

    cli()
