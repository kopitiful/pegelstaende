"""Precompute static JSON data for the Pegelstände dashboard.

Sources:
- PEGELONLINE (WSV): all German federal-waterway stations, live ~31 days of
  15-minute water level readings. Resampled to hourly for storage.
- GKD Bayern: long-term daily-mean history for a subset of Bavarian stations
  on the Donau and Main that could be matched to PEGELONLINE by name. Only
  stations GKD actually exposes for self-service download are included
  (see cache/matches_downloadable.json, built by cache/match_bayern.py +
  cache/check_downloadable.py).

Writes everything under docs/data/.
"""
import csv
import io
import json
import re
import statistics
import subprocess
import sys
import time
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "docs" / "data"
LIVE_DIR = DATA_DIR / "live"
HISTORY_DIR = DATA_DIR / "history"

PEGEL_BASE = "https://www.pegelonline.wsv.de/webservices/rest-api/v2"
GKD_ENQUEUE_URL = "https://www.gkd.bayern.de/de/downloadcenter/enqueue_download"

MATCHES_FILE = ROOT / "cache" / "matches_downloadable.json"


def curl(url, method="GET", data=None, max_time=30, retries=2):
    cmd = ["curl", "-s", "-L", "--max-time", str(max_time), "-A", "Mozilla/5.0"]
    if method == "POST":
        cmd += [
            "-X", "POST",
            "-H", "Content-Type: application/x-www-form-urlencoded",
            "-H", "X-Requested-With: XMLHttpRequest",
        ]
        for k, v in (data or {}).items():
            cmd += ["--data-urlencode", f"{k}={v}"]
    cmd.append(url)
    last_out = b""
    for attempt in range(retries + 1):
        result = subprocess.run(cmd, capture_output=True)
        last_out = result.stdout
        if result.returncode == 0 and last_out:
            return last_out
        time.sleep(1)
    return last_out


def curl_json(url, **kw):
    return json.loads(curl(url, **kw))


# ---------------------------------------------------------------------------
# PEGELONLINE: station list + live measurements
# ---------------------------------------------------------------------------

def fetch_pegel_stations():
    print("Fetching PEGELONLINE station list...", file=sys.stderr)
    stations = curl_json(f"{PEGEL_BASE}/stations.json?prettyprint=false")
    print(f"  {len(stations)} stations", file=sys.stderr)
    return stations


def hourly_resample(measurements):
    """[{timestamp, value}] (15min) -> ([iso_hour...], [mean_value...])"""
    buckets = defaultdict(list)
    for m in measurements:
        ts = m["timestamp"]
        hour_key = ts[:13]  # YYYY-MM-DDTHH
        buckets[hour_key].append(m["value"])
    hours = sorted(buckets)
    values = [round(statistics.fmean(buckets[h]), 1) for h in hours]
    timestamps = [f"{h}:00" for h in hours]
    return timestamps, values


def fetch_live_for_station(uuid):
    try:
        raw = curl(
            f"{PEGEL_BASE}/stations/{uuid}/W/measurements.json?start=P31D",
            max_time=30,
            retries=2,
        )
        measurements = json.loads(raw)
        if not isinstance(measurements, list) or not measurements:
            return uuid, None
        timestamps, values = hourly_resample(measurements)
        return uuid, {"t": timestamps, "v": values}
    except Exception as e:
        print(f"  live fetch failed for {uuid}: {e}", file=sys.stderr)
        return uuid, None


def fetch_all_live(stations, workers=10):
    print(f"Fetching live measurements for {len(stations)} stations...", file=sys.stderr)
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    ok, failed = 0, 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(fetch_live_for_station, s["uuid"]): s for s in stations}
        for i, fut in enumerate(as_completed(futures), 1):
            uuid, data = fut.result()
            if data:
                (LIVE_DIR / f"{uuid}.json").write_text(json.dumps(data, separators=(",", ":")))
                ok += 1
            else:
                failed += 1
            if i % 100 == 0:
                print(f"  {i}/{len(stations)}", file=sys.stderr)
    print(f"  live data: {ok} ok, {failed} failed/empty", file=sys.stderr)
    return ok


# ---------------------------------------------------------------------------
# GKD Bayern: long-term history for matched stations
# ---------------------------------------------------------------------------

