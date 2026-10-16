"""Playback queue and mpv subprocess integration.

The engine boundary for docs/SPEC.md §5. Free Apple Podcasts episodes are plain
range-seekable MP3s (§3.3), so mpv needs no special handling for them.

Nothing here may require a real mpv in tests: monkeypatch ``shutil.which``,
``_start_mpv``, or ``_command``, or inject a fake player.
"""

from __future__ import annotations

import json
import math
import shutil
import socket
import subprocess
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from applepodbar.models import QueueEpisode

MIN_PLAYBACK_SPEED = 0.5
MAX_PLAYBACK_SPEED = 3.0


class MpvNotFoundError(RuntimeError):
    """Raised when mpv is required but not installed."""


@dataclass(frozen=True)
class PlaybackProgress:
    """Current playback position and duration reported by mpv."""

    position_seconds: float = 0.0
    duration_seconds: float = 0.0

    def progress_ratio(self) -> float:
        """Return the position as a value suitable for a progress bar."""

        if self.duration_seconds <= 0:
            return 0.0
        return min(max(self.position_seconds / self.duration_seconds, 0.0), 1.0)

    @property
    def position_ms(self) -> int:
        """Return the position in milliseconds, for position sync."""

        return int(self.position_seconds * 1000)


def _finite_seconds(value: Any) -> float:
    """Normalize an optional mpv numeric property for display."""

    try:
        seconds = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return seconds if math.isfinite(seconds) and seconds >= 0 else 0.0


@dataclass
class PlaybackQueue:
    """In-memory playback queue.

    The currently playing episode is tracked separately from the next unplayed
    one, so appending does not imply playing.
    """

    episodes: list[QueueEpisode] = field(default_factory=list)
    current_index: int = -1
    next_index: int = 0

    @property
    def current(self) -> QueueEpisode | None:
        """Return the episode currently being played, if any."""

        if 0 <= self.current_index < len(self.episodes):
            return self.episodes[self.current_index]
        return None

    def append(self, episodes: list[QueueEpisode]) -> None:
        """Append episodes without changing playback state."""

        self.episodes.extend(episodes)

    def replace(self, episodes: list[QueueEpisode]) -> None:
        """Replace the queue without starting playback."""

        self.episodes = list(episodes)
        self.current_index = -1
        self.next_index = 0

    def next(self) -> QueueEpisode | None:
        """Advance to the next unplayed episode and return it."""

        if self.next_index < len(self.episodes):
            self.current_index = self.next_index
            self.next_index += 1
            return self.current
        self.current_index = -1
        return None

    def clear_current(self) -> None:
        """Mark that no queued episode is currently being played."""

        self.current_index = -1

    def labels(self) -> list[str]:
        """Return display labels for the queue."""

        return [
            f"{'▶ ' if index == self.current_index else '  '}{episode.label}"
            for index, episode in enumerate(self.episodes)
        ]


