"""
streaming.py — where a TV title or movie can be watched in Germany, and what it costs.

Source: JustWatch's public GraphQL endpoint (the one its own website uses; no key).
For every TV item the feed emits, one lookup runs in the same build, and the result
is frozen into the state like the detail stats: the item title then reads
"[85] Slow Horses: Season 6 (Apple TV · Abo)" instead of "(TV)". Availability that
changes later is NOT followed (that would rewrite titles the reader already has).

Rent/buy (25.10.2026, Johannes: always say where, with a direct link that opens the app):
the store is picked Apple TV Store -> Amazon -> cheapest other, price = cheapest in that
store, link = JustWatch's `standardWebURL` without partner parameters. Only items looked
up from now on carry the store (`rent`/`buy` in the summary); older summaries render as before.

Supplementary like the detail pages: a failed lookup never stops the feed, the item
keeps "(TV)" for good; several failures in a row set a flag that turns the run red
after the commit.
"""
from __future__ import annotations

import html
import re
import unicodedata
import urllib.parse

try:
    import requests
except ImportError:  # keeps the module importable for tests without requests
    requests = None

ENDPOINT = "https://apis.justwatch.com/graphql"
COUNTRY = "DE"
VERSION = 3

QUERY = """
query($country: Country!, $language: Language!, $filter: TitleFilter) {
  popularTitles(country: $country, first: 6, filter: $filter) {
    edges { node {
      objectType
      content(country: $country, language: $language) {
        title originalTitle originalReleaseYear externalIds { tmdbId }
      }
      offers(country: $country, platform: WEB) {
        monetizationType retailPriceValue currency standardWebURL package { clearName shortName }
      }
      ... on Show {
        seasons {
          content(country: $country, language: $language) { seasonNumber }
          offers(country: $country, platform: WEB) {
            monetizationType retailPriceValue currency standardWebURL package { clearName shortName }
          }
        }
      }
    } }
  }
}
"""


class LookupError_(RuntimeError):
    """The lookup itself failed (network, HTTP, GraphQL error, unexpected shape)."""


# --------------------------------------------------------------------------- #
# Title handling and matching
# --------------------------------------------------------------------------- #
SEASON_RE = re.compile(r"^(.*?)\s*:\s*Season (\d+)\s*$")
YEAR_RE = re.compile(r"\s*\((\d{4})\)\s*$")


def split_title(mc_title: str, release_date: str = "") -> tuple:
    """'The Diplomat (2023): Season 4' -> ('The Diplomat', 2023, 4).
    A show without a season suffix takes its year hint from the release date
    (that is the show's premiere); a season without "(YYYY)" gets no year hint,
    because its date is the season's, not the show's (it only bounds the show's
    start year from above, see lookup)."""
    season = None
    base = mc_title.strip()
    m = SEASON_RE.match(base)
    if m:
        base, season = m.group(1), int(m.group(2))
    year = None
    y = YEAR_RE.search(base)
    if y:
        year = int(y.group(1))
        base = base[: y.start()]
    elif season is None:
        ry = re.search(r"(\d{4})\s*$", release_date or "")
        year = int(ry.group(1)) if ry else None
    return base.strip(), year, season


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def _season_numbers(node: dict) -> list:
    out = []
    for s in node.get("seasons") or []:
        if isinstance(s, dict):
            n = (s.get("content") or {}).get("seasonNumber")
            if isinstance(n, int):
                out.append(n)
    return out


def pick_match(nodes: list, base: str, year=None, latest=None, season=None, kind="SHOW"):
    """The show whose English or original title equals ours, or None.
    `year` (the show's start year): JustWatch's year must agree within one.
    `latest` (a season's air year): the show cannot have started after it.
    `season` N >= 2: between several candidates, only shows with a season >= N-1
    count. Without a year hint, two remaining candidates are ambiguous -> None.
    An unknown provider beats a wrong one."""
    want = _norm(base)
    if not want:
        return None
    hits = []
    for n in nodes or []:
        if not isinstance(n, dict) or n.get("objectType") != kind:
            continue
        c = n.get("content") or {}
        if want not in (_norm(c.get("title")), _norm(c.get("originalTitle"))):
            continue
        jy = c.get("originalReleaseYear")
        if year is not None and (not isinstance(jy, int) or abs(jy - year) > 1):
            continue
        if latest is not None and isinstance(jy, int) and jy > latest:
            continue
        hits.append(n)
    if len(hits) > 1 and year is None and season is not None and season >= 2:
        # tie-break only: JustWatch often files an anthology season as its own
        # one-season show, so this cannot be a hard filter for a single hit
        # a show that already lists season N wins; else one that has N-1
        for need in (season, season - 1):
            best = [n for n in hits if max(_season_numbers(n) or [0]) >= need]
            if best:
                hits = best
                break
        else:
            hits = []
    if not hits:
        return None
    if len(hits) > 1 and year is None:
        return None
    return hits[0]


