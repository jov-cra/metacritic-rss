#!/usr/bin/env python3
"""
metacritic_feed.py — Build a personal RSS feed of Metacritic movie/TV releases
that have crossed a Metascore threshold.

THE CORE IDEA (the "event log" model)
-------------------------------------
Metacritic killed its own official RSS feeds after the 2023 Fandom relaunch, so
the old "filter Metacritic's feed" trick no longer works. This script instead
scrapes the public "newest releases" browse pages and emits an item into the
feed the FIRST time it is seen at or above your score threshold.

State is kept in state.json keyed by the item URL (which is also the RSS <guid>),
so every title appears exactly once — at the moment it first qualifies. That is
precisely what solves the problem you described: titles are often listed first
WITHOUT a score and only get one later, once enough critic reviews are in. We are
watching for the *transition* into "scored and good enough", not for the original
posting. The reader dedupes by <guid>, so nothing is ever shown twice.

No API key, no login — just the public browse pages. Output is plain RSS 2.0 that
works in Readwise Reader, Tapestry, or any RSS reader.

Usage:
    python metacritic_feed.py                    # normal run (writes feed.xml + state.json)
    python metacritic_feed.py --dry-run --debug  # print what it parses, write nothing
    python metacritic_feed.py --threshold 90 --media movie --pages 5

Everything is also configurable via environment variables (see README).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin
from xml.sax.saxutils import escape

try:
    import requests
except ImportError:  # keeps the module importable for tests without requests
    requests = None

from bs4 import BeautifulSoup

import streaming
import trailer

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
BASE = "https://www.metacritic.com"
BROWSE = {
    "movie": "/browse/movie/all/all/all-time/new/",
    "tv": "/browse/tv/all/all/all-time/new/",
}
DATE_RE = re.compile(r"([A-Z][a-z]{2}\.? \d{1,2}, \d{4})")
SCORE_RE = re.compile(r"(?<!\d)(\d{1,3})\s*Metascore")
HEADING_RE = re.compile(r"^h[1-6]$")
BADGE_RE = re.compile(r"\bmust-see\b")
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# Characters XML 1.0 simply forbids. escape() only rewrites & < > — a stray
# control byte in a scraped title would still make the whole feed unparseable,
# so strip them before they ever reach the document.
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _xml_text(value: str) -> str:
    """Escape for XML text content, minus the characters XML cannot carry."""
    return escape(CTRL_RE.sub("", value))


def _attr(value: str) -> str:
    """Make a URL safe inside an HTML attribute. ONLY the quote is escaped here:
    & < > are handled by the _xml_text() pass over the whole body, and escaping
    them twice would change every existing item's bytes for no gain. The quote is
    the one character that would actually break out of the attribute."""
    return value.replace('"', "&quot;")


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #
def fetch(url: str) -> str:
    """Fetch a page as raw HTML. Sends a real browser User-Agent."""
    if requests is None:
        raise RuntimeError("The 'requests' package is required: pip install requests")
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.text


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _clean_title(anchor, text: str, date_start: int) -> str:
    """Best-effort clean title. Prefers a heading element, falls back to the
    de-duplicated text that precedes the release date."""
    heading = anchor.find(HEADING_RE)
    if heading:
        t = re.sub(r"\s+", " ", BADGE_RE.sub("", heading.get_text(" ", strip=True))).strip()
        if t:
            return t

    pre = re.sub(r"\s+", " ", BADGE_RE.sub("", text[:date_start])).strip()
    words = pre.split()
    # Cards often render the title twice (image alt + visible heading), e.g.
    # "Blind Love Blind Love" -> collapse the duplicated halves.
    if words and len(words) % 2 == 0 and words[: len(words) // 2] == words[len(words) // 2:]:
        words = words[: len(words) // 2]
    return " ".join(words) or "Untitled"


def _best_poster(anchor) -> str | None:
    """The card's poster image URL. Metacritic renders <img srcset="…w96 1x, …w192 2x">;
    prefer the highest-density (2x) URL, fall back to plain src. Returns None when the
    card has no image, in which case the feed simply omits the thumbnail for that item."""
    img = anchor.find("img")
    if img is None:
        return None
    best = None
    for part in (img.get("srcset") or "").split(","):
        cand = part.strip().split(" ")
        if cand and cand[0].startswith("http"):
            best = cand[0]  # srcset is ordered 1x,2x -> last wins = highest density
    return best or img.get("src") or None


def parse_browse(html_text: str, media: str) -> list[dict]:
    """Parse a Metacritic browse page into a list of product dicts.

    A product card is a single <a> that (a) links to /movie/… or /tv/… and
    (b) contains a release date. Nav links match (a) but not (b), so the date
    check reliably separates real cards from menu links.
    """
    soup = BeautifulSoup(html_text, "html.parser")
    prefix = f"/{media}/"
    by_url: dict[str, dict] = {}

    for anchor in soup.find_all("a", href=True):
        path = anchor["href"].split("?")[0]
        if path.startswith(BASE):
            path = path[len(BASE):]
        if not path.startswith(prefix):
            continue

        text = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True))
        date_match = DATE_RE.search(text)
        if not date_match:
            continue  # menu/other link without a release date -> skip

        url = urljoin(BASE, path)
        if not url.endswith("/"):
            url += "/"

        score_match = SCORE_RE.search(text)
        score = int(score_match.group(1)) if score_match else None
        if score is not None and not (0 <= score <= 100):
            score = None  # reject out-of-range matches (a real Metascore is 0-100)

        title = _clean_title(anchor, text, date_match.start())
        if media == "tv" and score == 0 and SEASON_PATH_RE.match(url[len(BASE):]):
            score = None   # a season without a Metascore, as in parse_season
        # Since 21.10.2026 the TV browse cards link to SEASONS (/tv/<show>/season-N/)
        # instead of shows. Season 1 is the show itself: fold it back onto the show
        # URL, which has been the guid of every new show since July — otherwise each
        # new show would appear twice ("Phony" and "Phony: Season 1").
        if media == "tv":
            sm = SEASON_PATH_RE.match(url[len(BASE):])
            if sm and int(sm.group(2)) == 1:
                url = f"{BASE}/tv/{sm.group(1)}/"
                title = SEASON1_TITLE_RE.sub("", title) or title

        record = {
            "url": url,
            "title": title,
            "score": score,
            "date": date_match.group(1),
            "media": media,
            "poster": _best_poster(anchor),
            "_len": len(text),
        }
        # Keep the richest entry per URL (the full card, not a nested sub-link),
        # but never lose a poster that only one of the duplicate anchors carried.
        prev = by_url.get(url)
        if prev is None or record["_len"] > prev["_len"]:
            if prev is not None and not record.get("poster"):
                record["poster"] = prev.get("poster")
            by_url[url] = record
        elif not prev.get("poster") and record.get("poster"):
            prev["poster"] = record["poster"]

    result = list(by_url.values())
    for r in result:
        r.pop("_len", None)
    return result


# --------------------------------------------------------------------------- #
# Detail-page enrichment: critic/user counts + a short top-critic quote
# --------------------------------------------------------------------------- #
DETAIL_VERSION = 3  # bump to force re-enrichment of already-stored items (v3: adds og:image)
CRITIC_POS_RE = re.compile(r"(\d+)%\s*Positive\s+\d+\s+Reviews")  # critic (Reviews, not user Ratings)
USER_RE = re.compile(r"Based on ([\d,]+)\s+(?:User\s+)?Ratings\s+(10(?:\.0)?|\d(?:\.\d)?)(?![\d.])")
DETAIL_MAX_TRIES = 5     # after this many empty detail pages an item keeps its basic description
DEGRADED_AFTER = 3       # consecutive empty/failed detail fetches that stop enrichment for this run


def fetch_detail(url: str) -> str:
    return fetch(url)


def parse_detail(html_text: str) -> dict:
    """Best-effort extraction from a title's detail page. Every field is
    optional; the feed description degrades gracefully if a field is missing.
    Returns {} when nothing useful was found."""
    soup = BeautifulSoup(html_text, "html.parser")
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    d: dict = {}

    m = re.search(r"Based on ([\d,]+)\s+Critic Reviews", text)
    if m:
        d["critic_count"] = int(m.group(1).replace(",", ""))
    mp = CRITIC_POS_RE.search(text)
    if mp:
        d["pos"] = int(mp.group(1))

    # user score + rating count — only when an actual user score exists.
    # "Available after N ratings" is a THRESHOLD (how many are needed), not a
    # count, so we must not render it as if there were N ratings.
    # Metacritic writes "Based on 17 User Ratings 7.5": count and score are read
    # from the same match (until 17.10.2026 the pattern lacked "User" and no user
    # score was ever found). Accepts 10 and 10.0, rejects anything above 10.
    mu = USER_RE.search(text)
    if mu and float(mu.group(2)) <= 10:
        d["user_count"] = int(mu.group(1).replace(",", ""))
        d["user_score"] = float(mu.group(2))
    elif re.search(r"User score(?:\s+Available after \d+ ratings)?\s+tbd", text):
        d["user_tbd"] = True

    # High-res poster: the detail page's og:image is ~1200px wide (the browse-card
    # thumbnail is only 192px and the CDN rejects larger sizes on that signed URL).
    # This page is already fetched for the stats above, so grabbing it is free.
    og = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "twitter:image"})
    if og and og.get("content"):
        d["image"] = og["content"]

    d["v"] = DETAIL_VERSION
    return d


def enrich_missing(emitted: dict, cap: int, delay: float) -> tuple[int, bool]:
    """Fetch detail pages for emitted items lacking `detail` (new qualifiers +
    one-time backfill), newest first. Once set, `detail` is frozen -> feed stays
    deterministic (no churn) after backfill completes.

    `cap` bounds ATTEMPTS, failures included, and the delay applies after every
    attempt. A page without a critic count (layout change, soft-block served with
    200) is NOT stored as done: the item is retried on later runs, and after
    DETAIL_MAX_TRIES empty pages it is frozen with an empty detail so it cannot
    eat the budget forever. DEGRADED_AFTER consecutive misses stop enrichment for
    this run and return degraded=True, which the workflow turns into a red run
    AFTER the (still correct) feed and state have been committed.
    Returns (enriched, degraded)."""
    todo = sorted(
        (u for u, m in emitted.items()
         if "detail" not in m or (m.get("detail") or {}).get("v") != DETAIL_VERSION),
        key=lambda u: emitted[u].get("emitted_at", ""), reverse=True,
    )
    done = attempts = misses = 0
    for url in todo:
        if attempts >= cap:
            break
        attempts += 1
        d = None
        fetched = False
        try:
            html_text = fetch_detail(url)
            fetched = True
            d = parse_detail(html_text)
        except Exception as exc:
            print(f"[warn] detail fetch failed for {url}: {exc}", file=sys.stderr)
        time.sleep(delay)
        if d and "critic_count" in d:
            emitted[url]["detail"] = d
            emitted[url].pop("detail_tries", None)
            done += 1
            misses = 0
            continue
        misses += 1
        if not fetched:
            # a network error (429/5xx) says nothing about the page: no try is
            # used up, otherwise an outage would freeze fresh items empty for good
            if misses >= DEGRADED_AFTER:
                print(f"[warn] {misses} detail fetches in a row failed; stopping enrichment "
                      "for this run", file=sys.stderr)
                return done, True
            continue
        tries = emitted[url].get("detail_tries", 0) + 1
        if tries >= DETAIL_MAX_TRIES:
            emitted[url]["detail"] = {"v": DETAIL_VERSION, "empty": True}
            emitted[url].pop("detail_tries", None)
        else:
            emitted[url]["detail_tries"] = tries
        if misses >= DEGRADED_AFTER:
            print(f"[warn] {misses} detail pages in a row without critic stats; "
                  "stopping enrichment for this run", file=sys.stderr)
            return done, True
    return done, False


# --------------------------------------------------------------------------- #
# New seasons of running shows (17.10.2026)
# --------------------------------------------------------------------------- #
# The TV browse pages list new SHOWS only. A new season of a running show has its
# own URL (/tv/<show>/season-N/) and its own Metascore but never appears there as
# a card. Season links are collected from the header navigation of every TV
# browse page plus Metacritic's TV premiere-dates article, then each candidate
# season page is checked for a score. Season 1 is skipped: a new show is already
# covered by its show card.
SEASON_PATH_RE = re.compile(r"^/tv/([^/]+)/season-(\d+)/?$")
SEASON1_TITLE_RE = re.compile(r"\s*:\s*Season 1\s*$")
PREMIERE_PAGE = "/news/tv-premiere-dates/"
SEASON_GIVE_UP_DAYS = 60
SEASON_LATE_DAYS = 21     # premiered longer ago -> dated by premiere, not "now"


def season_links(html_text: str) -> set[str]:
    soup = BeautifulSoup(html_text, "html.parser")
    out = set()
    for a in soup.find_all("a", href=True):
        path = a["href"].split("?")[0].split("#")[0]
        if path.startswith(BASE):
            path = path[len(BASE):]
        m = SEASON_PATH_RE.match(path)
        if m and int(m.group(2)) >= 2:
            out.add(f"{BASE}/tv/{m.group(1)}/season-{int(m.group(2))}/")
    return out


class SeasonPageError(ValueError):
    """The page has no TVSeason JSON-LD at all — the markup changed under us."""


def parse_season(html_text: str, url: str) -> dict:
    """Read a season page. Identity, title and score come from the page's JSON-LD
    (@type TVSeason whose url is the requested one — a redirect to the show page
    raises). The premiere date comes from the hero block; JSON-LD datePublished is
    the SHOW's date and must not be used. score is None while the season has no
    Metascore yet."""
    soup = BeautifulSoup(html_text, "html.parser")
    ld = None
    for t in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(t.string or "")
        except json.JSONDecodeError:
            continue
        nodes = data if isinstance(data, list) else [data]
        nodes = [n for d in nodes if isinstance(d, dict) for n in (d.get("@graph") or [d])]
        for n in nodes:
            typ = n.get("@type") if isinstance(n, dict) else None
            if typ == "TVSeason" or (isinstance(typ, list) and "TVSeason" in typ):
                ld = n
                break
        if ld is not None:
            break
    if ld is None:
        raise SeasonPageError(f"no TVSeason JSON-LD on {url}")
    def _path(u: str) -> str:
        return re.sub(r"^https?://(www\.)?metacritic\.com", "", (u or "").split("?")[0]).rstrip("/")
    if _path(ld.get("url")) != _path(url):
        raise ValueError(f"not the season page for {url} (redirected?)")
    rating = ld.get("aggregateRating") or {}
    score = rating.get("ratingValue")
    # A season without a Metascore yet (fewer than 4 reviews) carries ratingValue 0
    # in its JSON-LD — that is "no score", not a score of 0 (two seasons went out
    # as "[0]" on 20./21.10.2026).
    score = int(score) if isinstance(score, (int, float)) and 0 < score <= 100 else None
    hero = soup.select_one(".hero-release-date__value")
    dm = DATE_RE.search(hero.get_text(" ", strip=True)) if hero else None
    detail = parse_detail(html_text)
    if "critic_count" not in detail and isinstance(rating.get("reviewCount"), int):
        detail["critic_count"] = rating["reviewCount"]
    return {"url": url, "title": (ld.get("name") or "").strip() or "Untitled",
            "score": score, "date": dm.group(1) if dm else "", "media": "tv",
            "detail": detail, "image": ld.get("image")}


def _date_ordinal(raw: str):
    try:
        return datetime.strptime((raw or "").replace(".", "").strip(), "%b %d, %Y").date().toordinal()
    except ValueError:
        return None


def check_seasons(state: dict, emitted: dict, links: set, args, now: datetime) -> tuple[int, int, int]:
    """Register new season links as candidates and check up to args.season_max of
    them for a score. Candidate order rotates with the 2-hour slot, so every
    candidate gets its turn without writing a "last checked" stamp into the state
    on every run (that would commit every run). A candidate without a score after
    SEASON_GIVE_UP_DAYS is marked gave_up and never fetched again; the marker stays
    so the same link cannot re-enter as new. Returns (new, attempts, failures)."""
    cands = state.setdefault("seasons", {})
    today = now.date().isoformat()
    for url in sorted(links):
        if url not in emitted and url not in cands:
            cands[url] = {"first_seen": today}
    today_ord = now.date().toordinal()
    pending = []
    for url in sorted(cands):
        c = cands[url]
        if url in emitted:
            del cands[url]               # came in through a browse card (since 21.10.2026)
            continue
        if c.get("gave_up"):
            continue
        try:
            first = datetime.fromisoformat(c["first_seen"]).date().toordinal()
        except (KeyError, ValueError):
            first = today_ord
        premiere = _date_ordinal(c.get("premiere", ""))
        if premiere is not None and premiere > today_ord:
            continue                      # not aired yet: no score possible, save the budget
        # Give up only on a candidate whose page was read successfully at least
        # once: if the season markup breaks, every check fails, and a final
        # gave_up marker would lose those seasons for good even after a fix.
        # (A page that never reads at all is dropped after half a year at the latest.)
        if ((c.get("ok") and max(first, premiere or 0) + SEASON_GIVE_UP_DAYS < today_ord)
                or first + 3 * SEASON_GIVE_UP_DAYS < today_ord):
            c["gave_up"] = True
            continue
        pending.append(url)
    if not pending:
        return 0, 0, 0, 0
    offset = (int(now.timestamp() // 7200) * max(args.season_max, 1)) % len(pending)
    order = pending[offset:] + pending[:offset]
    new = attempts = failures = structural = 0
    for url in order[: args.season_max]:
        attempts += 1
        try:
            s = parse_season(fetch_detail(url), url)
        except Exception as exc:
            failures += 1
            if isinstance(exc, SeasonPageError):
                structural += 1
            print(f"[warn] season check failed for {url}: {exc}", file=sys.stderr)
            time.sleep(args.detail_delay)
            continue
        time.sleep(args.detail_delay)
        if args.debug:
            print(f"       [season] score={str(s['score']):>4}  {s['title']}  ({s['date']})  ::  {url}")
        if s["score"] is None or s["score"] < args.threshold:
            c = cands[url]
            if not c.get("ok"):
                c["ok"] = True                         # written once
            if s["date"] and c.get("premiere") != s["date"]:
                c["premiere"] = s["date"]              # written once; drives skip + give-up
            continue
        entry = {"title": s["title"], "score": s["score"], "media": "tv",
                 "release_date": s["date"], "emitted_at": now.isoformat()}
        aired = _date_ordinal(s["date"])
        # A season that premiered weeks ago (first deploy, or found late) is dated
        # by its premiere instead of "now", so it slots in where it belongs rather
        # than piling up at the top of the reader.
        late = aired is not None and aired < today_ord - SEASON_LATE_DAYS
        if not args.seed_from_release and not late:
            entry["feed_date"] = now.isoformat()
        if s.get("image"):
            entry["image"] = s["image"]
        if "critic_count" in s["detail"]:
            entry["detail"] = s["detail"]          # same page, no second fetch
        if getattr(args, "providers", False):
            entry["providers_due"] = True
            if getattr(args, "trailers", False):
                entry["trailer_due"] = True
        emitted[url] = entry
        del cands[url]
        new += 1
    return new, attempts, failures, structural


# --------------------------------------------------------------------------- #
# Streaming availability in Germany (21.10.2026), see streaming.py
# --------------------------------------------------------------------------- #
# Forward-only: an item is marked `providers_due` at the moment it is emitted
# (process_cards / check_seasons), and only marked items are looked up — in the
# same run, before the feed is written, so the title readers first get is final.
# Items emitted before this existed keep "(TV)" byte-identical.


def _freeze_rest(emitted: dict, urls, why: str) -> None:
    """Items about to be published without a lookup keep "(TV)" for good."""
    for u in urls:
        m = emitted[u]
        if m.pop("providers_due", None):
            m["providers"] = {"v": streaming.VERSION, "match": False, why: True}


def enrich_providers(emitted: dict, cap: int, delay: float, lookup=streaming.lookup,
                     today: str = "") -> tuple[int, bool]:
    """Look up where each newly emitted item (TV and movie) streams in Germany and freeze the
    result under `providers` (with the lookup date). Every marked item is settled
    in this run, before the feed is written, so a title never changes after the
    reader has it: a failed lookup, items beyond `cap` and items left after
    DEGRADED_AFTER failures in a row (-> degraded=True) are frozen as "(TV)".
    Returns (done, degraded)."""
    todo = sorted((u for u, m in emitted.items() if m.get("providers_due")),
                  key=lambda u: (emitted[u].get("emitted_at", ""), u), reverse=True)
    done = attempts = misses = 0
    for i, url in enumerate(todo):
        if attempts >= cap:
            _freeze_rest(emitted, todo[i:], "skipped")
            break
        attempts += 1
        meta = emitted[url]
        meta.pop("providers_due", None)
        try:
            rec = lookup(meta["title"], meta.get("release_date", ""), meta.get("media", "tv"))
        except Exception as exc:
            print(f"[warn] streaming lookup failed for {url}: {exc}", file=sys.stderr)
            time.sleep(delay)
            meta["providers"] = {"v": streaming.VERSION, "match": False, "failed": True}
            misses += 1
            if misses >= DEGRADED_AFTER:
                print(f"[warn] {misses} streaming lookups in a row failed; stopping for this run",
                      file=sys.stderr)
                _freeze_rest(emitted, todo, "failed")   # published now with "(TV)"
                return done, True
            continue
        time.sleep(delay)
        misses = 0
        if today:
            rec["checked"] = today
        meta["providers"] = rec
        done += 1
    return done, False


# --------------------------------------------------------------------------- #
# Official YouTube trailer (25.10.2026), see trailer.py
# --------------------------------------------------------------------------- #
# Forward-only like the streaming line: `trailer_due` is set at emit, the lookup runs in
# the same build after enrich_providers (it needs the TMDB id from that match), and the
# result is frozen. Items without a found trailer get no link, ever.


def _freeze_trailers(emitted: dict, urls, why: str) -> None:
    for u in urls:
        m = emitted[u]
        if m.pop("trailer_due", None):
            m["trailer"] = {"v": trailer.VERSION, "key": None, why: True}


def enrich_trailers(emitted: dict, cap: int, delay: float, token: str, lookup=trailer.lookup,
                    today: str = "") -> tuple[int, bool]:
    """Look up the official YouTube trailer of each newly emitted item and freeze it under
    `trailer`. No token, a rejected token, or DEGRADED_AFTER failures in a row -> degraded
    (the run turns red after the commit). An item without TMDB id or without an official
    trailer is a normal outcome, not a failure. Returns (found, degraded)."""
    todo = sorted((u for u, m in emitted.items() if m.get("trailer_due")),
                  key=lambda u: (emitted[u].get("emitted_at", ""), u), reverse=True)
    if not todo:
        return 0, False
    if not token:
        print("[warn] TMDB_TOKEN missing: new items get no trailer link", file=sys.stderr)
        _freeze_trailers(emitted, todo, "no_token")
        return 0, True
    found = attempts = misses = 0
    for i, url in enumerate(todo):
        if attempts >= cap:
            _freeze_trailers(emitted, todo[i:], "skipped")
            break
        meta = emitted[url]
        tm = (meta.get("providers") or {}).get("tmdb")
        if not tm:
            meta.pop("trailer_due", None)
            meta["trailer"] = {"v": trailer.VERSION, "key": None, "no_id": True}
            continue
        attempts += 1
        season = None
        m = re.search(r"/season-(\d+)/?$", url)
        if m:
            season = int(m.group(1))
        try:
            key = lookup(tm, meta.get("media", "tv"), season, token)
        except trailer.AuthError as exc:
            print(f"[warn] {exc}; stopping trailer lookups for this run", file=sys.stderr)
            _freeze_trailers(emitted, todo, "failed")
            return found, True
        except trailer.TrailerError as exc:
            print(f"[warn] trailer lookup failed for {url}: {exc}", file=sys.stderr)
            meta.pop("trailer_due", None)
            meta["trailer"] = {"v": trailer.VERSION, "key": None, "failed": True}
            misses += 1
            time.sleep(delay)
            if misses >= DEGRADED_AFTER:
                print(f"[warn] {misses} trailer lookups in a row failed; stopping for this run",
                      file=sys.stderr)
                _freeze_trailers(emitted, todo, "failed")
                return found, True
            continue
        time.sleep(delay)
        misses = 0
        meta.pop("trailer_due", None)
        rec = {"v": trailer.VERSION, "key": key}
        if today:
            rec["checked"] = today
        meta["trailer"] = rec
        found += bool(key)
    return found, False


def _de_date(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%d.%m.%Y")
    except (ValueError, TypeError):
        return ""


def media_label(meta: dict) -> str:
    """What goes in the parentheses of the item title: the streaming label for
    items that have one (TV since 21.10., movies since 22.10.2026), else
    "Movie"/"TV" as before."""
    p = meta.get("providers") or {}
    if p.get("match"):
        return streaming.label(p.get("summary") or {})
    return "Movie" if meta.get("media") == "movie" else "TV"


# --------------------------------------------------------------------------- #
# State machine (the "emit once, when it first qualifies" logic)
# --------------------------------------------------------------------------- #
def load_state(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {"emitted": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        # Do NOT silently start fresh: an empty state would re-emit every
        # currently-qualifying title and spam the reader. Abort so the last
        # good committed state is preserved.
        raise SystemExit(
            f"[abort] {path} is corrupt JSON ({exc}). Refusing to start with an empty "
            "state. Restore the file from git history or delete it deliberately to reset."
        )
    if not isinstance(data, dict):
        raise SystemExit(f"[abort] {path} is not a JSON object ({type(data).__name__}); refusing to start.")
    return data


def _atomic_write(path: str, text: str) -> None:
    """Write via a temp file + os.replace so a killed job can never leave a
    half-written feed.xml or state.json behind."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def save_state(path: str, state: dict) -> None:
    _atomic_write(path, json.dumps(state, indent=2, ensure_ascii=False))


