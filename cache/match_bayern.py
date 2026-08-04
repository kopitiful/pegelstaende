"""Match PEGELONLINE stations in Bavaria against GKD Bayern stations by name."""
import json
import re
import subprocess

PEGEL_URL = "https://www.pegelonline.wsv.de/webservices/rest-api/v2/stations.json?prettyprint=false"
GKD_TABELLEN_URL = "https://www.gkd.bayern.de/de/fluesse/wasserstand/tabellen"

# Rough Bavaria bounding box
BY_LAT_MIN, BY_LAT_MAX = 47.2, 50.6
BY_LON_MIN, BY_LON_MAX = 8.9, 13.9


def normalize(name: str) -> str:
    name = name.lower()
    name = name.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    name = re.sub(r"[^a-z0-9]", "", name)
    return name


def fetch(url):
    result = subprocess.run(
        ["curl", "-s", "--max-time", "30", "-A", "Mozilla/5.0", url],
        capture_output=True, check=True,
    )
    return result.stdout


def main():
    print("Fetching PEGELONLINE stations...")
    pegel = json.loads(fetch(PEGEL_URL))
    print(f"  {len(pegel)} total stations")

    bavaria_candidates = [
        s for s in pegel
        if s.get("latitude") and s.get("longitude")
        and BY_LAT_MIN <= s["latitude"] <= BY_LAT_MAX
        and BY_LON_MIN <= s["longitude"] <= BY_LON_MAX
    ]
    print(f"  {len(bavaria_candidates)} candidates in Bavaria bbox")

    print("Fetching GKD Bayern table...")
    html = fetch(GKD_TABELLEN_URL).decode("utf-8")

    # rows: <td data-text="NAME"><ul...><li><a href="...URL...">NAME</a>...
    # followed by <td data-text="GEWAESSERID">GEWAESSER</td>
    row_re = re.compile(
        r'href="(https://www\.gkd\.bayern\.de/de/fluesse/wasserstand/([a-z_]+)/([a-z0-9-]+)/messwerte)[^"]*">([^<]+)</a>.*?'
        r'<td\s+class="left"\s+data-text="[^"]*">([^<]+)</td>',
        re.S
    )
    gkd_stations = []
    for m in row_re.finditer(html):
        url, gebiet, slug, name, gewaesser = m.groups()
        station_id_match = re.search(r"-(\d+)$", slug)
        if not station_id_match:
            continue
        station_id = station_id_match.group(1)
        gkd_stations.append({
            "name": name.strip(),
            "gewaesser": gewaesser.strip(),
            "gebiet": gebiet,
            "station_id": station_id,
            "slug": slug,
        })
    print(f"  {len(gkd_stations)} GKD stations parsed")

    # Build lookup by normalized name
    gkd_by_name = {}
    for g in gkd_stations:
        gkd_by_name.setdefault(normalize(g["name"]), []).append(g)

    matches = []
    for p in bavaria_candidates:
        pname = normalize(p["shortname"])
        candidates = gkd_by_name.get(pname, [])
        if not candidates:
            continue
        # prefer one whose gewaesser matches PEGELONLINE water name
        pwater = normalize(p["water"]["longname"])
        best = None
        for c in candidates:
            if normalize(c["gewaesser"]) == pwater:
                best = c
                break
        if not best:
            best = candidates[0]
        matches.append({
            "pegel_uuid": p["uuid"],
            "pegel_name": p["shortname"],
            "pegel_water": p["water"]["longname"],
            "gkd_name": best["name"],
            "gkd_gewaesser": best["gewaesser"],
            "gkd_gebiet": best["gebiet"],
            "gkd_station_id": best["station_id"],
            "gkd_slug": best["slug"],
        })

    print(f"  {len(matches)} matches found")
    with open("cache/gkd_stations.json", "w") as f:
        json.dump(gkd_stations, f, ensure_ascii=False, indent=2)
    with open("cache/matches.json", "w") as f:
        json.dump(matches, f, ensure_ascii=False, indent=2)

    for m in matches[:30]:
        print(f"  {m['pegel_name']:20s} ({m['pegel_water']:15s}) -> GKD {m['gkd_station_id']} {m['gkd_gebiet']}")


if __name__ == "__main__":
    main()
