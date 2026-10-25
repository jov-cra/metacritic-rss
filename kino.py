"""
kino.py — Munich cinemas showing a new movie in the original version, with a booking link
per showing (25.10.2026, Johannes: "nicht nur ein Titel ist im Kino, sondern auch wo und wann").

Copied from the filme-serien skill (fs_kino.py, 25.10.2026): the indiekino and Cinema
Filmtheater parsers, the original-version rule and the fail-closed kinoheld booking match.
Differences: requests instead of fs_net, no cache, no TMDB identity search — a feed item
is matched to the programme by its JustWatch titles (exact, normalised) and year ±1.

Snapshot at emit like the streaming line: one lookup when a movie that JustWatch marks
"im Kino" is emitted, frozen with its date; never refreshed (the reader already has the item).
Supplementary: a failed programme never stops the feed, it sets a flag (red run after commit).
"""
from __future__ import annotations

import datetime as dt
import html as htmlmod
import json
import re
import urllib.parse
from html import escape as _esc

try:
    import requests
except ImportError:  # keeps the module importable for tests without requests
    requests = None

try:
    from zoneinfo import ZoneInfo
    BERLIN = ZoneInfo("Europe/Berlin")
except Exception:  # pragma: no cover
    BERLIN = None

from streaming import _norm

VERSION = 1
WINDOW_DAYS = 7
MAX_SHOWS = 3
UA = "Mozilla/5.0 (metacritic-rss personal feed)"


class KinoFehler(RuntimeError):
    """A programme or the booking directory could not be read."""


INDIEKINO_URL = "https://www.indiekino.de/kinoprogramm/en/muenchen/film_kino"
CINEMA_URL = "https://cinema-muenchen.com/de/programmuebersicht/"
CINEMA_NAME = "Cinema Filmtheater"
FASSUNG_OV = {"OV", "OmU", "OmeU"}
VERANSTALTUNG_RE = re.compile(
    r"sneak|überraschungsfilm|surprise|marathon|double feature|\bDF:|live[- ]?(?:übertragung|viewing)|"
    r"in cinema\b|world tour|\bkonzert\b|\bopera\b|\boper\b|ballett|\bpreview\b|vorpremiere", re.I)
DEUTSCHSPRACHIG_RE = re.compile(r"Deutschland|Österreich|Schweiz|\bD\b|\bA\b|\bCH\b|BRD|DDR")
CINEMA_FASSUNG = {"ov": "OV", "omu": "OmU", "omeu": "OmeU", "orig.m.engl.u": "OmeU", "deutsch": "DE"}
CINEMA_SAALPLAN = ("https://webshop.cinema-muenchen.de/landingpage?center=30000000014BPIBFDE"
                   "&language=DE&page=seatingplan&performance={}")
CINEMA_PERF_RE = re.compile(r"[0-9A-Z]{18}")
KINOHELD_URL = "https://next-live.kinoheld.de/graphql"
KINOHELD_ID = {
    "muenchen_museum-lichtspiele": "995", "muenchen_leopold": "3653", "muenchen_abc-kino": "3651",
    "muenchen_monopol_am_nordbad": "981", "muenchen_rio_filmpalast": "748", "muenchen_neues_maxim": "3755",
    "muenchen_arena_filmtheater": "29", "muenchen_studio_isabella": "1184",
    "muenchen_mathaeser_der_filmpalast": "951", "muenchen_royal_filmpalast": "1111",
    "muenchen_neues_rex": "1007", "muenchen_neues_rottmann": "1008", "muenchen_cincinnati": "180",
    "muenchen_cadillac_und_veranda": "110", "muenchen_theatiner-film": "1199",
    "muenchen_astorcinema_lounge_im_bayerischen_hof": "1503", "muenchen_astor_film_lounge_im_arri": "31",
}
VORSTELLUNG_DEEPLINK = [re.compile(p) for p in (
    r"https://www\.mathaeser\.de/mm/programm/vorstellung/[0-9A-Z]{10,30}$",
    r"https://royal-muenchen\.de/event/\?showID=\d+",
    r"https://booking\.cinetixx\.de/frontend/index\.html\?cinemaId=\d+&showId=\d+",
    r"https://kinotickets\.express/[a-z0-9-]+/booking/\d+$",
    r"https://theatiner-film\.de/product/[a-z0-9-]+-\d{4}-uhr/?$",
    r"https://ticket-cloud\.de/[A-Za-z0-9-]+/Show/\d+/?$",
    r"https://muenchen\.premiumkino\.de/vorstellung/[a-z0-9-]+/\d{8}/\d{4}/",
    r"https://webshop\.cinema-muenchen\.de/landingpage\?.*performance=[0-9A-Z]{18}",
)]
KINOHELD_LIMIT = 500   # höchstens 500 je Abfrage; eine volle Antwort kann abgeschnitten sein
KINOHELD_Q = ("query($c: ID, $d: [Date!]) { shows(cinemaId: $c, dates: $d, limit: 500) { data { beginning isBookable deeplink "
              "urlSlug audioLanguage { name } subtitleLanguage { name } movie { title titleOriginal } "
              "cinema { urlSlug bookingType url } } } }")