def process_cards(cards: list[dict], emitted: dict, threshold: int, now: datetime,
                  seed_from_release: bool = False, providers: bool = False,
                  trailers: bool = False) -> int:
    """Add newly-qualifying cards to `emitted`. Returns count of new items.

    A card qualifies when it has a numeric score >= threshold AND its URL has
    not been emitted before. Once emitted, a title is never re-emitted, even if
    its score later changes — the <guid> stays stable and the reader shows it once.

    `feed_date` is frozen here, at first qualification, and drives <pubDate>/order
    (see _effective_date). `seed_from_release` omits it for a one-time backfill so
    the historical catch-up dates by release instead of piling up at the top.
    `image` (the browse poster) is stored when present; absent -> no thumbnail.
    """
    new = 0
    for c in cards:
        if c["score"] is None or c["score"] < threshold:
            continue
        if c["url"] in emitted:
            continue
        entry = {
            "title": c["title"],
            "score": c["score"],
            "media": c["media"],
            "release_date": c["date"],
            "emitted_at": now.isoformat(),
        }
        if not seed_from_release:
            entry["feed_date"] = now.isoformat()
        if c.get("poster"):
            entry["image"] = c["poster"]
        if providers:
            entry["providers_due"] = True
            if trailers:
                entry["trailer_due"] = True
        emitted[c["url"]] = entry
        new += 1
    return new


