"""
trailer.py — the official YouTube trailer of a new item, via TMDB (25.10.2026).

The TMDB id comes from the JustWatch match that streaming.py already makes (JustWatch
returns `externalIds.tmdbId`), so there is no second title search. Only official
YouTube videos of type Trailer or Teaser count; nothing found -> no link at all
(Johannes 25.10.2026: "wenn es bei YouTube nichts gibt, dann lass es weg").

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
VERSION = 1
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


def pick(videos: list, season=None) -> str | None:
    """Best official YouTube trailer/teaser -> video id, or None.
    season (for series-level videos of a season item): the name must mention it."""
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
    if not ok:
        return None
    lang = {code: i for i, code in enumerate(LANGS)}
    # stable sorts, last one wins: Trailer before Teaser, en before de, then newest, then id
    ok.sort(key=lambda v: v["key"])
    ok.sort(key=lambda v: str(v.get("published_at") or ""), reverse=True)
    ok.sort(key=lambda v: (v.get("type") != "Trailer", lang.get(v.get("iso_639_1"), len(LANGS))))
    return ok[0]["key"]


def lookup(tmdb_id: str, media: str, season, token: str, fetch=fetch_videos) -> str | None:
    """-> YouTube id or None. Raises TrailerError/AuthError on failure.
    movie: /movie/<id>; show: /tv/<id>; season N: /tv/<id>/season/N, else the show's
    videos that name season N (for season 1 any show trailer)."""
    if not re.fullmatch(r"\d+", str(tmdb_id or "")):
        return None
    if media == "movie":
        return pick(fetch(f"movie/{tmdb_id}", token))
    if season is None:
        return pick(fetch(f"tv/{tmdb_id}", token))
    key = pick(fetch(f"tv/{tmdb_id}/season/{int(season)}", token))
    if key:
        return key
    show = fetch(f"tv/{tmdb_id}", token)
    return pick(show) if int(season) == 1 else pick(show, season=int(season))


def link_html(key: str) -> str:
    """The line appended to the item description (raw HTML; the feed escapes it once)."""
    if not KEY_RE.match(key or ""):
        return ""
    return f'<a href="https://www.youtube.com/watch?v={key}">▶ Trailer (YouTube)</a>'
