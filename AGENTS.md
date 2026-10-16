# applepodbar agent notes

Read [`docs/SPEC.md`](docs/SPEC.md) first. It is evidence-backed — every API
claim in it was verified live against the production AMP API, with the probe
noted — so prefer it over guessing or re-deriving endpoint behaviour.

## Architecture overview

- **Textual TUI (`src/applepodbar/app.py`)**: `ApplepodbarApp` owns the UI,
  keybindings, view state, the now-playing line, and the queue pane. View
  content is still placeholder; see §11 of the spec for the build order.
- **Playback (`src/applepodbar/playback.py`)**: `PlaybackQueue` is an in-memory
  queue that tracks the playing episode separately from the next unplayed one,
  so appending does not imply playing. `MpvPlayer` drives a persistent `mpv`
  over JSON IPC.
- **Data models (`src/applepodbar/models.py`)**: `Show`, `Episode`,
  `BrowserItem`, `QueueEpisode` are the shared boundary objects. Both the AMP
  client and the RSS fallback must produce *these same types* — that is what
  keeps the fallback invisible to the UI.
- **Settings (`src/applepodbar/settings.py`)**: preferences in `config.toml`,
  credentials in `tokens.json` at mode `0600`. Keep them in separate files.

## Things that will bite you

- **Never vendor the Apple developer token.** It is an ES256 JWT with a ~70-day
  life, scraped from the web bundle at runtime. A committed one is both a broken
  app in two months and not ours to ship. See spec §4.1.
- **The JWT regex must match `eyJ0eXAiOiJKV1QiLCJhbGciOiJFUzI1NiI`**, because the
  header is `{"typ":"JWT","alg":"ES256",…}`. The intuitive
  `eyJhbGciOiJFUzI1NiI` prefix matches nothing and fails silently.
- **403/40300 and 400/40008 are different bugs.** `403 Authentication required`
  means sign in. `400 Unknown library resource type` means *our* path is wrong.
  Do not collapse them into one "API error" branch.
- **`limit[episodes]=N` has literal brackets.** Pass params as a dict; with curl
  use `-g`.
- **Send `Origin: https://podcasts.apple.com`** — the developer token is
  origin-scoped.
- **Paid content is FairPlay-protected and out of scope.** Verified against a
  real subscription: `PLUS` and `PSUB` audio is `SAMPLE-AES` HLS with an `skd://`
  key (`com.apple.streamingkeydelivery`). mpv and ffmpeg cannot decode it, and
  making them is out of scope. Never hand a
  `play.itunes.apple.com/.../hls/playlist.m3u8` URL to mpv. Spec §7.
- **`subscribe/PSUB` does NOT mean an episode is paid.** It is an upsell offer
  present on every episode of a show that sells a subscription, free ones
  included. Paid means a **`get`** offer whose type is not `STDQ`. Getting this
  wrong marks the entire free catalogue as unplayable.

## Testing

No test may require network access, a real `mpv`, or a real browser.

Use Textual's `App.run_test()` for UI behaviour, with fake catalog/player
objects. Two hard-won rules carried over from plexbar:

- Patch `load_config` / `load_tokens` (or `on_mount`) unless the test is
  specifically exercising startup.
- The composed search `Input` eats ordinary letter keys. Hide `#search` and
  focus `#browser-list` before `pilot.press("l")` and friends.

Assert `Static` text with `str(widget.render())`.

## Local validation

```bash
uv run pytest -q
uv run ruff check && uv run ruff format --check
uv run mypy src/applepodbar tests
uvx prek run --all-files
```