def _datum(text: str, heute: dt.date) -> dt.date | None:
    if "TODAY" in text or "HEUTE" in text.upper():
        return heute
    m = re.match(r"\s*(\d{1,2})\.(\d{1,2})\.", text)
    if not m:
        return None
    tag, monat = int(m.group(1)), int(m.group(2))
    jahr = heute.year + (1 if monat < heute.month - 6 else 0)
    try:
        return dt.date(jahr, monat, tag)
    except ValueError:
        return None


def parse_indiekino(text: str, heute: dt.date) -> tuple:
    """-> (filme, stat). film: {quelle, titel, alt_titel, jahr, land, regie, vorstellungen[]}.
    vorstellung: {kino, kino_slug, datum, zeit, fassung (OV/OmU/OmeU/None)}."""
    bloecke = re.split(r"(?=<div\s+id='gp_\d+')", text)[1:]
    filme, stat = [], {"bloecke": len(bloecke), "termine": 0, "mit_fassung": 0, "unbekannte_fassung": 0,
                       "ausserhalb": 0, "ohne_titel": 0, "ohne_datum": 0, "uebernommen": 0}
    for b in bloecke:
        # Titel mit oder ohne Link, auch class='titel special' (Review 25.10.: sonst fehlten u. a.
        # „Der perfekte Urlaub“ und weitere deutsche Filme still)
        t = re.search(r"<span class='titel[^']*'>\s*(?:<a href='([^']+)'>)?([^<]+)", b)
        if not t:
            stat["ohne_titel"] += 1
            continue
        titel = htmlmod.unescape(t.group(2)).strip()
        zeile = re.search(r"</h2><p>(.*?)</p>", b, re.S)
        z = htmlmod.unescape(re.sub(r"<[^>]+>", "", zeile.group(1))) if zeile else ""
        alt = None
        m = re.match(r"\s*\((.+?)\)\s*\|\s*(.*)$", z)
        if m:
            alt, z = m.group(1).strip(), m.group(2)
        teile = [p.strip() for p in z.split("|")]
        land, jahr = None, None
        if teile:
            mj = re.search(r"^(.*?)\s*(\d{4})\s*$", teile[0])
            if mj:
                land, jahr = mj.group(1).strip() or None, int(mj.group(2))
        regie = next((p[2:].strip() for p in teile if p.startswith("R:")), None)
        film = {"quelle": "indiekino", "titel": titel, "alt_titel": alt, "jahr": jahr, "land": land,
                "regie": regie, "link": ("https://www.indiekino.de" + t.group(1)) if t.group(1) else None,
                "vorstellungen": []}
        for pa in re.split(r"<div class='pa ", b)[1:]:
            k = re.search(r"<h3 class='kino[^']*'><a href='/kinoprogramm/en/muenchen/kinos/([^']+)'>([^<]+)</a>", pa)
            if not k:
                continue
            slug, kino = k.group(1), htmlmod.unescape(k.group(2)).strip()
            for tb in re.split(r"<div class='termin", pa)[1:]:
                dm = re.search(r"<p class='datum'>(?:<span class='heute'>)?([^<]+)<", tb)
                datum = _datum(dm.group(1) if dm else "", heute)
                for zp in re.findall(r"<p class='zeit[^']*'>(.*?)</p>", tb, re.S):
                    fm = re.search(r"<span class='fassung'>([^<]+)</span>", zp)
                    fassung = fm.group(1).strip() if fm else None
                    uhren = re.findall(r"<span class='zi[^']*'>(\d{1,2}:\d{2})</span>", zp)
                    tickets = re.findall(r"href='(https://www\.kinoheld\.de/[^']+)'", zp)
                    ticket = _kinoheld_sauber(tickets[0]) if len(uhren) == 1 and len(tickets) == 1 else None
                    uhren = [u.zfill(5) for u in uhren]   # „9:30“ → „09:30“ (Vergleich als Text)
                    for uhr in uhren:
                        stat["termine"] += 1
                        if not slug.startswith("muenchen_"):
                            stat["ausserhalb"] += 1  # Gräfelfing u. Ä.: nicht München
                            continue
                        if fassung:
                            stat["mit_fassung"] += 1
                            if fassung not in FASSUNG_OV:
                                stat["unbekannte_fassung"] += 1
                                continue
                        if not datum:
                            stat["ohne_datum"] += 1
                            continue
                        stat["uebernommen"] += 1
                        v = {"kino": kino, "kino_slug": slug, "datum": datum.isoformat(),
                             "zeit": uhr, "fassung": fassung, "land": land}
                        if ticket:
                            v["buchung"] = {"url": ticket, "art": "vorstellung"}
                        film["vorstellungen"].append(v)
        if film["vorstellungen"]:
            filme.append(film)
    return filme, stat


