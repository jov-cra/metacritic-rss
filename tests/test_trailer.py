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
    assert tr.lookup("1", "movie", None, "t", fetch) == "MMMMMMMMMMM"
    assert tr.lookup("2", "tv", 3, "t", fetch) == "SSSSSSSSSSS"
    assert tr.lookup("2", "tv", 1, "t", fetch) == "OOOOOOOOOOO"  # season 1: any show trailer
    assert tr.lookup("x1", "movie", None, "t", fetch) is None, "keine Ziffern-ID, keine Abfrage"
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
    with_key = mf.describe_item(_meta(trailer={"v": 1, "key": "dQw4w9WgXcQ"}))
    assert with_key == base + '<br /><a href="https://www.youtube.com/watch?v=dQw4w9WgXcQ">▶ Trailer (YouTube)</a>'
    assert mf.describe_item(_meta(trailer={"v": 1, "key": None, "no_id": True})) == base


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

    def lookup(tm, media, season, token):
        seen.append((tm, media, season))
        return "dQw4w9WgXcQ" if tm == "123" else None
    found, degraded = mf.enrich_trailers(em, 10, 0, "tok", lookup=lookup, today="2026-10-25")
    assert (found, degraded) == (1, False)
    assert em[f"{B}/movie/a/"]["trailer"] == {"v": 1, "key": "dQw4w9WgXcQ", "checked": "2026-10-25"}
    assert em[f"{B}/tv/b/season-2/"]["trailer"]["key"] is None and ("9", "tv", 2) in seen
    assert em[f"{B}/movie/c/"]["trailer"].get("no_id")
    assert not any("trailer_due" in m for m in em.values())


def test_enrich_trailers_loud_without_token_or_on_auth_error():
    em = {f"{B}/movie/a/": _meta(trailer_due=True)}
    assert mf.enrich_trailers(em, 10, 0, "") == (0, True)
    assert em[f"{B}/movie/a/"]["trailer"]["no_token"]
    em = {f"{B}/movie/a/": _meta(trailer_due=True), f"{B}/movie/b/": _meta(trailer_due=True)}

    def boom(*a):
        raise tr.AuthError("TMDB rejected the token (HTTP 401)")
    assert mf.enrich_trailers(em, 10, 0, "tok", lookup=boom) == (0, True)
    assert all(m["trailer"]["key"] is None and "trailer_due" not in m for m in em.values())


def test_enrich_trailers_degrades_after_failures():
    em = {f"{B}/movie/{i}/": _meta(trailer_due=True, emitted_at=f"2026-10-25T10:0{i}:00") for i in range(5)}

    def fail(*a):
        raise tr.TrailerError("HTTP 500")
    found, degraded = mf.enrich_trailers(em, 10, 0, "tok", lookup=fail)
    assert degraded and not any("trailer_due" in m for m in em.values())


def test_streaming_lookup_keeps_tmdb_id():
    node = {"objectType": "MOVIE", "content": {"title": "Film", "originalTitle": "Film", "originalReleaseYear": 2026,
                                              "externalIds": {"tmdbId": 123}}, "offers": []}
    rec = st.lookup("Film", "Oct 1, 2026", "movie", fetch=lambda base, kind: [node])
    assert rec["match"] and rec["tmdb"] == "123"
