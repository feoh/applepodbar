"""Offline user-library contracts; no real account or HTTP required."""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest

from applepodbar import settings
from applepodbar.amp_client import AmpClient, AuthRequired
from applepodbar.settings import Tokens, save_tokens


def test_signed_in_library_paths_and_pagination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        settings, "load_session", lambda: type("Session", (), {"token": "user"})()
    )
    path = tmp_path / "tokens.json"
    save_tokens(
        Tokens(
            developer_token="dev",
            developer_token_expires_at=int(time.time()) + 86400 * 7,
        ),
        path,
    )
    paths: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.headers["media-user-token"] == "user"
        if request.url.path.endswith("/storefront"):
            return httpx.Response(
                200, json={"data": [{"id": "GB", "type": "storefronts"}]}
            )
        if request.url.path.endswith("/page2"):
            return httpx.Response(
                200, json={"data": [{"id": "s2", "attributes": {"name": "Show 2"}}]}
            )
        if request.url.path.endswith("/podcasts"):
            return httpx.Response(
                200,
                json={
                    "data": [{"id": "s1", "attributes": {"name": "Show 1"}}],
                    "next": "/v1/me/page2?offset=25",
                },
            )
        if "episodes" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "e1",
                            "attributes": {
                                "name": "Episode",
                                "assetUrl": "https://cdn.invalid/ep.mp3",
                            },
                        }
                    ]
                },
            )
        return httpx.Response(
            200, json={"data": [{"id": "c1", "attributes": {"name": "Channel"}}]}
        )

    amp = AmpClient(
        client=httpx.Client(transport=httpx.MockTransport(respond)), tokens_path=path
    )
    assert [show.id for show in amp.library_shows()] == ["s1", "s2"]
    assert [ep.id for ep in amp.saved_episodes()] == ["e1"]
    assert [ep.id for ep in amp.up_next()] == ["e1"]
    assert [ep.id for ep in amp.latest_episodes()] == ["e1"]
    assert [ep.id for ep in amp.updated_episodes()] == ["e1"]
    assert [ep.id for ep in amp.recently_played()] == ["e1"]
    assert len(amp.library_channels()) == 1
    assert len(amp.library_stations()) == 1
    assert amp.account_storefront() == "gb"
    assert "/v1/me/library/next-episodes" in paths
    assert "/v1/me/library/recent/updated/podcast-episodes" in paths
    assert "/v1/me/recent/played/podcast-episodes" in paths


def test_follow_save_and_reversal_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        settings, "load_session", lambda: type("Session", (), {"token": "fixture"})()
    )
    path = tmp_path / "tokens.json"
    save_tokens(
        Tokens(
            developer_token="dev",
            developer_token_expires_at=int(time.time()) + 86400 * 7,
        ),
        path,
    )
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(202 if request.method == "POST" else 204)

    amp = AmpClient(
        client=httpx.Client(transport=httpx.MockTransport(respond)), tokens_path=path
    )
    amp.follow_show("show1")
    amp.unfollow_show("show1")
    amp.save_episode("ep2")
    amp.unsave_episode("ep2")
    assert [(r.method, r.url.path) for r in requests] == [
        ("POST", "/v1/me/library/podcasts/show1"),
        ("DELETE", "/v1/me/library/podcasts/show1"),
        ("POST", "/v1/me/library/podcast-episodes/ep2"),
        ("DELETE", "/v1/me/library/podcast-episodes/ep2"),
    ]
    assert all(r.headers["media-user-token"] == "fixture" for r in requests)


def test_repeated_401_on_me_requires_new_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
            developer_token_expires_at=int(time.time()) + 86400 * 7,
        ),
        path,
    )
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _r: httpx.Response(401, json={"errors": [{"code": "40100"}]})
        )
    )
    amp = AmpClient(client=client, tokens_path=path)
    # Force provider refresh is stubbed; the second 401 is the user-session error.
    monkeypatch.setattr(
        amp.provider, "get", lambda *, force=False: "new-dev" if force else "dev"
    )
    with pytest.raises(AuthRequired, match="login"):
        amp.up_next()
    assert not session