def gkd_download_history(gebiet, slug, station_id):
    """Run the enqueue_download -> poll -> download flow, return raw zip bytes."""
    resp = curl(
        GKD_ENQUEUE_URL,
        method="POST",
        data={
            "zr": "gesamt",
            "beginn": "",
            "email": "",
            "ende": "",
            "geprueft": "0",
            "wertart": "tmw",
            "t": f'{{"{station_id}":["fluesse.wasserstand"]}}',
            "f": "",
        },
        max_time=30,
    )
    payload = json.loads(resp)
    if payload.get("result") != "success":
        raise RuntimeError(f"enqueue failed: {payload}")
    deeplink = payload["deeplink"]

    for _ in range(20):
        time.sleep(3)
        status_html = curl(deeplink, max_time=20).decode("utf-8", errors="ignore")
        if "steht zur Verfügung" in status_html:
            break
    else:
        raise RuntimeError("download did not become ready in time")

    zip_bytes = curl(f"{deeplink}&dl=1", max_time=60)
    return zip_bytes


def parse_gkd_zip(zip_bytes):
    """Parse all CSVs in the zip, merge into sorted daily series."""
    daily = {}  # date -> (mean, max, min)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in zf.namelist():
            if not name.endswith(".csv"):
                continue
            raw = zf.read(name)
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = raw.decode("latin-1")
            lines = text.splitlines()
            header_idx = None
            for i, line in enumerate(lines):
                if line.startswith("Datum;"):
                    header_idx = i
                    break
            if header_idx is None:
                continue
            reader = csv.reader(lines[header_idx + 1:], delimiter=";")
            for row in reader:
                if len(row) < 4:
                    continue
                date_str = row[0].strip()
                if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
                    continue
                try:
                    mean = float(row[1].replace(",", "."))
                except ValueError:
                    continue
                try:
                    mx = float(row[2].replace(",", "."))
                    mn = float(row[3].replace(",", "."))
                except ValueError:
                    mx = mn = mean
                daily[date_str] = (mean, mx, mn)
    dates = sorted(daily)
    return {
        "d": dates,
        "mean": [daily[d][0] for d in dates],
        "max": [daily[d][1] for d in dates],
        "min": [daily[d][2] for d in dates],
    }


def fetch_all_history(matches):
    print(f"Fetching GKD Bayern history for {len(matches)} stations...", file=sys.stderr)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    results = {}
    for m in matches:
        sid = m["gkd_station_id"]
        print(f"  {m['pegel_name']} (GKD {sid})...", file=sys.stderr)
        try:
            zip_bytes = gkd_download_history(m["gkd_gebiet"], m["gkd_slug"], sid)
            series = parse_gkd_zip(zip_bytes)
            if not series["d"]:
                print(f"    no data parsed, skipping", file=sys.stderr)
                continue
            (HISTORY_DIR / f"{sid}.json").write_text(json.dumps(series, separators=(",", ":")))
            results[m["pegel_uuid"]] = {
                "gkd_id": sid,
                "from": series["d"][0],
                "to": series["d"][-1],
            }
            print(f"    {len(series['d'])} days, {series['d'][0]} - {series['d'][-1]}", file=sys.stderr)
        except Exception as e:
            print(f"    failed: {e}", file=sys.stderr)
    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    stations = fetch_pegel_stations()

    matches = []
    if MATCHES_FILE.exists():
        matches = json.loads(MATCHES_FILE.read_text())
    history_index = fetch_all_history(matches)

    fetch_all_live(stations)

    out_stations = []
    for s in stations:
        hist = history_index.get(s["uuid"])
        out_stations.append({
            "uuid": s["uuid"],
            "name": s["shortname"],
            "water": s["water"]["longname"],
            "km": s.get("km"),
            "lat": s.get("latitude"),
            "lon": s.get("longitude"),
            "agency": s.get("agency"),
            "history": hist,
        })
    out_stations.sort(key=lambda s: (s["water"], s["name"]))

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "stations.json").write_text(
        json.dumps(out_stations, ensure_ascii=False, separators=(",", ":"))
    )
    (DATA_DIR / "meta.json").write_text(
        json.dumps({"updated": datetime.utcnow().isoformat() + "Z"}, separators=(",", ":"))
    )
    print(f"Done. {len(out_stations)} stations, {len(history_index)} with long-term history.", file=sys.stderr)


if __name__ == "__main__":
    main()
