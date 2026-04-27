#!/usr/bin/env python3
"""
Weekly Spotify <-> Tidal playlist sync.

First run: interactive browser/URL auth for both platforms.
Subsequent runs: fully non-interactive via cached tokens.
GitHub Actions: set SPOTIFY_CACHE and TIDAL_SESSION secrets.
"""

import datetime
import difflib
import logging
import os
import re
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import resend
import spotipy
import tidalapi
from dotenv import load_dotenv
from spotipy.oauth2 import SpotifyOAuth


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HIGH_CONF_THRESHOLD = 0.85
TIDAL_SESSION_FILE = "tidal_session.json"
SPOTIFY_CACHE_FILE = ".cache"

SPOTIFY_SCOPES = (
    "playlist-read-private "
    "playlist-modify-private "
    "playlist-modify-public"
)

# Sorted longest-first so multi-word phrases match before their substrings
_NOISE_WORDS = sorted(
    [
        "remaster", "remastered", "remix", "radio edit", "version",
        "single version", "original mix", "acoustic", "live", "edit",
    ],
    key=len,
    reverse=True,
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class TrackInfo:
    title: str
    artist: str
    platform_id: str   # Spotify URI ("spotify:track:...") or Tidal int ID as str
    platform: str      # "spotify" | "tidal"
    norm_key: str = field(init=False)

    def __post_init__(self) -> None:
        self.norm_key = normalize(self.artist + " " + self.title)


@dataclass
class SyncAction:
    action: str                    # "ADDED" | "SKIPPED" | "UNCERTAIN"
    direction: str                 # "spotify→tidal" | "tidal→spotify"
    source: TrackInfo
    matched: Optional[TrackInfo]   # The platform match (None if not found)
    confidence: Optional[float]    # SequenceMatcher ratio (None for SKIPPED)
    note: str = ""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    load_dotenv()
    required = [
        "SPOTIFY_CLIENT_ID",
        "SPOTIFY_CLIENT_SECRET",
        "SPOTIFY_REDIRECT_URI",
        "SPOTIFY_PLAYLIST_ID",
        "TIDAL_PLAYLIST_ID",
        "RESEND_API_KEY",
        "RESEND_FROM",
        "RESEND_TO",
    ]
    config = {k: os.environ.get(k, "") for k in required}
    config["LOG_DIR"] = os.environ.get("LOG_DIR", "./logs")
    # Optional: token file contents injected by CI via secrets
    config["SPOTIFY_CACHE"] = os.environ.get("SPOTIFY_CACHE", "")
    config["TIDAL_SESSION"] = os.environ.get("TIDAL_SESSION", "")

    missing = [k for k in required if not config[k]]
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
    return config


# ---------------------------------------------------------------------------
# CI credential bootstrap
# ---------------------------------------------------------------------------

def setup_credentials_from_env(config: dict) -> None:
    """
    On GitHub Actions the token files are passed as secrets.
    Write them to disk before the auth functions run.
    """
    if config["SPOTIFY_CACHE"]:
        Path(SPOTIFY_CACHE_FILE).write_text(config["SPOTIFY_CACHE"])
    if config["TIDAL_SESSION"]:
        Path(TIDAL_SESSION_FILE).write_text(config["TIDAL_SESSION"])


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def get_spotify_client(config: dict) -> spotipy.Spotify:
    auth = SpotifyOAuth(
        client_id=config["SPOTIFY_CLIENT_ID"],
        client_secret=config["SPOTIFY_CLIENT_SECRET"],
        redirect_uri=config["SPOTIFY_REDIRECT_URI"],
        scope=SPOTIFY_SCOPES,
        cache_path=SPOTIFY_CACHE_FILE,
        open_browser=True,
    )
    return spotipy.Spotify(auth_manager=auth)


def get_tidal_session(config: dict) -> tidalapi.Session:
    session = tidalapi.Session()
    try:
        loaded = session.load_session_from_file(TIDAL_SESSION_FILE)
    except Exception:
        loaded = False

    if loaded and session.check_login():
        return session

    # First run or refresh failure — interactive PKCE login
    session.login_oauth_simple()
    session.save_session_to_file(TIDAL_SESSION_FILE)
    if not session.check_login():
        raise RuntimeError("Tidal authentication failed after login attempt.")
    return session


# ---------------------------------------------------------------------------
# Fetch playlist tracks
# ---------------------------------------------------------------------------

def get_spotify_tracks(sp: spotipy.Spotify, playlist_id: str) -> list[TrackInfo]:
    tracks: list[TrackInfo] = []
    offset = 0
    while True:
        results = sp.playlist_items(playlist_id, limit=50, offset=offset)
        for item in results["items"]:
            t = item.get("track")
            if not t or t.get("is_local"):
                continue
            tracks.append(TrackInfo(
                title=t["name"],
                artist=t["artists"][0]["name"],
                platform_id=t["uri"],
                platform="spotify",
            ))
        if results["next"] is None:
            break
        offset += 50
    return tracks


def get_tidal_tracks(session: tidalapi.Session, playlist_id: str) -> list[TrackInfo]:
    playlist = session.playlist(playlist_id)
    return [
        TrackInfo(
            title=track.name,
            artist=track.artist.name,
            platform_id=str(track.id),
            platform="tidal",
        )
        for track in playlist.tracks()
    ]


# ---------------------------------------------------------------------------
# Normalization and matching
# ---------------------------------------------------------------------------

def normalize(text: str) -> str:
    text = text.lower()
    # Strip parenthesized / bracketed content: "(Remastered 2011)", "[feat. X]"
    text = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]\s*", " ", text)
    # Strip "feat / ft / featuring ..." to end of clause
    text = re.sub(r"\b(?:feat\.?|ft\.?|featuring)\b.*", "", text)
    # Strip noise words (longest first to avoid partial matches)
    for word in _NOISE_WORDS:
        text = text.replace(word, " ")
    # Strip punctuation, collapse whitespace
    text = re.sub(r"[^\w\s]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def search_spotify(sp: spotipy.Spotify, track: TrackInfo) -> list[TrackInfo]:
    query = f"artist:{track.artist} track:{track.title}"
    results = sp.search(q=query, type="track", limit=5)
    return [
        TrackInfo(
            title=item["name"],
            artist=item["artists"][0]["name"],
            platform_id=item["uri"],
            platform="spotify",
        )
        for item in results["tracks"]["items"]
    ]


def search_tidal(session: tidalapi.Session, track: TrackInfo) -> list[TrackInfo]:
    query = f"{track.artist} {track.title}"
    results = session.search(query, models=[tidalapi.Track], limit=5)
    return [
        TrackInfo(
            title=t.name,
            artist=t.artist.name,
            platform_id=str(t.id),
            platform="tidal",
        )
        for t in results.get("tracks", [])
    ]


def find_best_match(
    source: TrackInfo,
    candidates: list[TrackInfo],
) -> tuple[Optional[TrackInfo], float]:
    """Return (best_candidate, similarity_ratio). Returns (None, 0.0) if no candidates."""
    if not candidates:
        return None, 0.0
    best: Optional[TrackInfo] = None
    best_ratio = 0.0
    for c in candidates:
        ratio = difflib.SequenceMatcher(None, source.norm_key, c.norm_key).ratio()
        if ratio > best_ratio:
            best, best_ratio = c, ratio
    return best, best_ratio


# ---------------------------------------------------------------------------
# Add tracks
# ---------------------------------------------------------------------------

def add_to_tidal(
    session: tidalapi.Session,
    playlist_id: str,
    track: TrackInfo,
) -> None:
    playlist = session.playlist(playlist_id)
    playlist.add([int(track.platform_id)])


def add_to_spotify(
    sp: spotipy.Spotify,
    playlist_id: str,
    track: TrackInfo,
) -> None:
    sp.playlist_add_items(playlist_id, [track.platform_id])


# ---------------------------------------------------------------------------
# Sync one direction
# ---------------------------------------------------------------------------

def sync_direction(
    sp: spotipy.Spotify,
    tidal_session: tidalapi.Session,
    source_tracks: list[TrackInfo],
    target_tracks: list[TrackInfo],
    direction: str,
    spotify_playlist_id: str,
    tidal_playlist_id: str,
    logger: logging.Logger,
) -> list[SyncAction]:
    actions: list[SyncAction] = []
    target_keys = {t.norm_key for t in target_tracks}

    for source in source_tracks:
        if source.norm_key in target_keys:
            actions.append(SyncAction(
                action="SKIPPED",
                direction=direction,
                source=source,
                matched=None,
                confidence=None,
                note="already in target playlist",
            ))
            continue

        if direction == "spotify→tidal":
            candidates = search_tidal(tidal_session, source)
        else:
            candidates = search_spotify(sp, source)

        best, ratio = find_best_match(source, candidates)

        if not candidates:
            logger.debug("No search results: %s — %s", source.artist, source.title)
            actions.append(SyncAction(
                action="UNCERTAIN",
                direction=direction,
                source=source,
                matched=None,
                confidence=0.0,
                note="no search results found on target platform",
            ))
            continue

        if ratio >= HIGH_CONF_THRESHOLD:
            try:
                if direction == "spotify→tidal":
                    add_to_tidal(tidal_session, tidal_playlist_id, best)
                else:
                    add_to_spotify(sp, spotify_playlist_id, best)
                actions.append(SyncAction(
                    action="ADDED",
                    direction=direction,
                    source=source,
                    matched=best,
                    confidence=ratio,
                ))
                logger.info(
                    "ADDED  [%s]  \"%s\" by %s  (confidence %.0f%%)",
                    direction, source.title, source.artist, ratio * 100,
                )
            except Exception as exc:
                actions.append(SyncAction(
                    action="UNCERTAIN",
                    direction=direction,
                    source=source,
                    matched=best,
                    confidence=ratio,
                    note=f"API error when adding: {exc}",
                ))
                logger.warning(
                    "FAILED to add [%s] \"%s\" by %s: %s",
                    direction, source.title, source.artist, exc,
                )
        else:
            actions.append(SyncAction(
                action="UNCERTAIN",
                direction=direction,
                source=source,
                matched=best,
                confidence=ratio,
                note=f"best match confidence {ratio:.0%} is below threshold {HIGH_CONF_THRESHOLD:.0%}",
            ))
            logger.debug(
                "UNCERTAIN [%s] \"%s\" — best match \"%s\" at %.0f%%",
                direction, source.title, best.title if best else "none", ratio * 100,
            )

    return actions


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logger(log_dir: str) -> tuple[logging.Logger, Path]:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d_%H-%M-%S")
    log_path = Path(log_dir) / f"sync_{ts}.log"

    logger = logging.getLogger("sync")
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(message)s")
    fh = logging.FileHandler(log_path)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger, log_path


def write_log_report(logger: logging.Logger, actions: list[SyncAction]) -> None:
    added_s2t = [a for a in actions if a.action == "ADDED" and a.direction == "spotify→tidal"]
    added_t2s = [a for a in actions if a.action == "ADDED" and a.direction == "tidal→spotify"]
    skipped = [a for a in actions if a.action == "SKIPPED"]
    uncertain = [a for a in actions if a.action == "UNCERTAIN"]

    logger.info("=" * 60)
    logger.info("SYNC SUMMARY")
    logger.info("=" * 60)

    logger.info("--- ADDED (Spotify → Tidal): %d ---", len(added_s2t))
    for a in added_s2t:
        logger.info('  "%s" by %s  (confidence: %.0f%%)',
                    a.source.title, a.source.artist, (a.confidence or 0) * 100)

    logger.info("--- ADDED (Tidal → Spotify): %d ---", len(added_t2s))
    for a in added_t2s:
        logger.info('  "%s" by %s  (confidence: %.0f%%)',
                    a.source.title, a.source.artist, (a.confidence or 0) * 100)

    logger.info("--- SKIPPED (already in sync): %d ---", len(skipped))
    for a in skipped:
        logger.info('  "%s" by %s', a.source.title, a.source.artist)

    logger.info("--- UNCERTAIN (needs manual review): %d ---", len(uncertain))
    for a in uncertain:
        if a.matched:
            match_str = (
                f'best match: "{a.matched.title}" by {a.matched.artist}'
                f" ({(a.confidence or 0):.0%})"
            )
        else:
            match_str = "no results found"
        logger.info('  [%s] "%s" by %s — %s — %s',
                    a.direction, a.source.title, a.source.artist, match_str, a.note)

    logger.info("=" * 60)


# ---------------------------------------------------------------------------
# Email report
# ---------------------------------------------------------------------------

def _esc(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
    )


def _track_rows(items: list[SyncAction]) -> str:
    rows = []
    for a in items:
        conf = f"{(a.confidence or 0):.0%}"
        rows.append(
            f'<tr><td>{_esc(a.source.title)}</td>'
            f'<td>{_esc(a.source.artist)}</td>'
            f'<td class="conf-high">{conf}</td></tr>'
        )
    return "\n".join(rows)


def _uncertain_rows(items: list[SyncAction]) -> str:
    rows = []
    for a in items:
        if a.matched:
            match_title = _esc(a.matched.title)
            match_artist = _esc(a.matched.artist)
            conf = f"{(a.confidence or 0):.0%}"
        else:
            match_title = match_artist = "&#8212;"
            conf = "no results"
        rows.append(
            f"<tr>"
            f"<td>{_esc(a.source.title)}</td>"
            f"<td>{_esc(a.source.artist)}</td>"
            f"<td>{_esc(a.direction)}</td>"
            f"<td>{match_title}</td>"
            f"<td>{match_artist}</td>"
            f'<td class="conf-low">{conf}</td>'
            f"<td>{_esc(a.note)}</td>"
            f"</tr>"
        )
    return "\n".join(rows)


def _changes_section(title: str, items: list[SyncAction]) -> str:
    if not items:
        return ""
    return f"""
    <h2>{_esc(title)} ({len(items)})</h2>
    <table>
      <tr><th>Track</th><th>Artist</th><th>Confidence</th></tr>
      {_track_rows(items)}
    </table>"""


def build_html_report(
    actions: list[SyncAction],
    run_timestamp: str,
    log_file_path: Path,
) -> str:
    added_to_tidal = [a for a in actions if a.action == "ADDED" and a.direction == "spotify→tidal"]
    added_to_spotify = [a for a in actions if a.action == "ADDED" and a.direction == "tidal→spotify"]
    skipped = [a for a in actions if a.action == "SKIPPED"]
    uncertain = [a for a in actions if a.action == "UNCERTAIN"]

    uncertain_section = ""
    if uncertain:
        uncertain_section = f"""
    <h2>&#9888; Needs Manual Review ({len(uncertain)})</h2>
    <p>These tracks could not be matched with sufficient confidence. Please add them manually.</p>
    <table>
      <tr>
        <th>Source Track</th><th>Artist</th><th>Direction</th>
        <th>Best Match Found</th><th>Match Artist</th>
        <th>Confidence</th><th>Note</th>
      </tr>
      {_uncertain_rows(uncertain)}
    </table>"""

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body  {{ font-family: Arial, sans-serif; font-size: 14px; color: #222;
           max-width: 960px; margin: 0 auto; padding: 24px; }}
  h1    {{ color: #1DB954; margin-bottom: 4px; }}
  h2    {{ color: #333; border-bottom: 2px solid #eee; padding-bottom: 4px; margin-top: 32px; }}
  table {{ border-collapse: collapse; width: 100%; margin-bottom: 16px; }}
  th    {{ background: #f4f4f4; text-align: left; padding: 8px 12px; font-weight: 600; }}
  td    {{ padding: 8px 12px; border-bottom: 1px solid #eee; }}
  tr:last-child td {{ border-bottom: none; }}
  .num  {{ font-size: 18px; font-weight: bold; }}
  .conf-high {{ color: #155724; }}
  .conf-low  {{ color: #856404; font-weight: bold; }}
  .footer {{ color: #aaa; font-size: 12px; margin-top: 40px;
             border-top: 1px solid #eee; padding-top: 12px; }}
</style>
</head>
<body>

<h1>Spotify &#8596; Tidal Sync Report</h1>
<p>Run at: <strong>{_esc(run_timestamp)}</strong></p>
<p style="color:#888">Log: <code>{_esc(str(log_file_path))}</code></p>

<h2>Summary</h2>
<table style="width:auto">
  <tr><th>Added to Tidal</th><td class="num">{len(added_to_tidal)}</td></tr>
  <tr><th>Added to Spotify</th><td class="num">{len(added_to_spotify)}</td></tr>
  <tr><th>Already in sync (skipped)</th><td class="num">{len(skipped)}</td></tr>
  <tr><th>&#9888; Uncertain &#8212; needs review</th><td class="num">{len(uncertain)}</td></tr>
</table>

{_changes_section("Added to Tidal", added_to_tidal)}
{_changes_section("Added to Spotify", added_to_spotify)}
{uncertain_section}

<p class="footer">
  Generated automatically by sync.py &mdash;
  re-run with <code>python sync.py</code>
</p>
</body>
</html>"""


def send_email_report(
    config: dict,
    subject: str,
    html: str,
    logger: logging.Logger,
) -> None:
    resend.api_key = config["RESEND_API_KEY"]
    resend.Emails.send({
        "from": config["RESEND_FROM"],
        "to": [config["RESEND_TO"]],
        "subject": subject,
        "html": html,
    })
    logger.info("Email report sent to %s", config["RESEND_TO"])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    try:
        config = load_config()
        logger, log_path = setup_logger(config["LOG_DIR"])
        run_start = datetime.datetime.utcnow()
        logger.info("Sync run started at %s UTC", run_start.strftime("%Y-%m-%d %H:%M:%S"))

        setup_credentials_from_env(config)

        logger.info("Authenticating with Spotify...")
        sp = get_spotify_client(config)

        logger.info("Authenticating with Tidal...")
        tidal_session = get_tidal_session(config)

        logger.info("Fetching Spotify playlist...")
        spotify_tracks = get_spotify_tracks(sp, config["SPOTIFY_PLAYLIST_ID"])
        logger.info("Fetching Tidal playlist...")
        tidal_tracks = get_tidal_tracks(tidal_session, config["TIDAL_PLAYLIST_ID"])
        logger.info(
            "Fetched %d Spotify tracks, %d Tidal tracks",
            len(spotify_tracks), len(tidal_tracks),
        )

        logger.info("Syncing Spotify → Tidal...")
        actions_s2t = sync_direction(
            sp, tidal_session,
            source_tracks=spotify_tracks,
            target_tracks=tidal_tracks,
            direction="spotify→tidal",
            spotify_playlist_id=config["SPOTIFY_PLAYLIST_ID"],
            tidal_playlist_id=config["TIDAL_PLAYLIST_ID"],
            logger=logger,
        )

        # Re-fetch Tidal after additions so the reverse pass doesn't re-flag them
        tidal_tracks_updated = get_tidal_tracks(tidal_session, config["TIDAL_PLAYLIST_ID"])

        logger.info("Syncing Tidal → Spotify...")
        actions_t2s = sync_direction(
            sp, tidal_session,
            source_tracks=tidal_tracks_updated,
            target_tracks=spotify_tracks,
            direction="tidal→spotify",
            spotify_playlist_id=config["SPOTIFY_PLAYLIST_ID"],
            tidal_playlist_id=config["TIDAL_PLAYLIST_ID"],
            logger=logger,
        )

        all_actions = actions_s2t + actions_t2s
        write_log_report(logger, all_actions)

        run_ts_str = run_start.strftime("%Y-%m-%d %H:%M UTC")
        added_count = sum(1 for a in all_actions if a.action == "ADDED")
        uncertain_count = sum(1 for a in all_actions if a.action == "UNCERTAIN")
        subject = (
            f"Spotify↔Tidal Sync: {added_count} added, "
            f"{uncertain_count} need review — {run_ts_str}"
        )
        html = build_html_report(all_actions, run_ts_str, log_path)
        send_email_report(config, subject, html, logger)

        logger.info("Sync run complete.")

    except Exception:
        logging.getLogger("sync").error("Sync failed:\n%s", traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
