# applepodbar — Specification

**Status:** implementation in progress
**Last verified:** 2026-10-16
**Project:** `wp-applepodbar-apple-podcasts-terminal-player-4a5c40`

A keyboard-driven terminal podcast player for Apple Podcasts, built with Python
and [Textual](https://textual.textualize.io/), in the style of
[plexbar](https://github.com/feoh/plexbar) and
[tidalbar](https://github.com/feoh/tidalbar).

> [!IMPORTANT]
> applepodbar is an independent project, not affiliated with or endorsed by
> Apple. It uses the undocumented AMP API that backs the podcasts.apple.com web
> player. Apple can change or withdraw that API without notice. No DRM bypass is
> implemented; applepodbar plays only the plain audio URLs the web player itself
> receives.

---

## 1. Goals

| # | Goal | Verified feasible |
|---|------|-------------------|
| G1 | Browse and search the Apple Podcasts catalog | Yes — §3.2 |
| G2 | Play free episodes locally through `mpv` | Yes — §3.3 |
| G3 | Signed-in Library (followed shows, saved episodes) | Yes, needs user token — §3.4 |
| G4 | Up Next queue | Yes, needs user token — §3.4 |
| G5 | Cross-device playback-position sync | Yes, needs user token — §3.5 |
| G6 | Zero-auth RSS / iTunes Search fallback | Yes — §3.6 |
| G7 | Follow/unfollow shows, save/unsave episodes | Endpoints identified — §3.4 |
| — | Paid Subscriptions content | **No** — FairPlay DRM, out of scope, §7 |

### Non-goals

- **No DRM bypass.** If content is not served as a plain fetchable asset to the
  web player, applepodbar does not play it.
- **No credential storage of an Apple ID password.** applepodbar never sees the
  password; the user types it into a real browser (§4.2).
- **No ad-skipping, no feed scraping at scale, no redistribution.**
- Not a podcast *manager* — no OPML import/export, no local download library in
  v1 (streaming only).

### Resolved out of scope

- **S1: paid Apple Podcasts Subscriptions content.** Verified 2026-10-16 against a
  real paid subscription and found to be FairPlay-protected. **Out of scope** —
  see §7.

---

## 2. Risk posture

This is a personal project. The owner explicitly accepts the terms-of-service
risk of using an unsupported private API. The design nonetheless minimizes
exposure:

- Requests mimic the web player's own headers and respect its rate of access;
  no bulk crawling.
- The developer token is **scraped fresh at runtime**, never vendored into the
  repo (it is Apple's token, it expires, and committing it would be both broken
  and inappropriate — see §4.1).
- The user token is stored with user-only file permissions in the platform
  config dir, and is never logged.

---

## 3. Verified API findings

Everything in this section was probed live against the production API on
**2026-10-16**. Each row is reproducible with `curl`.

### 3.1 Hosts and auth

| Thing | Value |
|---|---|
| Catalog + user API | `https://amp-api.podcasts.apple.com/v1` |
| Web player origin | `https://podcasts.apple.com` |
| Developer token header | `Authorization: Bearer <jwt>` |
| User token header | `media-user-token: <token>` |

The `media-user-token` header name was read directly out of the web bundle, not
guessed. **Verified live with a signed-in account 2026-10-16:** all Library,
Up Next, recents, and storefront GETs listed below returned 200 with the
media-user-token alone (plus developer Bearer JWT and Origin); no browser
cookies were needed.

The developer token is an ES256 JWT. The one observed on 2026-10-16 had:

```json
{"iss": "UM7N9RTT7G", "iat": 1792084873, "exp": 1798132873,
 "root_https_origin": ["apple.com"]}
```

That is **issued 2026-10-15, expiring 2026-12-24** — a ~70-day life. This is the
single most important operational fact in this spec: **the token must be scraped,
not hardcoded.** A vendored token would silently break the app roughly quarterly.

`Origin: https://podcasts.apple.com` must be sent; the token is origin-scoped.

### 3.2 Catalog endpoints — work with the developer token alone

No user sign-in required. All returned HTTP 200:

```
GET /v1/catalog/{storefront}/search?term=…&types=podcasts&limit=…
GET /v1/catalog/{storefront}/podcasts/{id}
GET /v1/catalog/{storefront}/podcasts/{id}?include=episodes&limit[episodes]=N
GET /v1/catalog/{storefront}/podcasts/{id}/episodes?limit=…
GET /v1/catalog/{storefront}/charts?types=podcasts&limit=…
GET /v1/editorial/{storefront}/groupings?platform=web&name=podcasts
```

Note `limit[episodes]=N` contains brackets — it must not be URL-mangled
(`curl -g`, and `params` passed as a plain dict in `httpx`).

Show `attributes` include: `artistName`, `artwork`, `contentRating`, `copyright`,
`description`, `genreNames`, `kind`, `name`, `offers`, `releaseDateTime`,
`releaseFrequency`, `seasonNumbers`, `subscribable`, `subscriptionUrl`,
`trackCount`, `url`, `websiteUrl`.

`offers` entries are `{kind, type}` with `type` in `{STDQ, PLUS, PSUB, PRMO}`.
**A `subscribe/PSUB` offer is an upsell and appears on free episodes too** — only
a `get` offer whose type is not `STDQ` marks paid content. See §7.

Episode `attributes` include: `assetUrl`, `durationInMilliseconds`, `guid`,
`name`, `itunesTitle`, `description`, `artwork`, `releaseDateTime`,
`contentAdvisory`, `offers`, `kind`, `mediaKind`, `url`.

### 3.3 Playback — free episodes expose a plain MP3

Confirmed on *The Daily* (`1200361736`), episode `1000796640093`:

- `assetUrl` is a tracking-redirect chain
  (`dts.podtrac.com` → `pdst.fm` → `pscrb.fm` → `nyt.simplecastaudio.com`).
- A ranged request returns **HTTP 206, `Content-Type: audio/mpeg`**.

Range support means seeking works, which means `mpv` can treat it as an ordinary
seekable stream. **No special player handling is required for free episodes.**

`durationInMilliseconds` gives total length without probing the asset.

### 3.4 Library endpoints — require the user token

Extracted from the web bundle (authoritative, not guessed):

```
/v1/me/library/podcasts                          followed shows
/v1/me/library/podcast-episodes                  saved episodes
/v1/me/library/next-episodes                     Up Next
/v1/me/library/latest-episodes                   latest from followed shows
/v1/me/library/podcast-channels                  subscribed paid channels
/v1/me/library/podcast-stations
/v1/me/library/recent/updated/podcast-episodes
/v1/me/recent/played/podcast-episodes            recently played
/v1/me/playback/positions/podcast-episodes       position sync (§3.5)
/v1/me/storefront                                user's storefront
```

Behaviour without a user token distinguishes two failure modes, which the client
must treat differently:

- `GET /v1/me/library/podcasts` → **403** `{"title":"Forbidden","detail":"Authentication required for request","code":"40300"}`
- `GET /v1/me/library/shows` → **400** `{"title":"Invalid Path Value","detail":"Unknown library resource type 'shows'","code":"40008"}`

A 403/40300 means "sign in" and should trigger the login flow or the anonymous
fallback. A 400/40008 is a **bug in applepodbar** (wrong path) and must surface
as an error, not as a login prompt. Do not collapse these. For most `/me` list
routes, `limit=50` instead gives **400/40005**; `limit=25` works. Pagination
`next` is a relative URL starting `/v1/me/` with an `offset` query, not a
catalog-relative path.

**Mutations, captured from a real browser and independently replayed with httpx
on 2026-10-16:**

```
POST   /v1/me/library/podcasts/{showId}           follow (HTTP 202)
DELETE /v1/me/library/podcasts/{showId}           unfollow (HTTP 204)
POST   /v1/me/library/podcast-episodes/{episodeId} save (HTTP 202)
DELETE /v1/me/library/podcast-episodes/{episodeId} unsave (HTTP 204)
```

The web player adds `with` and `l` query keys, but direct requests without
them succeeded. POST has no JSON body. The public test show and episode were
restored to unfollowed/unsaved immediately after probing. **Mark Played** was
not live-tested at the owner's request; bundle code maps it to a completed
playback-position write (§3.5) with `positionInMilliseconds=duration`.

### 3.5 Position sync — exact wire format

**Corrected by live browser-network capture and a successful HTTP 204 write on
2026-10-16** (the earlier bundle-only interpretation below was wrong):

```
GET /v1/me/playback/positions/podcast-episodes?ids={episodeId}
PUT /v1/me/playback/positions/podcast-episodes/{episodeId}
```

The bundle's `.withIds([id])` builds a resource path for PUT, not `ids[]`.
The web player also sends `with` and `l` query parameters on the PUT, but a
live write without them returned 204. PUT to the collection URL with
`ids[]=ID` returned **405** (Allow: GET,OPTIONS); do not use that shape.

Body:

```json
{
  "type": "playback-positions",
  "attributes": {
    "positionInMilliseconds": 123000,
    "recordedAtTimestamp": "2026-10-16T21:00:00.000Z",
    "completed": false
  }
}
```

Observed semantics from the bundle: when `completed` is not supplied explicitly
it defaults to `position == duration`; `recordedAtTimestamp` is the client's
current time in ISO-8601. Reads with `ids=ID` come back with
`positionInMilliseconds` and `completed`; the player resumes at
`positionInMilliseconds / 1000` seconds. A headed/headless Playwright playback
probe captured the PUT to the `/{episodeId}` resource and HTTP 204 response;
then the same current position was written back successfully via httpx.

This is G5 (cross-device sync) fully specified. It is a real two-way sync: write
on pause/stop/exit/episode-end, read on episode load.

### 3.6 Zero-auth fallback

`itunes.apple.com` needs no token at all and bridges cleanly to the AMP catalog:

```
GET https://itunes.apple.com/search?term=…&entity=podcast&limit=…
GET https://itunes.apple.com/lookup?id={collectionId}
```

Critically, **`collectionId` from iTunes Search is the same id as the AMP catalog
podcast id.** Verified: iTunes `collectionId` 1200361736 for *The Daily* is the
same id that `GET /v1/catalog/us/podcasts/1200361736` resolves. The lookup also
returns `feedUrl` (e.g. `https://feeds.simplecast.com/Sl5CSM3S`), so the fallback
can fetch the publisher's RSS directly and read enclosure URLs.

This makes the fallback a genuine drop-in rather than a parallel universe: the
same show id works in both backends.

---

## 4. Authentication design

Two tokens, acquired two different ways, with very different lifetimes.

### 4.1 Developer token — scraped at runtime

1. `GET https://podcasts.apple.com/{storefront}/podcast/…` (any show page) with a
   browser-like `User-Agent`, following redirects.
2. Parse the HTML for the main module bundle:
   `<script type="module" crossorigin src="/assets/index~<hash>.js">`.
   The hash changes on every web deploy, so it must be read from the HTML, not
   cached as a URL.
3. `GET` that bundle, scan three-part `eyJ…` JWT candidates, decode each
   header and select one with `alg=ES256` and a valid `exp` claim. The literal
   `…NiI` prefix only matches headers with certain following fields: a header
   ending immediately after `"ES256"` instead encodes to `…NiJ9`. A naive
   `eyJhbGciOiJFUzI1NiI` prefix also fails because `typ` comes first.
4. Decode the payload, read `exp`, cache the token to disk with its expiry.
5. Re-scrape when within 24h of `exp`, or on any 401 from the API.

Failure of this step is fatal and must produce a clear, actionable error
("could not extract Apple's web player token — the web player may have
changed"), not a generic HTTP error.

### 4.2 User token — one-time Playwright capture

The owner's chosen approach, and the only one that does not require handling the
Apple ID password or implementing Apple's SRP/2FA dance.

1. `applepodbar login` launches a **headed** Chromium via Playwright at
   `https://podcasts.apple.com`.
2. The user signs in themselves, including 2FA, in that real browser window.
   applepodbar neither reads nor stores the password.
3. applepodbar listens for the `media-user-token` header on the web player's
   authenticated AMP requests (including popup requests) and captures cookies
   for later investigation. After signing in, open Library if no request has
   yet carried a user token.
4. The browser closes. This is a once-per-machine operation.

Storage: the user token and captured cookie jar go in the OS keyring. The
cached developer JWT alone goes in `tokens.json` (mode `0600`). The user token
is never logged or echoed to the TUI. On Linux, a working keyring backend must
be installed and unlocked for signed-in features.

Playwright is an **optional dependency** (`applepodbar[login]`), because the
anonymous catalog experience must work without a 400MB browser download. The
login command tells the user how to install it if missing.

Re-auth: a 403/40300 on a `/v1/me/` route means the user token has expired or
been revoked. The client clears it and tells the user to re-run
`applepodbar login`.

---

## 5. Architecture

Mirrors plexbar's module boundaries so the two projects stay legible together.

```
src/applepodbar/
  __init__.py
  __main__.py
  app.py            # ApplepodbarApp: Textual UI, keybindings, view state
  models.py         # Show, Episode, BrowserItem, ItemKind, QueueEpisode
  settings.py       # config load/save, token persistence, storefront
  tokens.py         # developer-token scrape + cache (§4.1)
  login.py          # Playwright media-user-token capture (§4.2)  [optional dep]
  amp_client.py     # AMP catalog + /me client (httpx)
  rss_client.py     # iTunes Search + RSS fallback (§3.6)
  catalog.py        # backend-agnostic facade over amp_client / rss_client
  playback.py       # MpvPlayer (JSON IPC) + PlaybackQueue
  positions.py      # position read/write + sync policy (§3.5)
tests/
docs/SPEC.md
```

### Key boundaries

- **`catalog.py` is the only thing `app.py` talks to for content.** It decides
  per call whether to use AMP (token available) or RSS (anonymous/degraded), so
  the UI never branches on auth state for data access.
- **`amp_client.py` owns header assembly and error translation.** It converts the
  403/40300 vs 400/40008 distinction (§3.4) into two different exception types:
  `AuthRequired` and `AmpClientError`. The UI handles `AuthRequired`; everything
  else is a bug or an outage.
- **`playback.py` mirrors plexbar's `MpvPlayer`**: a persistent `mpv` process
  driven over JSON IPC. Tests must never need a real `mpv` — monkeypatch
  `shutil.which`, `_start_mpv`, `_command`, or inject a fake player.
- **`positions.py` holds the sync *policy*** (when to write), separate from the
  client that performs the HTTP. Policy: write on pause, stop, episode end,
  app exit, and on a periodic tick while playing (30s), coalesced so a
  pause-immediately-after-tick does not double-write. Read on episode load, and
  resume if the stored position is >30s in and not `completed`.

### Data model

`models.py` is the shared boundary type set, exactly like plexbar's. Both
`amp_client` and `rss_client` produce the *same* `Show`/`Episode` objects, which
is what makes the fallback transparent. `Episode` carries `asset_url`,
`duration_ms`, `guid`, and `source` (`amp` | `rss`) so playback and position sync
can tell whether server-side sync is even possible.

---

## 6. UI

Textual, keyboard-first, following plexbar's layout: browser pane, side pane,
now-playing label, artwork, queue, status line, footer.

Views: **Home** (editorial groupings / charts), **Search**, **Library**
(followed shows, saved episodes), **Up Next**, **Show detail** (episode list),
**Now Playing**.

Anonymous mode shows Home, Search, and Show detail, with Library and Up Next
replaced by a prompt to run `applepodbar login`.

Keybindings follow plexbar where the concept matches (`/` search, `f` focus now
playing, `s` stop, space play/pause, `q` quit), plus podcast-specific ones:
seek ±30s/±15s, playback speed, mark played.

### Testing Textual behaviour

Use `App.run_test()` with fake `catalog` and `player` objects, per plexbar's
hard-won notes: patch `on_mount` unless the test is specifically exercising
setup, and hide/defocus the search `Input` before pressing letter-key bindings,
or the `Input` eats them.

---

## 7. Paid Apple Podcasts Subscriptions — RESOLVED: out of scope

**Verified 2026-10-16 against a real paid subscription (Cool Zone Media, giving
ad-free Behind the Bastards). Paid audio is FairPlay-protected. applepodbar will
not play it.**

### The four `priceType` values

The real enum, from the bundle, is `{PLUS, PRMO, PSUB, STDQ}` — not the
`STDQ`/`PSUB` pair the first pass assumed:

| priceType | Meaning | Stream |
|---|---|---|
| `STDQ` | Free, ad-supported | plain MP3, playable |
| `PLUS` | Ad-free subscriber tier | FairPlay HLS |
| `PSUB` | Subscription-only episode | FairPlay HLS |
| `PRMO` | Promo | not observed |

Behind the Bastards (`1373812661`) carries
`[{"kind":"get","type":"PLUS"},{"kind":"get","type":"STDQ"},{"kind":"subscribe","type":"PSUB"}]`.

### Where the paid audio is, and is not

It is **not** in the catalog API. Paginating all 400 episodes of the show while
signed in, with `with=entitlements` *and* `extend=mediaEnclosures`, returned:

```
offer histogram: {"get/STDQ": 400, "subscribe/PSUB": 400}
non-STDQ "get" offers: 0
mediaEnclosures returned: 0
```

The same held for `/v1/me/library/next-episodes` and `latest-episodes`. Note that
`subscribe/PSUB` is an **upsell** offer, present on every episode including free
ones; it does not mark an episode as paid. Classify by a `get` offer whose type
is not `STDQ`.

The paid enclosures are delivered in the **signed-in web page's
`serialized-server-data` blob**, not by any AMP endpoint we found. That blob
carried 226 enclosures: 139 `STDQ`, 52 `PSUB`, 24 `PLUS`.

### The stream format

Every paid (`PLUS` and `PSUB`) enclosure points at:

```
https://play.itunes.apple.com/WebObjects/MZPlay.woa/podcast/hls/playlist.m3u8?cc=US&a={episodeId}&id={assetId}&l=en-us
```

That playlist declares its key in an `EXT-X-SESSION-DATA` block
(`DATA-ID="com.apple.hls.keys"`), base64 of:

```json
[{"keyformatversion":["1"],
  "method":"SAMPLE-AES",
  "keyformat":"com.apple.streamingkeydelivery",
  "uri":"skd://itunes.apple.com/p1525307190/c6"}]
```

`SAMPLE-AES` + `com.apple.streamingkeydelivery` + an `skd://` key URI is
**FairPlay Streaming**. Confirmed on both tiers independently — a `PSUB` full
episode (`skd://itunes.apple.com/p1525307190/c6`) and a `PLUS` asset
(`skd://itunes.apple.com/p631380742/a`).

ffmpeg's practical verdict on the same URL:

```
[hls] Unable to open key file data:text/plain;base64,AAAAOHBzc2g...
[hls] Error when loading first segment ...
Invalid data found when processing input
```

(The key blob is a PSSH initialisation box.) The free `STDQ` MP3 of the *same
episode* plays in mpv without complaint.

### Decision

**Closed won't-do**, exactly on the condition pre-registered in the first pass.
Playing this would require defeating FairPlay, which §1 rules out. Note this is
not a limitation we can engineer around: the free ad-supported version of a show
remains fully playable, so a subscriber can still listen to the show in
applepodbar — with ads.

### What the UI must do

Since paid episodes are unplayable, the UI must not pretend otherwise:

- Treat an episode as paid when it has a `get` offer whose type is not `STDQ`.
- Paid episodes should still be *listed* (they are part of the show), marked
  clearly, and refuse to play with an explanation plus the `subscriptionUrl`
  for listening in Apple's own client.
- Never surface a `play.itunes.apple.com/.../hls/playlist.m3u8` URL to mpv; it
  will fail with an opaque decode error.

### Incidental findings

- `/v1/catalog/{sf}/podcast-channels/{id}` works, and `.../podcasts` lists the
  channel's shows. `.../shows` is a 400. `?views=top-shows` also works.
- `/v1/me/library/podcast-channels` lists subscribed paid channels and is how to
  tell which channels a user is entitled to.
- `isEntitledOffer` appears on episode records in the web blob.
- `streamUrl: "appleInternal://misconfiguredEpisode"` exists as a real sentinel
  value in the bundle; guard against it.

---

## 8. Configuration

Platform-standard user config dir via `platformdirs` (same as plexbar):

```
<config_dir>/applepodbar/
  config.toml        storefront, UI prefs, playback speed, seek increments,
                     catalog_backend = "amp" | "rss"
  tokens.json        dev token + exp  (mode 0600)
  local_library.json local RSS follows and resume positions (mode 0600)
OS keyring            media-user-token + captured cookies
```

Storefront defaults to `us`, overridable in config, and refreshed from
`/v1/me/storefront` once signed in.

---

## 9. Tooling and validation

Matching plexbar: Python 3.12+, `uv` for everything, `uv_build` backend, MIT
licence, `pytest`, `ruff`, `mypy`, pre-commit via `prek`.

```bash
uv run pytest -q
uv run prek run --all-files
uv run mypy src/applepodbar tests
```

Dependencies: `textual`, `httpx`, `platformdirs`, `feedparser` (RSS fallback).
Optional extra `login`: `playwright`.

No test may require network access, a real `mpv`, or a real browser. The AMP
client is tested against recorded fixtures captured from the probes in §3.

---

## 10. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Dev-token scrape breaks on a web deploy | Medium | Parse bundle URL from HTML each time; clear error message; §4.1 |
| Token expires (~70 days) | Certain | Cache with `exp`, auto-rescrape; §4.1 |
| Apple changes/withdraws AMP API | Low-medium | RSS fallback (§3.6) keeps core browse+play working with zero auth |
| `media-user-token` capture breaks | Medium | Only affects Library/Up Next/sync; catalog unaffected |
| Paid content is DRM-protected | **Confirmed** | Out of scope; free tier still plays, UI marks paid episodes unplayable (§7) |
| Playwright install friction | Medium | Optional extra; anonymous mode fully functional without it |

---

## 11. Implementation order

The task graph already encodes this. Dependency-correct order:

1. `tk-scaffold-…` — repo, uv, Textual shell, config, mpv boundary **(unblocks everything)**
2. `tk-amp-catalog-client-…` — §3.2, §4.1
3. `tk-one-time-playwright-browser-login-…` — §4.2
4. `tk-textual-ui-…` — §6
5. `tk-signed-in-library-up-next-…` — §3.4
6. `tk-cross-device-playback-position-sync-…` — §3.5
7. `tk-zero-auth-rss-itunes-search-fallback-…` — §3.6
8. `tk-follow-unfollow-shows-and-save-episodes-…`
9. ~~`tk-spike-paid-…`~~ — **done, closed won't-do** (§7)
10. `tk-packaging-readme-ci-…`