def parse_cinema(text: str) -> tuple:
    i = text.find("var pmkinoFrontVars = ")
    if i < 0:
        raise KinoFehler("Cinema: Programmdaten (pmkinoFrontVars) nicht gefunden")
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[i + len("var pmkinoFrontVars = "):])
        filme_roh = obj["apiData"]["movies"]["items"]
        vorst_roh = obj["apiData"]["performances"]["items"]
    except (ValueError, KeyError, TypeError) as e:
        raise KinoFehler(f"Cinema: Programmdaten unlesbar ({e})") from e
    if not isinstance(filme_roh, dict) or not isinstance(vorst_roh, dict):
        raise KinoFehler("Cinema: Programmdaten haben eine andere Form")
    stat = {"filme": len(filme_roh), "termine": len(vorst_roh), "ohne_film": 0, "unbekannte_fassung": 0,
            "ohne_zeit": 0, "uebernommen": 0}
    try:
        seiten = {k: ((v or {}).get("uri_list") or {}).get("de")
                  for k, v in ((obj.get("routesData") or {}).get("movies") or {}).get("items", {}).items()}
    except AttributeError:
        seiten = {}
    filme = {}
    for pk, m in filme_roh.items():
        if not isinstance(m, dict):
            continue
        filme[pk] = {"quelle": "cinema", "titel": (m.get("origTitle") or m.get("title") or "").strip(),
                     "alt_titel": (m.get("title") or "").strip() or None,
                     "jahr": int(m["productionYear"]) if str(m.get("productionYear") or "").isdigit() else None,
                     "start": m.get("startingDate"), "laufzeit": m.get("length") or None,
                     "regie": _cinema_regie(m.get("description") or ""),
                     "link": _https(seiten.get(pk)) or CINEMA_URL, "vorstellungen": []}
    for p in vorst_roh.values():
        if not isinstance(p, dict):
            continue
        f = filme.get(p.get("moviePk"))
        if not f:
            stat["ohne_film"] += 1
            continue
        attr = [str(a.get("name") or "").strip().lower() for a in p.get("attributes") or [] if isinstance(a, dict)]
        fassungen = [CINEMA_FASSUNG[a] for a in attr if a in CINEMA_FASSUNG]
        if len(set(fassungen)) != 1:
            stat["unbekannte_fassung"] += 1  # keine oder widersprüchliche Fassung: nicht übernehmen
            continue
        ts = p.get("timeUtc")
        if not isinstance(ts, (int, float)):
            stat["ohne_zeit"] += 1
            continue
        stat["uebernommen"] += 1
        t = dt.datetime.fromtimestamp(ts / 1000, tz=dt.timezone.utc)
        t = t.astimezone(BERLIN) if BERLIN else t
        fa = fassungen[0]
        v = {"kino": CINEMA_NAME, "kino_slug": "cinema", "datum": t.date().isoformat(),
             "zeit": t.strftime("%H:%M"), "fassung": None if fa == "DE" else fa, "land": None}
        pid = str(p.get("id") or "")
        if CINEMA_PERF_RE.fullmatch(pid):
            v["buchung"] = {"url": CINEMA_SAALPLAN.format(pid), "art": "vorstellung"}
        else:
            v["buchung"] = {"url": f["link"], "art": "film"}
        f["vorstellungen"].append(v)
    return [f for f in filme.values() if f["vorstellungen"] and f["titel"]], stat