def offers_for(node: dict, season=None) -> tuple:
    """-> (offers, own). For a show (no season number) the show's offers. For a
    season its own offers. When JustWatch has none for it (typical for a season
    that is new in the US and not yet in Germany): the offers of the EARLIER
    seasons with own=False, so the label can say where those are instead of
    claiming the new one is there. No earlier season listed -> ([], False)."""
    if season is None:
        offers = node.get("offers")
        if offers is None:
            raise LookupError_("show without offers field")
        return offers, True
    listed = [s for s in node.get("seasons") or [] if isinstance(s, dict)]
    if len(listed) == 1 and (listed[0].get("content") or {}).get("seasonNumber") == 1 and season > 1:
        # An anthology season (Monster: The Lizzie Borden Story = Monster S4) that
        # JustWatch files as its own one-season show: its offers ARE this season's.
        return listed[0].get("offers") or [], True
    earlier = []
    for s in node.get("seasons") or []:
        if not isinstance(s, dict):
            continue
        n = (s.get("content") or {}).get("seasonNumber")
        if n == season and s.get("offers"):
            return s["offers"], True
        if isinstance(n, int) and n < season:
            earlier += s.get("offers") or []
    return earlier, False


# --------------------------------------------------------------------------- #
# Offers -> label
# --------------------------------------------------------------------------- #
# JustWatch names -> the names people use. Channel variants fold onto the service.
RENAME = {
    "disney plus": "Disney+",
    "paramount plus": "Paramount+",
    "amazon prime video": "Prime Video",
    "amazon prime video with ads": "Prime Video",
    "netflix standard with ads": "Netflix",
    "netflix basic with ads": "Netflix",
    "apple tv plus": "Apple TV",
    "sky go": "Sky",
    "sky x": "Sky",
    "wow fiction": "WOW",
    "rtl plus": "RTL+",
    "magenta tv": "MagentaTV",
    "magenta tv+": "MagentaTV",
    "magenta tv plus": "MagentaTV",
    "magentatv": "MagentaTV",
    "mgm plus": "MGM+",
    "joyn plus": "Joyn",
    "hbo max": "HBO Max",
    "ard mediathek": "ARD Mediathek",
    "zdf": "ZDF",
}
CHANNEL_RE = re.compile(r"\s+(amazon channel|apple tv channel|channel)$", re.I)
# FREE/ADS from these services is a sample episode or a promo, not a free title
# (seen: Slow Horses "FREE" on Apple TV, a Paramount+ show "FREE" on Prime Video).
NOT_FREE = {"Netflix", "Prime Video", "Disney+", "Apple TV", "HBO Max", "WOW", "Sky", "Paramount+"}
PHYSICAL_RE = re.compile(r"dvd|blu-?ray|thalia|weltbild|media ?markt|saturn", re.I)
# Order in the title: the big services first, then alphabetical.
# HBO Max first (Johannes 25.10.2026: "wo es HBO gibt, wird das priorisiert"). Order is applied
# when a lookup is stored, so items already in the feed keep their frozen text.
PRIORITY = ["HBO Max", "Netflix", "Prime Video", "Disney+", "Apple TV", "WOW", "Sky", "Paramount+",
            "RTL+", "Joyn", "MagentaTV", "ARD Mediathek", "ZDF", "arte"]


def service_name(clear_name: str) -> str:
    n = CHANNEL_RE.sub("", (clear_name or "").strip())
    return RENAME.get(n.lower(), n)


def _order(names: set) -> list:
    return sorted(names, key=lambda n: (PRIORITY.index(n) if n in PRIORITY else len(PRIORITY), n.lower()))


def _join(names: list, cap: int = 2) -> str:
    shown = ", ".join(names[:cap])
    return shown + (f" +{len(names) - cap}" if len(names) > cap else "")


def _euro(value: float) -> str:
    return f"{value:.2f}".replace(".", ",") + " €"


