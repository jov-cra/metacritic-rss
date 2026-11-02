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
a candidate only counts when YouTube's own country list (`availableCountries` in the player
microformat) names DE. The runner sits in the US, so whether the video plays *there* says
nothing about Germany; the country list is the same from everywhere. Source 1 is YouTube's
player endpoint (youtubei/v1/player, WEB client, JSON): it carries the list even when it
refuses playback to a datacenter IP (its status is then UNPLAYABLE for every video, so the
status is ignored there; an age gate, `isFamilySafe: false`, counts as unclear). Source 2,
only when source 1 gives no list, is the watch page (list plus status OK). First live run
02.11.2026: the runner's watch pages carry no list (bot check), hence source 1.
Fail-closed: no list from either source -> unclear -> no link (the item itself links to
Metacritic). There is no name-only fallback any more when YouTube can't be read.

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
API_YT = "https://www.googleapis.com/youtube/v3/videos"
PLAYER_URL = "https://www.youtube.com/youtubei/v1/player?prettyPrint=false"
PLAYER_CLIENT = {"clientName": "WEB", "clientVersion": "2.20251101.00.00", "hl": "en", "gl": "US"}
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0)"
CANARY = "dQw4w9WgXcQ"   # plays in every country since 2009: no answer for it = YouTube outage, not the video


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


def parse_player(data) -> Check:
    """youtubei/v1/player JSON -> Check. The playability status is ignored (a datacenter IP
    gets UNPLAYABLE for everything); the country list decides, an age gate is unclear."""
    if not isinstance(data, dict) or not isinstance(data.get("playabilityStatus"), dict):
        return UNREAD
    mf = (data.get("microformat") or {}).get("playerMicroformatRenderer") or {}
    vd = data.get("videoDetails") or {}
    raw = str(vd.get("lengthSeconds") or mf.get("lengthSeconds") or "")
    seconds = int(raw) if raw.isdigit() else None
    ac = mf.get("availableCountries")
    countries = {c for c in ac if isinstance(c, str) and re.fullmatch(r"[A-Z]{2}", c)} if isinstance(ac, list) else set()
    if not countries:
        return Check(True, None, seconds)          # no list: unclear, never a link
    if COUNTRY not in countries:
        return Check(True, False, seconds)         # region-locked away from DE
    if mf.get("isFamilySafe") is False:
        return Check(True, None, seconds)          # age gate: needs a login in DE too
    return Check(True, True, seconds)


ISO_DUR_RE = re.compile(r"^P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")


def _iso_seconds(raw) -> int | None:
    m = ISO_DUR_RE.match(str(raw or ""))
    if not m or not any(m.groups()):
        return None
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + s


def parse_api(data) -> Check:
    """YouTube Data API v3 videos.list (part=contentDetails,status) -> Check.
    regionRestriction absent = everywhere; `allowed` must name DE, `blocked` must not.
    No item = deleted or private (not playable anywhere); age-restricted = unclear."""
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return UNREAD
    if not data["items"]:
        return Check(True, False, None)
    it = data["items"][0] if isinstance(data["items"][0], dict) else {}
    cd = it.get("contentDetails") or {}
    st = it.get("status") or {}
    seconds = _iso_seconds(cd.get("duration"))
    if st.get("privacyStatus") == "private" or st.get("uploadStatus") not in (None, "processed"):
        return Check(True, False, seconds)
    rr = cd.get("regionRestriction") or {}
    if isinstance(rr.get("allowed"), list) and COUNTRY not in rr["allowed"]:
        return Check(True, False, seconds)
    if isinstance(rr.get("blocked"), list) and COUNTRY in rr["blocked"]:
        return Check(True, False, seconds)
    if (cd.get("contentRating") or {}).get("ytRating") == "ytAgeRestricted":
        return Check(True, None, seconds)          # age gate: needs a login in DE too
    return Check(True, True, seconds)


def api_check(key: str, timeout: float = 10.0) -> Check:
    """Source 0: YouTube Data API v3 with YOUTUBE_API_KEY (1 quota unit per video). The key
    goes only into the X-goog-api-key header, never into a URL, a message or the state."""
    api_key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if requests is None or not api_key or not KEY_RE.match(key or ""):
        return UNREAD
    try:
        resp = requests.get(API_YT, timeout=timeout, params={"part": "contentDetails,status", "id": key},
                            headers={"X-goog-api-key": api_key, "Accept": "application/json"})
    except Exception:
        return UNREAD
    if resp.status_code != 200:
        print(f"[warn] YouTube Data API: HTTP {resp.status_code} (key rejected or quota?)", file=sys.stderr)
        return UNREAD
    try:
        return parse_api(resp.json())
    except ValueError:
        return UNREAD


def player_check(key: str, timeout: float = 10.0) -> Check:
    """Source 1: YouTube's player endpoint (JSON, no key, no cookie)."""
    if requests is None or not KEY_RE.match(key or ""):
        return UNREAD
    try:
        resp = requests.post(PLAYER_URL, timeout=timeout, json={"videoId": key, "context": {"client": PLAYER_CLIENT}},
                             headers={"User-Agent": UA, "Content-Type": "application/json"})
        return parse_player(resp.json()) if resp.status_code == 200 else UNREAD
    except Exception:
        return UNREAD


def watch_check(key: str, timeout: float = 10.0) -> Check:
    """Source 2: the watch page (UNREAD on network/HTTP trouble)."""
    if requests is None or not KEY_RE.match(key or ""):
        return UNREAD
    try:
        resp = requests.get(f"https://www.youtube.com/watch?v={key}", timeout=timeout,
                            headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
                                     "Cookie": "CONSENT=YES+1; SOCS=CAI"})
    except Exception:
        return UNREAD
    return parse_watch(resp.text) if resp.status_code == 200 else UNREAD


def youtube_check(key: str, sources=None) -> Check:
    """Playable in DE? Source 0 (Data API, when YOUTUBE_API_KEY is set), then source 1 (player
    endpoint), then source 2 (watch page) while the answer is still unclear. The first
    definite answer wins; the length is taken from any source."""
    best = UNREAD
    for src in (sources or (api_check, player_check, watch_check)):
        c = src(key)
        if c.de is not None:
            return c if c.seconds is not None else c._replace(seconds=best.seconds)
        if c.read and (not best.read or best.seconds is None):
            best = c
    return best


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
        a, p, w = api_check(key), player_check(key), watch_check(key)
        c = youtube_check(key, sources=(lambda k: a, lambda k: p, lambda k: w))
        print(json.dumps({"key": key, "de": c.de, "seconds": c.seconds, "link": c.de is True,
                          "api": a._asdict(), "player": p._asdict(), "watch": w._asdict()}))
    print(json.dumps({"youtube_api_key_set": bool(os.environ.get("YOUTUBE_API_KEY", "").strip())}))
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--probe":
        raw = sys.argv[2:] or re.split(r"[\s,]+", os.environ.get("PROBE_KEYS", ""))
        sys.exit(probe([k for k in raw if k]))
    print("usage: python trailer.py --probe KEY [KEY ...]  (or PROBE_KEYS env)", file=sys.stderr)
    sys.exit(2)
