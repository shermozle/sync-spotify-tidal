#!/usr/bin/env python3
"""
Weekly Spotify <-> Tidal playlist sync.

Usage:
  python sync.py           # sync real playlists
  python sync.py --test    # write proposed changes to test playlists instead

First run: interactive browser/URL auth for both platforms.
Subsequent runs: fully non-interactive via cached tokens.
GitHub Actions: set SPOTIFY_CACHE and TIDAL_SESSION secrets.
"""

import argparse
import datetime
import difflib
import logging
import os
import re
import traceback
from dataclasses import dataclass
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
TIDAL_SESSION_FILE = Path("tidal_session.json")
SPOTIFY_CACHE_FILE = ".cache"

SPOTIFY_SCOPES = (
    "playlist-read-private "
    "playlist-modify-private "
    "playlist-modify-public"
)

# Keywords that indicate a non-studio variant of a track.
# Checked against raw title + album text (before any normalisation).
# If a source track lacks these but the candidate has them, a penalty is applied
# to prevent studio recordings from being matched against live/remix/demo versions.
_VARIANT_KEYWORDS = [
    "live", "remix", "acoustic", "demo", "cover", "instrumental",
    "extended", "reprise", "a cappella", "acapella", "unplugged",
]

# Composite score weights: title carries most of the signal; album disambiguates.
_W_TITLE = 0.65
_W_ALBUM = 0.35
# Score penalty when source is a studio cut but candidate is a variant.
_VARIANT_PENALTY = 0.4


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class TrackInfo:
    title: str
    artist: str
    album: str
    platform_id: str   # Spotify URI ("spotify:track:...") or Tidal int ID as str
    platform: str      # "spotify" | "tidal"

    def norm(self) -> str:
        """
        Gentle normalisation for deduplication and SequenceMatcher comparison.
        Strips feat./ft., punctuation, and collapses whitespace.
        Deliberately keeps live/remix/acoustic/etc. so they affect the score.
        """
        return _normalize(self.artist + " " + self.title)


@dataclass
class SyncAction:
    action: str                    # "ADDED" | "SKIPPED" | "UNCERTAIN"
    direction: str                 # "spotify→tidal" | "tidal→spotify"
    source: TrackInfo
    matched: Optional[TrackInfo]   # Best candidate found (None if no results)
    score: Optional[float]         # Composite score 0–1 (None for SKIPPED)
    note: str = ""


