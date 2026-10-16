"""Zero-auth directory/feed fixtures and local resume state."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import httpx
import pytest

from applepodbar.catalog import Catalog
from applepodbar.local_library import LocalLibrary
from applepodbar.models import Source
from applepodbar.rss_client import RssClient
from applepodbar.tokens import DeveloperTokenError

SHOW = {
    "collectionId": 1200361736,
    "collectionName": "The Daily",
    "artistName": "NYT",
    "feedUrl": "https://feed.example.invalid/rss",
}
FEED = b"""<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel><title>The Daily</title><item><title>Episode 1</title><guid>ep1</guid><itunes:duration>1:02</itunes:duration><enclosure url="https://cdn.example.invalid/ep1.mp3" type="audio/mpeg" length="123" /></item><item><title>Text only</title><guid>text</guid></item></channel></rss>"""


def test_itunes_search_lookup_rss_audio_and_ids() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/search" or request.url.path == "/lookup":
            return httpx.Response(200, json={"results": [SHOW]})
        return httpx.Response(200, content=FEED)

    rss = RssClient(httpx.Client(transport=httpx.MockTransport(respond)))
    assert [(s.id, s.source) for s in rss.search("daily")] == [
        ("1200361736", Source.RSS)
    ]
    show, episodes = rss.show("1200361736")
    assert show.id == "1200361736"
    assert len(episodes) == 1
    assert episodes[0].asset_url == "https://cdn.example.invalid/ep1.mp3"
    assert episodes[0].duration_ms == 62_000
    assert not episodes[0].supports_position_sync
    assert [e.id for e in rss.show("1200361736")[1]] == [e.id for e in episodes]
    assert requests[0].url.params["entity"] == "podcast"
    assert requests[1].url.params["id"] == "1200361736"


def test_local_follow_and_resume_file_is_private(tmp_path: Path) -> None:
    path = tmp_path / "local_library.json"
    library = LocalLibrary(path)
    library.follow("12")
    library.follow("12")
    library.follow("34")
    library.set_position("ep1", 42_000)
    assert LocalLibrary(path).followed_shows() == ["12", "34"]
    assert LocalLibrary(path).position_ms("ep1") == 42_000
    assert json.loads(path.read_text())["positions"]["ep1"] == 42_000
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    library.unfollow("12")
    assert library.followed_shows() == ["34"]


def test_explicit_rss_and_automatic_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenAmp:
        def charts(self) -> None:
            raise DeveloperTokenError("bundle changed")

        def close(self) -> None:
            pass

    class FakeRss:
        def __init__(self, *, storefront: str = "us") -> None:
            pass

        def search(self, term: str) -> list[object]:
            return [term]

        def close(self) -> None:
            pass

    monkeypatch.setattr("applepodbar.catalog.RssClient", FakeRss)
    catalog = Catalog(backend="rss")
    try:
        assert catalog.charts() == ["podcast"]
        catalog._use_rss = False
        catalog.amp.close()
        catalog.amp = BrokenAmp()  # type: ignore[assignment]
        assert catalog.charts() == ["podcast"]
        assert catalog.using_rss
    finally:
        catalog.close()
