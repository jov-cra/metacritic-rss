"""
trailer.py — the official YouTube trailer of a new item, via TMDB (25.10.2026).

The TMDB id comes from the JustWatch match that streaming.py already makes (JustWatch
returns `externalIds.tmdbId`), so there is no second title search. Only official
YouTube videos of type Trailer or Teaser count; nothing found -> no link at all
(Johannes 25.10.2026: "wenn es bei YouTube nichts gibt, dann lass es weg").
No short cuts: a candidate under MIN_SECONDS (YouTube's own length) is skipped (Johannes
25.10.2026: a 15-second "Emmy Trailer" for Mare of Easttown "sagt einem gar nichts"); when
YouTube's length can't be read, names like "TV Spot" or "15" are skipped instead.

Frozen at emit like the streaming line: one lookup when the item is emitted, never
again. Supplementary: a failed lookup never stops the feed, but several failures in a
row (or a missing/rejected token) set a flag that turns the run red after the commit.
The token only ever goes into the Authorization header — never into a URL, an
exception text or the state.
"""
from __future__ import annotations

import re

try:
    import requests
except ImportError:  # keeps the module importable for tests without requests
    requests = None

API = "https://api.themoviedb.org/3"
VERSION = 2
MIN_SECONDS = 60                              # shorter = spot/teaser cut, not a trailer
SHORT_RE = re.compile(r"\b(?:15|30)\s*(?:s|sec|secs|seconds)?\b|\bspots?\b|\bemmy\b|\bfyc\b|\bclip\b", re.I)
KEY_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")   # a YouTube video id
LANGS = ("en", "de")                          # preference; other languages only as last resort


class TrailerError(RuntimeError):
    """The lookup itself failed (network, HTTP, unexpected shape)."""


class AuthError(TrailerError):
    """TMDB rejected the token (401/403): nothing will work until it is fixed."""


def fetch_videos(path: str, token: str, timeout: float = 12.0) -> list:
    """GET /<path>/videos -> results list. 404 -> [] (TMDB has no such title/season)."""
    try:
        resp = requests.get(f"{API}/{path}/videos",
                            params={"include_video_language": "en,de,null"},
                            headers={"Authorization": f"Bearer {token}", "accept": "application/json"},
                            timeout=timeout)
    except Exception as exc:  # network
        raise TrailerError(f"{path}: {type(exc).__name__}") from exc
    if resp.status_code in (401, 403):
        raise AuthError(f"TMDB rejected the token (HTTP {resp.status_code})")
    if resp.status_code == 404:
        return []
    if resp.status_code != 200:
        raise TrailerError(f"{path}: HTTP {resp.status_code}")
    try:
        data = resp.json()
        results = data["results"]
    except (ValueError, KeyError, TypeError) as exc:
        raise TrailerError(f"{path}: unexpected response shape") from exc
    if not isinstance(results, list):
        raise TrailerError(f"{path}: results is not a list")
    return results


def ranked(videos: list, season=None) -> list:
    """All official YouTube trailers/teasers, best first (see pick)."""
    ok = []
    for v in videos:
        if not isinstance(v, dict):
            continue
        if v.get("site") != "YouTube" or v.get("official") is not True:
            continue
        if v.get("type") not in ("Trailer", "Teaser") or not KEY_RE.match(str(v.get("key") or "")):
            continue
        if season is not None and not re.search(rf"\bseason\s*{season}\b|\bs0?{season}\b",
                                                str(v.get("name") or ""), re.I):
            continue
        ok.append(v)
    lang = {code: i for i, code in enumerate(LANGS)}
    # stable sorts, last one wins: Trailer before Teaser, en before de, then newest, then id
    ok.sort(key=lambda v: v["key"])
    ok.sort(key=lambda v: str(v.get("published_at") or ""), reverse=True)
    ok.sort(key=lambda v: (v.get("type") != "Trailer", lang.get(v.get("iso_639_1"), len(LANGS))))
    return ok


def pick(videos: list, season=None) -> str | None:
    """Best official YouTube trailer/teaser -> video id, or None (no length check).
    season (for series-level videos of a season item): the name must mention it."""
    r = ranked(videos, season)
    return r[0]["key"] if r else None


def youtube_length(key: str, timeout: float = 10.0):
    """Length in seconds from the YouTube watch page, or None (blocked, consent page, changed)."""
    if requests is None or not KEY_RE.match(key or ""):
        return None
    try:
        resp = requests.get(f"https://www.youtube.com/watch?v={key}", timeout=timeout,
                            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0)",
                                     "Accept-Language": "en-US,en;q=0.9"})
        m = re.search(r'"lengthSeconds":"(\d+)"', resp.text if resp.status_code == 200 else "")
        return int(m.group(1)) if m else None
    except Exception:
        return None


def choose(cands: list, length=youtube_length, tries: int = 5):
    """First candidate of at least MIN_SECONDS; if no length is readable, the first whose
    name doesn't look like a spot. None when only short cuts exist."""
    fallback = None
    for v in cands[:tries]:
        n = length(v["key"])
        if n is not None:
            if n >= MIN_SECONDS:
                return v["key"]
            continue
        if fallback is None and not SHORT_RE.search(str(v.get("name") or "")):
            fallback = v["key"]
    return fallback


def lookup(tmdb_id: str, media: str, season, token: str, fetch=fetch_videos, length=youtube_length) -> str | None:
    """-> YouTube id or None. Raises TrailerError/AuthError on failure.
    movie: /movie/<id>; show: /tv/<id>; season N: /tv/<id>/season/N, else the show's
    videos that name season N (for season 1 any show trailer)."""
    if not re.fullmatch(r"\d+", str(tmdb_id or "")):
        return None
    if media == "movie":
        return choose(ranked(fetch(f"movie/{tmdb_id}", token)), length)
    if season is None:
        return choose(ranked(fetch(f"tv/{tmdb_id}", token)), length)
    cands = ranked(fetch(f"tv/{tmdb_id}/season/{int(season)}", token))
    show = fetch(f"tv/{tmdb_id}", token)
    cands += ranked(show) if int(season) == 1 else ranked(show, season=int(season))
    seen, uniq = set(), []
    for v in cands:
        if v["key"] not in seen:
            seen.add(v["key"])
            uniq.append(v)
    return choose(uniq, length)


def link_html(key: str) -> str:
    """The line appended to the item description (raw HTML; the feed escapes it once)."""
    if not KEY_RE.match(key or ""):
        return ""
    return f'<a href="https://www.youtube.com/watch?v={key}">▶ Trailer (YouTube)</a>'
