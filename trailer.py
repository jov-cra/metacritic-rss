"""
trailer.py — the official YouTube trailer of a new item, via TMDB (25.10.2026).

The TMDB id comes from the JustWatch match that streaming.py already makes (JustWatch
returns `externalIds.tmdbId`), so there is no second title search. Only official
YouTube videos of type Trailer or Teaser count; nothing found -> no link at all
(Johannes 25.10.2026: "wenn es bei YouTube nichts gibt, dann lass es weg").
No short cuts: a candidate under MIN_SECONDS (YouTube's own length) is skipped (Johannes
25.10.2026: a 15-second "Emmy Trailer" for Mare of Easttown "sagt einem gar nichts"); when
YouTube's length can't be read, names like "TV Spot" or "15" are skipped instead.

Playable in Germany (02.11.2026, Johannes 01.11.2026: "der erste Trailer ist in DE nicht
verfügbar. sowas musst du bitte immer checken"; The Night Of's official trailer is US-only):
a candidate only counts when the watch page's `availableCountries` list names DE and the
page itself reports the video as playable. The runner sits in the US, so the playability
status alone says nothing about Germany; the country list is the same from everywhere.
Fail-closed: no readable page or no country list -> unclear -> no link (the item itself
links to Metacritic). There is no name-only fallback any more when the page can't be read.

Frozen at emit like the streaming line: one lookup when the item is emitted, never
again. Supplementary: a failed lookup never stops the feed, but several failures in a
row (or a missing/rejected token, or YouTube pages that can't be read) set a flag that
turns the run red after the commit. The token only ever goes into the Authorization
header — never into a URL, an exception text or the state.
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import NamedTuple

try:
    import requests
except ImportError:  # keeps the module importable for tests without requests
    requests = None

API = "https://api.themoviedb.org/3"
VERSION = 3                                   # 3: checked playable in DE (02.11.2026)
MIN_SECONDS = 60                              # shorter = spot/teaser cut, not a trailer
SHORT_RE = re.compile(r"\b(?:15|30)\s*(?:s|sec|secs|seconds)?\b|\bspots?\b|\bemmy\b|\bfyc\b|\bclip\b", re.I)
KEY_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")   # a YouTube video id
LANGS = ("en", "de")                          # preference; other languages only as last resort
COUNTRY = "DE"                                # where Johannes watches


class Check(NamedTuple):
    """What the YouTube watch page says about one video.
    read: the page was parsed (a playability status was found).
    de: True playable in DE, False not available in DE, None unclear.
    seconds: YouTube's length, or None."""
    read: bool
    de: bool | None
    seconds: int | None


UNREAD = Check(False, None, None)


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


def parse_watch(text: str) -> Check:
    """YouTube watch page -> Check. The country list (`availableCountries` in the player
    microformat) is the same wherever the page is fetched from; the playability status is
    the fetcher's own (US on GitHub's runners). DE counts as playable only when both agree."""
    st = re.search(r'"playabilityStatus":\{"status":"([A-Z_]+)"', text or "")
    if not st:
        return UNREAD
    m = re.search(r'"lengthSeconds":"(\d+)"', text)
    seconds = int(m.group(1)) if m else None
    lc = re.search(r'"availableCountries":\[([^\]]*)\]', text)
    countries = set(re.findall(r'"([A-Z]{2})"', lc.group(1))) if lc else set()
    if not countries:
        return Check(True, None, seconds)          # no list: unclear, never a link
    if COUNTRY not in countries:
        return Check(True, False, seconds)         # region-locked away from DE
    if st.group(1) != "OK":
        return Check(True, None, seconds)          # listed for DE but not playable here (age gate, bot check)
    return Check(True, True, seconds)


def youtube_check(key: str, timeout: float = 10.0) -> Check:
    """Fetch the watch page of one video -> Check (UNREAD on network/HTTP trouble)."""
    if requests is None or not KEY_RE.match(key or ""):
        return UNREAD
    try:
        resp = requests.get(f"https://www.youtube.com/watch?v={key}", timeout=timeout,
                            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0)",
                                     "Accept-Language": "en-US,en;q=0.9",
                                     "Cookie": "CONSENT=YES+1; SOCS=CAI"})
    except Exception:
        return UNREAD
    return parse_watch(resp.text) if resp.status_code == 200 else UNREAD


def choose(cands: list, check=youtube_check, tries: int = 5):
    """First candidate that is playable in DE and at least MIN_SECONDS long; when the page
    was read and DE confirmed but YouTube shows no length, the name must not look like a
    spot. Unread or unclear pages never count. None when nothing qualifies."""
    for v in cands[:tries]:
        c = check(v["key"])
        if c.de is not True:
            continue
        if c.seconds is not None:
            if c.seconds >= MIN_SECONDS:
                return v["key"]
            continue
        if not SHORT_RE.search(str(v.get("name") or "")):
            return v["key"]
    return None


def lookup(tmdb_id: str, media: str, season, token: str, fetch=fetch_videos, check=youtube_check) -> str | None:
    """-> YouTube id or None. Raises TrailerError/AuthError on failure.
    movie: /movie/<id>; show: /tv/<id>; season N: /tv/<id>/season/N, else the show's
    videos that name season N (for season 1 any show trailer)."""
    if not re.fullmatch(r"\d+", str(tmdb_id or "")):
        return None
    if media == "movie":
        return choose(ranked(fetch(f"movie/{tmdb_id}", token)), check)
    if season is None:
        return choose(ranked(fetch(f"tv/{tmdb_id}", token)), check)
    cands = ranked(fetch(f"tv/{tmdb_id}/season/{int(season)}", token))
    show = fetch(f"tv/{tmdb_id}", token)
    cands += ranked(show) if int(season) == 1 else ranked(show, season=int(season))
    seen, uniq = set(), []
    for v in cands:
        if v["key"] not in seen:
            seen.add(v["key"])
            uniq.append(v)
    return choose(uniq, check)


def link_html(key: str) -> str:
    """The line appended to the item description (raw HTML; the feed escapes it once)."""
    if not KEY_RE.match(key or ""):
        return ""
    return f'<a href="https://www.youtube.com/watch?v={key}">▶ Trailer (YouTube)</a>'


def probe(keys: list) -> int:
    """`python trailer.py --probe KEY ...`: print what this machine reads for each video
    (diagnostics from the runner; no token involved). Invalid keys are skipped."""
    for key in keys:
        if not KEY_RE.match(key):
            print(json.dumps({"key": "(invalid, skipped)"}))
            continue
        c = youtube_check(key)
        print(json.dumps({"key": key, "read": c.read, "de": c.de, "seconds": c.seconds,
                          "link": c.de is True}))
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--probe":
        raw = sys.argv[2:] or re.split(r"[\s,]+", os.environ.get("PROBE_KEYS", ""))
        sys.exit(probe([k for k in raw if k]))
    print("usage: python trailer.py --probe KEY [KEY ...]  (or PROBE_KEYS env)", file=sys.stderr)
    sys.exit(2)