def _cinema_regie(text: str) -> str | None:
    """„By the Russo Brothers,\nwith …“ / „Von X, mit …“ → Regie für die Gegenprobe."""
    m = re.match(r"\s*(?:By|Von|Regie:)\s+([^,\n]+)", text)
    return m.group(1).strip() if m else None


def _https(url):
    return url if isinstance(url, str) and url.startswith("https://") else None


def _kinoheld_sauber(url: str):
    """indiekino-Ticketlink ohne Widget-Parameter: …/kino/<ort>/<kino>/vorstellung/<id>[/<film>]."""
    u = urllib.parse.urlsplit(url)
    if u.scheme != "https" or u.netloc != "www.kinoheld.de" or "/vorstellung/" not in u.path:
        return None
    return urllib.parse.urlunsplit(("https", u.netloc, u.path, "", ""))


def kinoheld_tage(heute: dt.date) -> list:
    """Abfragefenster: heute bis heute+5 in Blöcken zu drei Tagen (kinoheld erlaubt höchstens drei
    Daten je Abfrage); deckt indiekino (heute plus drei Tage) ab."""
    tage = [(heute + dt.timedelta(days=i)).isoformat() for i in range(6)]
    return [tage[:3], tage[3:]]


def _beginn(iso) -> tuple:
    s = str(iso or "").replace("Z", "+00:00")
    s = re.sub(r"(\d{2}:\d{2}:\d{2})\.\d+", r"\1", s)   # Python 3.9: keine Bruchteile mit Zone
    try:
        t = dt.datetime.fromisoformat(s)
    except ValueError:
        return None, None
    if t.tzinfo is None:
        return None, None
    t = t.astimezone(BERLIN) if BERLIN else t
    return t.date().isoformat(), t.strftime("%H:%M")


def _titel_passt(show: dict, varianten: list) -> bool:
    m = show.get("movie") if isinstance(show.get("movie"), dict) else {}
    seite = {_norm(x) for x in (m.get("title"), m.get("titleOriginal")) if isinstance(x, str) and _norm(x)}
    for v in varianten:
        n = _norm(v) if isinstance(v, str) else ""
        if not n:
            continue
        for s in seite:
            if s == n or s.startswith(n + " ") or n.startswith(s + " "):
                return True
    return False


def _fassung_widerspricht(show: dict, v: dict, sprache) -> bool:
    def name(x):
        return str((x if isinstance(x, dict) else {}).get("name") or "").lower()
    audio, unter = name(show.get("audioLanguage")), name(show.get("subtitleLanguage"))
    if audio == "german" and sprache and sprache != "de":
        return True  # Synchronfassung
    if v.get("fassung") == "OmU" and unter and unter != "german":
        return True
    if v.get("fassung") == "OmeU" and unter and unter != "english":
        return True
    if v.get("fassung") == "OV" and unter:
        return True
    return False