# --------------------------------------------------------------------------- #
# RSS generation (reader-agnostic RSS 2.0)
# --------------------------------------------------------------------------- #
def _parse_release(meta: dict) -> datetime:
    """Release date string -> tz-aware UTC datetime, used for <pubDate> and feed
    order. Falls back to the emit time, then the epoch, so ordering is always a
    total order (no crashes, no ties broken by chance)."""
    raw = (meta.get("release_date") or "").replace(".", "").strip()
    try:
        return datetime.strptime(raw, "%b %d, %Y").replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(meta.get("emitted_at", ""))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return datetime.min.replace(tzinfo=timezone.utc)


def _effective_date(meta: dict) -> datetime:
    """Date used for <pubDate> and feed order. Prefers the stored `feed_date`
    (frozen once, at first qualification) so a title scored weeks after release
    still surfaces at the TOP of the reader instead of being buried at its release
    date. Items emitted before this field existed have no `feed_date` and fall
    back to the release date — so their bytes and order stay unchanged (no churn,
    no re-surfacing of already-read items)."""
    fd = meta.get("feed_date")
    if fd:
        try:
            dt = datetime.fromisoformat(fd)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            pass
    return _parse_release(meta)


def _feed_sort_key(kv: tuple[str, dict]):
    """Deterministic total order: newest first (feed_date, else release), URL as
    stable tiebreak. URL tiebreak guarantees byte-identical feed.xml between runs
    when the item set is unchanged (which keeps the workflow from committing churn)."""
    url, meta = kv
    return (_effective_date(meta), url)


