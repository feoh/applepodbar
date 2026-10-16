"""Textual application for applepodbar.

This is the application shell described in docs/SPEC.md §6. View switching,
keybindings, the queue, and the now-playing area are real; the content in each
view is placeholder until the catalog clients land (see docs/SPEC.md §11).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Input, Label, ListItem, ListView, Static

from applepodbar.models import BrowserItem, ItemKind, QueueEpisode, format_duration
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
            BrowserItem(ItemKind.CATEGORY, "top-shows", "Top Shows"),
            BrowserItem(ItemKind.CATEGORY, "top-episodes", "Top Episodes"),
            BrowserItem(ItemKind.CATEGORY, "browse", "Browse by Category"),
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
        Binding("h", "show_view('HOME')", "Home"),
        Binding("l", "show_view('LIBRARY')", "Library"),
        Binding("u", "show_view('UP_NEXT')", "Up Next"),
        Binding("f", "focus_now_playing", "Now playing"),
        Binding("space", "pause_resume", "Play/pause"),
        Binding("s", "stop", "Stop"),
        Binding("right", "seek_forward", "Seek +"),
        Binding("left", "seek_back", "Seek -"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.config: ApplepodbarConfig = ApplepodbarConfig()
        self.tokens: Tokens = Tokens()
        self.queue = PlaybackQueue()
        self.player: MpvPlayer | None = None
        self.state = ViewState(view=View.HOME)

    def compose(self) -> ComposeResult:
        """Build the widget tree."""

        yield Header()
        yield Input(placeholder="Search Apple Podcasts…", id="search")
        with Horizontal(id="body"):
            with Vertical(id="browser"):
                yield ListView(id="browser-list")
                yield Static("", id="message")
            with Vertical(id="side"):
                yield ListView(id="queue-list")
        yield Static("Nothing playing", id="now-playing")
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        """Load config and tokens, then render the initial view."""

        self.config = load_config()
        self.tokens = load_tokens()
        self.show_view(View.HOME)
        if not self.tokens.is_signed_in:
            self.set_status("Not signed in — catalog browsing only.")

    # -- view management ---------------------------------------------------

    def show_view(self, view: View) -> None:
        """Switch the browser pane to a view."""

        self.state = resolve_view(view, self.tokens)
        self.render_state()

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

        episode = self.queue.next()
        if episode is None:
            self.set_status("Queue is empty.")
            self.update_now_playing()
            return

        player = self.ensure_player()
        if player is None:
            return
        player.play(episode)
        self.update_now_playing()
        self.render_queue()

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
        self.set_status("Paused." if self.player.is_paused else "Playing.")

    def action_stop(self) -> None:
        """Stop playback."""

        if self.player is not None:
            self.player.stop()
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

    def on_unmount(self) -> None:
        """Shut mpv down with the app."""

        if self.player is not None:
            self.player.close()
            self.player = None


def main() -> None:
    """Run applepodbar."""

    ApplepodbarApp().run()
