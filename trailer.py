"""
trailer.py — the official YouTube trailer of a new item, via TMDB (25.10.2026).

The TMDB id comes from the JustWatch match that streaming.py already makes (JustWatch
returns `externalIds.tmdbId`), so there is no second title search. Only official
YouTube videos of type Trailer or Teaser count; nothing found -> no link at all
(Johannes 25.10.2026: "wenn es bei YouTube nichts gibt, dann lass es weg").
At least MIN_SECONDS long by YouTube's own duration (Johannes 25.10.2026: a 15-second
"Emmy Trailer" for Mare of Easttown "sagt einem gar nichts"); no readable duration = no link.

Playable in Germany (02.11.2026, Johannes 01.11.2026: "sowas musst du bitte immer
checken"): the only source is the YouTube Data API v3 (videos.list, part
contentDetails,status, secret YOUTUBE_API_KEY). It answers the same from everywhere, so the
US runner can judge Germany: no regionRestriction = everywhere, `allowed` must name DE,
`blocked` must not. Everything else is unclear -> no link (the item itself links to
Metacritic): age restriction, a video still processing, odd restriction lists. A response
that doesn't have the expected shape, a network error or a non-200 is UNREAD: it decides
nothing and uses nothing up (the caller keeps the item due). The player endpoint and the
watch page are kept for `--probe` only, never for a decision.

Frozen at emit like the streaming line: one lookup when the item is emitted, never again.
The TMDB token only ever goes into the Authorization header, the YouTube key only into the
X-goog-api-key header — never into a URL, an exception text, a log line or the state.
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
SHORT_RE = re.compile(r"\b(?:15|30)\s*(?:s|sec|secs|seconds)?\b|\bspots?\b|\bemmy\b|\bfyc\b|\bclip\b", re.I)  # kept for fs_trailer
KEY_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")   # a YouTube video id
LANGS = ("en", "de")                          # preference; other languages only as last resort
COUNTRY = "DE"                                # where Johannes watches
CC_RE = re.compile(r"^[A-Z]{2}$")             # ISO 3166-1 alpha-2
PRIVACY = ("public", "unlisted", "private")
API_YT = "https://www.googleapis.com/youtube/v3/videos"
PLAYER_URL = "https://www.youtube.com/youtubei/v1/player?prettyPrint=false"    # probe only
PLAYER_CLIENT = {"clientName": "WEB", "clientVersion": "2.20251101.00.00", "hl": "en", "gl": "US"}
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0)"
CANARY = "dQw4w9WgXcQ"   # health check: does the API answer at all (once per run)


class Check(NamedTuple):
    """What a source says about one video.
    read: a well-formed answer arrived (False: network, HTTP, unexpected shape).
    de: True playable in DE, False not available in DE, None unclear.
    seconds: YouTube's length, or None."""
    read: bool
    de: bool | None
    seconds: int | None


UNREAD = Check(False, None, None)


class CheckUnavailable(RuntimeError):
    """The DE check got no well-formed answer (network, quota, 5xx, shape): decide nothing."""


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


ISO_DUR_RE = re.compile(r"^P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")


def _iso_seconds(raw) -> int | None:
    m = ISO_DUR_RE.match(str(raw or ""))
    if not m or not any(m.groups()):
        return None
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + s


def _codes(value) -> set | None:
    """A non-empty list of two-letter country codes -> set, anything else -> None."""
    if not isinstance(value, list) or not value:
        return None
    if not all(isinstance(c, str) and CC_RE.match(c) for c in value):
        return None
    return set(value)