# ---------------------------------------------------------------------------
# Normalisation and scoring
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    """
    Strip feat/ft/featuring and everything after it, then remove punctuation
    and collapse whitespace. Keeps version qualifiers (live, remix, ...) so
    they show up in the SequenceMatcher comparison.
    """
    text = text.lower()
    text = re.sub(r"\b(?:feat\.?|ft\.?|featuring)\b.*", "", text)
    text = re.sub(r"[^\w\s]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _is_variant(track: TrackInfo) -> bool:
    """True if the track's title or album contains a variant keyword."""
    haystack = (track.title + " " + track.album).lower()
    return any(kw in haystack for kw in _VARIANT_KEYWORDS)


def _score_match(source: TrackInfo, candidate: TrackInfo) -> float:
    """
    Composite similarity score in [0, 1].

      65% — artist+title similarity (feat. stripped, qualifiers kept)
      35% — album name similarity

    A penalty of 0.4 is subtracted when the source appears to be a studio
    recording (no variant keyword in title/album) but the candidate is a
    variant (live, remix, acoustic, demo, ...).  This reliably pushes such
    false matches below the 0.85 threshold even when the title is identical.

    Examples:
      "Song" (studio) vs "Song (Live at X)" (live album)  → ~0.3  UNCERTAIN
      "Song" (studio) vs "Song" (same album, remaster)    → ~0.95 ADDED
      "Song" (studio) vs "Song" (compilation album)       → ~0.86 ADDED
    """
    title_sim = difflib.SequenceMatcher(
        None, source.norm(), candidate.norm()
    ).ratio()

    album_sim = difflib.SequenceMatcher(
        None,
        _normalize(source.album),
        _normalize(candidate.album),
    ).ratio()

    variant_penalty = _VARIANT_PENALTY if (not _is_variant(source) and _is_variant(candidate)) else 0.0

    return max(0.0, _W_TITLE * title_sim + _W_ALBUM * album_sim - variant_penalty)


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
    config["SPOTIFY_CACHE"] = os.environ.get("SPOTIFY_CACHE", "")
    config["TIDAL_SESSION"] = os.environ.get("TIDAL_SESSION", "")
    config["TEST_MODE"] = os.environ.get("TEST_MODE", "").lower() in ("1", "true", "yes")
    config["SPOTIFY_TEST_PLAYLIST_ID"] = os.environ.get("SPOTIFY_TEST_PLAYLIST_ID", "")
    config["TIDAL_TEST_PLAYLIST_ID"] = os.environ.get("TIDAL_TEST_PLAYLIST_ID", "")

    missing = [k for k in required if not config[k]]
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
    return config


# ---------------------------------------------------------------------------
# CI credential bootstrap
# ---------------------------------------------------------------------------

def setup_credentials_from_env(config: dict) -> None:
    """Write token files from env vars when running on CI."""
    if config["SPOTIFY_CACHE"]:
        Path(SPOTIFY_CACHE_FILE).write_text(config["SPOTIFY_CACHE"])
    if config["TIDAL_SESSION"]:
        TIDAL_SESSION_FILE.write_text(config["TIDAL_SESSION"])


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
                album=t["album"]["name"],
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
            album=track.album.name,
            platform_id=str(track.id),
            platform="tidal",
        )
        for track in playlist.tracks()
    ]


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def search_spotify(sp: spotipy.Spotify, track: TrackInfo) -> list[TrackInfo]:
    query = f"artist:{track.artist} track:{track.title}"
    results = sp.search(q=query, type="track", limit=5)
    return [
        TrackInfo(
            title=item["name"],
            artist=item["artists"][0]["name"],
            album=item["album"]["name"],
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
            album=t.album.name,
            platform_id=str(t.id),
            platform="tidal",
        )
        for t in results.get("tracks", [])
    ]


def find_best_match(
    source: TrackInfo,
    candidates: list[TrackInfo],
) -> tuple[Optional[TrackInfo], float]:
    """Return (best_candidate, composite_score). Returns (None, 0.0) if no candidates."""
    if not candidates:
        return None, 0.0
    best: Optional[TrackInfo] = None
    best_score = 0.0
    for c in candidates:
        s = _score_match(source, c)
        if s > best_score:
            best, best_score = c, s
    return best, best_score


# ---------------------------------------------------------------------------
# Add tracks
# ---------------------------------------------------------------------------

def add_to_tidal(
    session: tidalapi.Session,
    playlist_id: str,
    track: TrackInfo,
) -> None:
    session.playlist(playlist_id).add([int(track.platform_id)])


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
    target_spotify_playlist_id: str,
    target_tidal_playlist_id: str,
    logger: logging.Logger,
) -> list[SyncAction]:
    actions: list[SyncAction] = []
    # Use gentle-normalised key for deduplication; keeps qualifiers so
    # a live version and studio version of the same song are treated as distinct.
    target_keys = {t.norm() for t in target_tracks}

    for source in source_tracks:
        if source.norm() in target_keys:
            actions.append(SyncAction(
                action="SKIPPED",
                direction=direction,
                source=source,
                matched=None,
                score=None,
                note="already in target playlist",
            ))
            continue

        candidates = (
            search_tidal(tidal_session, source)
            if direction == "spotify→tidal"
            else search_spotify(sp, source)
        )

        best, score = find_best_match(source, candidates)

        if not candidates:
            logger.debug("No search results: %s — %s", source.artist, source.title)
            actions.append(SyncAction(
                action="UNCERTAIN",
                direction=direction,
                source=source,
                matched=None,
                score=0.0,
                note="no search results found on target platform",
            ))
            continue

        if score >= HIGH_CONF_THRESHOLD:
            try:
                if direction == "spotify→tidal":
                    add_to_tidal(tidal_session, target_tidal_playlist_id, best)
                else:
                    add_to_spotify(sp, target_spotify_playlist_id, best)
                actions.append(SyncAction(
                    action="ADDED",
                    direction=direction,
                    source=source,
                    matched=best,
                    score=score,
                ))
                logger.info(
                    'ADDED  [%s]  "%s" / %s  →  "%s" / %s  (score %.0f%%)',
                    direction,
                    source.title, source.album,
                    best.title, best.album,
                    score * 100,
                )
            except Exception as exc:
                actions.append(SyncAction(
                    action="UNCERTAIN",
                    direction=direction,
                    source=source,
                    matched=best,
                    score=score,
                    note=f"API error when adding: {exc}",
                ))
                logger.warning(
                    'FAILED to add [%s] "%s" by %s: %s',
                    direction, source.title, source.artist, exc,
                )
        else:
            variant_note = (
                " — candidate appears to be a different version"
                if best and _is_variant(best) and not _is_variant(source)
                else ""
            )
            actions.append(SyncAction(
                action="UNCERTAIN",
                direction=direction,
                source=source,
                matched=best,
                score=score,
                note=f"score {score:.0%} below threshold {HIGH_CONF_THRESHOLD:.0%}{variant_note}",
            ))
            logger.debug(
                'UNCERTAIN [%s] "%s" — best: "%s" / %s  score=%.0f%%',
                direction, source.title,
                best.title if best else "none",
                best.album if best else "",
                score * 100,
            )

    return actions


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logger(log_dir: str) -> tuple[logging.Logger, Path]:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d_%H-%M-%S")
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


