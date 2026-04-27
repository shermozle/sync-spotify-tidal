# CLAUDE.md — sync-spotify-tidal

## What this project does

A single Python script (`sync.py`) that runs weekly (via cron or GitHub Actions) to bidirectionally sync one playlist between Spotify and Tidal. It reads both playlists, searches for missing tracks on each platform, scores candidates, and adds the ones it's confident about. Uncertain matches are flagged in a log file and an HTML email report.

## Repository layout

```
sync.py                        # All logic — single file, ~800 lines
requirements.txt               # spotipy, tidalapi, resend, python-dotenv
.env.example                   # Template for all environment variables
.gitignore                     # Excludes .env, .cache, tidal_session.json, logs/
.github/workflows/sync.yml     # GitHub Actions weekly schedule + manual trigger
logs/                          # Created at runtime; one .log file per run
.cache                         # Spotify OAuth token cache (gitignored)
tidal_session.json             # Tidal OAuth session (gitignored)
```

## Key data structures

```python
@dataclass
class TrackInfo:
    title: str
    artist: str
    album: str
    platform_id: str   # Spotify URI "spotify:track:..." or Tidal int ID as str
    platform: str      # "spotify" | "tidal"

    def norm(self) -> str: ...   # artist+title, feat stripped, punctuation removed

@dataclass
class SyncAction:
    action: str                  # "ADDED" | "SKIPPED" | "UNCERTAIN"
    direction: str               # "spotify→tidal" | "tidal→spotify"
    source: TrackInfo
    matched: Optional[TrackInfo] # best candidate found (None = no results)
    score: Optional[float]       # composite score 0–1 (None for SKIPPED)
    note: str                    # human-readable reason
```

## Matching algorithm

The core scoring is in `_score_match()` (sync.py:115).

**Three factors:**

1. **Title similarity** (65%) — `difflib.SequenceMatcher` on `TrackInfo.norm()` for both source and candidate. `norm()` strips feat./ft./featuring and punctuation but **keeps** version qualifiers like "live", "remix", "acoustic" so they reduce the ratio.

2. **Album similarity** (35%) — same `SequenceMatcher` approach on normalized album names. This is what prevents a studio recording from matching a live album version even when the song title is identical.

3. **Variant penalty** (−0.4) — applied when the source has no variant keyword in its title or album, but the candidate does. `_is_variant()` checks against `_VARIANT_KEYWORDS = ["live", "remix", "acoustic", "demo", "cover", "instrumental", "extended", "reprise", "a cappella", "acapella", "unplugged"]`. This pushes a false studio-vs-live match from ~0.9 to ~0.3, well below threshold.

**Threshold:** `HIGH_CONF_THRESHOLD = 0.85`. Scores ≥ 0.85 → ADDED. Scores < 0.85 → UNCERTAIN.

**Deduplication:** before searching, each source track's `norm()` key is checked against the set of target track `norm()` keys. Version qualifiers are kept here too, so a live version on Tidal does not prevent a studio version from being added from Spotify.

## Authentication

**Spotify** — `spotipy.SpotifyOAuth` with scopes `playlist-read-private playlist-modify-private playlist-modify-public`. Token cached in `.cache`. First run opens a browser; subsequent runs refresh silently.

**Tidal** — `tidalapi.Session` with OAuth 2.0 PKCE. No Tidal developer account needed; tidalapi uses client IDs embedded in Tidal's own apps.
- Load attempt: `session.load_session_from_file("tidal_session.json")` — internally calls `token_refresh()` on every load since expiry is not persisted.
- If load fails or `check_login()` returns False: `session.login_oauth_simple()` prints a URL the user visits once, then `save_session_to_file()`.
- `check_login()` makes a live API call to confirm the token is accepted.

**CI bootstrap** — `setup_credentials_from_env()` writes `SPOTIFY_CACHE` and `TIDAL_SESSION` secret values to disk before auth runs. This is the only mechanism for non-interactive CI runs.

## Test mode

`--test` flag (or `TEST_MODE=true` env var). Reads the real playlists to determine what's missing, but writes proposed additions to `SPOTIFY_TEST_PLAYLIST_ID` / `TIDAL_TEST_PLAYLIST_ID` instead of the real playlists. The email report is clearly labelled TEST MODE.

After the Spotify→Tidal pass, the script re-fetches the real Tidal playlist (not the test one) before the Tidal→Spotify pass. This is intentional: in test mode, the real Tidal playlist hasn't changed, so we compare against its actual current state.

## `main()` flow

