"""
Tests for the Munich cinema line (25.10.2026): programme parsing, matching a feed item to the
programme, the fail-closed booking link, the frozen snapshot and its description. All offline.
"""
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kino  # noqa: E402
import metacritic_feed as mf  # noqa: E402

FX = Path(__file__).parent / "fixtures"
IK = (FX / "indiekino_muenchen_2026-10-25.html").read_text(encoding="utf-8")
NOW = dt.datetime(2026, 10, 25, 15, 0, tzinfo=kino.BERLIN)


def test_indiekino_parses_and_keeps_ticket_links():
    films, stat = kino.parse_indiekino(IK, dt.date(2026, 10, 25))
    kino.pruefe_indiekino(stat)
    ali = [f for f in films if f["titel"] == "Ali G: Who Iz I?"][0]
    assert ali["jahr"] == 2026 and any(v["fassung"] == "OV" for v in ali["vorstellungen"])
    links = [v["buchung"]["url"] for f in films for v in f["vorstellungen"] if v.get("buchung")]
    assert links and all(u.startswith("https://www.kinoheld.de/kino/muenchen/") and "?" not in u for u in links)


def test_match_needs_exact_title_and_year():
    films = [{"titel": "Fatherland", "alt_titel": "Ojczyzna", "jahr": 2026, "vorstellungen": []},
             {"titel": "Fatherland", "jahr": 1986, "vorstellungen": []},
             {"titel": "Fatherland Sneak Preview", "jahr": 2026, "vorstellungen": []}]
    assert kino.match_films(films, ["Vaterland", "Ojczyzna"], 2026) == [films[0]]
    assert kino.match_films(films, ["Fatherlands"], 2026) == []


def _show(beginn, title, slug="3689095522", orig="", kino_slug="studio-isabella-muenchen", typ="FIRST_PARTY",
          bookable=True, deeplink=None):
    return {"beginning": beginn, "isBookable": bookable, "deeplink": deeplink, "urlSlug": slug,
            "audioLanguage": None, "subtitleLanguage": None, "movie": {"title": title, "titleOriginal": orig},
            "cinema": {"urlSlug": kino_slug, "bookingType": typ}}


def test_snapshot_next_original_showings_with_booking():
    film = {"titel": "Fatherland", "vorstellungen": [
        {"kino": "Studio Isabella", "kino_slug": "muenchen_studio_isabella", "datum": "2026-10-25",
         "zeit": "20:30", "fassung": "OmeU", "land": "Polen"},
        {"kino": "Studio Isabella", "kino_slug": "muenchen_studio_isabella", "datum": "2026-10-25",
         "zeit": "11:00", "fassung": "OmeU", "land": "Polen"},          # past
        {"kino": "Rex", "kino_slug": "muenchen_neues_rex", "datum": "2026-10-26",
         "zeit": "18:00", "fassung": None, "land": "Polen"},            # dubbed: not original
        {"kino": "Werkstattkino", "kino_slug": "muenchen_werkstattkino", "datum": "2026-10-27",
         "zeit": "20:15", "fassung": "OmU", "land": "Polen"}]}
    calls = []

    def shows_for(kid):
        calls.append(kid)
        return [_show("2026-10-25T20:30:00+01:00", "VATERLAND", orig="Fatherland")]
    out = kino.snapshot([film], "pl", NOW, shows_for, ["Fatherland"])
    assert [(s["datum"], s["zeit"], s["art"]) for s in out] == [("2026-10-25", "20:30", "vorstellung"),
                                                                 ("2026-10-27", "20:15", "programm")]
    assert out[0]["url"] == "https://www.kinoheld.de/kino/muenchen/studio-isabella-muenchen/vorstellung/3689095522"
    assert calls == ["1184"]
    line = kino.describe({"shows": out}, "25.10.2026")
    assert line.startswith("Kino München im Original (25.10.2026): ")
    assert '<a href="https://www.kinoheld.de/kino/muenchen/studio-isabella-muenchen/vorstellung/3689095522">So 25.10. 20:30 OmeU</a> Studio Isabella' in line
    assert 'Di 27.10. 20:15 OmU Werkstattkino (<a href="https://www.indiekino.de/kinoprogramm/en/muenchen/kinos/muenchen_werkstattkino">Programm</a>)' in line


def test_snapshot_kinoheld_down_falls_back_to_programme():
    film = {"titel": "X", "vorstellungen": [{"kino": "Monopol", "kino_slug": "muenchen_monopol_am_nordbad",
                                            "datum": "2026-10-26", "zeit": "20:00", "fassung": "OV", "land": "USA"}]}
    out = kino.snapshot([film], "en", NOW, lambda kid: None, ["X"])
    assert out[0]["art"] == "programm"


def _item(cinema=True):
    return {"title": "Fatherland", "media": "movie", "release_date": "Jun 19, 2026", "score": 86,
            "emitted_at": "2026-10-25T14:00:00+00:00", "kino_due": True,
            "providers": {"v": 3, "match": True, "tmdb": "1437696", "titles": ["Fatherland", "Ojczyzna"],
                          "year": 2026, "summary": {"cinema": cinema}, "checked": "2026-10-25"}}


