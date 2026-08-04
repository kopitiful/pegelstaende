import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor

matches = json.load(open("cache/matches.json"))


def fetch(url):
    r = subprocess.run(
        ["curl", "-s", "--max-time", "15", "-A", "Mozilla/5.0", url],
        capture_output=True,
    )
    return r.stdout.decode("utf-8", errors="ignore")


def check(m):
    url = f"https://www.gkd.bayern.de/de/fluesse/wasserstand/{m['gkd_gebiet']}/{m['gkd_slug']}/download"
    html = fetch(url)
    downloadable = 'id="wizard"' in html and "kein Daten-Download möglich" not in html
    date_range = None
    match = re.search(r"Datenbestand vom ([\d.]+) bis zum\s*([\d.]+)", html)
    if match:
        date_range = (match.group(1), match.group(2))
    return {**m, "downloadable": downloadable, "date_range": date_range}


with ThreadPoolExecutor(max_workers=8) as ex:
    results = list(ex.map(check, matches))

ok = [r for r in results if r["downloadable"]]
print(f"{len(ok)}/{len(results)} downloadable")
for r in results:
    flag = "OK " if r["downloadable"] else "no "
    print(f"{flag} {r['pegel_name']:25s} {r['gkd_station_id']:10s} {r['date_range']}")

json.dump(ok, open("cache/matches_downloadable.json", "w"), ensure_ascii=False, indent=2)
