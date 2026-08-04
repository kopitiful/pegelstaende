# Pegelstände Deutschland

Live-Dashboard für Wasserstände deutscher Flüsse: [kopitiful.github.io/pegelstaende](https://kopitiful.github.io/pegelstaende)

## Datenquellen & Scope

- **PEGELONLINE (WSV)**: alle 787 Messstellen an deutschen Bundeswasserstraßen, live ~31 Tage 15-Minuten-Rohdaten (bundesweit → "1 Woche" / "1 Monat").
- **GKD Bayern** (Bayerisches Landesamt für Umwelt, CC BY 4.0): echte Langzeit-Historie (bis zu 125 Jahre Tagesmittelwerte) für 15 bayerische Pegel an Donau und Main, die sich per Name mit PEGELONLINE-Stationen matchen ließen → "1 Jahr" bis "50 Jahre".

PEGELONLINE selbst hält keine Jahreshistorie vor (nur rollierende ~30 Tage). Andere Bundesländer (Hessen, Baden-Württemberg, Niedersachsen, NRW) wurden geprüft, boten aber keinen offenen Self-Service-Zugang zu historischen Daten — daher ist Langzeit-Historie aktuell auf Bayern (Donau/Main) beschränkt.

## Architektur

Statisches Precompute-Pattern (kein Server):

```
precompute.py              → docs/data/*.json (lokal + GitHub Actions)
.github/workflows/update.yml → täglich 05:00 UTC
docs/index.html             → Frontend (Vanilla JS, Chart.js via CDN)
docs/data/
  stations.json              → 787 Stationen inkl. history-Flag
  meta.json                  → Update-Zeitstempel
  live/{uuid}.json            → stündlich aggregiert, letzte ~31 Tage
  history/{gkd_id}.json       → Tagesmittel/-max/-min, volle GKD-Historie
```

## Bayern-Matching neu erzeugen

Falls GKD Bayern weitere Stationen freischaltet:

```
python3 cache/match_bayern.py        # Name-Matching PEGELONLINE ↔ GKD
python3 cache/check_downloadable.py  # prüft welche GKD-Stationen Download erlauben
```

Ergebnis landet in `cache/matches_downloadable.json`, das `precompute.py` einliest.