def write_log_report(
    logger: logging.Logger,
    actions: list[SyncAction],
    test_mode: bool,
) -> None:
    added_s2t = [a for a in actions if a.action == "ADDED" and a.direction == "spotify→tidal"]
    added_t2s = [a for a in actions if a.action == "ADDED" and a.direction == "tidal→spotify"]
    skipped   = [a for a in actions if a.action == "SKIPPED"]
    uncertain = [a for a in actions if a.action == "UNCERTAIN"]

    mode_tag = " [TEST MODE]" if test_mode else ""
    logger.info("=" * 60)
    logger.info("SYNC SUMMARY%s", mode_tag)
    logger.info("=" * 60)

    logger.info("--- ADDED (Spotify → Tidal): %d ---", len(added_s2t))
    for a in added_s2t:
        logger.info(
            '  "%s" / %s  →  "%s" / %s  (score: %.0f%%)',
            a.source.title, a.source.album,
            a.matched.title, a.matched.album,
            (a.score or 0) * 100,
        )

    logger.info("--- ADDED (Tidal → Spotify): %d ---", len(added_t2s))
    for a in added_t2s:
        logger.info(
            '  "%s" / %s  →  "%s" / %s  (score: %.0f%%)',
            a.source.title, a.source.album,
            a.matched.title, a.matched.album,
            (a.score or 0) * 100,
        )

    logger.info("--- SKIPPED (already in sync): %d ---", len(skipped))
    for a in skipped:
        logger.info('  "%s" by %s  [%s]', a.source.title, a.source.artist, a.source.album)

    logger.info("--- UNCERTAIN (needs manual review): %d ---", len(uncertain))
    for a in uncertain:
        match_str = (
            f'best candidate: "{a.matched.title}" / {a.matched.album} ({(a.score or 0):.0%})'
            if a.matched else "no results found"
        )
        logger.info(
            '  [%s] "%s" / %s — %s — %s',
            a.direction, a.source.title, a.source.album, match_str, a.note,
        )

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


def _added_rows(items: list[SyncAction]) -> str:
    rows = []
    for a in items:
        score = f"{(a.score or 0):.0%}"
        rows.append(
            f"<tr>"
            f"<td>{_esc(a.source.title)}</td>"
            f"<td>{_esc(a.source.artist)}</td>"
            f"<td>{_esc(a.source.album)}</td>"
            f"<td>{_esc(a.matched.title if a.matched else '')}</td>"
            f"<td>{_esc(a.matched.album if a.matched else '')}</td>"
            f'<td class="conf-high">{score}</td>'
            f"</tr>"
        )
    return "\n".join(rows)