def describe_item(meta: dict) -> str:
    """Feed <description>: critic/user stats + a short top-critic quote.
    Falls back to a basic line if the detail page couldn't be enriched."""
    d = meta.get("detail") or {}
    label = "Movie" if meta["media"] == "movie" else "TV"
    has_info = any(k in d for k in ("critic_count", "pos", "user_score"))
    p = meta.get("providers") or {}
    stream = (f'<br />{streaming.describe(p.get("summary") or {}, _de_date(p.get("checked", "")))}'
              if p.get("match") else "")
    tl = trailer.link_html((meta.get("trailer") or {}).get("key") or "")
    if tl:
        stream += f"<br />{tl}"
    if not has_info:
        return f'Metascore {meta["score"]} · {label} · Released {meta.get("release_date", "")}' + stream

    parts = []
    # The critic score is already in the item title, so only repeat it here when
    # there is a user score to contrast it against (critics vs. users).
    if d.get("user_score") is not None:
        parts.append(f'Critics {meta["score"]}')
    if d.get("critic_count"):
        parts.append(f'{d["critic_count"]} reviews')
    if d.get("pos") is not None:
        parts.append(f'{d["pos"]}% positive')
    if d.get("user_score") is not None:
        u = f'Users {d["user_score"]:g}'
        if d.get("user_count"):
            u += f' ({d["user_count"]} ratings)'
        parts.append(u)

    return " · ".join(parts) + stream