def summarize(offers: list) -> dict:
    """Group offers into gratis / Abo / Leihen / Kaufen. A service with a free tier
    and a paid one (Joyn and Joyn Plus) counts as gratis: free beats paid. FREE
    from the big subscription services is a sample, see NOT_FREE."""
    flat, free = set(), set()
    rent, buy = [], []
    rent_o, buy_o = [], []
    cinema = False
    for o in offers or []:
        if not isinstance(o, dict):
            continue
        if o.get("monetizationType") == "CINEMA":
            cinema = True                 # new movie running in German cinemas
            continue
        pkg = (o.get("package") or {}).get("clearName") or ""
        if not pkg or PHYSICAL_RE.search(pkg):
            continue
        name = service_name(pkg)
        kind = o.get("monetizationType")
        price = o.get("retailPriceValue")
        if kind == "FLATRATE":
            flat.add(name)
        elif kind in ("FREE", "ADS") and name not in NOT_FREE:
            free.add(name)
        elif kind == "RENT" and isinstance(price, (int, float)) and price > 0:
            rent.append((float(price), name))
            rent_o.append((float(price), _shop(o), o.get("standardWebURL")))
        elif kind == "BUY" and isinstance(price, (int, float)) and price > 0:
            buy.append((float(price), name))
            buy_o.append((float(price), _shop(o), o.get("standardWebURL")))
    flat -= free
    out: dict = {}
    if free:
        out["free"] = _order(free)
    if flat:
        out["flat"] = _order(flat)
    if rent:
        p, n = min(rent)
        out["rent_from"], out["rent_at"] = p, n
    if buy:
        p, n = min(buy)
        out["buy_from"], out["buy_at"] = p, n
    if rent_o:
        out["rent"] = pick_shop(rent_o)
    if buy_o:
        out["buy"] = pick_shop(buy_o)
    if cinema:
        out["cinema"] = True
    return out


SHOP_FIRST = ("Apple TV Store", "Amazon")
SHOP_SHORT = {"itu": "Apple TV Store", "amz": "Amazon"}
SHOP_HOSTS = {"Apple TV Store": {"tv.apple.com"},
              "Amazon": {"watch.amazon.de", "www.amazon.de", "app.primevideo.com", "www.primevideo.com"}}
PARTNER = {"at", "ct", "itscg", "itsct", "mt", "tag", "ref", "ref_", "linkcode", "camp", "campaign", "creative",
           "uct", "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term"}


def _shop(o: dict) -> str:
    pkg = o.get("package") or {}
    return SHOP_SHORT.get(pkg.get("shortName") or "") or service_name(pkg.get("clearName") or "")


def clean_url(url, shop: str):
    """https link without partner parameters; Apple/Amazon only on their own hosts. Else None."""
    if not isinstance(url, str) or not url:
        return None
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    if u.scheme != "https" or not u.netloc:
        return None
    if shop in SHOP_HOSTS and u.netloc.lower() not in SHOP_HOSTS[shop]:
        return None
    q = [(k, v) for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True) if k.lower() not in PARTNER]
    return urllib.parse.urlunsplit((u.scheme, u.netloc, u.path, urllib.parse.urlencode(q), ""))


def pick_shop(offers: list) -> dict:
    """[(price, shop, url)] of one kind -> {"shop", "price", "url"}: Apple TV Store, else Amazon,
    else the cheapest store; price = cheapest in that store; url may be None."""
    per = {}
    for price, shop, url in offers:
        per.setdefault(shop, []).append((price, url))
    shop = next((s for s in SHOP_FIRST if s in per), None)
    if shop is None:
        shop = min(per, key=lambda s: (min(p for p, _ in per[s]), s.lower()))
    rows = sorted(per[shop], key=lambda x: x[0])
    url = next((u for u in (clean_url(r[1], shop) for r in rows) if u), None)
    return {"shop": shop, "price": rows[0][0], "url": url}


def label(summary: dict) -> str:
    """The part that replaces "TV" in the item title."""
    parts = []
    if summary.get("free"):
        parts.append(f'{_join(summary["free"])} · gratis')
    if summary.get("flat"):
        parts.append(f'{_join(summary["flat"])} · Abo')
    if not parts:
        if summary.get("rent"):
            parts.append(f'Leihen bei {summary["rent"]["shop"]} ab {_euro(summary["rent"]["price"])}')
        elif summary.get("rent_from") is not None:
            parts.append(f'Leihen ab {_euro(summary["rent_from"])}')
        elif summary.get("buy"):
            parts.append(f'Kaufen bei {summary["buy"]["shop"]} ab {_euro(summary["buy"]["price"])}')
        elif summary.get("buy_from") is not None:
            parts.append(f'Kaufen ab {_euro(summary["buy_from"])}')
    if not parts and summary.get("cinema"):
        return "im Kino"
    if not parts:
        return "kein DE-Angebot"
    text = " | ".join(parts)
    return f"{text}, nur ältere Staffeln" if summary.get("older") else text


