"""Offline tests of web token discovery, catalog mapping and error handling."""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import httpx
import pytest

from applepodbar.amp_client import (
    AmpClient,
    AmpClientError,
    AuthRequired,
    parse_episode,
)
from applepodbar.settings import Tokens, load_tokens, save_tokens
from applepodbar.tokens import DeveloperTokenError, DeveloperTokenProvider


def jwt(expires: int) -> str:
    """Construct a fixture JWT with the header used by Apple's web bundle."""

    def encode(data: object) -> str:
        return (
            base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode())
            .decode()
            .rstrip("=")
        )

    return (
        f"{encode({'typ': 'JWT', 'alg': 'ES256'})}.{encode({'exp': expires})}.signature"
    )


def fixture_client(handler: httpx.MockTransport) -> httpx.Client:
    return httpx.Client(transport=handler)


def test_scrape_cache_expiry_and_preserve_user_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from applepodbar import settings

    monkeypatch.setattr(
        settings,
        "load_session",
        lambda: type("Session", (), {"token": "private-user"})(),
    )
    path = tmp_path / "tokens.json"
    save_tokens(Tokens(media_user_token="private-user"), path)
    token = jwt(int(time.time()) + 90 * 86400)
    urls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        if request.url.path == "/us/home":
            return httpx.Response(
                200, text='<script type="module" src="/assets/index~abc.js"></script>'
            )
        return httpx.Response(200, text=f'window.config="{token}"')

    provider = DeveloperTokenProvider(
        fixture_client(httpx.MockTransport(respond)), tokens_path=path
    )
    assert provider.get() == token
    assert provider.get() == token
    assert len(urls) == 2
    assert load_tokens(path).media_user_token == "private-user"
    assert load_tokens(path).developer_token_expires_at == int(time.time()) + 90 * 86400
    assert provider.get(force=True) == token
    assert len(urls) == 4


@pytest.mark.parametrize("missing_bundle", [True, False])
def test_missing_bundle_and_expired_token_fail_actionably(
    tmp_path: Path, missing_bundle: bool
) -> None:
    html = "no script" if missing_bundle else '<script src="/assets/index~abc.js">'
    script = jwt(1)

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text=html if request.url.path == "/us/home" else script
        )

    provider = DeveloperTokenProvider(
        fixture_client(httpx.MockTransport(respond)),
        tokens_path=tmp_path / "tokens.json",
    )
    with pytest.raises(DeveloperTokenError, match="web player may have changed"):
        provider.get()


def test_scraper_rejects_external_bundle_host(tmp_path: Path) -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text='<script src="https://attacker.invalid/assets/index~a.js">'
        )

    provider = DeveloperTokenProvider(
        fixture_client(httpx.MockTransport(respond)),
        tokens_path=tmp_path / "tokens.json",
    )
    with pytest.raises(DeveloperTokenError):
        provider.get()


