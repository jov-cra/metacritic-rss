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


def test_parse_extracts_poster():
    c = _cards_by_url()
    # srcset present -> highest-density (2x) URL wins
    assert c["https://www.metacritic.com/movie/rose-of-nevada/"]["poster"] == "https://img.test/rose_w192.jpg"
    # only a plain src -> that src
    assert c["https://www.metacritic.com/movie/leviticus/"]["poster"] == "x.jpg"
    # no <img> at all -> None (feed simply omits the thumbnail for that item)
    assert c["https://www.metacritic.com/movie/a-great-film/"]["poster"] is None


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


def test_process_cards_freezes_feed_date_and_stores_image():
    now = datetime(2026, 6, 20, tzinfo=timezone.utc)
    cards = [{"url": "https://m/movie/x/", "title": "X", "score": 70,
              "date": "Jun 1, 2026", "media": "movie", "poster": "https://img/x.jpg"}]
    emitted = {}
    mf.process_cards(cards, emitted, threshold=0, now=now)
    e = emitted["https://m/movie/x/"]
    assert e["feed_date"] == now.isoformat()   # frozen once, at qualification
    assert e["image"] == "https://img/x.jpg"

    # seed_from_release omits feed_date -> the one-time backfill dates by release
    em2 = {}
    mf.process_cards(cards, em2, threshold=0, now=now, seed_from_release=True)
    assert "feed_date" not in em2["https://m/movie/x/"]


def test_threshold_zero_admits_any_score_but_not_unscored():
    now = datetime(2026, 6, 20, tzinfo=timezone.utc)
    cards = [
        {"url": "https://m/movie/lo/", "title": "Lo", "score": 42,
         "date": "Jun 1, 2026", "media": "movie", "poster": None},
        {"url": "https://m/movie/no/", "title": "No", "score": None,
         "date": "Jun 1, 2026", "media": "movie", "poster": None},
    ]
    emitted = {}
    assert mf.process_cards(cards, emitted, threshold=0, now=now) == 1
    assert "https://m/movie/lo/" in emitted        # mixed score gets in
    assert "https://m/movie/no/" not in emitted     # unscored still excluded
    assert "image" not in emitted["https://m/movie/lo/"]  # no poster -> no image field


def test_effective_date_prefers_feed_date_else_release():
    d = mf._effective_date({"feed_date": "2026-07-09T00:00:00+00:00", "release_date": "May 22, 2026"})
    assert d.date().isoformat() == "2026-07-09"      # surfaces at qualifying time
    d2 = mf._effective_date({"release_date": "May 22, 2026"})
    assert d2.date().isoformat() == "2026-05-22"     # old items (no feed_date) unchanged


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
            "image": "https://img.test/rose.jpg?a=1&b=2",
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

    # Poster renders as an <img> inside the description; the well-formed parse above
    # proves the & in the poster URL is escaped. Item without a poster stays plain text.
    by_guid = {n.getElementsByTagName("guid")[0].firstChild.data:
               n.getElementsByTagName("description")[0].firstChild.data for n in item_nodes}
    assert by_guid["https://www.metacritic.com/movie/rose-of-nevada/"].startswith(
        '<img src="https://img.test/rose.jpg?a=1&b=2" alt="" />')
    assert not by_guid["https://www.metacritic.com/tv/star-city/"].startswith("<img")


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


# --------------------------------------------------------------------------- #
# 5. Detail-page enrichment (critic/user stats + top-critic quote)
# --------------------------------------------------------------------------- #
DETAIL = (Path(__file__).parent / "fixtures" / "sample_detail.html").read_text(encoding="utf-8")


def test_parse_detail_extracts_stats():
    d = mf.parse_detail(DETAIL)
    assert d["critic_count"] == 4
    assert d["pos"] == 100                 # critic positive %, lenient match
    assert d.get("user_tbd") is True       # user score is tbd
    assert "user_count" not in d           # "Available after 4 ratings" is a threshold, not a count
    assert d["image"] == "https://img.test/detail_hi.jpg?auto=webp&width=1200"  # hi-res og:image


def test_build_rss_prefers_hires_detail_image_over_browse_poster():
    emitted = {"https://m/movie/x/": {
        "title": "X", "score": 70, "media": "movie", "release_date": "Jun 1, 2026",
        "emitted_at": "2026-06-20T00:00:00+00:00", "image": "https://img/small.jpg",
        "detail": {"image": "https://img/big.jpg", "v": mf.DETAIL_VERSION},
    }}
    xml = mf.build_rss(list(emitted.items()), _Args())
    item = parseString(xml).getElementsByTagName("item")[0]
    desc = item.getElementsByTagName("description")[0].firstChild.data
    assert 'src="https://img/big.jpg"' in desc and "small.jpg" not in desc


def test_describe_item_no_quote_and_no_tbd_user():
    meta = {"score": 76, "media": "tv", "release_date": "Jun 30, 2026",
            "detail": mf.parse_detail(DETAIL)}
    desc = mf.describe_item(meta)
    assert desc == "4 reviews · 100% positive"   # no user score -> critic score omitted (it's in the title)
    assert "Critics" not in desc
    assert '"' not in desc                  # no quote
    assert "Users" not in desc              # tbd user score is omitted entirely


def test_describe_item_shows_real_user_score():
    meta = {"score": 82, "media": "movie",
            "detail": {"critic_count": 31, "pos": 74, "user_score": 6.8, "user_count": 540}}
    assert mf.describe_item(meta) == "Critics 82 · 31 reviews · 74% positive · Users 6.8 (540 ratings)"


def test_describe_item_falls_back_without_detail():
    meta = {"score": 76, "media": "tv", "release_date": "Jun 30, 2026"}
    assert mf.describe_item(meta) == "Metascore 76 · TV · Released Jun 30, 2026"


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