def test_enrich_kino_freezes_snapshot_and_flags_failures():
    film = {"titel": "Fatherland", "alt_titel": "Ojczyzna", "jahr": 2026, "quelle": "indiekino", "vorstellungen": [
        {"kino": "Studio Isabella", "kino_slug": "muenchen_studio_isabella", "datum": "2026-10-25",
         "zeit": "20:30", "fassung": "OmeU", "land": "Polen"}]}
    em = {"u": _item()}
    n, deg = mf.enrich_kino(em, NOW, "tok", load=lambda d: ([film], []),
                            shows=lambda kid, d: [_show("2026-10-25T20:30:00+01:00", "VATERLAND", orig="Fatherland")],
                            language=lambda t, tok: "pl")
    assert n == 1 and not deg and "kino_due" not in em["u"]
    assert em["u"]["kino"]["shows"][0]["art"] == "vorstellung" and em["u"]["kino"]["checked"] == "2026-10-25"
    assert "Kino München im Original (25.10.2026)" in mf.describe_item(em["u"])
    # both programmes down: frozen as failed, red run
    em = {"u": _item()}
    n, deg = mf.enrich_kino(em, NOW, "tok", load=lambda d: ([], ["indiekino", "cinema"]))
    assert deg and em["u"]["kino"] == {"v": kino.VERSION, "failed": True}
    # no token
    em = {"u": _item()}
    assert mf.enrich_kino(em, NOW, "", load=lambda d: 1 / 0)[1] is True
    # kinoheld down: programme link, red run
    em = {"u": _item()}

    def boom(kid, d):
        raise kino.KinoFehler("down")
    n, deg = mf.enrich_kino(em, NOW, "tok", load=lambda d: ([film], []), shows=boom, language=lambda t, tok: "pl")
    assert deg and em["u"]["kino"]["shows"][0]["art"] == "programm"


def test_enrich_kino_nothing_due_touches_nothing():
    em = {"u": {**_item(), "kino_due": False}}
    em["u"].pop("kino_due")
    before = json.dumps(em, sort_keys=True)
    assert mf.enrich_kino(em, NOW, "tok", load=lambda d: 1 / 0) == (0, False)
    assert json.dumps(em, sort_keys=True) == before
    assert "Kino München" not in mf.describe_item(em["u"]), "items without a snapshot render as before"


def test_providers_mark_only_cinema_movies():
    em = {"m": {"title": "Fatherland", "media": "movie", "release_date": "", "providers_due": True},
          "t": {"title": "Show", "media": "tv", "release_date": "", "providers_due": True}}
    rec_m = {"v": 3, "match": True, "titles": ["Fatherland"], "summary": {"cinema": True}}
    rec_t = {"v": 3, "match": True, "summary": {"cinema": True}}
    mf.enrich_providers(em, 10, 0, lookup=lambda t, r, m: rec_m if m == "movie" else rec_t, kino_on=True)
    assert em["m"].get("kino_due") and not em["t"].get("kino_due")
    em2 = {"m": {"title": "Fatherland", "media": "movie", "release_date": "", "providers_due": True}}
    mf.enrich_providers(em2, 10, 0, lookup=lambda t, r, m: rec_m, kino_on=False)
    assert "kino_due" not in em2["m"]



def test_single_showing_with_other_title_is_no_booking_link():
    film = {"titel": "Fatherland", "vorstellungen": [{"kino": "Mathäser", "kino_slug": "muenchen_mathaeser_der_filmpalast",
                                                     "datum": "2026-10-25", "zeit": "20:30", "fassung": "OmeU", "land": "Polen"}]}
    other = [_show("2026-10-25T20:30:00+01:00", "Paw Patrol", typ="THIRD_PARTY", bookable=False,
                   deeplink="https://www.mathaeser.de/mm/programm/vorstellung/271D8000023BWDJVCF")]
    assert kino.snapshot([film], "pl", NOW, lambda kid: other, ["Fatherland"])[0]["art"] == "programm"


def test_full_kinoheld_answer_counts_as_cut_off():
    full = {"data": {"shows": {"data": [{}] * kino.KINOHELD_LIMIT}}}
    import pytest
    with pytest.raises(kino.KinoFehler):
        kino.kinoheld_shows("951", dt.date(2026, 10, 25), post=lambda u, b: full)


def test_partial_programme_is_marked():
    line = kino.describe({"shows": [{"datum": "2026-10-26", "zeit": "20:00", "kino": "Cinema Filmtheater",
                                     "fassung": "OV", "url": "https://x.example/a?b=1&c=2", "art": "vorstellung"}],
                          "partial": ["indiekino"]}, "25.10.2026")
    assert line.startswith("Kino München im Original (25.10.2026), Programm unvollständig: ")
    assert 'href="https://x.example/a?b=1&amp;c=2"' in line


def test_cinema_line_is_valid_xml_in_the_feed():
    import xml.etree.ElementTree as ET
    from types import SimpleNamespace
    meta = {**_item(), "kino": {"v": 1, "checked": "2026-10-25", "shows": [
        {"datum": "2026-10-26", "zeit": "20:00", "kino": "Cinema & Co", "fassung": "OV",
         "url": "https://webshop.cinema-muenchen.de/landingpage?center=1&language=DE&performance=X", "art": "vorstellung"}]}}
    meta.pop("kino_due")
    args = SimpleNamespace(threshold=0, feed_title="t", feed_link="https://www.metacritic.com/", feed_self="",
                           trailers=False)
    xml = mf.build_rss([("https://www.metacritic.com/movie/fatherland/", meta)], args, "2026-10-25T14:00:00+00:00")
    root = ET.fromstring(xml.encode("utf-8"))
    desc = root.find("./channel/item/description").text
    assert '<a href="https://webshop.cinema-muenchen.de/landingpage?center=1&amp;language=DE&amp;performance=X">' in desc
