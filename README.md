# Metacritic Score Feed

Ein persönlicher RSS-Feed für **Metacritic-Filme und -Serien, die einen Metascore ≥ Schwellwert erreichen** (Default **61** = Metacritics „generally favorable"-Grenze, grünes Band 61–100). Läuft **kostenlos** per GitHub Actions (Actions-Minuten sind für öffentliche Repos gratis), erzeugt eine `feed.xml`, die du in **Readwise Reader**, **Tapestry** oder jedem anderen RSS-Reader abonnierst.

---

## Warum das nötig ist (und warum der alte Reddit-Ansatz nicht mehr geht)

Der klassische r/rss-Trick war: Metacritics **eigene** offizielle RSS-Feeds nehmen und nur die Einträge durchlassen, die schon einen Score haben (siehe [claytono/metacritic-rss](https://github.com/claytono/metacritic-rss)). Das Problem: **Metacritic hat nach dem Fandom-Relaunch (~2023) alle eigenen RSS-Feeds abgeschaltet.** Die Input-Quelle existiert nicht mehr.

Dieses Projekt holt die Daten deshalb selbst von den öffentlichen „Newest Releases"-Browse-Seiten und wendet dieselbe Filter-Logik an.

## Der Kern-Trick: „Score kommt erst später"

Genau dein Bedenken — viele Titel werden zuerst **ohne** Score gelistet und bekommen erst später einen, wenn genug Kritiken da sind. Die Lösung ist ein **Event-Log-Modell**:

- Das Skript schaut regelmäßig auf die neuesten Releases (dort erscheinen bewertete **und** noch unbewertete Titel).
- Ein Titel wandert **genau in dem Moment** in den Feed, in dem er **zum ersten Mal** den Schwellwert überschreitet.
- Der Zustand steht in `state.json`, indexiert über die Titel-URL (= die RSS-`<guid>`). Dadurch erscheint jeder Titel **exakt einmal** — beim ersten Qualifizieren.
- Der `pubDate` eines Eintrags ist der Zeitpunkt, an dem er qualifiziert hat (nicht das Kinostart-Datum). So taucht ein Film auch dann frisch oben im Reader auf, wenn sein Release-Datum schon etwas zurückliegt und der Score erst jetzt kam.

Du überwachst also den **Übergang** „bewertet und gut genug", nicht das ursprüngliche Einstellen. Der Reader dedupliziert über die `<guid>`, es gibt nie Doppelungen.

---

## Schnelltest (lokal)

```bash
pip install -r requirements.txt

# Nur anschauen, nichts schreiben — zeigt jede geparste Karte:
python metacritic_feed.py --dry-run --debug --media movie --pages 1

# Echten Lauf machen (schreibt feed.xml + state.json):
python metacritic_feed.py --threshold 61 --media movie,tv --pages 3
```

Beim ersten echten Lauf werden **alle** aktuell sichtbaren, qualifizierenden Titel in den Feed geschrieben (der „Startbestand"). Danach kommen nur noch neu qualifizierende dazu.

---

## Braucht es ein neues Repo?

**Nein.** Der ganze Ordner `metacritic-rss/` ist in sich abgeschlossen und kann als Unterordner in ein **bestehendes** GitHub-Repo. Zwei Dinge müssen aber erfüllt sein, damit ein *lebendiger* Feed entsteht:

1. **Ein Scheduler** muss das Skript regelmäßig ausführen → dafür ist `.github/workflows/feed.yml` (GitHub Actions) zuständig. Ein reiner „Skill", den du in einer Session aufrufst, reicht **nicht** — der läuft nur on-demand, nicht automatisch im Hintergrund.
2. **Die `feed.xml` muss öffentlich erreichbar sein**, damit Readwise/Tapestry sie abrufen können.
   - Ist dein Repo **öffentlich** → passt, raw- oder Pages-URL funktioniert direkt.
   - Ist dein Repo **privat** → die Raw-URL braucht ein Token (für Reader unbrauchbar), und GitHub Pages auf privaten Repos gibt es nur in bezahlten Plänen. Lösung siehe unten.

### Privates Repo? Kein Problem — drei Wege

Dein Code hier enthält **keine Geheimnisse** (nur ein öffentlicher Scraper), es spricht also nichts dagegen, ihn öffentlich zu machen. Trotzdem muss die `feed.xml` an einen öffentlich erreichbaren Ort:

1. **Eigenes kleines öffentliches Repo** (empfohlen, am einfachsten). Leg *nur für diesen Feed* ein neues **öffentliches** Repo an, wirf diese Dateien rein, Actions + Pages/raw an. Dein privates Memory-/Skills-Repo bleibt unberührt. Das ist der kürzeste Weg zu einer testbaren URL.
2. **Code privat lassen, nur die `feed.xml` in einen öffentlichen Gist schreiben.** Der Workflow läuft im privaten Repo, `state.json` wird dort committet, und nur die `feed.xml` wird in einen öffentlichen Gist gepusht. Feed-URL = `https://gist.githubusercontent.com/DEIN_USER/GIST_ID/raw/feed.xml` (zeigt immer die neueste Version). Nötig: ein Token mit `gist`-Scope als Repo-Secret `GIST_TOKEN`. Publish-Step statt des Commit-Steps:

   ```yaml
   - name: Publish feed.xml to public gist
     env:
       GIST_TOKEN: ${{ secrets.GIST_TOKEN }}
       GIST_ID: "DEINE_GIST_ID"
     run: |
       python - <<'PY'
       import json, os, urllib.request
       body = json.dumps({"files": {"feed.xml": {"content": open("feed.xml").read()}}}).encode()
       req = urllib.request.Request(
           f"https://api.github.com/gists/{os.environ['GIST_ID']}",
           data=body, method="PATCH",
           headers={"Authorization": f"token {os.environ['GIST_TOKEN']}",
                    "Accept": "application/vnd.github+json"})
       urllib.request.urlopen(req).read()
       print("gist updated")
       PY
       # state.json weiterhin ins (private) Repo committen:
       git config user.name "github-actions[bot]"
       git config user.email "github-actions[bot]@users.noreply.github.com"
       git add state.json && git commit -m "Update state [skip ci]" || echo "no changes"
       git push
   ```
3. **GitHub Pro/Team** → dann kannst du Pages direkt auf dem privaten Repo aktivieren.

## Deployment per GitHub Actions

1. Diese Dateien in dein Repo legen (neu **oder** als Unterordner in ein bestehendes).
2. Der Workflow `.github/workflows/feed.yml` läuft automatisch **alle 30 Minuten** (einstellbar, siehe unten) und manuell über „Run workflow" im Actions-Tab. Er baut `feed.xml` und committet sie mit `state.json` zurück.
3. **Schwellwert & Co. einstellen:** im Workflow unter `env:` (`MC_THRESHOLD`, `MC_MEDIA`, `MC_PAGES`).

### Feed öffentlich hosten — zwei Wege

**A) GitHub Pages (sauberer Content-Type)**
Repo → *Settings* → *Pages* → *Build and deployment* → *Deploy from a branch* → Branch `main`, Ordner `/ (root)` → *Save*.
Dein Feed ist dann erreichbar unter:
```
https://DEIN_USER.github.io/metacritic-rss/feed.xml
```
Trag diese URL zusätzlich als `MC_FEED_SELF` im Workflow ein (optional, aber sauber).

**B) Ohne Pages — direkt die Raw-URL abonnieren (Null Konfiguration)**
```
https://raw.githubusercontent.com/DEIN_USER/metacritic-rss/main/feed.xml
```
Sowohl Readwise Reader als auch Tapestry akzeptieren diese URL problemlos.

---

## Im Reader abonnieren

- **Readwise Reader:** *Add feed / Manage feeds* → Feed-URL einfügen. (Reader hat einen eigenen „Feed"-Bereich; dort landen die Einträge.)
- **Tapestry:** *Add Timeline* / Connector **RSS** → Feed-URL einfügen.

Beide fragen dieselbe `feed.xml` einfach periodisch ab.

---

## Konfiguration

Alles per CLI-Flag **oder** Umgebungsvariable (Flag schlägt ENV):

| ENV | Flag | Default | Bedeutung |
|-----|------|---------|-----------|
| `MC_THRESHOLD` | `--threshold` | `61` | Minimaler Metascore (61 = „generally favorable") |
| `MC_MEDIA` | `--media` | `movie,tv` | `movie`, `tv` oder beides |
| `MC_PAGES` | `--pages` | `3` | Browse-Seiten pro Medium (~24 Titel/Seite) |
| `MC_FEED_MAX` | `--feed-max` | `100` | Max. Einträge im Feed |
| `MC_OUT` | `--out` | `feed.xml` | Ausgabedatei |
| `MC_STATE` | `--state` | `state.json` | Zustandsdatei |
| `MC_FEED_TITLE` | `--feed-title` | auto | Feed-Titel |
| `MC_FEED_SELF` | `--feed-self` | – | Öffentliche Feed-URL (atom:self-Link) |
| `MC_DETAIL` | `--detail`/`--no-detail` | `1` | Detailseite pro neuem Titel holen (Critic/User-Stats + Top-Zitat) |
| `MC_DETAIL_MAX` | `--detail-max` | `60` | Max. Detail-Abrufe pro Lauf (begrenzt das einmalige Nachladen) |
| `MC_DETAIL_DELAY` | `--detail-delay` | `0.6` | Sekunden Pause zwischen Detail-Abrufen (Höflichkeit) |
| – | `--dry-run` | – | Nichts schreiben, nur berichten |
| – | `--debug` | – | Jede geparste Karte ausgeben |

Die `<description>` enthält damit einen kompakten Review-Abriss statt nur des Scores, z. B.:
`Critics 76 · 4 reviews · 100% positive · Users tbd (4 ratings) · „A thoroughly original and quite wonderful take…" — Los Angeles Times`. Pro Titel wird die Detailseite **einmal** geholt und im State eingefroren (kein Extra-Traffic bei Folgeläufen); scheitert das Parsen, fällt der Eintrag sauber auf die Score-Zeile zurück.

**Schwellwert später ändern:** einfach `MC_THRESHOLD` anpassen. Absenken lässt beim nächsten Lauf neue Titel rein; Anheben blendet künftige unter dem Wert aus (bereits im Feed stehende bleiben, bis sie aus den letzten `MC_FEED_MAX` herausrutschen).

**`MC_PAGES` bewusst wählen:** Titel sind nach Release-Datum sortiert. Ein Film, der Wochen nach Release erst seinen Score bekommt, steht entsprechend tiefer in der Liste. Mehr Seiten = mehr solcher „Nachzügler" werden erwischt, aber auch mehr Requests. 3 Seiten sind ein guter Startwert bei 8-Stunden-Takt.

---

## Wie „live" ist der Feed?

RSS ist **pull-basiert** — ein Titel kann nur auftauchen, wenn (a) das Skript läuft und ihn sieht, und (b) dein Reader den Feed danach abruft. Echte „In-dem-Moment"-Zustellung gibt es nicht (Metacritic bietet kein Push/Webhook). Was du steuerst, ist der **Takt**:

- Kürzerer Cron (z. B. `*/30` oder `*/15`) → Titel erscheinen schneller **und weniger geclustert**, weil jeder Eintrag seinen eigenen `emitted_at`-Zeitstempel bekommt (der wird zum `pubDate`).
- Längerer Cron (z. B. alle 8 h) → sparsamer, aber alle Titel eines Fensters kommen als Block.

Realistische Frische = `max(Cron-Intervall + GitHub-Verzögerung, Reader-Abrufintervall)`. GitHub Actions ist „best effort" (kann 5–15 min nachhängen), und Reader wie Readwise/Tapestry pollen ohnehin nur alle paar Minuten bis Stunden. Deshalb ist **alle 30 min** der Sweet Spot: fühlt sich nahezu live an, bleibt fair gegenüber Metacritic. `*/5` ginge technisch, bringt aber wenig, wenn dein Reader eh nur stündlich schaut — und erzeugt viel Traffic.

Praktischer Nebeneffekt: Der `pubDate` jedes Eintrags ist das **echte Release-Datum** (nicht der Qualifizierungs-Moment) — die Einträge verteilen sich also über ihre Kalenderdaten und tauchen nicht als „alle jetzt"-Klumpen auf. Neu hinzukommende Titel markiert dein Reader trotzdem als ungelesen (per `<guid>`), auch wenn ihr Datum etwas zurückliegt.

## Grenzen & Troubleshooting

- **Scraping ist kein offizielles API.** Ändert Metacritic das Seiten-Layout, kann das Parsen brechen. Dann zuerst `python metacritic_feed.py --dry-run --debug` laufen lassen — es zeigt, was gefunden wird. Die Parse-Logik sitzt gekapselt in `parse_browse()` / `_clean_title()` in `metacritic_feed.py` und ist bewusst robust gebaut (jede Produktkarte ist ein `<a>` mit Release-Datum; der Score wird über das Label „Metascore" erkannt).
- **Cloudflare/Blockade:** Falls Requests mal mit 403 abgewiesen werden, hilft meist `pip install cloudscraper` und `requests` durch `cloudscraper.create_scraper()` ersetzen — oder auf die inoffizielle JSON-API (`internal-prod.apigee.fandom.net`) umstellen. Aktuell liefern die Browse-Seiten aber sauberes server-seitiges HTML.
- **Rate-Limiting / Fairness:** 8-Stunden-Takt × wenige Seiten ist harmlos. Bitte nicht auf Minutentakt drehen.
- **Terms of Use:** Nur für den persönlichen Gebrauch gedacht. Metacritic/Fandom-ToS beachten.

---

## Alternativen (falls du keinen eigenen Code betreiben willst)

- **RSSHub** hat eine Metacritic-Route (`/metacritic/release/...`), aber ohne Score-Filter und aktuell teils auf veraltete URLs gemünzt.
- **RSS.app** generiert Metacritic-Feeds hosted, aber Score-Filter steckt hinter dem Bezahlplan.

Der Vorteil dieses Projekts: exakt deine Logik (Score-Schwellwert + „einmal beim Qualifizieren"), reader-agnostisch, ohne Abo.

---

## Tests

```bash
pip install -r requirements.txt
python tests/test_parse.py      # oder: python -m pytest -q
```

Die Tests decken Parsing, den Schwellwert, das „einmal emittieren", den **„Score kommt später"-Fall** und wohlgeformtes RSS ab — alles offline, ohne Netzwerk.
