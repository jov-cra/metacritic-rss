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
    assert tr.choose([v("SSSSSSSSSSS")], check=ok(30)) is None, "only short cuts: no link"
    assert tr.choose([v("SSSSSSSSSSS")], check=ok(None)) is None, "unknown duration: unclear, no link"


# --------------------------------------------------------------------------- #
# Playable in Germany (02.11.2026): the YouTube Data API decides, nothing else
# --------------------------------------------------------------------------- #

def api_json(rr=None, duration="PT1M24S", rating=None, privacy="public", items=True, vid="Z5uv7_ysu9M",
             upload="processed"):
    cd = {"duration": duration}
    if rr is not None:
        cd["regionRestriction"] = rr
    if rating:
        cd["contentRating"] = {"ytRating": rating}
    it = {"id": vid, "contentDetails": cd, "status": {"uploadStatus": upload, "privacyStatus": privacy}}
    return {"kind": "youtube#videoListResponse", "items": [it] if items else []}


def test_parse_api_region_restriction():
    assert tr.parse_api(api_json()) == tr.Check(True, True, 84), "no restriction: everywhere"
    assert tr.parse_api(api_json({"allowed": ["US"]}, "PT1M31S")) == tr.Check(True, False, 91)   # The Night Of
    assert tr.parse_api(api_json({"allowed": ["DE", "AT"]})).de is True
    assert tr.parse_api(api_json({"blocked": ["DE"]})).de is False
    assert tr.parse_api(api_json({"blocked": ["US"]})).de is True
    assert tr.parse_api(api_json(rating="ytAgeRestricted")) == tr.Check(True, None, 84), "age gate: unclear"
    assert tr.parse_api(api_json(privacy="private")).de is False
    assert tr.parse_api(api_json(items=False)) == tr.Check(True, False, None), "deleted or private"


def test_parse_api_odd_answers_are_unclear():
    assert tr.parse_api(api_json(upload="uploaded")) == tr.Check(True, None, 84), "still processing: unclear"
    assert tr.parse_api(api_json(upload="rejected")).de is None
    assert tr.parse_api(api_json({"allowed": []})) == tr.Check(True, None, 84), "empty list: unclear"
    assert tr.parse_api(api_json({"blocked": []})).de is None
    assert tr.parse_api(api_json({"allowed": ["DEU"]})).de is None, "only two-letter codes"
    assert tr.parse_api(api_json({"allowed": ["de"]})).de is None
    assert tr.parse_api(api_json({"allowed": "DE"})).de is None, "not a list"
    assert tr.parse_api(api_json({"allowed": ["DE"], "blocked": ["US"]})).de is None, "both kinds: unclear"
    assert tr.parse_api(api_json({"other": ["DE"]})).de is None
    assert tr.parse_api(api_json({})).de is None
    assert tr.parse_api(api_json("DE")).de is None


def test_parse_api_broken_shapes_decide_nothing():
    assert tr.parse_api({"items": [{}]}) == tr.UNREAD
    assert tr.parse_api({"items": ["x"]}) == tr.UNREAD
    assert tr.parse_api({"items": [api_json()["items"][0]] * 2}) == tr.UNREAD, "exactly one item"
    assert tr.parse_api({"error": {"code": 403}}) == tr.UNREAD
    assert tr.parse_api(None) == tr.UNREAD
    assert tr.parse_api({"items": None}) == tr.UNREAD
    it = api_json()["items"][0]
    assert tr.parse_api({"items": [{**it, "contentDetails": []}]}) == tr.UNREAD
    assert tr.parse_api({"items": [{**it, "status": None}]}) == tr.UNREAD
    assert tr.parse_api({"items": [{**it, "status": {"uploadStatus": "processed"}}]}) == tr.UNREAD
    assert tr.parse_api({"items": [{**it, "status": {**it["status"], "privacyStatus": "x"}}]}) == tr.UNREAD
    assert tr.parse_api(api_json(), key="Z5uv7_ysu9M").de is True
    assert tr.parse_api(api_json(), key="556N5vojtp0") == tr.UNREAD, "answer for another video"


