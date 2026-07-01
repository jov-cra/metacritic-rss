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


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


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

        record = {
            "url": url,
            "title": _clean_title(anchor, text, date_match.start()),
            "score": score,
            "date": date_match.group(1),
            "media": media,
            "_len": len(text),
        }
        # Keep the richest entry per URL (the full card, not a nested sub-link).
        prev = by_url.get(url)
        if prev is None or record["_len"] > prev["_len"]:
            by_url[url] = record

    result = list(by_url.values())
    for r in result:
        r.pop("_len", None)
    return result


# --------------------------------------------------------------------------- #
# Detail-page enrichment: critic/user counts + a short top-critic quote
# --------------------------------------------------------------------------- #
REVIEW_SPLIT_RE = re.compile(
    r"(\d+)%\s*Positive\s+\d+\s+Reviews\s+(\d+)%\s*Mixed\s+\d+\s+Reviews\s+(\d+)%\s*Negative\s+\d+\s+Reviews"
)


def fetch_detail(url: str) -> str:
    return fetch(url)


def parse_detail(html_text: str) -> dict:
    """Best-effort extraction from a title's detail page. Every field is
    optional; the feed description degrades gracefully if a field is missing.
    Returns {} when nothing useful was found."""
    soup = BeautifulSoup(html_text, "html.parser")
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    d: dict = {}

    m = re.search(r"Based on (\d+)\s+Critic Reviews", text)
    if m:
        d["critic_count"] = int(m.group(1))
    m = REVIEW_SPLIT_RE.search(text)
    if m:
        d["pos"], d["mixed"], d["neg"] = int(m.group(1)), int(m.group(2)), int(m.group(3))

    # user ratings count + score (score is often "tbd" until enough ratings)
    m = re.search(r"Based on (\d+)\s+Ratings", text)
    if m:
        d["user_count"] = int(m.group(1))
        mu = re.search(r"User score\D{0,40}?(\d(?:\.\d)?)", text)
        if mu:
            d["user_score"] = float(mu.group(1))
    else:
        m = re.search(r"Available after (\d+)\s+ratings", text)
        if m:
            d["user_count"] = int(m.group(1))

    # top critic quote: publications render as /publication/ links whose text is
    # "<score> <Publication>"; the quote is the longest text run in that review card.
    for a in soup.find_all("a", href=re.compile(r"^/publication/")):
        mm = re.match(r"(\d{1,3})\s+(.+)", re.sub(r"\s+", " ", a.get_text(" ", strip=True)))
        if not mm:
            continue
        card = a
        for _ in range(6):
            card = card.parent
            if card is None or "FULL REVIEW" in card.get_text(" ", strip=True):
                break
        if card is None:
            continue
        candidates = [
            s.strip() for s in card.stripped_strings
            if len(s.strip()) > 25 and not s.strip().startswith("By ") and "FULL REVIEW" not in s
        ]
        quote = max(candidates, key=len, default=None)
        if quote:
            d["quote_score"] = int(mm.group(1))
            d["quote_pub"] = mm.group(2).strip()
            d["quote"] = quote
            break
    return d


def enrich_missing(emitted: dict, cap: int, delay: float) -> int:
    """Fetch detail pages for emitted items lacking `detail` (new qualifiers +
    one-time backfill), newest first, up to `cap` per run. Once set, `detail` is
    frozen -> feed stays deterministic (no churn) after backfill completes."""
    todo = sorted(
        (u for u, m in emitted.items() if "detail" not in m),
        key=lambda u: emitted[u].get("emitted_at", ""), reverse=True,
    )
    done = 0
    for url in todo:
        if done >= cap:
            break
        try:
            emitted[url]["detail"] = parse_detail(fetch_detail(url))
        except Exception as exc:
            print(f"[warn] detail fetch failed for {url}: {exc}", file=sys.stderr)
            continue
        done += 1
        time.sleep(delay)
    return done


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


def process_cards(cards: list[dict], emitted: dict, threshold: int, now: datetime) -> int:
    """Add newly-qualifying cards to `emitted`. Returns count of new items.

    A card qualifies when it has a numeric score >= threshold AND its URL has
    not been emitted before. Once emitted, a title is never re-emitted, even if
    its score later changes — the <guid> stays stable and the reader shows it once.
    """
    new = 0
    for c in cards:
        if c["score"] is None or c["score"] < threshold:
            continue
        if c["url"] in emitted:
            continue
        emitted[c["url"]] = {
            "title": c["title"],
            "score": c["score"],
            "media": c["media"],
            "release_date": c["date"],
            "emitted_at": now.isoformat(),
        }
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


def _feed_sort_key(kv: tuple[str, dict]):
    """Deterministic total order: newest release first, URL as stable tiebreak.
    URL tiebreak guarantees byte-identical feed.xml between runs when the item
    set is unchanged (which is what keeps the workflow from committing churn)."""
    url, meta = kv
    return (_parse_release(meta), url)