def kinoheld_link(show: dict):
    c = show.get("cinema") if isinstance(show.get("cinema"), dict) else {}
    if c.get("bookingType") == "FIRST_PARTY":
        if show.get("isBookable") is True and show.get("urlSlug") and c.get("urlSlug"):
            return f"https://www.kinoheld.de/kino/muenchen/{c['urlSlug']}/vorstellung/{show['urlSlug']}"
        return None
    dl = _https(show.get("deeplink"))
    if not dl or not any(m.match(dl) for m in VORSTELLUNG_DEEPLINK):
        return None  # Startseite, Programm- oder Filmseite: kein Vorstellungslink
    return dl


def kinoheld_zuordnen(v: dict, shows: list, varianten: list, sprache=None):
    """Vorstellung → Buchungslink oder None. Kino schon durch die Show-Liste fest; Beginn auf die
    Minute; mehrere zur selben Minute → Titel muss genau eine auswählen; Fassung darf nicht
    widersprechen. Im Zweifel None."""
    gleich = [s for s in shows if isinstance(s, dict) and _beginn(s.get("beginning")) == (v["datum"], v["zeit"])]
    gleich = [s for s in gleich if _titel_passt(s, varianten)]   # Titel immer, auch bei nur einem Treffer
    if len(gleich) != 1 or _fassung_widerspricht(gleich[0], v, sprache):
        return None
    return kinoheld_link(gleich[0])


def programmseite(v: dict) -> str:
    if v.get("kino_slug") == "cinema":
        return CINEMA_URL
    return f"https://www.indiekino.de/kinoprogramm/en/muenchen/kinos/{v.get('kino_slug')}"


def buchung(v: dict, shows_je_kino: dict, varianten: list, sprache=None) -> dict:
    """-> {url, art: vorstellung|film|programm}. Reihenfolge: Link aus der Programmquelle
    (indiekino-Ticket, Cinema-Saalplan), kinoheld, Filmseite, Programmseite."""
    b = v.get("buchung") or {}
    if b.get("art") == "vorstellung" and b.get("url"):
        return b
    kid = KINOHELD_ID.get(v.get("kino_slug") or "")
    if kid and isinstance(shows_je_kino.get(kid), list):
        url = kinoheld_zuordnen(v, shows_je_kino[kid], varianten, sprache)
        if url:
            return {"url": url, "art": "vorstellung"}
    if b.get("url"):
        return b
    return {"url": programmseite(v), "art": "programm"}


def original_vorstellung(v: dict, sprache: str | None) -> bool:
    """Original heißt: OmU/OmeU in jeder Sprache; OV nur bei englischem oder deutschem Original
    (OV ohne Untertitel in einer dritten Sprache versteht er nicht); ohne Fassungsangabe nur,
    wenn der Film im Original deutsch ist (Johannes 25.10.2026)."""
    f = v.get("fassung")
    if f in ("OmU", "OmeU"):
        return True
    if f == "OV":
        return sprache in ("en", "de")
    # ohne Angabe: nur ein deutscher Film aus einem deutschsprachigen Land (Koproduktionen mit
    # falscher TMDB-Sprache gingen sonst synchronisiert als „Original“ durch)
    return f is None and sprache == "de" and bool(DEUTSCHSPRACHIG_RE.search(v.get("land") or ""))


def pruefe_indiekino(stat: dict) -> None:
    if stat["bloecke"] < 10 or stat["termine"] < 20:
        raise KinoFehler(f"indiekino: nur {stat['bloecke']} Filme / {stat['termine']} Termine gelesen (Seite geändert?)")
    if stat["mit_fassung"] == 0:
        raise KinoFehler("indiekino: kein Termin mit Fassungsangabe (Markup geändert?)")
    if stat.get("uebernommen", 1) == 0:
        raise KinoFehler("indiekino: kein Termin übernommen (Datumsformat geändert?)")
    if stat.get("ohne_titel", 0) > stat["bloecke"] // 10 or stat.get("ohne_datum", 0) > stat["termine"] // 10:
        raise KinoFehler(f"indiekino: {stat.get('ohne_titel')} Filme ohne Titel, {stat.get('ohne_datum')} Termine "
                         "ohne Datum (Markup geändert?)")