def test_iso_duration():
    assert tr._iso_seconds("PT1M24S") == 84
    assert tr._iso_seconds("PT2H") == 7200
    assert tr._iso_seconds("PT45S") == 45
    assert tr._iso_seconds("P0D") == 0
    assert tr._iso_seconds("") is None and tr._iso_seconds("PT") is None and tr._iso_seconds("1:24") is None


class _Resp:
    def __init__(self, status=200, data=None):
        self.status_code, self._data = status, data

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data


def test_api_check_needs_the_key_and_keeps_it_out_of_the_url(monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    assert tr.api_check("Z5uv7_ysu9M") == tr.UNREAD, "no key: no call"
    seen = {}

    def fake_get(url, timeout, params, headers):
        seen.update(url=url, params=params, headers=headers)
        return _Resp(200, api_json())
    monkeypatch.setenv("YOUTUBE_API_KEY", "SECRET-KEY")
    monkeypatch.setattr(tr.requests, "get", fake_get)
    assert tr.api_check("Z5uv7_ysu9M") == tr.Check(True, True, 84)
    assert seen["headers"]["X-goog-api-key"] == "SECRET-KEY"
    assert "SECRET-KEY" not in seen["url"] and "SECRET-KEY" not in str(seen["params"])


def test_api_check_disturbances_are_unread(monkeypatch, capsys):
    monkeypatch.setenv("YOUTUBE_API_KEY", "SECRET-KEY")
    for resp in (_Resp(403, {"error": "quotaExceeded"}), _Resp(500, None), _Resp(200, ValueError("no json"))):
        monkeypatch.setattr(tr.requests, "get", lambda *a, _r=resp, **kw: _r)
        assert tr.api_check("Z5uv7_ysu9M") == tr.UNREAD

    def boom(*a, **kw):
        raise ConnectionError("down")
    monkeypatch.setattr(tr.requests, "get", boom)
    assert tr.api_check("Z5uv7_ysu9M") == tr.UNREAD
    assert "SECRET-KEY" not in capsys.readouterr().err


def test_only_the_api_decides(monkeypatch):
    """Player endpoint and watch page are diagnosis only (they would pass an age gate)."""
    called = []
    monkeypatch.setattr(tr, "player_check", lambda k: called.append("player") or tr.Check(True, True, 84))
    monkeypatch.setattr(tr, "watch_check", lambda k: called.append("watch") or tr.Check(True, True, 84))
    monkeypatch.setattr(tr, "api_check", lambda k: tr.UNREAD)
    assert tr.youtube_check("Z5uv7_ysu9M") == tr.UNREAD and called == []


def test_api_healthy_means_the_api_answers(monkeypatch):
    monkeypatch.setattr(tr, "api_check", lambda k: tr.UNREAD)
    assert tr.api_healthy() is False
    monkeypatch.setattr(tr, "api_check", lambda k: tr.Check(True, None, 213) if k == tr.CANARY else tr.UNREAD)
    assert tr.api_healthy() is True, "a well-formed answer is enough, DE status of the canary is irrelevant"


def test_choose_skips_blocked_and_unclear_candidates():
    """The Night Of 01.11.2026: the US-only 'Official Trailer' ranks first; the UK trailer plays in DE."""
    cands = tr.ranked([v("556N5vojtp0", name="Official Trailer", date="2016-06-01"),
                       v("Z5uv7_ysu9M", name="Official UK Trailer", date="2016-05-01")])
    assert cands[0]["key"] == "556N5vojtp0"
    checks = {"556N5vojtp0": tr.Check(True, False, 91), "Z5uv7_ysu9M": tr.Check(True, True, 84)}
    assert tr.choose(cands, check=checks.get) == "Z5uv7_ysu9M"
    only_blocked = {"556N5vojtp0": tr.Check(True, False, 91), "Z5uv7_ysu9M": tr.Check(True, False, 84)}
    assert tr.choose(cands, check=only_blocked.get) is None
    assert tr.choose(cands, check=lambda k: tr.Check(True, None, 84)) is None, "unclear never becomes a link"


def test_choose_raises_when_the_check_has_no_answer():
    cands = [v("556N5vojtp0"), v("Z5uv7_ysu9M")]
    checks = {"556N5vojtp0": tr.Check(True, False, 91), "Z5uv7_ysu9M": tr.UNREAD}
    try:
        tr.choose(cands, check=checks.get)
    except tr.CheckUnavailable:
        pass
    else:
        raise AssertionError("an unanswered check must not end as 'no trailer'")


def test_enrich_trailers_records_blocked_and_unclear():
    em = {f"{B}/movie/a/": _meta(trailer_due=True, emitted_at="2026-11-02T10:02:00"),
          f"{B}/movie/b/": _meta(trailer_due=True, emitted_at="2026-11-02T10:01:00")}

    def lookup(tm, media, season, token, check=None):
        return tr.choose([v("556N5vojtp0"), v("Z5uv7_ysu9M")], check=check)
    checks = iter([tr.Check(True, False, 91), tr.Check(True, False, 84),        # a: both blocked in DE
                   tr.Check(True, None, 91), tr.Check(True, None, 84)])         # b: both unclear
    found, degraded = mf.enrich_trailers(em, 10, 0, "tok", lookup=lookup, check=lambda k: next(checks))
    assert (found, degraded) == (0, False)
    assert em[f"{B}/movie/a/"]["trailer"] == {"v": tr.VERSION, "key": None, "blocked_de": 2}
    assert em[f"{B}/movie/b/"]["trailer"] == {"v": tr.VERSION, "key": None, "unchecked": True}


def test_enrich_trailers_quota_mid_run_freezes_nothing():
    """Health check passed, then the quota runs out after the first item: the rest stays due."""
    em = {f"{B}/movie/{i}/": _meta(trailer_due=True, emitted_at=f"2026-11-02T10:0{i}:00") for i in range(3)}
    answers = iter([tr.Check(True, True, 84)] + [tr.UNREAD] * 10)

    def lookup(tm, media, season, token, check=None):
        return tr.choose([v("Z5uv7_ysu9M")], check=check)
    found, degraded = mf.enrich_trailers(em, 10, 0, "tok", lookup=lookup, check=lambda k: next(answers))
    assert (found, degraded) == (1, True)
    assert em[f"{B}/movie/2/"]["trailer"]["key"] == "Z5uv7_ysu9M", "newest first, before the quota ran out"
    for i in (0, 1):
        assert em[f"{B}/movie/{i}/"].get("trailer_due") and "trailer" not in em[f"{B}/movie/{i}/"]


def test_reverify_keeps_playable_links_byte_identical():
    m = _meta(trailer={"v": 2, "key": "Z5uv7_ysu9M", "checked": "2026-10-30"})
    before = mf.describe_item(_meta(trailer={"v": 3, "key": "Z5uv7_ysu9M"}))
    em = {f"{B}/movie/a/": m}
    assert mf.reverify_trailers(em, check=lambda k: tr.Check(True, True, 84), today="2026-11-02", delay=0) is False
    assert m["trailer"] == {"v": 3, "key": "Z5uv7_ysu9M", "checked": "2026-10-30", "de_checked": "2026-11-02"}
    assert mf.describe_item(m) == before


def test_reverify_drops_links_blocked_in_de_or_unclear():
    em = {f"{B}/tv/x/": _meta(trailer={"v": 2, "key": "556N5vojtp0"}),
          f"{B}/tv/y/": _meta(trailer={"v": 2, "key": "Z5uv7_ysu9M"})}
    answers = {"556N5vojtp0": tr.Check(True, False, 91), "Z5uv7_ysu9M": tr.Check(True, None, 84)}
    assert mf.reverify_trailers(em, check=answers.get, today="2026-11-02", delay=0) is False
    assert em[f"{B}/tv/x/"]["trailer"] == {"v": tr.VERSION, "key": None, "blocked_de": True,
                                         "was": "556N5vojtp0", "checked": "2026-11-02"}
    assert em[f"{B}/tv/y/"]["trailer"] == {"v": tr.VERSION, "key": None, "unchecked": True,
                                         "was": "Z5uv7_ysu9M", "checked": "2026-11-02"}
    assert all("Trailer" not in mf.describe_item(m) for m in em.values())


def test_reverify_without_answer_changes_nothing():
    em = {f"{B}/movie/{i}/": _meta(trailer={"v": 2, "key": "Z5uv7_ysu9M"}) for i in range(3)}
    for _ in range(5):
        assert mf.reverify_trailers(em, check=lambda k: tr.UNREAD, delay=0) is True
    assert all(m["trailer"] == {"v": 2, "key": "Z5uv7_ysu9M"} for m in em.values())
    assert all("Trailer" not in mf.describe_item(m) for m in em.values()), "hidden while unconfirmed"


ARGS = SimpleNamespace(trailer_delay=0, trailer_max=10)


def _never(*a, **kw):
    raise AssertionError("must not be called")


def test_trailer_step_without_key_is_quiet(monkeypatch):
    """No key: no lookup, nothing frozen, items stay due, not degraded (green run)."""
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    em = {f"{B}/movie/a/": _meta(trailer_due=True), f"{B}/movie/b/": _meta(trailer={"v": 2, "key": "Z5uv7_ysu9M"})}
    snapshot = repr(em)
    assert mf.trailer_step(em, ARGS, "2026-11-02", healthy=_never, reverify=_never, enrich=_never) == (False, "missing")
    assert repr(em) == snapshot


def test_trailer_step_key_set_but_api_silent_is_red_and_changes_nothing(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "SECRET-KEY")
    em = {f"{B}/movie/a/": _meta(trailer_due=True)}
    snapshot = repr(em)
    assert mf.trailer_step(em, ARGS, "2026-11-02", healthy=lambda: False, reverify=_never,
                           enrich=_never) == (True, "set")
    assert repr(em) == snapshot


def test_trailer_step_healthy_runs_reverify_then_enrich(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "SECRET-KEY")
    monkeypatch.setenv("TMDB_TOKEN", "tok")
    calls = []
    em = {f"{B}/movie/a/": _meta(trailer_due=True)}
    out = mf.trailer_step(em, ARGS, "2026-11-02", healthy=lambda: True,
                          reverify=lambda *a, **kw: calls.append("reverify") or False,
                          enrich=lambda *a, **kw: calls.append("enrich") or (1, False))
    assert out == (False, "set") and calls == ["reverify", "enrich"]
    calls.clear()
    out = mf.trailer_step(em, ARGS, "2026-11-02", healthy=lambda: True,
                          reverify=lambda *a, **kw: calls.append("reverify") or True, enrich=_never)
    assert out == (True, "set") and calls == ["reverify"], "no answer in reverify: stop, red"
    assert mf.trailer_step({}, ARGS, "2026-11-02", healthy=_never) == (False, "set"), "nothing pending: no API call"


# --- diagnosis only (probe) ---

def test_parse_watch_and_player_still_parse_for_the_probe():
    assert tr.parse_watch(page("UNPLAYABLE", ("US",), 91)) == tr.Check(True, False, 91)
    assert tr.parse_watch("") == tr.UNREAD
    pj = {"playabilityStatus": {"status": "UNPLAYABLE"}, "videoDetails": {"lengthSeconds": "84"},
          "microformat": {"playerMicroformatRenderer": {"availableCountries": ["DE", "US"]}}}
    assert tr.parse_player(pj) == tr.Check(True, True, 84)