def describe_item(meta: dict) -> str:
    """Feed <description>: critic/user stats + a short top-critic quote.
    Falls back to a basic line if the detail page couldn't be enriched."""
    d = meta.get("detail") or {}
    label = "Movie" if meta["media"] == "movie" else "TV"
    if not d:
        return f'Metascore {meta["score"]} · {label} · Released {meta.get("release_date", "")}'

    parts = [f'Critics {meta["score"]}']
    if d.get("critic_count"):
        parts.append(f'{d["critic_count"]} reviews')
    if d.get("pos") is not None:
        parts.append(f'{d["pos"]}% positive')
    if d.get("user_score") is not None:
        u = f'Users {d["user_score"]:g}'
        if d.get("user_count"):
            u += f' ({d["user_count"]} ratings)'
        parts.append(u)
    elif d.get("user_count") is not None:
        parts.append(f'Users tbd ({d["user_count"]} ratings)')

    line = " · ".join(parts)
    if d.get("quote"):
        q = d["quote"]
        if len(q) > 110:
            q = q[:109].rsplit(" ", 1)[0].rstrip(",;:—- ") + "…"
        pub = d.get("quote_pub", "")
        line += f' · "{q}"' + (f" — {pub}" if pub else "")
    return line


def build_rss(items: list[tuple[str, dict]], args, last_build: str | None = None) -> str:
    """items: list of (url, meta) already sorted for display.
    last_build: ISO timestamp for <lastBuildDate>. Deriving it from state (newest
    emit time) instead of 'now' keeps the file byte-identical between runs when
    nothing changed -> the workflow's `git diff` skips the commit (no churn)."""
    try:
        lb = datetime.fromisoformat(last_build) if last_build else datetime.now(timezone.utc)
    except (ValueError, TypeError):
        lb = datetime.now(timezone.utc)
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
        "<channel>",
        f"<title>{escape(args.feed_title)}</title>",
        f"<link>{escape(args.feed_link)}</link>",
        f"<description>{escape('Metacritic movie & TV releases scoring ' + str(args.threshold) + '+ (Metascore). Auto-generated.')}</description>",
        "<language>en</language>",
        f"<lastBuildDate>{format_datetime(lb)}</lastBuildDate>",
    ]
    if args.feed_self:
        out.append(f'<atom:link href="{escape(args.feed_self)}" rel="self" type="application/rss+xml"/>')

    for url, meta in items:
        label = "Movie" if meta["media"] == "movie" else "TV"
        title = f'[{meta["score"]}] {meta["title"]} ({label})'
        pub = format_datetime(_parse_release(meta))  # pubDate = actual release date
        desc = describe_item(meta)
        out += [
            "<item>",
            f"<title>{escape(title)}</title>",
            f"<link>{escape(url)}</link>",
            f'<guid isPermaLink="false">{escape(url)}</guid>',
            f"<pubDate>{pub}</pubDate>",
            f"<category>{escape(label)}</category>",
            f"<description>{escape(desc)}</description>",
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

    for media in args.media:
        for page in range(1, args.pages + 1):
            url = BASE + BROWSE[media]
            if page > 1:
                url += f"?page={page}"
            pages_tried += 1
            try:
                html_text = fetch(url)
            except Exception as exc:  # a single failed page shouldn't kill the whole run
                print(f"[warn] fetch failed for {url}: {exc}", file=sys.stderr)
                continue
            pages_ok += 1
            cards = parse_browse(html_text, media)
            scanned += len(cards)
            print(f"[scan] {media} page {page}: {len(cards)} cards")
            if args.debug:
                for c in cards:
                    flag = "OK " if (c["score"] and c["score"] >= args.threshold) else "   "
                    print(f"       [{flag}] score={str(c['score']):>4}  {c['title']}  ::  {c['url']}")
            new_total += process_cards(cards, emitted, args.threshold, now)

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

    # Enrich new/unenriched items with critic/user stats + a top-critic quote.
    if args.detail and not args.dry_run and args.detail_max > 0:
        got = enrich_missing(emitted, args.detail_max, args.detail_delay)
        if got:
            print(f"[detail] enriched {got} item(s) with critic/user info")

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
    print(
        f"Scanned {scanned} cards across {args.media} x {args.pages} page(s), {pages_ok}/{pages_tried} ok. "
        f"{new_total} new qualifier(s) >= {args.threshold}. Feed now has {len(items)} item(s) -> {args.out}"
    )
    return new_total


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Metacritic score-threshold RSS feed generator")
    p.add_argument("--threshold", type=int, default=int(_env("MC_THRESHOLD", "61")),
                   help="minimum Metascore to include (default 61 = Metacritic 'generally favorable')")
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
                   help="fetch each new title's detail page for critic/user stats + a quote (default on)")
    p.add_argument("--no-detail", dest="detail", action="store_false",
                   help="skip detail pages; use the basic description")
    p.add_argument("--detail-max", type=int, default=int(_env("MC_DETAIL_MAX", "60")),
                   help="max detail pages to fetch per run (default 60; bounds the one-time backfill)")
    p.add_argument("--detail-delay", type=float, default=float(_env("MC_DETAIL_DELAY", "0.6")),
                   help="seconds to wait between detail fetches (politeness, default 0.6)")
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
        args.feed_title = f"Metacritic — Movies & TV (Metascore {args.threshold}+)"
    run(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
