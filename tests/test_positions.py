"""Playback-position wire format and coalescing, with no Apple account."""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from applepodbar import settings
from applepodbar.amp_client import AmpClient
from applepodbar.positions import PlaybackPosition, PositionSync
from applepodbar.settings import Tokens, save_tokens


def test_position_read_uses_ids_but_put_targets_episode_resource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        settings, "load_session", lambda: type("Session", (), {"token": "fixture"})()
    )
    path = tmp_path / "tokens.json"
    save_tokens(
        Tokens(
            developer_token="fixture",
            developer_token_expires_at=int(time.time()) + 86400 * 7,
        ),
        path,
    )
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "42",
                            "type": "playback-positions",
                            "attributes": {
                                "positionInMilliseconds": 72_000,
                                "completed": False,
                            },
                        }
                    ]
                },
            )
        return httpx.Response(204)

    amp = AmpClient(
        client=httpx.Client(transport=httpx.MockTransport(respond)), tokens_path=path
    )
    positions = PositionSync(amp)
    assert positions.read("42").resume_seconds(100_000) == 72
    assert requests[0].url.params["ids"] == "42"
    assert "ids[]" not in requests[0].url.params
    assert positions.write("42", 73_000, 100_000, force=True)
    assert requests[1].url.path.endswith("/podcast-episodes/42")
    assert not requests[1].url.params
    assert requests[1].headers["media-user-token"] == "fixture"
    body = json.loads(requests[1].content)
    assert body["type"] == "playback-positions"
    assert body["attributes"]["positionInMilliseconds"] == 73_000
    assert body["attributes"]["completed"] is False
    assert body["attributes"]["recordedAtTimestamp"].endswith("Z")


def test_resume_threshold_completed_and_missing_data() -> None:
    assert PlaybackPosition(29_999).resume_seconds() == 0
    assert PlaybackPosition(30_000).resume_seconds() == 0
    assert PlaybackPosition(45_000).resume_seconds(100_000) == 45
    assert PlaybackPosition(100_000).resume_seconds(100_000) == 0
    assert PlaybackPosition(45_000, completed=True).resume_seconds() == 0


def test_periodic_and_lifecycle_writes_coalesce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeAmp:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def put(
            self, _path: str, _params: object, body: dict[str, object]
        ) -> dict[str, object]:
            self.calls.append(body)
            return {}

    clock = [1000.0]
    monkeypatch.setattr("applepodbar.positions.time.monotonic", lambda: clock[0])
    fake = FakeAmp()
    positions = PositionSync(fake)  # type: ignore[arg-type]
    assert positions.write("ep", 40_000, 100_000)
    clock[0] += 1
    assert not positions.write("ep", 40_500, 100_000)
    assert not positions.write("ep", 40_500, 100_000, force=True)
    assert positions.write("ep", 46_000, 100_000, force=True)
    clock[0] += 30
    assert positions.write("ep", 100_000, 100_000)
    assert fake.calls[-1]["attributes"]["completed"] is True  # type: ignore[index]
    assert len(fake.calls) == 3