class MpvPlayer:
    """Persistent mpv controller using JSON IPC."""

    def __init__(self) -> None:
        if shutil.which("mpv") is None:
            msg = "mpv was not found on PATH; install mpv to use applepodbar playback."
            raise MpvNotFoundError(msg)
        self._process: subprocess.Popen[bytes] | None = None
        self._socket_dir = tempfile.TemporaryDirectory(prefix="applepodbar-mpv-")
        self._socket_path = Path(self._socket_dir.name) / "mpv.sock"
        self._paused = False
        self._loaded = False
        self._speed = 1.0
        self._request_id = 0
        self._start_mpv()

    def __del__(self) -> None:
        """Best-effort cleanup if a caller forgets to close the player."""

        with suppress(Exception):
            self.close()

    @property
    def is_running(self) -> bool:
        """Return whether mpv is currently running."""

        return self._process is not None and self._process.poll() is None

    @property
    def is_paused(self) -> bool:
        """Return whether playback is paused."""

        return self._paused

    @property
    def speed(self) -> float:
        """Return the current playback speed multiplier."""

        return self._speed

    def play(self, episode: QueueEpisode, start_seconds: float = 0.0) -> None:
        """Start playing an episode, replacing whatever is loaded.

        ``start_seconds`` resumes mid-episode, which is how a position read from
        Apple's sync endpoint (docs/SPEC.md §3.5) gets applied.
        """

        self._ensure_running()
        options = f"start={max(start_seconds, 0.0)}" if start_seconds > 0 else ""
        command: list[object] = ["loadfile", episode.stream_url, "replace"]
        if options:
            command.extend([0, options])
        self._command(command)
        self._paused = False
        self._loaded = True

    def playback_progress(self) -> PlaybackProgress:
        """Return the current position and duration from mpv."""

        if not self._loaded:
            return PlaybackProgress()
        self._ensure_running()
        return PlaybackProgress(
            position_seconds=_finite_seconds(self._get_property("time-pos")),
            duration_seconds=_finite_seconds(self._get_property("duration")),
        )

    def pause_resume(self) -> None:
        """Toggle pause/resume in mpv."""

        if not self._loaded:
            return
        self._ensure_running()
        self._command(["cycle", "pause"])
        self._paused = not self._paused

    def seek(self, offset_seconds: float) -> None:
        """Seek relative to the current position.

        Safe because episode assets answer ranged requests (docs/SPEC.md §3.3).
        """

        if not self._loaded:
            return
        self._ensure_running()
        self._command(["seek", offset_seconds, "relative"])

    def set_speed(self, speed: float) -> float:
        """Clamp and apply a playback speed multiplier, returning what was set."""

        clamped = min(max(speed, MIN_PLAYBACK_SPEED), MAX_PLAYBACK_SPEED)
        self._ensure_running()
        self._command(["set_property", "speed", clamped])
        self._speed = clamped
        return clamped

    def reap_finished(self) -> bool:
        """Report whether the episode finished and mpv returned to idle."""

        if not self._loaded:
            return False
        if not self.is_running:
            self._loaded = False
            self._paused = False
            return True
        if not self._get_property("idle-active"):
            return False
        self._loaded = False
        self._paused = False
        return True

    def stop(self) -> None:
        """Stop the loaded episode while keeping mpv ready for commands."""

        if self.is_running and self._loaded:
            self._command(["stop"])
        self._loaded = False
        self._paused = False

    def close(self) -> None:
        """Terminate mpv and clean up the IPC socket."""

        if self._process is not None and self._process.poll() is None:
            try:
                self._command(["quit"])
                self._process.wait(timeout=2)
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                self._process.terminate()
                try:
                    self._process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._process.kill()
        self._process = None
        self._loaded = False
        self._paused = False
        self._socket_dir.cleanup()

    def _start_mpv(self) -> None:
        if self._socket_path.exists():
            self._socket_path.unlink()
        self._process = subprocess.Popen(
            [
                "mpv",
                "--idle=yes",
                "--no-video",
                "--really-quiet",
                f"--input-ipc-server={self._socket_path}",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._wait_for_socket()

    def _ensure_running(self) -> None:
        if self.is_running:
            return
        self._start_mpv()

    def _wait_for_socket(self) -> None:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if self._socket_path.exists():
                return
            if self._process is not None and self._process.poll() is not None:
                msg = "mpv exited before its IPC socket became available."
                raise RuntimeError(msg)
            time.sleep(0.01)
        msg = "Timed out waiting for mpv IPC socket."
        raise RuntimeError(msg)

    def _command(self, command: list[object]) -> dict[str, Any]:
        return self._request({"command": command})

    def _get_property(self, name: str) -> Any:
        return self._command(["get_property", name]).get("data")

    def _request(self, payload: dict[str, object]) -> dict[str, Any]:
        self._request_id += 1
        request_id = self._request_id
        request = json.dumps({**payload, "request_id": request_id}).encode() + b"\n"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.5)
            client.connect(str(self._socket_path))
            client.sendall(request)
            response = self._read_response(client, request_id)
        if response.get("error") not in {None, "success"}:
            msg = f"mpv command failed: {response['error']}"
            raise RuntimeError(msg)
        return response

    def _read_response(
        self, client: socket.socket, expected_request_id: int
    ) -> dict[str, Any]:
        buffer = b""
        while True:
            chunk = client.recv(65536)
            if not chunk:
                msg = "mpv closed its IPC socket before responding."
                raise RuntimeError(msg)
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if not line:
                    continue
                try:
                    data = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError as exc:
                    msg = "mpv returned invalid JSON over IPC."
                    raise RuntimeError(msg) from exc
                if not isinstance(data, dict):
                    # RuntimeError, not TypeError: every mpv IPC failure is a
                    # RuntimeError by contract, and close() catches exactly that.
                    msg = "mpv returned an invalid IPC response."
                    raise RuntimeError(msg)  # noqa: TRY004
                if data.get("request_id") == expected_request_id:
                    return data
