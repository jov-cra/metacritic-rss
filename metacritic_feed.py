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
SCORE_RE = re.compile(r"(\d{1,3})\s*Metascore")
HEADING_RE = re.compile(r"^h[1-6]$")
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
        t = re.sub(r"\s+", " ", heading.get_text(" ", strip=True)).strip()
        t = t.replace("must-see", "").strip()
        if t:
            return t

    pre = text[:date_start].replace("must-see", "").strip()
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
        if score is not None and score > 100:
            score = None  # guard against a stray number matched before "Metascore"

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
# State machine (the "emit once, when it first qualifies" logic)
# --------------------------------------------------------------------------- #
def load_state(path: str) -> dict:
    p = Path(path)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"[warn] {path} was invalid JSON; starting fresh", file=sys.stderr)
    return {"emitted": {}}


def save_state(path: str, state: dict) -> None:
    Path(path).write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


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
def build_rss(items: list[tuple[str, dict]], args) -> str:
    """items: list of (url, meta) already sorted newest-emitted-first."""
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
        "<channel>",
        f"<title>{escape(args.feed_title)}</title>",
        f"<link>{escape(args.feed_link)}</link>",
        f"<description>{escape('Metacritic movie & TV releases scoring ' + str(args.threshold) + '+ (Metascore). Auto-generated.')}</description>",
        "<language>en</language>",
        f"<lastBuildDate>{format_datetime(datetime.now(timezone.utc))}</lastBuildDate>",
    ]
    if args.feed_self:
        out.append(f'<atom:link href="{escape(args.feed_self)}" rel="self" type="application/rss+xml"/>')

    for url, meta in items:
        label = "Movie" if meta["media"] == "movie" else "TV"
        title = f'[{meta["score"]}] {meta["title"]} ({label})'
        try:
            pub = format_datetime(datetime.fromisoformat(meta["emitted_at"]))
        except (ValueError, KeyError):
            pub = format_datetime(datetime.now(timezone.utc))
        desc = (
            f'Metascore: {meta["score"]}  •  {label}  •  '
            f'Released: {meta.get("release_date", "")}'
            f'<br/><a href="{url}">View on Metacritic</a>'
        )
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

    for media in args.media:
        for page in range(1, args.pages + 1):
            url = BASE + BROWSE[media]
            if page > 1:
                url += f"?page={page}"
            try:
                html_text = fetch(url)
            except Exception as exc:  # network / HTTP problems shouldn't kill the run
                print(f"[warn] fetch failed for {url}: {exc}", file=sys.stderr)
                continue
            cards = parse_browse(html_text, media)
            scanned += len(cards)
            if args.debug:
                for c in cards:
                    flag = "OK " if (c["score"] and c["score"] >= args.threshold) else "   "
                    print(f"  [{flag}] {media:5} score={str(c['score']):>4}  {c['title']}  ::  {c['url']}")
            new_here = process_cards(cards, emitted, args.threshold, now)
            new_total += new_here

    items = sorted(emitted.items(), key=lambda kv: kv[1]["emitted_at"], reverse=True)[: args.feed_max]
    xml = build_rss(items, args)

    if args.dry_run:
        print(
            f"[dry-run] scanned {scanned} cards; {new_total} new qualifier(s) "
            f">= {args.threshold}; feed would contain {len(items)} item(s). "
            "Nothing written."
        )
        return new_total

    Path(args.out).write_text(xml, encoding="utf-8")
    save_state(args.state, state)
    print(
        f"Scanned {scanned} cards across {args.media} x {args.pages} page(s). "
        f"{new_total} new qualifier(s) >= {args.threshold}. "
        f"Feed now has {len(items)} item(s) -> {args.out}"
    )
    return new_total


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Metacritic score-threshold RSS feed generator")
    p.add_argument("--threshold", type=int, default=int(_env("MC_THRESHOLD", "85")),
                   help="minimum Metascore to include (default 85)")
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
