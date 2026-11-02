"""
Tests for the trailer link (25.10.2026): picking the official YouTube trailer from
TMDB, freezing it at emit, the description line, and the loud failure modes. Offline.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import metacritic_feed as mf  # noqa: E402
import streaming as st  # noqa: E402
import trailer as tr  # noqa: E402

B = "https://www.metacritic.com"


def v(key, typ="Trailer", lang="en", official=True, site="YouTube", date="2026-09-01", name="Official Trailer"):
    return {"key": key, "type": typ, "iso_639_1": lang, "official": official, "site": site,
            "published_at": date + "T10:00:00.000Z", "name": name}


def ok(seconds):
    """check() stub: every video playable in DE with this length (None = length unreadable)."""
    return lambda k: tr.Check(True, True, seconds)


def page(status="OK", countries=("DE", "US"), seconds=120):
    """Minimal YouTube watch-page fragments as the parser sees them."""
    s = '{"playabilityStatus":{"status":"%s","reason":"x"},' % status
    if seconds is not None:
        s += '"videoDetails":{"lengthSeconds":"%d"},' % seconds
    if countries is not None:
        s += '"microformat":{"playerMicroformatRenderer":{"availableCountries":[%s]}}' % ",".join(
            f'"{c}"' for c in countries)
    return s + "}"


def test_pick_prefers_official_trailer_en_newest():
    vids = [v("AAAAAAAAAAA", typ="Teaser"), v("BBBBBBBBBBB", lang="de"),
            v("CCCCCCCCCCC", date="2026-08-01"), v("DDDDDDDDDDD", date="2026-09-20"),
            v("EEEEEEEEEEE", official=False, date="2026-10-01"), v("FFFFFFFFFFF", site="Vimeo"),
            v("GGGGGGGGGGG", typ="Featurette")]
    assert tr.pick(vids) == "DDDDDDDDDDD"
    assert tr.pick([v("AAAAAAAAAAA", typ="Teaser"), v("EEEEEEEEEEE", official=False)]) == "AAAAAAAAAAA"
    assert tr.pick([v("EEEEEEEEEEE", official=False)]) is None, "inoffiziell zählt nie"
    assert tr.pick([v("bad key")]) is None


def test_pick_season_name_filter():
    vids = [v("AAAAAAAAAAA", name="Season 2 Official Trailer"), v("BBBBBBBBBBB", name="Official Trailer")]
    assert tr.pick(vids, season=2) == "AAAAAAAAAAA"
    assert tr.pick(vids, season=3) is None


def test_lookup_paths():
    calls = []

    def fetch(path, token):
        calls.append(path)
        return {"movie/1": [v("MMMMMMMMMMM")], "tv/2/season/3": [],
                "tv/2": [v("SSSSSSSSSSS", name="Season 3 Trailer"), v("OOOOOOOOOOO")]}.get(path, [])
    assert tr.lookup("1", "movie", None, "t", fetch, check=ok(120)) == "MMMMMMMMMMM"
    assert tr.lookup("2", "tv", 3, "t", fetch, check=ok(120)) == "SSSSSSSSSSS"
    assert tr.lookup("2", "tv", 1, "t", fetch, check=ok(120)) == "OOOOOOOOOOO"  # season 1: any show trailer
    assert tr.lookup("x1", "movie", None, "t", fetch, check=ok(120)) is None, "keine Ziffern-ID, keine Abfrage"
    assert "tv/2/season/3" in calls


def test_link_html_validates_key():
    assert tr.link_html("dQw4w9WgXcQ") == '<a href="https://www.youtube.com/watch?v=dQw4w9WgXcQ">▶ Trailer (YouTube)</a>'
    assert tr.link_html('x" onmouseover="') == ""


def _meta(**extra):
    m = {"title": "Film", "score": 80, "media": "movie", "release_date": "Oct 1, 2026",
         "emitted_at": "2026-10-25T10:00:00+00:00",
         "detail": {"critic_count": 20, "pos": 90},
         "providers": {"v": 2, "match": True, "summary": {"flat": ["Netflix"]}, "checked": "2026-10-25",
                       "tmdb": "123"}}
    m.update(extra)
    return m



def test_describe_item_appends_link_only_with_key():
    base = mf.describe_item(_meta())
    with_key = mf.describe_item(_meta(trailer={"v": 3, "key": "dQw4w9WgXcQ"}))
    assert with_key == base + '<br /><a href="https://www.youtube.com/watch?v=dQw4w9WgXcQ">▶ Trailer (YouTube)</a>'
    assert mf.describe_item(_meta(trailer={"v": 3, "key": None, "no_id": True})) == base


def test_describe_item_hides_links_not_checked_for_de():
    """02.11.2026: a link frozen before the DE check (v2) is not shown until reverified."""
    base = mf.describe_item(_meta())
    assert mf.describe_item(_meta(trailer={"v": 2, "key": "dQw4w9WgXcQ"})) == base
    assert mf.describe_item(_meta(trailer={"v": 2, "key": "dQw4w9WgXcQ", "de_tries": 1})) == base


def test_old_items_without_trailer_are_byte_identical():
    m = _meta()
    m.pop("providers")
    assert "Trailer" not in mf.describe_item(m)


def test_enrich_trailers_freeze_and_outcomes():
    em = {
        f"{B}/movie/a/": _meta(trailer_due=True),
        f"{B}/tv/b/season-2/": _meta(media="tv", trailer_due=True, providers={"v": 2, "match": True, "summary": {}, "tmdb": "9"}),
        f"{B}/movie/c/": _meta(trailer_due=True, providers={"v": 2, "match": False}),
    }
    seen = []

    def lookup(tm, media, season, token, check=None):
        seen.append((tm, media, season))
        return "dQw4w9WgXcQ" if tm == "123" else None
    found, degraded = mf.enrich_trailers(em, 10, 0, "tok", lookup=lookup, today="2026-10-25")
    assert (found, degraded) == (1, False)
    assert em[f"{B}/movie/a/"]["trailer"] == {"v": tr.VERSION, "key": "dQw4w9WgXcQ", "checked": "2026-10-25"}
    assert em[f"{B}/tv/b/season-2/"]["trailer"]["key"] is None and ("9", "tv", 2) in seen
    assert em[f"{B}/movie/c/"]["trailer"].get("no_id")
    assert not any("trailer_due" in m for m in em.values())


def test_enrich_trailers_loud_without_token_or_on_auth_error():
    em = {f"{B}/movie/a/": _meta(trailer_due=True)}
    assert mf.enrich_trailers(em, 10, 0, "") == (0, True)
    assert em[f"{B}/movie/a/"]["trailer"]["no_token"]
    em = {f"{B}/movie/a/": _meta(trailer_due=True), f"{B}/movie/b/": _meta(trailer_due=True)}

    def boom(*a, **kw):
        raise tr.AuthError("TMDB rejected the token (HTTP 401)")
    assert mf.enrich_trailers(em, 10, 0, "tok", lookup=boom) == (0, True)
    assert all(m["trailer"]["key"] is None and "trailer_due" not in m for m in em.values())


def test_enrich_trailers_degrades_after_failures():
    em = {f"{B}/movie/{i}/": _meta(trailer_due=True, emitted_at=f"2026-10-25T10:0{i}:00") for i in range(5)}

    def fail(*a, **kw):
        raise tr.TrailerError("HTTP 500")
    found, degraded = mf.enrich_trailers(em, 10, 0, "tok", lookup=fail)
    assert degraded and not any("trailer_due" in m for m in em.values())


def test_streaming_lookup_keeps_tmdb_id():
    node = {"objectType": "MOVIE", "content": {"title": "Film", "originalTitle": "Film", "originalReleaseYear": 2026,
                                              "externalIds": {"tmdbId": 123}}, "offers": []}
    rec = st.lookup("Film", "Oct 1, 2026", "movie", fetch=lambda base, kind: [node])
    assert rec["match"] and rec["tmdb"] == "123"


def test_short_cuts_are_skipped():
    """Mare of Easttown 25.10.2026: the season video was a 15-second "Emmy Trailer"."""
    season = [v("EEEEEEEEEEE", name="Emmy Trailer 15", date="2021-08-23")]
    show = [v("OOOOOOOOOOO", name="Official Trailer", date="2021-04-04")]
    fetch = lambda path, token: {"tv/1/season/1": season, "tv/1": show}.get(path, [])
    lengths = {"EEEEEEEEEEE": 15, "OOOOOOOOOOO": 146}
    assert tr.lookup("1", "tv", 1, "t", fetch, check=lambda k: tr.Check(True, True, lengths[k])) == "OOOOOOOOOOO"
    # DE confirmed but no length on the page: the name decides
    assert tr.lookup("1", "tv", 1, "t", fetch, check=ok(None)) == "OOOOOOOOOOO"
    assert tr.choose([v("SSSSSSSSSSS", name="TV Spot 1")], check=ok(None)) is None
    assert tr.choose([v("SSSSSSSSSSS")], check=ok(30)) is None, "only short cuts: no link"


# --------------------------------------------------------------------------- #
# Playable in Germany (02.11.2026)
# --------------------------------------------------------------------------- #

def test_parse_watch_reads_the_country_list_not_the_location():
    # The Night Of, official trailer 556N5vojtp0: US-only. From DE the page says UNPLAYABLE,
    # from a US runner it says OK — the country list without DE decides both times.
    assert tr.parse_watch(page("UNPLAYABLE", ("US",), 91)) == tr.Check(True, False, 91)
    assert tr.parse_watch(page("OK", ("US",), 91)) == tr.Check(True, False, 91)
    assert tr.parse_watch(page("OK", ("DE", "US", "GB"), 84)) == tr.Check(True, True, 84)
    assert tr.parse_watch(page("OK", ("DEU", "US"), 84)).de is False, "only exact two-letter codes"


def test_parse_watch_is_fail_closed():
    assert tr.parse_watch("") == tr.UNREAD
    assert tr.parse_watch("<html>consent</html>") == tr.UNREAD
    assert tr.parse_watch(page("OK", None, 84)) == tr.Check(True, None, 84), "no country list: unclear"
    assert tr.parse_watch(page("OK", (), 84)).de is None, "empty list: unclear"
    assert tr.parse_watch(page("LOGIN_REQUIRED", ("DE", "US"), 84)).de is None, "age gate / bot check: unclear"


def test_choose_skips_blocked_and_unclear_candidates():
    """The Night Of 01.11.2026: the US-only 'Official Trailer' ranks first; the UK trailer plays in DE."""
    cands = tr.ranked([v("556N5vojtp0", name="Official Trailer", date="2016-06-01"),
                       v("Z5uv7_ysu9M", name="Official UK Trailer", date="2016-05-01")])
    assert cands[0]["key"] == "556N5vojtp0"
    checks = {"556N5vojtp0": tr.Check(True, False, 91), "Z5uv7_ysu9M": tr.Check(True, True, 84)}
    assert tr.choose(cands, check=checks.get) == "Z5uv7_ysu9M"
    only_blocked = {"556N5vojtp0": tr.Check(True, False, 91), "Z5uv7_ysu9M": tr.Check(True, False, 84)}
    assert tr.choose(cands, check=only_blocked.get) is None
    assert tr.choose(cands, check=lambda k: tr.UNREAD) is None, "unread pages never become a link"
    assert tr.choose(cands, check=lambda k: tr.Check(True, None, 84)) is None, "unclear never becomes a link"


def test_enrich_trailers_records_blocked_and_unchecked():
    em = {f"{B}/movie/a/": _meta(trailer_due=True, emitted_at="2026-11-02T10:02:00"),
          f"{B}/movie/b/": _meta(trailer_due=True, emitted_at="2026-11-02T10:01:00")}

    def lookup(tm, media, season, token, check=None):
        return tr.choose([v("556N5vojtp0"), v("Z5uv7_ysu9M")], check=check)
    checks = iter([tr.Check(True, False, 91), tr.Check(True, False, 84),   # a: both blocked in DE
                   tr.UNREAD, tr.UNREAD])                                  # b: nothing readable
    found, degraded = mf.enrich_trailers(em, 10, 0, "tok", lookup=lookup, check=lambda k: next(checks))
    assert (found, degraded) == (0, False), "one unreadable lookup is not yet degraded"
    assert em[f"{B}/movie/a/"]["trailer"] == {"v": tr.VERSION, "key": None, "blocked_de": 2}
    assert em[f"{B}/movie/b/"]["trailer"] == {"v": tr.VERSION, "key": None, "unchecked": True}


def test_enrich_trailers_degrades_when_youtube_is_unreadable():
    em = {f"{B}/movie/{i}/": _meta(trailer_due=True, emitted_at=f"2026-11-02T10:0{i}:00") for i in range(5)}

    def lookup(tm, media, season, token, check=None):
        return tr.choose([v("Z5uv7_ysu9M")], check=check)
    found, degraded = mf.enrich_trailers(em, 10, 0, "tok", lookup=lookup, check=lambda k: tr.UNREAD)
    assert (found, degraded) == (0, True)
    assert all(m["trailer"]["key"] is None and "trailer_due" not in m for m in em.values())
    assert all(mf.describe_item(m) == mf.describe_item(_meta()) for m in em.values())


def test_reverify_keeps_playable_links_byte_identical():
    m = _meta(trailer={"v": 2, "key": "Z5uv7_ysu9M", "checked": "2026-10-30"})
    before = mf.describe_item(_meta(trailer={"v": 3, "key": "Z5uv7_ysu9M"}))
    em = {f"{B}/movie/a/": m}
    assert mf.reverify_trailers(em, check=lambda k: tr.Check(True, True, 84), today="2026-11-02", delay=0) is False
    assert m["trailer"] == {"v": 3, "key": "Z5uv7_ysu9M", "checked": "2026-10-30", "de_checked": "2026-11-02"}
    assert mf.describe_item(m) == before


def test_reverify_drops_links_blocked_in_de():
    em = {f"{B}/tv/x/": _meta(trailer={"v": 2, "key": "556N5vojtp0"})}
    assert mf.reverify_trailers(em, check=lambda k: tr.Check(True, False, 91), today="2026-11-02", delay=0) is False
    assert em[f"{B}/tv/x/"]["trailer"] == {"v": tr.VERSION, "key": None, "blocked_de": True,
                                         "was": "556N5vojtp0", "checked": "2026-11-02"}
    assert "Trailer" not in mf.describe_item(em[f"{B}/tv/x/"])


def test_reverify_unclear_hides_then_gives_up():
    """One video without an answer while YouTube answers for the others: item-specific, tries count."""
    em = {f"{B}/movie/a/": _meta(trailer={"v": 2, "key": "AAAAAAAAAAA"}),
          f"{B}/movie/b/": _meta(trailer={"v": 2, "key": "BBBBBBBBBBB"})}
    check = lambda k: tr.UNREAD if k == "AAAAAAAAAAA" else tr.Check(True, True, 90)
    for n in range(1, mf.REVERIFY_TRIES):
        assert mf.reverify_trailers(em, check=check, delay=0) is False
        assert em[f"{B}/movie/a/"]["trailer"]["de_tries"] == n
        assert "Trailer" not in mf.describe_item(em[f"{B}/movie/a/"])
        assert "Trailer" in mf.describe_item(em[f"{B}/movie/b/"])
    mf.reverify_trailers(em, check=check, delay=0)
    assert em[f"{B}/movie/a/"]["trailer"] == {"v": tr.VERSION, "key": None, "unchecked": True, "was": "AAAAAAAAAAA"}
    # nothing left to check afterwards
    assert mf.reverify_trailers(em, check=lambda k: 1 / 0, delay=0) is False


def test_reverify_outage_uses_up_no_try():
    """02.11.2026: the runner got no country list for any video; that must not drop links for good."""
    em = {f"{B}/movie/a/": _meta(trailer={"v": 2, "key": "Z5uv7_ysu9M", "de_tries": 2})}
    for _ in range(5):
        assert mf.reverify_trailers(em, check=lambda k: tr.UNREAD, delay=0) is True, "all unclear: degraded"
    assert em[f"{B}/movie/a/"]["trailer"] == {"v": 2, "key": "Z5uv7_ysu9M", "de_tries": 2}
    assert "Trailer" not in mf.describe_item(em[f"{B}/movie/a/"]), "still hidden while unchecked"
    mf.reverify_trailers(em, check=lambda k: tr.Check(True, True, 84), delay=0)
    assert "Trailer" in mf.describe_item(em[f"{B}/movie/a/"])


def test_reverify_degrades_when_pages_read_but_no_country_list():
    """First live run 02.11.2026: the runner's watch pages parsed (status found) but carried no
    country list; all old links were hidden while the run stayed green. Now that is degraded."""
    em = {f"{B}/movie/{i}/": _meta(trailer={"v": 2, "key": "Z5uv7_ysu9M"}) for i in range(2)}
    assert mf.reverify_trailers(em, check=lambda k: tr.Check(True, None, None), delay=0) is True


def player_json(countries=("DE", "US"), seconds=84, status="UNPLAYABLE", family_safe=True):
    mf_ = {"lengthSeconds": str(seconds), "isFamilySafe": family_safe}
    if countries is not None:
        mf_["availableCountries"] = list(countries)
    return {"playabilityStatus": {"status": status, "reason": "Video unavailable"},
            "videoDetails": {"lengthSeconds": str(seconds)},
            "microformat": {"playerMicroformatRenderer": mf_}}


def test_parse_player_ignores_status_and_reads_the_list():
    # a datacenter IP gets UNPLAYABLE for every video; the list still decides
    assert tr.parse_player(player_json(("DE", "US"))) == tr.Check(True, True, 84)
    assert tr.parse_player(player_json(("US",), 91)) == tr.Check(True, False, 91)   # The Night Of 556N5vojtp0
    assert tr.parse_player(player_json(None)).de is None
    assert tr.parse_player(player_json(())).de is None
    assert tr.parse_player(player_json(family_safe=False)).de is None, "age gate: unclear"
    assert tr.parse_player({"error": {"code": 400}}) == tr.UNREAD
    assert tr.parse_player(None) == tr.UNREAD
    assert tr.parse_player({"playabilityStatus": {"status": "ERROR"}}) == tr.Check(True, None, None), "deleted video"


def test_youtube_check_falls_back_to_the_watch_page_only_when_unclear():
    calls = []

    def src(answer, name):
        def f(k):
            calls.append(name)
            return answer
        return f
    yes, no, unclear = tr.Check(True, True, 84), tr.Check(True, False, 91), tr.Check(True, None, 84)
    assert tr.youtube_check("x", sources=(src(yes, "p"), src(no, "w"))) == yes and calls == ["p"]
    calls.clear()
    assert tr.youtube_check("x", sources=(src(unclear, "p"), src(no, "w"))) == no and calls == ["p", "w"]
    assert tr.youtube_check("x", sources=(src(tr.UNREAD, "p"), src(tr.UNREAD, "w"))) == tr.UNREAD
    # definite answer without length borrows the length an unclear source did read
    assert tr.youtube_check("x", sources=(src(unclear, "p"), src(tr.Check(True, True, None), "w"))) == yes
