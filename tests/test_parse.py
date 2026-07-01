"""
Unit tests for metacritic_feed.

Covers the three things that carry all the risk:
  1. Parsing the browse page (scores, unscored items, excluding nav links).
  2. The "emit once, when it first qualifies" state machine — including the
     key case: an item that was UNscored later gets a qualifying score.
  3. Well-formed RSS output with stable GUIDs and RFC-822 dates.

Run:  python -m pytest -q     (or)     python tests/test_parse.py
"""
import sys
from datetime import datetime, timezone
from pathlib import Path
from xml.dom.minidom import parseString

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import metacritic_feed as mf  # noqa: E402

FIXTURE = (Path(__file__).parent / "fixtures" / "sample_movie_browse.html").read_text(encoding="utf-8")


def _cards_by_url():
    return {c["url"]: c for c in mf.parse_browse(FIXTURE, "movie")}


# --------------------------------------------------------------------------- #
# 1. Parsing
# --------------------------------------------------------------------------- #
def test_parse_excludes_nav_links():
    urls = set(_cards_by_url())
    # Nav links to /movie/toy-story-5/ and /movie/obsession-2025/ have no date.
    assert "https://www.metacritic.com/movie/toy-story-5/" not in urls
    assert "https://www.metacritic.com/movie/obsession-2025/" not in urls


def test_parse_finds_all_real_cards():
    urls = set(_cards_by_url())
    assert urls == {
        "https://www.metacritic.com/movie/leviticus/",
        "https://www.metacritic.com/movie/blind-love/",
        "https://www.metacritic.com/movie/rose-of-nevada/",
        "https://www.metacritic.com/movie/a-great-film/",
    }


def test_parse_scores_and_titles():
    c = _cards_by_url()
    assert c["https://www.metacritic.com/movie/leviticus/"]["score"] == 81
    assert c["https://www.metacritic.com/movie/rose-of-nevada/"]["score"] == 88
    assert c["https://www.metacritic.com/movie/a-great-film/"]["score"] == 93
    # Unscored item -> score is None, not 0
    assert c["https://www.metacritic.com/movie/blind-love/"]["score"] is None
    # Titles are clean (heading preferred, "must-see" badge stripped)
    assert c["https://www.metacritic.com/movie/rose-of-nevada/"]["title"] == "Rose of Nevada"
    assert c["https://www.metacritic.com/movie/blind-love/"]["title"] == "Blind Love"


def test_query_string_stripped_from_guid():
    assert "https://www.metacritic.com/movie/a-great-film/" in _cards_by_url()


def test_title_fallback_without_heading():
    """If Metacritic ever drops the heading tag, titles must still come out
    clean from the fallback path: title is duplicated (img alt + visible text)
    with a 'must-see' badge wedged in, score glued to the label with no space."""
    html = (
        '<div><a href="/movie/rose-of-nevada/">'
        "Rose of Nevada must-see Rose of Nevada"
        "Jun 19, 2026 • Rated R"
        "30 years ago, the Rose of Nevada disappeared at sea."
        "88Metascore"
        "</a></div>"
    )
    (card,) = mf.parse_browse(html, "movie")
    assert card["title"] == "Rose of Nevada"   # de-duplicated, badge stripped
    assert card["score"] == 88                 # matched even with no space before "Metascore"
    assert card["date"] == "Jun 19, 2026"


# --------------------------------------------------------------------------- #
# 2. State machine
# --------------------------------------------------------------------------- #
def test_emit_once_and_threshold():
    cards = mf.parse_browse(FIXTURE, "movie")
    emitted = {}
    now = datetime(2026, 6, 20, tzinfo=timezone.utc)

    # threshold 85 -> only rose (88) and a-great-film (93) qualify; 81 and unscored do not
    new = mf.process_cards(cards, emitted, threshold=85, now=now)
    assert new == 2
    assert set(emitted) == {
        "https://www.metacritic.com/movie/rose-of-nevada/",
        "https://www.metacritic.com/movie/a-great-film/",
    }

    # Running again over the same cards emits nothing new (dedup by URL/guid)
    new_again = mf.process_cards(cards, emitted, threshold=85, now=now)
    assert new_again == 0
    assert len(emitted) == 2


def test_score_assigned_later_is_the_whole_point():
    """An item first seen UNscored must be emitted later, once it crosses the
    threshold — and an already-emitted item whose score rises must NOT re-emit."""
    emitted = {}
    t1 = datetime(2026, 6, 20, tzinfo=timezone.utc)

    # Day 1: blind-love has no score, rose is 88.
    day1 = [
        {"url": "https://www.metacritic.com/movie/blind-love/", "title": "Blind Love",
         "score": None, "date": "Jun 19, 2026", "media": "movie"},
        {"url": "https://www.metacritic.com/movie/rose-of-nevada/", "title": "Rose of Nevada",
         "score": 88, "date": "Jun 19, 2026", "media": "movie"},
    ]
    assert mf.process_cards(day1, emitted, 85, t1) == 1
    assert "https://www.metacritic.com/movie/blind-love/" not in emitted

    # Day 2: blind-love now has 86 (crossed threshold); rose rose to 95.
    t2 = datetime(2026, 6, 22, tzinfo=timezone.utc)
    day2 = [
        {"url": "https://www.metacritic.com/movie/blind-love/", "title": "Blind Love",
         "score": 86, "date": "Jun 19, 2026", "media": "movie"},
        {"url": "https://www.metacritic.com/movie/rose-of-nevada/", "title": "Rose of Nevada",
         "score": 95, "date": "Jun 19, 2026", "media": "movie"},
    ]
    assert mf.process_cards(day2, emitted, 85, t2) == 1  # only blind-love is new
    assert emitted["https://www.metacritic.com/movie/blind-love/"]["score"] == 86
    # rose keeps its ORIGINAL emit score/time — not re-emitted
    assert emitted["https://www.metacritic.com/movie/rose-of-nevada/"]["score"] == 88
    assert emitted["https://www.metacritic.com/movie/rose-of-nevada/"]["emitted_at"] == t1.isoformat()


