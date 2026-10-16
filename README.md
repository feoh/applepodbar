# applepodbar

A keyboard-driven terminal podcast player for [Apple
Podcasts](https://podcasts.apple.com), built with Python and
[Textual](https://textual.textualize.io/).

> [!IMPORTANT]
> applepodbar is an independent, early-stage project and is not affiliated with
> or endorsed by Apple. It uses the undocumented AMP API that backs the
> podcasts.apple.com web player, so Apple can change or withdraw it without
> notice. No DRM bypass is implemented — applepodbar plays only the plain audio
> URLs the web player itself receives.

## Status

Early. The application shell runs: view switching, keybindings, the playback
queue, the now-playing area, and the `mpv` engine boundary are in place, with
placeholder content in each view. The catalog, library, and sync clients are
next — see [`docs/SPEC.md`](docs/SPEC.md) for the full design and the
implementation order.

## Planned features

- Browse and search the Apple Podcasts catalog
- Stream free episodes locally through `mpv`
- Signed-in Library (followed shows, saved episodes) and Up Next
- Cross-device playback-position sync with Apple's own apps
- A zero-auth RSS / iTunes Search fallback that keeps browsing and playback
  working with no sign-in at all

## Requirements

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/)
- [`mpv`](https://mpv.io/) on `PATH` (needed for playback; browsing works
  without it)
- An Apple account, only for the signed-in features

## Installation for development

```bash
git clone git@github.com:feoh/applepodbar.git
cd applepodbar
uv sync
uv run applepodbar
```

## Signing in

Catalog browsing, search, and playback of free episodes need no sign-in at all.

Library, Up Next, and position sync need a per-user token. applepodbar captures
it once by opening a real browser window for you to sign in to Apple yourself —
**it never sees or stores your Apple ID password.** This needs the optional
`login` extra:

```bash
uv sync --extra login
uv run applepodbar login
```

## Configuration

Configuration lives in the platform-standard user config directory:

```
<config_dir>/applepodbar/
  config.toml   storefront and playback preferences
  tokens.json   cached tokens, written user-only (mode 600)
```

## Development

```bash
uv run pytest -q
uv run prek run --all-files
uv run mypy src/applepodbar tests
```

## License

MIT — see [LICENSE](LICENSE).