def parse_api(data, key: str | None = None) -> Check:
    """YouTube Data API v3 videos.list (part=contentDetails,status) -> Check.
    Shape errors -> UNREAD (decide nothing). No item for a valid id = deleted or private.
    regionRestriction: absent = everywhere; exactly `allowed` or `blocked` with a non-empty
    list of two-letter codes, else unclear. Age-restricted or still processing = unclear."""
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return UNREAD
    items = data["items"]
    if not items:
        return Check(True, False, None)
    if len(items) != 1 or not isinstance(items[0], dict):
        return UNREAD
    it = items[0]
    if key is not None and it.get("id") != key:
        return UNREAD
    cd, st = it.get("contentDetails"), it.get("status")
    if not isinstance(cd, dict) or not isinstance(st, dict) or st.get("privacyStatus") not in PRIVACY:
        return UNREAD
    seconds = _iso_seconds(cd.get("duration"))
    if st["privacyStatus"] == "private":
        return Check(True, False, seconds)
    if st.get("uploadStatus") != "processed":
        return Check(True, None, seconds)          # still processing, failed, rejected: unclear
    if "regionRestriction" in cd:
        rr = cd["regionRestriction"]
        if not isinstance(rr, dict) or len(rr) != 1 or set(rr) - {"allowed", "blocked"}:
            return Check(True, None, seconds)
        kind, raw = next(iter(rr.items()))
        codes = _codes(raw)
        if codes is None:
            return Check(True, None, seconds)
        if (kind == "allowed") != (COUNTRY in codes):
            return Check(True, False, seconds)
    rating = cd.get("contentRating")
    if rating is not None and not isinstance(rating, dict):
        return Check(True, None, seconds)
    if (rating or {}).get("ytRating") == "ytAgeRestricted":
        return Check(True, None, seconds)          # age gate: needs a login in DE too
    return Check(True, True, seconds)


def api_key() -> str:
    return os.environ.get("YOUTUBE_API_KEY", "").strip()


def api_check(key: str, timeout: float = 10.0) -> Check:
    """The DE check: YouTube Data API v3 (1 quota unit per video). UNREAD without key, on
    network trouble, non-200 or an unexpected shape. The key only goes into a header."""
    k = api_key()
    if requests is None or not k or not KEY_RE.match(key or ""):
        return UNREAD
    try:
        resp = requests.get(API_YT, timeout=timeout, params={"part": "contentDetails,status", "id": key},
                            headers={"X-goog-api-key": k, "Accept": "application/json"})
    except Exception:
        return UNREAD
    if resp.status_code != 200:
        print(f"[warn] YouTube Data API: HTTP {resp.status_code} (key rejected, quota or outage)",
              file=sys.stderr)
        return UNREAD
    try:
        return parse_api(resp.json(), key)
    except ValueError:
        return UNREAD


def api_healthy() -> bool:
    """Does the API answer (key accepted, quota left, expected shape)? Asked once per run about
    CANARY through the same function the trailer checks use."""
    return api_check(CANARY).read


def youtube_check(key: str) -> Check:
    """Playable in DE? Only the Data API decides (02.11.2026)."""
    return api_check(key)


def choose(cands: list, check=youtube_check, tries: int = 5):
    """First candidate that is playable in DE and at least MIN_SECONDS long. Not in DE,
    unclear or without a duration: skipped. A candidate without a well-formed answer raises
    CheckUnavailable, so an outage never turns into a frozen "no trailer"."""
    for v in cands[:tries]:
        c = check(v["key"])
        if not c.read:
            raise CheckUnavailable(v["key"])
        if c.de is True and c.seconds is not None and c.seconds >= MIN_SECONDS:
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


# --- player endpoint and watch page: diagnosis in --probe only, never a decision ---


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


def probe(keys: list) -> int:
    """`python trailer.py --probe KEY ...`: what this machine reads for each video. The
    decision (`de`, `link`) is the API's; player and watch page are printed for diagnosis
    only. No secret is printed. Invalid keys are skipped."""
    for key in keys:
        if not KEY_RE.match(key):
            print(json.dumps({"key": "(invalid, skipped)"}))
            continue
        a, p, w = api_check(key), player_check(key), watch_check(key)
        print(json.dumps({"key": key, "de": a.de, "seconds": a.seconds,
                          "link": a.de is True and (a.seconds or 0) >= MIN_SECONDS,
                          "api": a._asdict(), "player (diagnosis)": p._asdict(),
                          "watch (diagnosis)": w._asdict()}))
    print(json.dumps({"youtube_api_key_set": bool(api_key()), "api_healthy": api_healthy()}))
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--probe":
        raw = sys.argv[2:] or re.split(r"[\s,]+", os.environ.get("PROBE_KEYS", ""))
        sys.exit(probe([k for k in raw if k]))
    print("usage: python trailer.py --probe KEY [KEY ...]  (or PROBE_KEYS env)", file=sys.stderr)
    sys.exit(2)