def pruefe_cinema(stat: dict) -> None:
    if stat["filme"] == 0 or stat["termine"] == 0:
        raise KinoFehler("Cinema: leeres Programm gelesen (Seite geändert?)")
    if stat.get("ohne_zeit", 0) > stat["termine"] // 10 or stat.get("uebernommen", 1) == 0:
        raise KinoFehler(f"Cinema: {stat.get('ohne_zeit')} Vorstellungen ohne Zeit (Format geändert?)")
    if stat["ohne_film"] > stat["termine"] // 2:
        raise KinoFehler(f"Cinema: {stat['ohne_film']} Vorstellungen ohne Film (Format geändert?)")


# --------------------------------------------------------------------------- #
# Feed side: fetch, match, snapshot, describe
# --------------------------------------------------------------------------- #
def _get(url: str, timeout: int = 15) -> str:
    if requests is None:
        raise KinoFehler("the 'requests' package is required")
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    except Exception as exc:
        raise KinoFehler(f"{url[:80]}: {type(exc).__name__}") from exc
    if r.status_code != 200:
        raise KinoFehler(f"{url[:80]}: HTTP {r.status_code}")
    return r.text


def load_program(today: dt.date, get=_get) -> tuple:
    """-> (films, failed_sources). Each source guarded on its own; one may fail."""
    films, failed = [], []
    for name, url in (("indiekino", INDIEKINO_URL), ("cinema", CINEMA_URL)):
        try:
            text = get(url)
            if name == "indiekino":
                f, stat = parse_indiekino(text, today)
                pruefe_indiekino(stat)
            else:
                f, stat = parse_cinema(text)
                pruefe_cinema(stat)
            films += f
        except Exception as exc:   # supplementary source: never fatal (reported, run turns red)
            print(f"[warn] cinema programme {name} unreadable: {type(exc).__name__}: {exc}")
            failed.append(name)
    return films, failed


def kinoheld_shows(cinema_id: str, today: dt.date, post=None) -> list:
    """Showings of one cinema from today to today+5 (two requests, at most three dates each).
    A full answer (KINOHELD_LIMIT) may be cut off: KinoFehler. Raises KinoFehler."""
    shows = []
    for days in kinoheld_tage(today):
        body = {"query": KINOHELD_Q, "variables": {"c": cinema_id, "d": days}}
        try:
            if post is not None:
                d = post(KINOHELD_URL, body)
            else:
                if requests is None:
                    raise KinoFehler("the 'requests' package is required")
                r = requests.post(KINOHELD_URL, json=body, timeout=10, headers={"User-Agent": UA})
                if r.status_code != 200:
                    raise KinoFehler(f"kinoheld {cinema_id}: HTTP {r.status_code}")
                d = r.json()
        except KinoFehler:
            raise
        except Exception as exc:
            raise KinoFehler(f"kinoheld {cinema_id}: {type(exc).__name__}") from exc
        if not isinstance(d, dict) or d.get("errors"):
            raise KinoFehler(f"kinoheld {cinema_id}: GraphQL error")
        try:
            part = d["data"]["shows"]["data"]
        except (KeyError, TypeError) as exc:
            raise KinoFehler(f"kinoheld {cinema_id}: unexpected shape") from exc
        if not isinstance(part, list):
            raise KinoFehler(f"kinoheld {cinema_id}: data is not a list")
        if len(part) >= KINOHELD_LIMIT:
            raise KinoFehler(f"kinoheld {cinema_id}: full answer ({len(part)}), may be cut off")
        shows += part
    return shows


def _film_year(f: dict):
    if f.get("quelle") == "cinema":   # productionYear is often wrong there; the start date is not
        s = str(f.get("start") or "")
        return int(s[:4]) if s[:4].isdigit() else f.get("jahr")
    return f.get("jahr")


