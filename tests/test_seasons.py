"""
Tests for new seasons, the user-score fix and the detail guard (17.10.2026).
Fixtures are trimmed from real Metacritic pages of that day. All offline.
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import metacritic_feed as mf  # noqa: E402

FX = Path(__file__).parent / "fixtures"
SEASON = (FX / "sample_season.html").read_text(encoding="utf-8")
NAV = (FX / "sample_tv_nav.html").read_text(encoding="utf-8")
PREM = (FX / "sample_premieres.html").read_text(encoding="utf-8")
S4 = "https://www.metacritic.com/tv/the-diplomat-2023/season-4/"
NOW = datetime(2026, 10, 17, 20, 0, tzinfo=timezone.utc)


def _args(**kw):
    base = dict(threshold=0, season_max=10, detail_delay=0, seed_from_release=False, debug=False)
    base.update(kw)
    return SimpleNamespace(**base)


# --------------------------------------------------------------------------- #
# User score (real page: "Based on 17 User Ratings 7.5")
# --------------------------------------------------------------------------- #
def test_user_score_read_from_real_page():
    d = mf.parse_detail(SEASON)
    assert d["critic_count"] == 12 and d["pos"] == 92
    assert d["user_count"] == 17 and d["user_score"] == 7.5


def test_user_score_ten_and_old_wording():
    for text, want in (("Based on 3 User Ratings 10 My Score", 10.0),
                       ("Based on 1,204 Ratings 6.1 x", 6.1),
                       ("Based on 40 User Ratings 10.0 x", 10.0)):
        d = mf.parse_detail(f"<html><body><p>{text}</p></body></html>")
        assert d.get("user_score") == want, text
    d = mf.parse_detail("<html><body><p>Based on 40 User Ratings 12 x</p></body></html>")
    assert "user_score" not in d


# --------------------------------------------------------------------------- #
# Season discovery and parsing
# --------------------------------------------------------------------------- #
def test_season_links_from_nav_and_premiere_page():
    nav = mf.season_links(NAV)
    assert S4 in nav and len(nav) == 5
    prem = mf.season_links(PREM)
    assert len(prem) > 20
    assert all(u.startswith("https://www.metacritic.com/tv/") and u.endswith("/") for u in prem)
    assert not any(u.endswith("/season-1/") for u in nav | prem)     # new shows come as cards


def test_episode_and_show_links_are_ignored():
    html = ('<a href="/tv/x/season-2/episode-1-pilot/">e</a><a href="/tv/x/">s</a>'
            '<a href="https://www.metacritic.com/tv/y/season-3/?ref=nav">y</a>')
    assert mf.season_links(html) == {"https://www.metacritic.com/tv/y/season-3/"}


def test_parse_season_real_page():
    s = mf.parse_season(SEASON, S4)
    assert s["title"] == "The Diplomat (2023): Season 4"
    assert s["score"] == 90
    assert s["date"] == "Oct 15, 2026"            # hero date, not JSON-LD datePublished (2023)
    assert s["detail"]["critic_count"] == 12


def test_parse_season_rejects_other_page():
    import pytest
    with pytest.raises(ValueError):
        mf.parse_season(SEASON, "https://www.metacritic.com/tv/the-diplomat-2023/season-3/")
    with pytest.raises(ValueError):
        mf.parse_season("<html><body>show page</body></html>", S4)


def test_unscored_season_has_no_score():
    html = SEASON.replace('"ratingValue":90', '"ratingValue":null')
    assert '"ratingValue":null' in html
    assert mf.parse_season(html, S4)["score"] is None


# --------------------------------------------------------------------------- #
# check_seasons: emit once, budget, rotation, give-up
# --------------------------------------------------------------------------- #
def _with_fetch(fn, pages):
    original = mf.fetch_detail
    mf.fetch_detail = lambda url: pages[url] if isinstance(pages[url], str) else (_ for _ in ()).throw(pages[url])
    try:
        return fn()
    finally:
        mf.fetch_detail = original


def test_scored_season_is_emitted_once_with_detail():
    state, emitted = {}, {}
    new, tried, failed = _with_fetch(lambda: mf.check_seasons(state, emitted, {S4}, _args(), NOW), {S4: SEASON})
    assert (new, tried, failed) == (1, 1, 0)
    e = emitted[S4]
    assert e["title"] == "The Diplomat (2023): Season 4" and e["score"] == 90
    assert e["feed_date"] == NOW.isoformat() and e["detail"]["critic_count"] == 12
    assert S4 not in state["seasons"]
    # second run: nothing to do, nothing fetched
    assert _with_fetch(lambda: mf.check_seasons(state, emitted, {S4}, _args(), NOW), {}) == (0, 0, 0)


def test_unscored_season_stays_pending_without_state_churn():
    html = SEASON.replace('"ratingValue":90', '"ratingValue":null')
    state, emitted = {}, {}
    _with_fetch(lambda: mf.check_seasons(state, emitted, {S4}, _args(), NOW), {S4: html})
    snap = json.dumps(state, sort_keys=True)
    _with_fetch(lambda: mf.check_seasons(state, emitted, set(), _args(), NOW + timedelta(hours=2)), {S4: html})
    assert json.dumps(state, sort_keys=True) == snap          # no "last checked" stamp
    assert S4 in state["seasons"] and not emitted             # still checked without the link


def test_fetch_error_is_not_a_missing_score():
    state, emitted = {}, {}
    res = _with_fetch(lambda: mf.check_seasons(state, emitted, {S4}, _args(), NOW), {S4: RuntimeError("503")})
    assert res == (0, 1, 1) and S4 in state["seasons"] and not state["seasons"][S4].get("gave_up")


def test_budget_counts_attempts_and_rotation_reaches_everyone():
    urls = {f"https://www.metacritic.com/tv/show{i}/season-2/" for i in range(25)}
    seen = []
    state, emitted = {}, {}
    def fake(url):
        seen.append(url)
        raise RuntimeError("down")
    original = mf.fetch_detail
    mf.fetch_detail = fake
    try:
        for slot in range(3):
            _, tried, _ = mf.check_seasons(state, emitted, urls, _args(season_max=10),
                                           NOW + timedelta(hours=2 * slot))
            assert tried == 10
    finally:
        mf.fetch_detail = original
    assert set(seen) == urls                                    # 3 slots x 10 cover all 25


def test_candidate_gives_up_after_60_days_and_never_returns():
    state = {"seasons": {S4: {"first_seen": "2026-08-01"}}}
    res = _with_fetch(lambda: mf.check_seasons(state, {}, {S4}, _args(), NOW), {})
    assert res == (0, 0, 0) and state["seasons"][S4]["gave_up"] is True


def test_future_premiere_is_skipped_and_counts_from_premiere():
    later = "https://www.metacritic.com/tv/silo/season-4/"
    state = {"seasons": {later: {"first_seen": "2026-08-01", "premiere": "Jul 9, 2027"}}}
    assert _with_fetch(lambda: mf.check_seasons(state, {}, set(), _args(), NOW), {}) == (0, 0, 0)
    assert not state["seasons"][later].get("gave_up")
    # aired 20 days ago, first seen long before: still checked, not given up
    state = {"seasons": {later: {"first_seen": "2026-06-01", "premiere": "Sep 27, 2026"}}}
    html = SEASON.replace('"ratingValue":90', '"ratingValue":null').replace(S4, later)
    res = _with_fetch(lambda: mf.check_seasons(state, {}, set(), _args(), NOW), {later: RuntimeError("x")})
    assert res == (0, 1, 1) and not state["seasons"][later].get("gave_up")


def test_premiere_learned_from_page_is_stored_once():
    html = SEASON.replace('"ratingValue":90', '"ratingValue":null')
    state = {}
    _with_fetch(lambda: mf.check_seasons(state, {}, {S4}, _args(), NOW), {S4: html})
    assert state["seasons"][S4]["premiere"] == "Oct 15, 2026"


# --------------------------------------------------------------------------- #
# Detail guard
# --------------------------------------------------------------------------- #
def _emitted(n):
    return {f"https://www.metacritic.com/movie/m{i}/": {"title": f"M{i}", "score": 70, "media": "movie",
            "release_date": "Oct 1, 2026", "emitted_at": f"2026-10-0{i % 9 + 1}T00:00:00+00:00"}
            for i in range(n)}


def test_empty_detail_is_not_frozen_and_run_degrades():
    emitted = _emitted(5)
    original = mf.fetch_detail
    mf.fetch_detail = lambda url: "<html><body>Access denied</body></html>"
    try:
        done, degraded = mf.enrich_missing(emitted, cap=60, delay=0)
    finally:
        mf.fetch_detail = original
    assert done == 0 and degraded is True
    assert sum(1 for m in emitted.values() if "detail" in m) == 0
    assert sum(m.get("detail_tries", 0) for m in emitted.values()) == 3   # stopped after 3


def test_detail_frozen_empty_after_max_tries():
    emitted = _emitted(1)
    url = next(iter(emitted))
    emitted[url]["detail_tries"] = mf.DETAIL_MAX_TRIES - 1
    original = mf.fetch_detail
    mf.fetch_detail = lambda u: "<html><body>nothing</body></html>"
    try:
        mf.enrich_missing(emitted, cap=60, delay=0)
    finally:
        mf.fetch_detail = original
    assert emitted[url]["detail"] == {"v": mf.DETAIL_VERSION, "empty": True}
    assert "detail_tries" not in emitted[url]
    assert "Metascore 70" in mf.describe_item(emitted[url])      # basic description


def test_detail_budget_counts_failures():
    emitted = _emitted(8)
    calls = []
    def boom(u):
        calls.append(u)
        raise RuntimeError("x")
    original = mf.fetch_detail
    mf.fetch_detail = boom
    try:
        mf.enrich_missing(emitted, cap=2, delay=0)
    finally:
        mf.fetch_detail = original
    assert len(calls) == 2


def test_season_item_in_rss():
    state, emitted = {}, {}
    _with_fetch(lambda: mf.check_seasons(state, emitted, {S4}, _args(), NOW), {S4: SEASON})
    args = SimpleNamespace(threshold=0, feed_title="t", feed_link="https://www.metacritic.com/", feed_self="")
    xml = mf.build_rss(sorted(emitted.items(), key=mf._feed_sort_key, reverse=True), args, NOW.isoformat())
    assert "<title>[90] The Diplomat (2023): Season 4 (TV)</title>" in xml
    assert "12 reviews · 92% positive" in xml and "Users 7.5 (17 ratings)" in xml