def build_rss(items: list[tuple[str, dict]], args, last_build: str | None = None) -> str:
    """items: list of (url, meta) already sorted for display.
    last_build: ISO timestamp for <lastBuildDate>. Deriving it from state (newest
    emit time) instead of 'now' keeps the file byte-identical between runs when
    nothing changed -> the workflow's `git diff` skips the commit (no churn)."""
    try:
        lb = datetime.fromisoformat(last_build) if last_build else datetime.now(timezone.utc)
    except (ValueError, TypeError):
        lb = datetime.now(timezone.utc)
    chan_desc = (
        "Metacritic movie & TV releases with a Metascore (any), auto-generated."
        if args.threshold <= 0
        else f"Metacritic movie & TV releases scoring {args.threshold}+ (Metascore). Auto-generated."
    )
    if getattr(args, "trailers", False):
        # TMDB attribution (required for API use; logo and notice also in the README)
        chan_desc += " Trailer links: this product uses the TMDB API but is not endorsed or certified by TMDB."
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
        "<channel>",
        f"<title>{escape(args.feed_title)}</title>",
        f"<link>{escape(args.feed_link)}</link>",
        f"<description>{escape(chan_desc)}</description>",
        "<language>en</language>",
        f"<lastBuildDate>{format_datetime(lb)}</lastBuildDate>",
    ]
    if args.feed_self:
        out.append(f'<atom:link href="{escape(args.feed_self)}" rel="self" type="application/rss+xml"/>')

    for url, meta in items:
        label = "Movie" if meta["media"] == "movie" else "TV"   # <category> stays as it was
        title = f'[{meta["score"]}] {meta["title"]} ({media_label(meta)})'
        pub = format_datetime(_effective_date(meta))  # pubDate = qualifying time, else release
        desc = describe_item(meta)
        # Poster as an <img> inside the (HTML) description — the one image channel
        # every reader (Readwise/Tapestry/Reeder) renders. escape() entity-encodes
        # the whole body once; the reader decodes it back to HTML and shows the img.
        # Prefer the hi-res detail og:image (added at enrichment); fall back to the
        # small browse poster so a fresh item still has a thumbnail before enrichment.
        img = (meta.get("detail") or {}).get("image") or meta.get("image")
        body = f'<img src="{_attr(img)}" alt="" /><br />{desc}' if img else desc
        out += [
            "<item>",
            f"<title>{_xml_text(title)}</title>",
            f"<link>{escape(url)}</link>",
            f'<guid isPermaLink="false">{escape(url)}</guid>',
            f"<pubDate>{pub}</pubDate>",
            f"<category>{escape(label)}</category>",
            f"<description>{_xml_text(body)}</description>",
            "</item>",
        ]

    out += ["</channel>", "</rss>"]
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def run(args) -> int:
    state = load_state(args.state)
    emitted = state.setdefault("emitted", {})
    now = datetime.now(timezone.utc)
    new_total = 0
    scanned = 0
    pages_ok = 0
    pages_tried = 0
    # Per-medium counters. The global guards below cannot see a HALF-dead run:
    # if only the TV markup changes, movie still yields cards and the run stays
    # green while the TV half of the feed quietly stops forever.
    stats = {m: {"pages_ok": 0, "cards": 0, "scored": 0} for m in args.media}
    links: set = set()
    seasons_degraded = False

    for media in args.media:
        for page in range(1, args.pages + 1):
            url = BASE + BROWSE[media]
            if page > 1:
                url += f"?page={page}"
            pages_tried += 1
            html_text = None
            for attempt in range(2):   # one retry: 429/5xx on a single page is common
                try:
                    html_text = fetch(url)
                    break
                except Exception as exc:
                    if attempt == 0:
                        time.sleep(max(args.browse_delay, 1.0) * 3)
                        continue
                    print(f"[warn] fetch failed for {url}: {exc}", file=sys.stderr)
            if html_text is None:
                continue
            time.sleep(args.browse_delay)   # politeness between browse pages
            pages_ok += 1
            stats[media]["pages_ok"] += 1
            cards = parse_browse(html_text, media)
            if media == "tv" and args.seasons:
                links |= season_links(html_text)
            stats[media]["cards"] += len(cards)
            stats[media]["scored"] += sum(1 for c in cards if c["score"] is not None)
            scanned += len(cards)
            print(f"[scan] {media} page {page}: {len(cards)} cards")
            if args.debug:
                for c in cards:
                    flag = "OK " if (c["score"] is not None and c["score"] >= args.threshold) else "   "
                    print(f"       [{flag}] score={str(c['score']):>4}  {c['title']}  ::  {c['url']}")
            new_total += process_cards(cards, emitted, args.threshold, now,
                                       seed_from_release=args.seed_from_release,
                                       providers=args.providers,
                                       trailers=args.trailers)

    # Fail loudly instead of silently freezing the feed:
    #   pages_ok == 0  -> network / total outage
    #   scanned == 0   -> pages loaded but no cards: soft-block (Cloudflare 200) or layout change
    if pages_ok == 0:
        raise SystemExit(f"[abort] all {pages_tried} page fetch(es) failed; feed/state left untouched.")
    if scanned == 0:
        raise SystemExit(
            f"[abort] {pages_ok} page(s) loaded but yielded 0 cards — likely a soft-block or "
            "layout change. Feed/state left untouched."
        )
    # Per-medium guards. Each of these is a state that CANNOT happen on a healthy
    # run (every browse page carries scored titles), so hitting one means the
    # scrape changed under us — abort rather than let the feed quietly go stale.
    for media, st in stats.items():
        if st["pages_ok"] == 0:
            raise SystemExit(f"[abort] every '{media}' browse page failed to load; feed/state left untouched.")
        if st["cards"] == 0:
            raise SystemExit(
                f"[abort] '{media}' loaded {st['pages_ok']} page(s) but yielded 0 cards — "
                "layout change or soft-block. Feed/state left untouched."
            )
        if st["scored"] == 0:
            raise SystemExit(
                f"[abort] '{media}' yielded {st['cards']} card(s) but not one with a Metascore — "
                "the score markup changed (SCORE_RE no longer matches). Feed/state left untouched."
            )

    # New seasons of running shows: header-nav links (collected above) plus the
    # premiere-dates article. Both are supplementary — a failure here is logged,
    # never fatal, and never blocks the movie/show feed.
    if "tv" in args.media and args.seasons:
        try:
            links |= season_links(fetch(BASE + PREMIERE_PAGE))
        except Exception as exc:
            print(f"[warn] premiere page failed: {exc}", file=sys.stderr)
        if not links:
            print("[warn] no season links found (navigation and premiere page)", file=sys.stderr)
        s_new, s_tried, s_failed, s_struct = check_seasons(state, emitted, links, args, now)
        new_total += s_new
        print(f"[seasons] {len(links)} link(s), checked {s_tried} ({s_failed} failed), {s_new} new season(s)")
        # Fail loud, but after the commit: the season branch is supplementary, so
        # a broken season page or a link source that dried up must not stop the
        # feed — yet it must not go unnoticed either.
        if not links or (s_tried >= 5 and s_struct == s_tried):
            seasons_degraded = True
            print("[warn] season discovery degraded (no links, or no season page readable)",
                  file=sys.stderr)

    # Enrich new/unenriched items with critic/user stats + a top-critic quote.
    degraded = False
    if args.detail and not args.dry_run and args.detail_max > 0:
        got, degraded = enrich_missing(emitted, args.detail_max, args.detail_delay)
        if got:
            print(f"[detail] enriched {got} item(s) with critic/user info")

    # Where new TV items stream in Germany. Supplementary: never fatal.
    providers_degraded = False
    if not args.dry_run:
        got, providers_degraded = enrich_providers(emitted, max(args.provider_max, 0),
                                                   args.provider_delay, today=now.date().isoformat())
        if got:
            print(f"[streaming] looked up {got} item(s)")

    # Official YouTube trailer of new items (needs the TMDB id from the streaming match).
    trailers_degraded = False
    if not args.dry_run:
        got, trailers_degraded = enrich_trailers(emitted, max(args.trailer_max, 0), args.trailer_delay,
                                                 os.environ.get("TMDB_TOKEN", "").strip(),
                                                 today=now.date().isoformat())
        if got:
            print(f"[trailer] found {got} trailer(s)")

    items = sorted(emitted.items(), key=_feed_sort_key, reverse=True)[: args.feed_max]
    last_build = max((m.get("emitted_at", "") for m in emitted.values()), default=now.isoformat())

    if args.dry_run:
        print(
            f"[dry-run] scanned {scanned} cards ({pages_ok}/{pages_tried} pages ok); "
            f"{new_total} new qualifier(s) >= {args.threshold}; "
            f"feed would contain {len(items)} item(s). Nothing written."
        )
        return new_total

    # Deterministic output: identical bytes when nothing changed, so the workflow's
    # `git diff --cached --quiet` skips the commit and there is no every-run churn.
    _atomic_write(args.out, build_rss(items, args, last_build))
    save_state(args.state, state)
    if degraded or seasons_degraded or providers_degraded or trailers_degraded:
        # The feed and state above are correct and get committed; the workflow
        # turns these flags into a red run only after the commit step.
        gh_out = os.environ.get("GITHUB_OUTPUT")
        if gh_out:
            with open(gh_out, "a", encoding="utf-8") as fh:
                if degraded:
                    fh.write("degraded=true\n")
                if seasons_degraded:
                    fh.write("seasons_degraded=true\n")
                if providers_degraded:
                    fh.write("providers_degraded=true\n")
                if trailers_degraded:
                    fh.write("trailers_degraded=true\n")
    print(
        f"Scanned {scanned} cards across {args.media} x {args.pages} page(s), {pages_ok}/{pages_tried} ok. "
        f"{new_total} new qualifier(s) >= {args.threshold}. Feed now has {len(items)} item(s) -> {args.out}"
    )
    return new_total


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Metacritic score-threshold RSS feed generator")
    p.add_argument("--threshold", type=int, default=int(_env("MC_THRESHOLD", "0")),
                   help="minimum Metascore to include (default 0 = every title that has a Metascore; "
                        "e.g. 61 = Metacritic 'generally favorable')")
    p.add_argument("--media", default=_env("MC_MEDIA", "movie,tv"),
                   help="comma-separated: movie,tv (default 'movie,tv')")
    p.add_argument("--pages", type=int, default=int(_env("MC_PAGES", "3")),
                   help="how many browse pages per media to scan (default 3, ~24 items/page)")
    p.add_argument("--feed-max", type=int, default=int(_env("MC_FEED_MAX", "100")),
                   help="max items kept in the feed (default 100)")
    p.add_argument("--out", default=_env("MC_OUT", "feed.xml"))
    p.add_argument("--state", default=_env("MC_STATE", "state.json"))
    p.add_argument("--feed-title", default=_env("MC_FEED_TITLE", ""))
    p.add_argument("--feed-link", default=_env("MC_FEED_LINK", BASE + BROWSE["movie"]))
    p.add_argument("--feed-self", default=_env("MC_FEED_SELF", ""),
                   help="public URL where feed.xml is hosted (adds an atom:self link)")
    p.add_argument("--detail", dest="detail", action="store_true",
                   default=_env("MC_DETAIL", "1") not in ("0", "false", "False", ""),
                   help="fetch each new title's detail page for critic/user stats (default on)")
    p.add_argument("--no-detail", dest="detail", action="store_false",
                   help="skip detail pages; use the basic description")
    p.add_argument("--detail-max", type=int, default=int(_env("MC_DETAIL_MAX", "60")),
                   help="max detail pages to fetch per run (default 60; bounds the one-time backfill)")
    p.add_argument("--detail-delay", type=float, default=float(_env("MC_DETAIL_DELAY", "0.6")),
                   help="seconds to wait between detail fetches (politeness, default 0.6)")
    p.add_argument("--seasons", dest="seasons", action="store_true",
                   default=_env("MC_SEASONS", "1") not in ("0", "false", "False", ""),
                   help="also emit new seasons of running shows (default on)")
    p.add_argument("--no-seasons", dest="seasons", action="store_false")
    p.add_argument("--season-max", type=int, default=int(_env("MC_SEASON_MAX", "10")),
                   help="max season pages to check per run (default 10)")
    p.add_argument("--providers", dest="providers", action="store_true",
                   default=_env("MC_PROVIDERS", "1") not in ("0", "false", "False", ""),
                   help="look up where new titles stream in Germany (JustWatch, default on)")
    p.add_argument("--no-providers", dest="providers", action="store_false")
    p.add_argument("--provider-max", type=int, default=int(_env("MC_PROVIDER_MAX", "120")),
                   help="max streaming lookups per run (default 120; items beyond keep (TV))")
    p.add_argument("--provider-delay", type=float, default=float(_env("MC_PROVIDER_DELAY", "0.5")),
                   help="seconds to wait between streaming lookups (default 0.5)")
    p.add_argument("--trailers", dest="trailers", action="store_true",
                   default=_env("MC_TRAILERS", "1") not in ("0", "false", "False", ""),
                   help="link the official YouTube trailer of new titles (TMDB, needs TMDB_TOKEN; default on)")
    p.add_argument("--no-trailers", dest="trailers", action="store_false")
    p.add_argument("--trailer-max", type=int, default=int(_env("MC_TRAILER_MAX", "120")),
                   help="max trailer lookups per run (default 120; items beyond get no link)")
    p.add_argument("--trailer-delay", type=float, default=float(_env("MC_TRAILER_DELAY", "0.25")),
                   help="seconds to wait between trailer lookups (default 0.25)")
    p.add_argument("--browse-delay", type=float, default=float(_env("MC_BROWSE_DELAY", "0.4")),
                   help="seconds to wait between browse pages (politeness / 429 prophylaxis, default 0.4)")
    p.add_argument("--seed-from-release", dest="seed_from_release", action="store_true",
                   default=_env("MC_SEED_FROM_RELEASE", "0") not in ("0", "false", "False", ""),
                   help="one-time: date newly-emitted items by their release date instead of 'now'. "
                        "Use once when lowering the threshold so the historical backfill scatters by "
                        "date rather than piling up at the top of the reader.")
    p.add_argument("--dry-run", action="store_true", help="don't write files, just report")
    p.add_argument("--debug", action="store_true", help="print every parsed card")
    return p


def main(argv=None) -> int:
    args = build_argparser().parse_args(argv)
    args.media = [m.strip() for m in args.media.split(",") if m.strip() in BROWSE]
    if not args.media:
        print("[error] --media must include at least one of: movie, tv", file=sys.stderr)
        return 2
    if not args.feed_title:
        args.feed_title = (
            "Metacritic — Movies & TV (all scored)"
            if args.threshold <= 0
            else f"Metacritic — Movies & TV (Metascore {args.threshold}+)"
        )
    run(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
