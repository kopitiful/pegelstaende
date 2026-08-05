# LUBW-Handaufnahmen (Baden-Württemberg)

Stationen, die kein PEGELONLINE-Legacy-Archiv haben (extern eingespeist,
z.B. Betreiber "Regierungspräsidium Freiburg" statt WSV), aber über den
LUBW-Datendienst UDO (udo.lubw.baden-wuerttemberg.de) echte Tagesmittel-
Historie anbieten.

## Warum manuell statt automatisiert

UDO läuft auf Disy Cadenza (Wasserstand-Selector: `hydrologische_landespegel`).
Der CSV-Export ist session-gebunden und lässt sich nicht wie bei GKD Bayern
oder PEGELONLINE per curl nachbauen — nötig wäre ein echter (Headless-)
Browser (z.B. Playwright), was als CI-Abhängigkeit ein größerer Schritt ist.
Bisher nur einzeln per Hand exportiert, siehe `mapping.json`.

## Neue Station hinzufügen

1. https://udo.lubw.baden-wuerttemberg.de/public/p/pegel_messwerte_leer öffnen
2. Station suchen, Komponente "Wasserstand, W-Stand, cm", Produkt "Tagesmittelwert",
   Zeitraum möglichst weit (z.B. `01.01.1850 - <heute>`)
3. Export → CSV
4. Datei nach `{uuid}.json` konvertieren (siehe `cache/lubw_manual` Verarbeitung
   in `precompute.py`) und `mapping.json` ergänzen
5. `python3 precompute.py` (voller Lauf) einbinden lassen