def match_films(films: list, titles: list, year) -> list:
    """Programme films that are this item: an exact normalised title (either side's
    original or local title) and, when both years are known, year ±1."""
    want = {_norm(t) for t in titles if t and _norm(t)}
    out = []
    for f in films:
        names = {_norm(x) for x in (f.get("titel"), f.get("alt_titel")) if x and _norm(x)}
        if not names & want:
            continue
        if VERANSTALTUNG_RE.search((f.get("titel") or "") + " " + (f.get("alt_titel") or "")):
            continue
        fy = _film_year(f)
        if year and fy and abs(int(fy) - int(year)) > 1:
            continue
        out.append(f)
    return out


def snapshot(films: list, language, now: dt.datetime, shows_for, titles: list) -> list:
    """Next MAX_SHOWS original-version showings within WINDOW_DAYS, each with a booking
    link. shows_for(kinoheld_id) -> list or None (failed)."""
    today = now.date().isoformat()
    until = (now.date() + dt.timedelta(days=WINDOW_DAYS)).isoformat()
    hhmm = now.strftime("%H:%M")
    seen, rows = set(), []
    for f in films:
        for v in f.get("vorstellungen") or []:
            if not (today <= v["datum"] < until) or (v["datum"] == today and v["zeit"] < hhmm):
                continue
            if not original_vorstellung(v, language):
                continue
            key = (v["datum"], v["zeit"], v["kino"])
            if key in seen:
                continue
            seen.add(key)
            rows.append(v)
    rows.sort(key=lambda v: (v["datum"], v["zeit"], v["kino"]))
    out = []
    for v in rows[:MAX_SHOWS]:
        kid = KINOHELD_ID.get(v.get("kino_slug") or "")
        per = {}
        if kid and not (v.get("buchung") or {}).get("art") == "vorstellung":
            got = shows_for(kid)
            if isinstance(got, list):
                per[kid] = got
        b = buchung(v, per, titles, language)
        out.append({"datum": v["datum"], "zeit": v["zeit"], "kino": v["kino"],
                    "fassung": v.get("fassung") or "OV", "url": b["url"], "art": b["art"]})
    return out


WOCHENTAG = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")


def describe(rec: dict, checked: str = "") -> str:
    """One description line (raw HTML; the feed escapes it once more as XML text)."""
    shows = (rec or {}).get("shows") or []
    if not shows:
        return ""
    bits = []
    for s in shows:
        d = dt.date.fromisoformat(s["datum"])
        when = f'{WOCHENTAG[d.weekday()]} {d.day:02d}.{d.month:02d}. {s["zeit"]} {_esc(s["fassung"])}'
        if s.get("art") == "vorstellung":
            bits.append(f'<a href="{_esc(s["url"])}">{when}</a> {_esc(s["kino"])}')
        else:
            label = "Filmseite" if s.get("art") == "film" else "Programm"
            bits.append(f'{when} {_esc(s["kino"])} (<a href="{_esc(s["url"])}">{label}</a>)')
    head = f"Kino München im Original ({checked})" if checked else "Kino München im Original"
    if (rec or {}).get("partial"):
        head += ", Programm unvollständig"
    return f"{head}: " + " · ".join(bits)


def tmdb_language(tmdb_id: str, token: str, timeout: int = 12):
    """Original language of a movie (needed for the OV rule). Raises KinoFehler."""
    if requests is None:
        raise KinoFehler("the 'requests' package is required")
    try:
        r = requests.get(f"https://api.themoviedb.org/3/movie/{tmdb_id}",
                         headers={"Authorization": f"Bearer {token}", "accept": "application/json"},
                         timeout=timeout)
    except Exception as exc:
        raise KinoFehler(f"TMDB {tmdb_id}: {type(exc).__name__}") from exc
    if r.status_code != 200:
        raise KinoFehler(f"TMDB {tmdb_id}: HTTP {r.status_code}")
    try:
        return r.json().get("original_language")
    except ValueError as exc:
        raise KinoFehler(f"TMDB {tmdb_id}: no JSON") from exc