def _uncertain_rows(items: list[SyncAction]) -> str:
    rows = []
    for a in items:
        if a.matched:
            cand_title  = _esc(a.matched.title)
            cand_album  = _esc(a.matched.album)
            cand_artist = _esc(a.matched.artist)
            score = f"{(a.score or 0):.0%}"
        else:
            cand_title = cand_album = cand_artist = "&#8212;"
            score = "no results"
        rows.append(
            f"<tr>"
            f"<td>{_esc(a.source.title)}</td>"
            f"<td>{_esc(a.source.artist)}</td>"
            f"<td>{_esc(a.source.album)}</td>"
            f"<td>{_esc(a.direction)}</td>"
            f"<td>{cand_title}</td>"
            f"<td>{cand_artist}</td>"
            f"<td>{cand_album}</td>"
            f'<td class="conf-low">{score}</td>'
            f"<td>{_esc(a.note)}</td>"
            f"</tr>"
        )
    return "\n".join(rows)


def _added_section(heading: str, items: list[SyncAction]) -> str:
    if not items:
        return ""
    return f"""
    <h2>{_esc(heading)} ({len(items)})</h2>
    <table>
      <tr>
        <th>Source Track</th><th>Artist</th><th>Source Album</th>
        <th>Matched Track</th><th>Matched Album</th><th>Score</th>
      </tr>
      {_added_rows(items)}
    </table>"""