# --------------------------------------------------------------------------- #
# 3. RSS output
# --------------------------------------------------------------------------- #
class _Args:
    threshold = 85
    feed_title = "Metacritic — Movies & TV (Metascore 85+)"
    feed_link = "https://www.metacritic.com/browse/movie/all/all/all-time/new/"
    feed_self = "https://example.github.io/mc/feed.xml"


def test_rss_is_well_formed_and_complete():
    emitted = {
        "https://www.metacritic.com/movie/rose-of-nevada/": {
            "title": "Rose of Nevada & Friends", "score": 88, "media": "movie",
            "release_date": "Jun 19, 2026", "emitted_at": "2026-06-20T00:00:00+00:00",
        },
        "https://www.metacritic.com/tv/star-city/": {
            "title": "Star City", "score": 90, "media": "tv",
            "release_date": "May 29, 2026", "emitted_at": "2026-06-21T00:00:00+00:00",
        },
    }
    items = sorted(emitted.items(), key=lambda kv: kv[1]["emitted_at"], reverse=True)
    xml = mf.build_rss(items, _Args())

    dom = parseString(xml)  # raises if not well-formed (also proves & is escaped)
    item_nodes = dom.getElementsByTagName("item")
    assert len(item_nodes) == 2

    # newest emitted first
    first_title = item_nodes[0].getElementsByTagName("title")[0].firstChild.data
    assert first_title == "[90] Star City (TV)"

    guids = [n.getElementsByTagName("guid")[0].firstChild.data for n in item_nodes]
    assert "https://www.metacritic.com/movie/rose-of-nevada/" in guids

    # RFC-822 pubDate (readers require this format)
    pub = item_nodes[0].getElementsByTagName("pubDate")[0].firstChild.data
    from email.utils import parsedate_tz
    assert parsedate_tz(pub) is not None


# --------------------------------------------------------------------------- #
# 4. Release-date parsing, feed ordering, and state guards (QA-added)
# --------------------------------------------------------------------------- #
def test_parse_release_handles_period_and_fallback():
    assert mf._parse_release({"release_date": "Jun 19, 2026"}).date().isoformat() == "2026-06-19"
    assert mf._parse_release({"release_date": "Jul. 1, 2026"}).date().isoformat() == "2026-07-01"
    # missing/garbage release date -> falls back to emitted_at
    got = mf._parse_release({"release_date": "", "emitted_at": "2026-06-20T00:00:00+00:00"})
    assert got.date().isoformat() == "2026-06-20"


def test_feed_sorted_by_release_desc_with_url_tiebreak():
    a = ("https://m/tv/a/", {"release_date": "May 29, 2026"})
    b = ("https://m/movie/b/", {"release_date": "Jun 19, 2026"})
    assert [u for u, _ in sorted([a, b], key=mf._feed_sort_key, reverse=True)] == \
        ["https://m/movie/b/", "https://m/tv/a/"]  # Jun 19 newer than May 29
    # identical release dates -> deterministic URL tiebreak (reverse => 'z' before 'a')
    z = ("https://m/z/", {"release_date": "Jun 19, 2026"})
    aa = ("https://m/a/", {"release_date": "Jun 19, 2026"})
    assert [u for u, _ in sorted([aa, z], key=mf._feed_sort_key, reverse=True)] == \
        ["https://m/z/", "https://m/a/"]


def test_score_out_of_range_rejected():
    html = ('<a href="/movie/x/"><h3>X</h3><span>Jun 19, 2026</span>'
            '<div>120</div><span>Metascore</span></a>')
    (card,) = mf.parse_browse(html, "movie")
    assert card["score"] is None  # 120 is not a valid 0-100 Metascore


def test_corrupt_and_nondict_state_abort():
    import os, tempfile
    d = tempfile.mkdtemp()
    corrupt = os.path.join(d, "corrupt.json")
    Path(corrupt).write_text("{not valid json")
    try:
        mf.load_state(corrupt)
        assert False, "corrupt state should raise SystemExit"
    except SystemExit:
        pass
    nulled = os.path.join(d, "null.json")
    Path(nulled).write_text("null")
    try:
        mf.load_state(nulled)
        assert False, "non-dict state should raise SystemExit"
    except SystemExit:
        pass
    # missing file is a legit fresh start, NOT an abort
    assert mf.load_state(os.path.join(d, "does-not-exist.json")) == {"emitted": {}}


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL  {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    raise SystemExit(1 if failed else 0)