```
load_config() → setup_logger() → setup_credentials_from_env()
→ get_spotify_client() → get_tidal_session()
→ get_spotify_tracks() + get_tidal_tracks()
→ sync_direction(spotify→tidal, writes to target_tidal_id)
→ get_tidal_tracks() again   ← re-fetch so reverse pass sees the additions
→ sync_direction(tidal→spotify, writes to target_spotify_id)
→ write_log_report() → build_html_report() → send_email_report()
```

The entire `main()` body is wrapped in `try/except` that logs the traceback and re-raises, giving a non-zero exit code so cron and GitHub Actions can alert.

## Libraries

| Library | Version | Purpose |
|---|---|---|
| `spotipy` | 2.26.0 | Spotify Web API — playlist read/write, search |
| `tidalapi` | 0.8.11 | Tidal API (unofficial, EbbLabs/python-tidal) — playlist read/write, search |
| `resend` | 2.29.0 | Transactional email via Resend API |
| `python-dotenv` | 1.0.0 | Load `.env` file into environment |

## Tidalapi-specific gotchas

- `session.playlist(id)` returns a `UserPlaylist` when the playlist belongs to the authenticated user, otherwise a plain `Playlist`. `.add()` only exists on `UserPlaylist`. Both `TIDAL_PLAYLIST_ID` and `TIDAL_TEST_PLAYLIST_ID` must be owned by the authenticated Tidal account.
- `playlist.tracks()` returns all tracks with internal pagination. No manual pagination needed.
- `session.search(query, models=[tidalapi.Track], limit=5)` returns a dict; access results via `results.get("tracks", [])`.
- `session.load_session_from_file()` refreshes the access token on every call (expiry_time is not saved to the JSON file). This means each run hits Tidal's token endpoint once at startup — expected behaviour.

## Spotify-specific gotchas

- `playlist_items()` returns at most 50 tracks per call. `get_spotify_tracks()` paginates using `offset` until `results["next"] is None`.
- Items where `item["track"]` is `None` or `item["track"]["is_local"]` is True are silently skipped (local files don't have Spotify URIs).
- The search query uses `artist:X track:Y` field filters for better precision than a plain string query.

## How to test changes locally

There are no automated unit tests. Test changes end-to-end using test mode:

```bash
# 1. Set up a test playlist on each platform (empty is fine)
# 2. Add their IDs to .env:
#    SPOTIFY_TEST_PLAYLIST_ID=...
#    TIDAL_TEST_PLAYLIST_ID=...

# 3. Run in test mode — reads real playlists, writes to test playlists
python sync.py --test

# 4. Check:
#    - Terminal/log output for ADDED / SKIPPED / UNCERTAIN lines
#    - logs/sync_YYYY-MM-DD_HH-MM-SS.log for the full report
#    - Email report for matched albums (source vs matched)
#    - Test playlists in-app to verify the actual tracks

# 5. Run for real only once you're satisfied
python sync.py
```

To test the matching logic in isolation without making any API calls, you can construct `TrackInfo` objects directly and call `_score_match()`:

```python
from sync import TrackInfo, _score_match

studio = TrackInfo("Comfortably Numb", "Pink Floyd", "The Wall", "spotify:track:abc", "spotify")
live   = TrackInfo("Comfortably Numb", "Pink Floyd", "Pulse (Live)", "123", "tidal")
print(_score_match(studio, live))   # expect ~0.3 — UNCERTAIN due to variant penalty

remaster = TrackInfo("Comfortably Numb", "Pink Floyd", "The Wall (Remastered)", "123", "tidal")
print(_score_match(studio, remaster))  # expect ~0.95 — ADDED
```

## Adjustable constants (sync.py)

| Constant | Default | Effect |
|---|---|---|
| `HIGH_CONF_THRESHOLD` | `0.85` | Minimum composite score to auto-add a track |
| `_W_TITLE` | `0.65` | Weight of title similarity in the composite score |
| `_W_ALBUM` | `0.35` | Weight of album similarity in the composite score |
| `_VARIANT_PENALTY` | `0.4` | Score deduction when source is studio but candidate is a variant |
| `_VARIANT_KEYWORDS` | see source | Keywords that mark a track as a non-studio variant |

## Environment variables

See `.env.example` for the full list with comments. Required at runtime:

`SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `SPOTIFY_REDIRECT_URI`, `SPOTIFY_PLAYLIST_ID`, `TIDAL_PLAYLIST_ID`, `RESEND_API_KEY`, `RESEND_FROM`, `RESEND_TO`

Required for test mode: `SPOTIFY_TEST_PLAYLIST_ID`, `TIDAL_TEST_PLAYLIST_ID`

Required for CI (GitHub Actions): `SPOTIFY_CACHE`, `TIDAL_SESSION`