def build_html_report(
    actions: list[SyncAction],
    run_timestamp: str,
    log_file_path: Path,
    test_mode: bool,
) -> str:
    added_to_tidal   = [a for a in actions if a.action == "ADDED"    and a.direction == "spotify→tidal"]
    added_to_spotify = [a for a in actions if a.action == "ADDED"    and a.direction == "tidal→spotify"]
    skipped          = [a for a in actions if a.action == "SKIPPED"]
    uncertain        = [a for a in actions if a.action == "UNCERTAIN"]

    test_banner = ""
    if test_mode:
        test_banner = """
    <div style="background:#fff3cd;border:1px solid #ffc107;padding:12px 16px;
                border-radius:4px;margin-bottom:24px;color:#856404;font-weight:bold;">
      &#9888; TEST MODE &#8212; changes written to test playlists, not your main playlists.
      Review the matches below and confirm or add manually.
    </div>"""

    uncertain_section = ""
    if uncertain:
        uncertain_section = f"""
    <h2>&#9888; Needs Manual Review ({len(uncertain)})</h2>
    <p>These tracks were not matched with sufficient confidence.
       Check the best candidate and add manually if it looks right.</p>
    <table>
      <tr>
        <th>Source Track</th><th>Artist</th><th>Source Album</th>
        <th>Direction</th>
        <th>Best Candidate</th><th>Candidate Artist</th><th>Candidate Album</th>
        <th>Score</th><th>Note</th>
      </tr>
      {_uncertain_rows(uncertain)}
    </table>"""

    page_title = "Spotify &#8596; Tidal Sync Report" + (" (TEST)" if test_mode else "")

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body  {{ font-family: Arial, sans-serif; font-size: 14px; color: #222;
           max-width: 1100px; margin: 0 auto; padding: 24px; }}
  h1    {{ color: #1DB954; margin-bottom: 4px; }}
  h2    {{ color: #333; border-bottom: 2px solid #eee; padding-bottom: 4px; margin-top: 32px; }}
  table {{ border-collapse: collapse; width: 100%; margin-bottom: 16px; }}
  th    {{ background: #f4f4f4; text-align: left; padding: 8px 12px; font-weight: 600; }}
  td    {{ padding: 8px 12px; border-bottom: 1px solid #eee; vertical-align: top; }}
  tr:last-child td {{ border-bottom: none; }}
  .num       {{ font-size: 18px; font-weight: bold; }}
  .conf-high {{ color: #155724; }}
  .conf-low  {{ color: #856404; font-weight: bold; }}
  .footer    {{ color: #aaa; font-size: 12px; margin-top: 40px;
               border-top: 1px solid #eee; padding-top: 12px; }}
</style>
</head>
<body>

<h1>{page_title}</h1>
<p>Run at: <strong>{_esc(run_timestamp)}</strong></p>
<p style="color:#888">Log: <code>{_esc(str(log_file_path))}</code></p>

{test_banner}

<h2>Summary</h2>
<table style="width:auto">
  <tr><th>Added to Tidal</th>              <td class="num">{len(added_to_tidal)}</td></tr>
  <tr><th>Added to Spotify</th>            <td class="num">{len(added_to_spotify)}</td></tr>
  <tr><th>Already in sync (skipped)</th>   <td class="num">{len(skipped)}</td></tr>
  <tr><th>&#9888; Uncertain &#8212; needs review</th>
                                           <td class="num">{len(uncertain)}</td></tr>
</table>

{_added_section("Added to Tidal", added_to_tidal)}
{_added_section("Added to Spotify", added_to_spotify)}
{uncertain_section}

<p class="footer">
  Generated automatically by sync.py &mdash;
  <code>python sync.py</code> or <code>python sync.py --test</code>
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

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sync a playlist between Spotify and Tidal.")
    p.add_argument(
        "--test",
        action="store_true",
        help=(
            "Propose changes to test playlists (SPOTIFY_TEST_PLAYLIST_ID / "
            "TIDAL_TEST_PLAYLIST_ID) instead of the real playlists. "
            "Reads the real playlists to determine what's missing, then writes "
            "proposed additions to the test playlists so you can review them."
        ),
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    try:
        config = load_config()
        test_mode = args.test or config["TEST_MODE"]

        logger, log_path = setup_logger(config["LOG_DIR"])
        run_start = datetime.datetime.now(datetime.UTC)
        mode_label = " [TEST MODE]" if test_mode else ""
        logger.info(
            "Sync run started at %s UTC%s",
            run_start.strftime("%Y-%m-%d %H:%M:%S"), mode_label,
        )

        if test_mode:
            if not config["SPOTIFY_TEST_PLAYLIST_ID"] or not config["TIDAL_TEST_PLAYLIST_ID"]:
                raise ValueError(
                    "--test requires SPOTIFY_TEST_PLAYLIST_ID and "
                    "TIDAL_TEST_PLAYLIST_ID to be set in .env"
                )
            target_spotify_id = config["SPOTIFY_TEST_PLAYLIST_ID"]
            target_tidal_id   = config["TIDAL_TEST_PLAYLIST_ID"]
            logger.info(
                "Test mode: writing to Spotify test playlist %s and Tidal test playlist %s",
                target_spotify_id, target_tidal_id,
            )
        else:
            target_spotify_id = config["SPOTIFY_PLAYLIST_ID"]
            target_tidal_id   = config["TIDAL_PLAYLIST_ID"]

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
            target_spotify_playlist_id=target_spotify_id,
            target_tidal_playlist_id=target_tidal_id,
            logger=logger,
        )

        # Re-read the real Tidal playlist so the reverse pass knows what's
        # truly there (test-playlist additions don't count as "synced").
        tidal_tracks_updated = get_tidal_tracks(tidal_session, config["TIDAL_PLAYLIST_ID"])

        logger.info("Syncing Tidal → Spotify...")
        actions_t2s = sync_direction(
            sp, tidal_session,
            source_tracks=tidal_tracks_updated,
            target_tracks=spotify_tracks,
            direction="tidal→spotify",
            target_spotify_playlist_id=target_spotify_id,
            target_tidal_playlist_id=target_tidal_id,
            logger=logger,
        )

        all_actions = actions_s2t + actions_t2s
        write_log_report(logger, all_actions, test_mode)

        run_ts_str = run_start.strftime("%Y-%m-%d %H:%M UTC")
        added_count    = sum(1 for a in all_actions if a.action == "ADDED")
        uncertain_count = sum(1 for a in all_actions if a.action == "UNCERTAIN")
        test_tag = " [TEST]" if test_mode else ""
        subject = (
            f"Spotify↔Tidal Sync{test_tag}: {added_count} added, "
            f"{uncertain_count} need review — {run_ts_str}"
        )
        html = build_html_report(all_actions, run_ts_str, log_path, test_mode)
        send_email_report(config, subject, html, logger)

        logger.info("Sync run complete.")

    except Exception:
        logging.getLogger("sync").error("Sync failed:\n%s", traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