def describe(summary: dict, checked: str = "") -> str:
    """Longer line for the item description: every service, cheapest rent/buy with
    its store, and the lookup date (the result is frozen). Service names come from
    outside and are HTML-escaped: the description is rendered as HTML."""
    e = html.escape
    bits = []
    if summary.get("free"):
        bits.append("gratis bei " + ", ".join(e(n) for n in summary["free"]))
    if summary.get("flat"):
        bits.append("im Abo bei " + ", ".join(e(n) for n in summary["flat"]))
    for kind, word in (("rent", "leihen"), ("buy", "kaufen")):
        pick = summary.get(kind)
        if pick:   # since 25.10.2026: the preferred store, linked
            shop = (f'<a href="{e(pick["url"])}">{e(pick["shop"])}</a>' if pick.get("url")
                    else e(pick["shop"]))
            bits.append(f'{word} bei {shop} ab {_euro(pick["price"])}')
        elif summary.get(f"{kind}_from") is not None:
            bits.append(f'{word} ab {_euro(summary[f"{kind}_from"])} ({e(summary.get(f"{kind}_at", ""))})')
    if summary.get("cinema"):
        bits.append("im Kino")
    head = f"Streaming DE (JustWatch, {checked})" if checked else "Streaming DE (JustWatch)"
    if not bits:
        return f"{head}: kein Angebot gefunden"
    if summary.get("older"):
        return f"{head}: diese Staffel noch ohne Angebot; ältere Staffeln " + "; ".join(bits)
    return f"{head}: " + "; ".join(bits)


# --------------------------------------------------------------------------- #
# Lookup
# --------------------------------------------------------------------------- #
def search(base: str, kind: str = "SHOW", timeout: int = 20) -> list:
    if requests is None:
        raise LookupError_("the 'requests' package is required")
    body = {"query": QUERY, "variables": {
        "country": COUNTRY, "language": "en",
        "filter": {"searchQuery": base, "objectTypes": [kind]}}}
    try:
        resp = requests.post(ENDPOINT, json=body, timeout=timeout, headers={
            "User-Agent": "Mozilla/5.0 (metacritic-rss personal feed)",
            "Content-Type": "application/json"})
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # network, HTTP, JSON
        raise LookupError_(str(exc)) from exc
    # Strict: any GraphQL error fails the lookup. Partial data could carry a
    # nulled offers field, and that would be frozen as "kein DE-Angebot".
    if not isinstance(data, dict) or data.get("errors"):
        raise LookupError_(f"GraphQL: {str((data or {}).get('errors'))[:200]}")
    try:
        edges = data["data"]["popularTitles"]["edges"]
        return [e.get("node") for e in edges if isinstance(e, dict)]
    except (KeyError, TypeError, AttributeError) as exc:
        raise LookupError_(f"unexpected response shape: {exc}") from exc


def _tmdb(node: dict) -> str | None:
    """TMDB id of the matched JustWatch node (for the trailer lookup), digits only."""
    t = ((node.get("content") or {}).get("externalIds") or {}).get("tmdbId")
    return str(t) if t is not None and str(t).isdigit() else None


def lookup(mc_title: str, release_date: str = "", media: str = "tv", fetch=search) -> dict:
    """-> stored record. {"v", "match": False} when JustWatch has no unambiguous
    match, else {"v", "match": True, "summary": {...}}. Raises LookupError_ on failure.
    A movie is matched like a show without season: exact title, year ±1."""
    if media == "movie":
        base, year, _ = split_title(mc_title, release_date)
        node = pick_match(fetch(base, "MOVIE"), base, year, kind="MOVIE")
        if node is None:
            return {"v": VERSION, "match": False}
        rec = {"v": VERSION, "match": True, "summary": summarize(offers_for(node, None)[0])}
        if _tmdb(node):
            rec["tmdb"] = _tmdb(node)
        c = node.get("content") or {}
        names = [t for t in (c.get("title"), c.get("originalTitle")) if isinstance(t, str) and t]
        if rec["summary"].get("cinema") and names:   # for the Munich cinema line (kino.py)
            rec["titles"] = list(dict.fromkeys(names))
            if isinstance(c.get("originalReleaseYear"), int):
                rec["year"] = c["originalReleaseYear"]
        return rec
    base, year, season = split_title(mc_title, release_date)
    latest = None
    if season is not None and year is None:
        ry = re.search(r"(\d{4})\s*$", release_date or "")
        latest = int(ry.group(1)) if ry else None
    node = pick_match(fetch(base, "SHOW"), base, year, latest, season)
    if node is None:
        return {"v": VERSION, "match": False}
    offers, own = offers_for(node, season)
    summary = summarize(offers)
    if not own:
        summary["older"] = True
    if not own or season is None:
        # the link would lead to another season, or be ambiguous across seasons: store, no link
        for kind in ("rent", "buy"):
            if summary.get(kind):
                summary[kind]["url"] = None
    rec = {"v": VERSION, "match": True, "summary": summary}
    if _tmdb(node):
        rec["tmdb"] = _tmdb(node)
    return rec
