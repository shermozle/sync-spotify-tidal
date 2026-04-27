# sync-spotify-tidal

Bidirectionally syncs a single playlist between Spotify and Tidal on a weekly schedule. Tracks missing from either platform are searched on the other and added automatically when a confident match is found. Uncertain matches are flagged in a structured log file and an HTML email report sent via [Resend](https://resend.com), so you can manually review and fix them.

## How it works

Each run:

1. Fetches the current track list from both playlists
2. Searches for each missing track on the opposite platform
3. Scores each candidate using a composite of title similarity (65%) and album similarity (35%), with a penalty for version mismatches (live, remix, acoustic, demo, etc.)
4. Adds tracks that score ≥ 85% confidence; flags the rest for manual review
5. Writes a timestamped log to `logs/` and emails an HTML report

The matching strategy deliberately keeps version qualifiers (live, remix, acoustic…) in the comparison so that a studio recording never silently matches a live version of the same song.

## Prerequisites

- Python 3.9+
- A **Spotify** account with a registered app ([developer.spotify.com/dashboard](https://developer.spotify.com/dashboard))
- A **Tidal** account (no developer registration needed)
- A **Resend** account for email reports ([resend.com](https://resend.com) — the free tier is sufficient)

## Local setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure credentials

```bash
cp .env.example .env
```

Edit `.env` and fill in the values below. See the [Environment variables](#environment-variables) section for details on where to find each one.

### 3. First run — authenticate

```bash
python sync.py --test
```

On the first run:

- **Spotify**: a browser window will open automatically. Log in and authorise the app. The token is saved to `.cache` for future runs.
- **Tidal**: a URL is printed to the terminal. Open it in your browser, log in, and the session is saved to `tidal_session.json`.

Using `--test` on the first run writes proposed changes to your test playlists instead of your real ones, so you can verify matches before going live.

### 4. Review test results

Open your test playlists in Spotify and Tidal to check that the matched tracks look right. The email report shows the source track, artist, source album, matched track, and matched album side by side — mismatches are easy to spot.

### 5. Run for real

Once you're happy with the matches, sync your real playlists:

```bash
python sync.py
```

### 6. Schedule weekly runs (cron)

```
0 9 * * 1  cd /path/to/sync-spotify-tidal && python sync.py
```

This runs every Monday at 09:00. Both `.cache` and `tidal_session.json` persist OAuth tokens that refresh automatically, so no browser interaction is ever needed on scheduled runs.

---

## Test mode

Test mode lets you safely preview what the script would do before touching your real playlists.

**Setup:**

1. Create one empty playlist on Spotify and one on Tidal
2. Add their IDs to `.env`:
   ```
   SPOTIFY_TEST_PLAYLIST_ID=<id>
   TIDAL_TEST_PLAYLIST_ID=<uuid>
   ```

**Usage:**

```bash
python sync.py --test        # via flag
TEST_MODE=true python sync.py  # via env var
```

The script reads your real playlists to determine what's missing, then writes the proposed additions to the test playlists. The email report is clearly labelled **TEST MODE**. Check the test playlists in-app to verify the audio matches, then run without `--test` to apply the changes.

---

## Deploy to GitHub Actions

The included workflow (`.github/workflows/sync.yml`) runs every Monday at 09:00 UTC and can also be triggered manually from the Actions tab.

### Step 1 — Authenticate locally first

You must complete the first-run authentication on your local machine before CI can run non-interactively.

```bash
python sync.py --test   # completes OAuth for both platforms
```

This creates two files that contain the OAuth tokens:

- `.cache` — Spotify access + refresh token (JSON)
- `tidal_session.json` — Tidal session + refresh token (JSON)

### Step 2 — Push the repository to GitHub

```bash
git remote add origin https://github.com/<you>/sync-spotify-tidal.git
git push -u origin main
```

### Step 3 — Add repository secrets

Go to **Settings → Secrets and variables → Actions → New repository secret** and add each of the following:

| Secret | Value |
|---|---|
| `SPOTIFY_CLIENT_ID` | From your Spotify app dashboard |
| `SPOTIFY_CLIENT_SECRET` | From your Spotify app dashboard |
| `SPOTIFY_REDIRECT_URI` | `http://localhost:8888/callback` |
| `SPOTIFY_PLAYLIST_ID` | Bare playlist ID from the Spotify URL |
| `TIDAL_PLAYLIST_ID` | Tidal playlist UUID |
| `RESEND_API_KEY` | From resend.com |
| `RESEND_FROM` | Your verified sender address |
| `RESEND_TO` | Where to send the report |
| `SPOTIFY_CACHE` | Full contents of your local `.cache` file |
| `TIDAL_SESSION` | Full contents of your local `tidal_session.json` file |

To get the file contents for `SPOTIFY_CACHE` and `TIDAL_SESSION`:

```bash
cat .cache            # copy this entire JSON string
cat tidal_session.json  # copy this entire JSON string
```

### Step 4 — Trigger a test run

In the **Actions** tab, select **Spotify-Tidal Sync** and click **Run workflow**. Check the logs and the email report to confirm everything works before the first scheduled run.

### Token refresh

OAuth access tokens are short-lived, but refresh tokens are long-lived. The script refreshes the access token automatically on every run using the saved refresh token. The secrets (`SPOTIFY_CACHE` and `TIDAL_SESSION`) only need to be updated if you ever explicitly revoke access — normal token expiry is handled transparently.

---

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `SPOTIFY_CLIENT_ID` | Yes | Spotify app client ID |
| `SPOTIFY_CLIENT_SECRET` | Yes | Spotify app client secret |
| `SPOTIFY_REDIRECT_URI` | Yes | Must match what's set in the Spotify app dashboard. Use `http://localhost:8888/callback` |
| `SPOTIFY_PLAYLIST_ID` | Yes | Bare ID from the Spotify playlist URL: `open.spotify.com/playlist/<ID>` |
| `TIDAL_PLAYLIST_ID` | Yes | Tidal playlist UUID (visible in the share link) |
| `RESEND_API_KEY` | Yes | API key from resend.com |
| `RESEND_FROM` | Yes | Sender address (must be a verified domain in Resend) |
| `RESEND_TO` | Yes | Recipient address for the email report |
| `LOG_DIR` | No | Directory for log files. Default: `./logs` |
| `SPOTIFY_TEST_PLAYLIST_ID` | For `--test` | Spotify playlist ID to use in test mode |
| `TIDAL_TEST_PLAYLIST_ID` | For `--test` | Tidal playlist UUID to use in test mode |
| `TEST_MODE` | No | Set to `true` to enable test mode without the `--test` flag |
| `SPOTIFY_CACHE` | CI only | Full contents of `.cache` — used by GitHub Actions |
| `TIDAL_SESSION` | CI only | Full contents of `tidal_session.json` — used by GitHub Actions |

---

## Reading the email report

The report has four sections:

- **Summary** — counts of added, skipped, and uncertain tracks
- **Added to Tidal / Added to Spotify** — tracks successfully synced, with source album and matched album shown side by side so you can spot any wrong matches
- **Needs Manual Review** — tracks the script was not confident about, showing the best candidate found, its album, the match score, and a note explaining why it was flagged (e.g. _candidate appears to be a different version_)

Tracks in the "Needs Manual Review" section are never written to either playlist — you add them manually if the candidate looks right.

---

## Troubleshooting

**`Missing required environment variables`** — check that `.env` exists and all required keys have values. Make sure there are no trailing spaces.

**Tidal `RuntimeError: Tidal authentication failed`** — delete `tidal_session.json` and re-run to go through the login flow again.

**Spotify `SpotifyOauthError`** — delete `.cache` and re-run. A browser window will open to re-authenticate.

**Track not being added (stuck as UNCERTAIN)** — check the email report for the best candidate and its score. If it's a version mismatch (live vs studio), the variant penalty is working correctly — add it manually. If it looks like a legitimate match with a low score, the album names may be very different between platforms; add it manually and it will be detected as already in sync on the next run.

**GitHub Actions: `No such file or directory: '.cache'`** — the `SPOTIFY_CACHE` secret is missing or empty. Paste the full contents of your local `.cache` file as the secret value.

**Email not arriving** — verify that `RESEND_FROM` uses a domain you have verified in Resend. Check the Resend dashboard logs for delivery status.
