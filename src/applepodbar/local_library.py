"""Local follows and resume positions for zero-auth RSS listening."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from applepodbar.settings import CONFIG_DIR

LOCAL_LIBRARY_PATH = CONFIG_DIR / "local_library.json"


class LocalLibrary:
    """Minimal on-disk state, separate from the OS keyring and AMP Library."""

    def __init__(self, path: Path = LOCAL_LIBRARY_PATH) -> None:
        self.path = path

    def _read(self) -> dict[str, Any]:
        try:
            result = json.loads(self.path.read_text(encoding="utf-8"))
            return result if isinstance(result, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                dir=self.path.parent,
                prefix=".local-library-",
                delete=False,
                encoding="utf-8",
            ) as temp:
                name = temp.name
                json.dump(data, temp)
            os.chmod(name, 0o600)
            os.replace(name, self.path)
        finally:
            if name is not None:
                Path(name).unlink(missing_ok=True)

    def followed_shows(self) -> list[str]:
        """Return the list of locally followed Apple collection IDs."""

        return [str(value) for value in self._read().get("follows", [])]

    def follow(self, show_id: str) -> None:
        data = self._read()
        data["follows"] = sorted(set(data.get("follows", [])) | {show_id})
        self._write(data)

    def unfollow(self, show_id: str) -> None:
        data = self._read()
        data["follows"] = [
            value for value in data.get("follows", []) if value != show_id
        ]
        self._write(data)

    def position_ms(self, episode_id: str) -> int:
        """Return a local resume position, or zero when absent."""

        value = self._read().get("positions", {}).get(episode_id, 0)
        return value if isinstance(value, int) and value > 0 else 0

    def set_position(self, episode_id: str, milliseconds: int) -> None:
        data = self._read()
        positions = data.setdefault("positions", {})
        positions[episode_id] = max(0, milliseconds)
        self._write(data)