def test_search_episode_pagination_and_catalog_routes(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    save_tokens(
        Tokens(
            developer_token="cached",
            developer_token_expires_at=int(time.time()) + 86400 * 7,
        ),
        path,
    )
    seen: list[httpx.Request] = []
    episode = {
        "id": "22",
        "attributes": {
            "name": "Episode",
            "assetUrl": "https://cdn.example/ep.mp3",
            "durationInMilliseconds": 1234,
            "artwork": {"url": "https://img/{w}x{h}bb.{f}"},
        },
    }

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/search"):
            return httpx.Response(
                200,
                json={
                    "results": {
                        "podcasts": {
                            "data": [
                                {
                                    "id": "1",
                                    "attributes": {
                                        "name": "Show",
                                        "artistName": "Author",
                                    },
                                }
                            ]
                        }
                    }
                },
            )
        if request.url.path.endswith("/episodes"):
            return httpx.Response(
                200,
                json={
                    "data": [episode],
                    "next": "https://amp-api.podcasts.apple.com/v1/catalog/us/page2",
                },
            )
        if request.url.path.endswith("/page2"):
            return httpx.Response(
                200, json={"data": [{"id": "23", "attributes": {"name": "Other"}}]}
            )
        if request.url.path.endswith("/podcasts/1"):
            return httpx.Response(
                200, json={"data": [{"id": "1", "attributes": {"name": "Show"}}]}
            )
        if request.url.path.endswith("/podcast-episodes/22"):
            return httpx.Response(200, json={"data": [episode]})
        return httpx.Response(200, json={"data": []})

    with AmpClient(
        client=fixture_client(httpx.MockTransport(respond)), tokens_path=path
    ) as amp:
        assert amp.search_shows("daily")[0].author == "Author"
        assert amp.show("1").title == "Show"
        assert [e.id for e in amp.episodes("1")] == ["22", "23"]
        assert amp.episode("22").artwork_url == "https://img/300x300bb.jpg"
        amp.charts()
        amp.genres()
        amp.editorial_groupings()
    assert all(r.headers["Origin"] == "https://podcasts.apple.com" for r in seen)
    assert all(r.headers["Authorization"] == "Bearer cached" for r in seen)
    assert "with=entitlements%2ChlsVideo" in str(seen[0].url)
    assert "limit=100" in str(seen[2].url)
    assert seen[-1].url.path == "/v1/editorial/us/groupings"


def test_retry_401_refreshes_token_only_once(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    save_tokens(
        Tokens(
            developer_token="old",
            developer_token_expires_at=int(time.time()) + 86400 * 7,
        ),
        path,
    )
    fresh = jwt(int(time.time()) + 86400 * 90)
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/us/home":
            return httpx.Response(200, text='<script src="/assets/index~new.js">')
        if request.url.path.endswith("new.js"):
            return httpx.Response(200, text=fresh)
        if request.headers["Authorization"] == "Bearer old":
            return httpx.Response(401, json={"errors": [{"code": "40100"}]})
        return httpx.Response(200, json={"data": []})

    amp = AmpClient(
        client=fixture_client(httpx.MockTransport(respond)), tokens_path=path
    )
    amp.charts()
    assert requests[-1].headers["Authorization"] == f"Bearer {fresh}"
    assert len(requests) == 4


def test_auth_failure_is_distinct_from_invalid_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
            developer_token_expires_at=int(time.time()) + 86400 * 7,
            media_user_token="user",
        ),
        path,
    )

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/shows"):
            return httpx.Response(400, json={"errors": [{"code": "40008"}]})
        return httpx.Response(403, json={"errors": [{"code": "40300"}]})

    amp = AmpClient(
        client=fixture_client(httpx.MockTransport(respond)), tokens_path=path
    )
    with pytest.raises(AmpClientError, match="40008"):
        amp.get("me/library/shows")
    assert load_tokens(path).media_user_token == "user"
    with pytest.raises(AuthRequired, match="login"):
        amp.get("me/library/podcasts")
    assert load_tokens(path).media_user_token is None
    with pytest.raises(AuthRequired):
        amp.get("me/library/podcasts")


def test_pagination_does_not_forward_credentials_to_external_host(
    tmp_path: Path,
) -> None:
    path = tmp_path / "tokens.json"
    save_tokens(
        Tokens(
            developer_token="dev",
            developer_token_expires_at=int(time.time()) + 86400 * 7,
        ),
        path,
    )

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": [], "next": "https://evil.invalid/steal"}
        )

    amp = AmpClient(
        client=fixture_client(httpx.MockTransport(respond)), tokens_path=path
    )
    with pytest.raises(AmpClientError, match="pagination URL"):
        amp.episodes("1")


def test_paid_classification_ignores_subscribe_upsell() -> None:
    free = {
        "id": "1",
        "attributes": {
            "name": "Free",
            "assetUrl": "https://cdn.example/free.mp3",
            "offers": [
                {"kind": "get", "type": "STDQ"},
                {"kind": "subscribe", "type": "PSUB"},
            ],
        },
    }
    paid = {
        "id": "2",
        "attributes": {
            "name": "Paid",
            "assetUrl": "https://play.itunes.apple.com/hls/playlist.m3u8",
            "offers": [{"kind": "get", "type": "PLUS"}],
        },
    }
    assert parse_episode(free).is_playable
    assert not parse_episode(free).is_paid
    assert parse_episode(paid).is_paid
    assert not parse_episode(paid).is_playable
    assert parse_episode(paid).asset_url is None
