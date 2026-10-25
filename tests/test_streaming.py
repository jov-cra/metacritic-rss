"""
Tests for the 21.10.2026 changes: season cards on the TV browse page, the
"[0]" season score, and the streaming label (JustWatch, Germany). All offline.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import metacritic_feed as mf  # noqa: E402
import streaming as st  # noqa: E402

FX = Path(__file__).parent / "fixtures"
BROWSE = (FX / "sample_tv_browse_seasons.html").read_text(encoding="utf-8")
NOW = datetime(2026, 10, 21, 20, 0, tzinfo=timezone.utc)
B = "https://www.metacritic.com"


# --------------------------------------------------------------------------- #
# Browse page with season cards (real page, 21.10.2026)
# --------------------------------------------------------------------------- #
def test_season1_card_folds_onto_show_url():
    cards = {c["url"]: c for c in mf.parse_browse(BROWSE, "tv")}
    assert f"{B}/tv/phony/" in cards and cards[f"{B}/tv/phony/"]["title"] == "Phony"
    assert not any(u.endswith("/season-1/") for u in cards)
    assert cards[f"{B}/tv/the-diplomat-2023/season-4/"]["score"] == 90


def test_show_already_emitted_does_not_come_back_as_season_1():
    emitted = {f"{B}/tv/phony/": {"title": "Phony", "score": 81, "media": "tv",
                                  "release_date": "Oct 20, 2026", "emitted_at": "x"}}
    mf.process_cards(mf.parse_browse(BROWSE, "tv"), emitted, 0, NOW)
    assert emitted[f"{B}/tv/phony/"]["score"] == 81          # untouched
    assert not any("season-1" in u for u in emitted)


def test_movie_urls_untouched():
    html = f'<a href="/movie/season-1/">Season 1 Oct 1, 2026 80 Metascore</a>'
    assert mf.parse_browse(html, "movie")[0]["url"] == f"{B}/movie/season-1/"


def test_candidate_dropped_once_emitted_via_browse():
    url = f"{B}/tv/x/season-2/"
    state = {"seasons": {url: {"first_seen": "2026-10-20"}}}
    emitted = {url: {"title": "X: Season 2", "score": 70, "media": "tv"}}
    args = SimpleNamespace(threshold=0, season_max=10, detail_delay=0,
                           seed_from_release=False, debug=False)
    mf.check_seasons(state, emitted, set(), args, NOW)
    assert url not in state["seasons"]


def test_season_jsonld_rating_zero_is_no_score():
    html = ('<script type="application/ld+json">{"@type":"TVSeason","name":"X: Season 2",'
            '"url":"https://www.metacritic.com/tv/x/season-2/",'
            '"aggregateRating":{"ratingValue":0,"reviewCount":2}}</script>')
    assert mf.parse_season(html, f"{B}/tv/x/season-2/")["score"] is None


# --------------------------------------------------------------------------- #
# Streaming: titles, matching, label
# --------------------------------------------------------------------------- #
def test_split_title():
    assert st.split_title("The Diplomat (2023): Season 4") == ("The Diplomat", 2023, 4)
    assert st.split_title("Outlander: Blood of My Blood : Season 2") == ("Outlander: Blood of My Blood", None, 2)
    assert st.split_title("Phony", "Oct 20, 2026") == ("Phony", 2026, None)
    assert st.split_title("Cape Fear (2026)", "Jun 5, 2026") == ("Cape Fear", 2026, None)


def _node(title, year, offers=(), seasons=None, original=None):
    return {"objectType": "SHOW",
            "content": {"title": title, "originalTitle": original or title, "originalReleaseYear": year},
            "offers": [{"monetizationType": k, "retailPriceValue": p, "package": {"clearName": n}}
                       for k, n, p in offers],
            "seasons": seasons or []}


def _offers(*rows):
    return [{"monetizationType": k, "retailPriceValue": p, "package": {"clearName": n}} for k, n, p in rows]


def test_match_needs_exact_title_and_year():
    nodes = [_node("Only Fools and Horses", 1981), _node("Slow Horses", 2022)]
    assert st.pick_match(nodes, "Slow Horses", None)["content"]["originalReleaseYear"] == 2022
    assert st.pick_match(nodes, "Slow Horses", 2010) is None
    assert st.pick_match([_node("Slow Loop", 2022)], "Slow Horses", None) is None
    assert st.pick_match([_node("Its Always Sunny", 2005, original="It's Always Sunny")],
                         "It's Always Sunny", None) is not None


def test_label_abo_dedupes_channels_and_drops_sample_episodes():
    s = st.summarize(_offers(("FLATRATE", "Apple TV", None), ("FLATRATE", "Apple TV Amazon Channel", None),
                             ("FREE", "Apple TV", None)))
    assert st.label(s) == "Apple TV · Abo"
    s = st.summarize(_offers(("FLATRATE", "Paramount Plus", None), ("FREE", "Amazon Prime Video", None),
                             ("FLATRATE", "Paramount Plus Apple TV channel", None), ("FAST", "Paramount Plus", None)))
    assert st.label(s) == "Paramount+ · Abo"


def test_label_free_rent_buy_none():
    assert st.label(st.summarize(_offers(("FREE", "ZDF", None)))) == "ZDF · gratis"
    assert st.label(st.summarize(_offers(("RENT", "Apple TV Store", 3.99), ("BUY", "Amazon Video", 12.99)))) \
        == "Leihen ab 3,99 €"
    assert st.label(st.summarize(_offers(("BUY", "Amazon DVD / Blu-ray", 9.0), ("BUY", "Apple TV Store", 24.99)))) \
        == "Kaufen ab 24,99 €"
    assert st.label(st.summarize([])) == "kein DE-Angebot"
    many = st.summarize(_offers(("FLATRATE", "WOW", None), ("FLATRATE", "HBO Max", None), ("FLATRATE", "Sky Go", None)))
    assert st.label(many) == "HBO Max, WOW +1 · Abo"


def test_new_season_not_yet_in_germany():
    node = _node("The Lowdown", 2025, offers=[("FLATRATE", "Disney Plus", None)],
                 seasons=[{"content": {"seasonNumber": 1}, "offers": _offers(("FLATRATE", "Disney Plus", None))},
                          {"content": {"seasonNumber": 2}, "offers": []}])
    rec = st.lookup("The Lowdown: Season 2", "", fetch=lambda base, kind="SHOW": [node])
    assert st.label(rec["summary"]) == "Disney+ · Abo, nur ältere Staffeln"
    assert "diese Staffel noch ohne Angebot; ältere Staffeln im Abo bei Disney+" in st.describe(rec["summary"])
    rec = st.lookup("The Lowdown: Season 1", "", fetch=lambda base, kind="SHOW": [node])
    assert st.label(rec["summary"]) == "Disney+ · Abo"


# --------------------------------------------------------------------------- #
# Feed integration
# --------------------------------------------------------------------------- #
def _args():
    return SimpleNamespace(threshold=0, feed_title="t", feed_link="l", feed_self="")


def test_title_carries_label_only_for_new_tv_items():
    old = {"title": "Old", "score": 70, "media": "tv", "release_date": "Oct 1, 2026",
           "emitted_at": "2026-10-20T10:00:00+00:00"}
    emitted = {f"{B}/tv/old/": old}
    cards = [{"url": f"{B}/tv/new/", "title": "New", "score": 80, "date": "Oct 21, 2026", "media": "tv"},
             {"url": f"{B}/movie/film/", "title": "Film", "score": 60, "date": "Oct 21, 2026", "media": "movie"}]
    mf.process_cards(cards, emitted, 0, NOW, providers=True)
    assert emitted[f"{B}/tv/new/"].get("providers_due") and emitted[f"{B}/movie/film/"].get("providers_due")
    calls = []
    def fake(title, date, media="tv"):
        calls.append((title, media))
        if media == "movie":
            return {"v": 2, "match": True, "summary": {"rent_from": 4.99}}
        return {"v": 2, "match": True, "summary": {"flat": ["Netflix"]}}
    done, degraded = mf.enrich_providers(emitted, 10, 0, lookup=fake)
    assert (done, degraded, sorted(calls)) == (2, False, [("Film", "movie"), ("New", "tv")])
    assert not any("providers_due" in m for m in emitted.values())
    xml = mf.build_rss(sorted(emitted.items()), _args(), "2026-10-21T15:00:00+00:00")
    assert "[80] New (Netflix · Abo)" in xml and "[70] Old (TV)" in xml and "[60] Film (Leihen ab 4,99 €)" in xml
    assert "<category>Movie</category>" in xml
    assert "Streaming DE (JustWatch): im Abo bei Netflix" in xml
    assert "<category>TV</category>" in xml


def test_failures_freeze_as_tv_and_degrade():
    emitted = {f"{B}/tv/a{i}/": {"title": f"A{i}", "score": 1, "media": "tv", "providers_due": True,
                                 "emitted_at": f"2026-10-21T1{i}:00:00+00:00"} for i in range(5)}
    def boom(title, date, media="tv"):
        raise st.LookupError_("403")
    done, degraded = mf.enrich_providers(emitted, 10, 0, lookup=boom)
    assert degraded and done == 0
    assert all(m["providers"].get("failed") and "providers_due" not in m for m in emitted.values())
    assert all(mf.media_label(m) == "TV" for m in emitted.values())
    m = {"title": "Z", "score": 1, "media": "tv", "providers_due": True}
    mf.enrich_providers({"u": m}, 10, 0, lookup=lambda t, d, media="tv": {"v": 1, "match": False})
    film = {"title": "F", "score": 1, "media": "movie", "providers_due": True}
    mf.enrich_providers({"f": film}, 10, 0, lookup=lambda t, d, media="tv": {"v": 1, "match": False})
    assert mf.media_label(film) == "Movie"
    assert mf.media_label(m) == "TV"


def test_existing_state_untouched():
    import json
    state = json.loads((Path(__file__).resolve().parents[1] / "state.json").read_text())
    emitted = state["emitted"]
    assert not any("providers_due" in m for m in emitted.values())
    # Since 21.10. new items carry `providers`; the test is that nothing unmarked is looked up
    # or changed (the original assertion "no item has providers" went stale with the first emit).
    before = json.dumps(emitted, sort_keys=True)
    done, _ = mf.enrich_providers(emitted, 999, 0, lookup=lambda t, d, media="tv": 1 / 0)
    assert done == 0 and json.dumps(emitted, sort_keys=True) == before
    found, degraded = mf.enrich_trailers(emitted, 999, 0, "", lookup=lambda *a: 1 / 0)
    assert (found, degraded) == (0, False) and json.dumps(emitted, sort_keys=True) == before


def test_season_year_bound_and_robustness():
    old = _node("Below", 1998, offers=[("FLATRATE", "Netflix", None)])
    assert st.pick_match([old], "Below", None, latest=1990) is None
    assert st.pick_match([None, {"objectType": "SHOW"}, old], "Below", None, latest=2026) is old
    assert st.pick_match([old], "", None) is None
    assert st.summarize([None, {"package": None}, {"monetizationType": "FLATRATE",
                                                    "package": {"clearName": "Netflix"}}]) == {"flat": ["Netflix"]}
    node = _node("X", 2020, seasons=[None, {"content": None}])
    assert st.offers_for(node, 2) == ([], False)


def test_cap_freezes_the_rest_as_tv():
    emitted = {f"{B}/tv/c{i}/": {"title": f"C{i}", "score": 1, "media": "tv", "providers_due": True,
                                 "emitted_at": f"2026-10-21T1{i}:00:00+00:00"} for i in range(5)}
    ok = lambda t, d, media="tv": {"v": 2, "match": True, "summary": {"flat": ["Netflix"]}}
    done, degraded = mf.enrich_providers(emitted, 2, 0, lookup=ok, today="2026-10-21")
    assert (done, degraded) == (2, False)
    assert not any("providers_due" in m for m in emitted.values())
    assert sorted(mf.media_label(m) for m in emitted.values()) == ["Netflix · Abo"] * 2 + ["TV"] * 3
    done, _ = mf.enrich_providers(emitted, 0, 0, lookup=ok)     # next run: nothing left, nothing changes
    assert done == 0
    m = next(m for m in emitted.values() if mf.media_label(m) != "TV")
    assert "(JustWatch, 21.10.2026)" in mf.describe_item(m)


def test_graphql_errors_fail_the_lookup():
    class R:
        def raise_for_status(self): pass
        def json(self): return {"errors": [{"message": "x"}], "data": {"popularTitles": {"edges": []}}}
    import types
    old = st.requests
    st.requests = types.SimpleNamespace(post=lambda *a, **k: R())
    try:
        try:
            st.search("X")
            assert False
        except st.LookupError_:
            pass
    finally:
        st.requests = old
    try:
        st.offers_for({"offers": None}, None)
        assert False
    except st.LookupError_:
        pass


def test_free_tier_beats_paid_tier():
    s = st.summarize(_offers(("FREE", "Joyn", None), ("FLATRATE", "Joyn Plus", None),
                             ("FLATRATE", "Netflix", None)))
    assert st.label(s) == "Joyn · gratis | Netflix · Abo"


def test_older_seasons_buy_only_and_earlier_seasons_only():
    node = _node("Y", 2020, offers=[("FLATRATE", "Netflix", None)],
                 seasons=[{"content": {"seasonNumber": 1}, "offers": _offers(("BUY", "Apple TV Store", 9.99))},
                          {"content": {"seasonNumber": 3}, "offers": _offers(("FLATRATE", "Netflix", None))},
                          {"content": {"seasonNumber": 2}, "offers": []}])
    rec = st.lookup("Y: Season 2", "", fetch=lambda base, kind="SHOW": [node])
    assert st.label(rec["summary"]) == "Kaufen ab 9,99 €, nur ältere Staffeln"
    lone = _node("Z", 2020, offers=[("FLATRATE", "Netflix", None)])
    assert st.label(st.lookup("Z: Season 4", "", fetch=lambda base, kind="SHOW": [lone])["summary"]) == "kein DE-Angebot"


def test_ambiguous_or_too_few_seasons_is_no_match():
    a = _node("Time", 2021, seasons=[{"content": {"seasonNumber": n}, "offers": []} for n in (1, 2, 3)])
    b = _node("Time", 1990, seasons=[{"content": {"seasonNumber": n}, "offers": []} for n in range(1, 6)])
    assert st.pick_match([a, b], "Time", None, latest=2026, season=3) is None      # two candidates
    assert st.pick_match([a], "Time", None, latest=2026, season=3) is a
    assert st.pick_match([a, _node("Time", 1990, seasons=[{"content": {"seasonNumber": 1}, "offers": []}])],
                         "Time", None, latest=2026, season=3) is a            # tie broken by seasons
    l23 = _node("Lioness", 2023, seasons=[{"content": {"seasonNumber": n}, "offers": []} for n in (1, 2, 3)])
    l21 = _node("Lioness", 2021, seasons=[{"content": {"seasonNumber": n}, "offers": []} for n in (1, 2)])
    assert st.pick_match([l23, l21], "Lioness", None, latest=2026, season=3) is l23
    single = _node("Monster: X", 2026, seasons=[{"content": {"seasonNumber": 1}, "offers": []}])
    assert st.pick_match([single], "Monster: X", None, latest=2026, season=4) is single
    single["seasons"][0]["offers"] = _offers(("FLATRATE", "Netflix", None))
    rec = st.lookup("Monster: X: Season 4", "Sep 17, 2026", fetch=lambda base, kind="SHOW": [single])
    assert st.label(rec["summary"]) == "Netflix · Abo"       # anthology filed as a one-season show


def test_service_names_html_escaped_in_description():
    d = st.describe({"flat": ["A<b>&C"]})
    assert "A&lt;b&gt;&amp;C" in d


def test_browse_season_score_zero_is_no_score():
    html = '<a href="/tv/x/season-2/">X: Season 2 Oct 1, 2026 0 Metascore</a>'
    assert mf.parse_browse(html, "tv")[0]["score"] is None


def test_movie_lookup_matches_movies_only():
    film = {"objectType": "MOVIE", "content": {"title": "Masterplan", "originalTitle": "Masterplan",
            "originalReleaseYear": 2026}, "offers": _offers(("FLATRATE", "Netflix", None))}
    show = _node("Masterplan", 2026, offers=[("FLATRATE", "Disney Plus", None)])
    kinds = []
    def fetch(base, kind="SHOW"):
        kinds.append(kind)
        return [show, film]
    rec = st.lookup("Masterplan", "Oct 16, 2026", "movie", fetch=fetch)
    assert kinds == ["MOVIE"] and st.label(rec["summary"]) == "Netflix · Abo"
    assert not st.lookup("Masterplan", "Oct 16, 2010", "movie", fetch=fetch)["match"]


def test_cinema_only_movie():
    s = st.summarize(_offers(("CINEMA", "Cineplex", None)))
    assert st.label(s) == "im Kino" and "im Kino" in st.describe(s)
    s = st.summarize(_offers(("CINEMA", "Cineplex", None), ("RENT", "Apple TV Store", 5.99)))
    assert st.label(s) == "Leihen ab 5,99 €"
