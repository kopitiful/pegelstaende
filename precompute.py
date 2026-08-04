"""Precompute static JSON data for the Pegelstände dashboard.

Sources:
- PEGELONLINE (WSV) live REST API: all German federal-waterway stations,
  ~31 days of 15-minute water level readings. Resampled to hourly for the
  "1 Woche" / "1 Monat" views.
- PEGELONLINE legacy "historische Zeitreihen" download (undocumented, found
  via the help page at /gast/hilfe#hilfe_dwl_langfr_w_q): full 15-minute
  history since 2000-01-01 for every station. Aggregated to daily
  mean/max/min for the "1 Jahr" / "5 Jahre" / "10 Jahre" / "50 Jahre" views.
- GKD Bayern: for a subset of Bavarian Donau/Main stations that predate
  PEGELONLINE's own 2000 cutoff (some back to 1901), their older daily
  data is prepended to extend history further back (see cache/match_bayern.py
  + cache/check_downloadable.py -> cache/matches_downloadable.json).

Writes everything under docs/data/.
"""
import csv
import io
import json
import re
import statistics
import subprocess
import sys
import tempfile
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

PEGEL_API_BASE = "https://www.pegelonline.wsv.de/webservices/rest-api/v2"
PEGEL_SITE_BASE = "https://www.pegelonline.wsv.de"
GKD_ENQUEUE_URL = "https://www.gkd.bayern.de/de/downloadcenter/enqueue_download"

MATCHES_FILE = ROOT / "cache" / "matches_downloadable.json"

HISTORY_START = "2000-01-01T01:00:00+01"


def curl(url, method="GET", data=None, headers=None, cookie_jar=None, max_time=30, retries=2, follow=True):
    cmd = ["curl", "-s", "--max-time", str(max_time), "-A", "Mozilla/5.0"]
    if follow:
        cmd.append("-L")
    if cookie_jar:
        cmd += ["-b", cookie_jar, "-c", cookie_jar]
    for h in (headers or []):
        cmd += ["-H", h]
    if method == "POST":
        cmd += ["-X", "POST", "-H", "Content-Type: application/x-www-form-urlencoded"]
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


def curl_headers(url, method="GET", data=None, cookie_jar=None, max_time=30):
    """POST/GET without following redirects; return response headers as text."""
    cmd = ["curl", "-s", "-D", "-", "-o", "/dev/null", "--max-time", str(max_time), "-A", "Mozilla/5.0"]
    if cookie_jar:
        cmd += ["-b", cookie_jar, "-c", cookie_jar]
    if method == "POST":
        cmd += ["-X", "POST", "-H", "Content-Type: application/x-www-form-urlencoded"]
        for k, v in (data or {}).items():
            cmd += ["--data-urlencode", f"{k}={v}"]
    cmd.append(url)
    result = subprocess.run(cmd, capture_output=True)
    return result.stdout.decode("utf-8", errors="ignore")


def curl_json(url, **kw):
    return json.loads(curl(url, **kw))


class DailyAccumulator:
    """Streaming per-date sum/count/max/min, O(distinct dates) memory."""

    def __init__(self):
        self.sums = defaultdict(float)
        self.counts = defaultdict(int)
        self.maxes = {}
        self.mins = {}

    def add(self, date_key, value):
        self.sums[date_key] += value
        self.counts[date_key] += 1
        if date_key not in self.maxes or value > self.maxes[date_key]:
            self.maxes[date_key] = value
        if date_key not in self.mins or value < self.mins[date_key]:
            self.mins[date_key] = value

    def result(self):
        dates = sorted(self.sums)
        return {
            "d": dates,
            "mean": [round(self.sums[d] / self.counts[d], 1) for d in dates],
            "max": [self.maxes[d] for d in dates],
            "min": [self.mins[d] for d in dates],
        }


# ---------------------------------------------------------------------------
# PEGELONLINE: station list + live measurements (last ~31 days, hourly)
# ---------------------------------------------------------------------------

def fetch_pegel_stations():
    print("Fetching PEGELONLINE station list...", file=sys.stderr)
    stations = curl_json(f"{PEGEL_API_BASE}/stations.json?prettyprint=false")
    print(f"  {len(stations)} stations", file=sys.stderr)
    return stations


def hourly_resample(measurements):
    buckets = defaultdict(list)
    for m in measurements:
        hour_key = m["timestamp"][:13]
        buckets[hour_key].append(m["value"])
    hours = sorted(buckets)
    values = [round(statistics.fmean(buckets[h]), 1) for h in hours]
    timestamps = [f"{h}:00" for h in hours]
    return timestamps, values


def fetch_live_for_station(uuid):
    try:
        raw = curl(f"{PEGEL_API_BASE}/stations/{uuid}/W/measurements.json?start=P31D", max_time=30, retries=2)
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
# PEGELONLINE: long-term historical archive since 2000-01-01
# (undocumented legacy download, see /gast/hilfe#hilfe_dwl_langfr_w_q)
# ---------------------------------------------------------------------------

CSV_ROW_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) \d{2}:\d{2};(-?[\d,]+)$")


class NoLongTermArchive(Exception):
    """Station isn't part of PEGELONLINE's legacy long-term archive."""


def fetch_pegelonline_history(uuid, number, retries=2):
    """Full 2000-today series for one station, streamed and aggregated to
    daily mean/max/min without ever holding the full raw series in memory
    (some tidal stations report every minute -> hundreds of MB of raw data)."""
    last_err = None
    for attempt in range(retries + 1):
        try:
            return _fetch_pegelonline_history_once(uuid, number)
        except NoLongTermArchive:
            return None
        except Exception as e:
            last_err = e
            time.sleep(3 + attempt * 5)
    raise last_err


def _fetch_pegelonline_history_once(uuid, number):
    with tempfile.NamedTemporaryFile(suffix=".jar") as jarf, \
         tempfile.NamedTemporaryFile(suffix=".zip") as zipf:
        jar = jarf.name
        zip_path = zipf.name

        stammdaten_url = f"{PEGEL_SITE_BASE}/gast/stammdaten?pegelnr={number}"
        curl(stammdaten_url, cookie_jar=jar, max_time=20)

        now_full = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
        now = now_full[:-2] if now_full.endswith("00") else now_full  # "+0200" -> "+02"
        headers_text = curl_headers(
            f"{PEGEL_SITE_BASE}/gast/historische-zeitreihen/prepare-download",
            method="POST",
            data={
                "uuid": uuid,
                "parameter": "WASSERSTAND ROHDATEN",
                "start": HISTORY_START,
                "end": now,
                "format": "csv",
            },
            cookie_jar=jar,
            max_time=30,
        )
        m = re.search(r"[Ll]ocation:\s*(\S+)", headers_text)
        if not m:
            raise RuntimeError(f"no redirect location in response: {headers_text[:200]}")
        download_path = m.group(1).strip()
        if "errorpages" in download_path:
            raise NoLongTermArchive("station has no long-term archive in the legacy system")
        download_url = download_path if download_path.startswith("http") else PEGEL_SITE_BASE + download_path

        subprocess.run(
            ["curl", "-s", "-L", "--max-time", "180", "-A", "Mozilla/5.0",
             "-b", jar, "-c", jar, "-o", zip_path, download_url],
            check=True,
        )

        with open(zip_path, "rb") as f:
            magic = f.read(2)
        if magic != b"PK":
            size = Path(zip_path).stat().st_size
            raise RuntimeError(f"download did not return a zip ({size} bytes)")

        acc = DailyAccumulator()
        with zipfile.ZipFile(zip_path) as zf:
            csv_names = [n for n in zf.namelist() if n.endswith(".csv")]
            if not csv_names:
                raise RuntimeError("no csv file in zip")
            with zf.open(csv_names[0]) as raw, \
                 io.TextIOWrapper(raw, encoding="utf-8", errors="ignore") as text:
                next(text, None)  # header: timestamp;value
                for line in text:
                    m = CSV_ROW_RE.match(line.rstrip("\n"))
                    if not m:
                        continue
                    date_key, value_str = m.groups()
                    try:
                        acc.add(date_key, float(value_str.replace(",", ".")))
                    except ValueError:
                        continue

    result = acc.result()
    return result if result["d"] else None


def fetch_all_pegelonline_history(stations, workers=4):
    print(f"Fetching PEGELONLINE long-term history for {len(stations)} stations...", file=sys.stderr)
    results = {}
    ok, failed = 0, 0

    def job(s):
        return s["uuid"], fetch_pegelonline_history(s["uuid"], s["number"])

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(job, s): s for s in stations}
        for i, fut in enumerate(as_completed(futures), 1):
            s = futures[fut]
            try:
                uuid, series = fut.result()
            except Exception as e:
                print(f"  history failed for {s['shortname']}: {e}", file=sys.stderr)
                failed += 1
                continue
            if series and series["d"]:
                results[uuid] = series
                ok += 1
            else:
                failed += 1
            if i % 50 == 0:
                print(f"  {i}/{len(stations)} (ok={ok}, failed={failed})", file=sys.stderr)
    print(f"  PEGELONLINE history: {ok} ok, {failed} failed/empty", file=sys.stderr)
    return results


# ---------------------------------------------------------------------------
# GKD Bayern: extends history further back than 2000 for matched stations
# ---------------------------------------------------------------------------

def gkd_download_history(station_id):
    resp = curl(
        GKD_ENQUEUE_URL,
        method="POST",
        headers=["X-Requested-With: XMLHttpRequest"],
        data={
            "zr": "gesamt", "beginn": "", "email": "", "ende": "",
            "geprueft": "0", "wertart": "tmw",
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

    return curl(f"{deeplink}&dl=1", max_time=60)


def parse_gkd_zip(zip_bytes):
    daily = {}
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
            header_idx = next((i for i, line in enumerate(lines) if line.startswith("Datum;")), None)
            if header_idx is None:
                continue
            for row in csv.reader(lines[header_idx + 1:], delimiter=";"):
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
                    mx, mn = float(row[2].replace(",", ".")), float(row[3].replace(",", "."))
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


def fetch_gkd_extensions(matches):
    """Return {pegel_uuid: gkd_daily_series} for the matched Bavarian stations."""
    print(f"Fetching GKD Bayern extension history for {len(matches)} stations...", file=sys.stderr)
    results = {}
    for m in matches:
        sid = m["gkd_station_id"]
        print(f"  {m['pegel_name']} (GKD {sid})...", file=sys.stderr)
        try:
            series = parse_gkd_zip(gkd_download_history(sid))
            if series["d"]:
                results[m["pegel_uuid"]] = series
                print(f"    {len(series['d'])} days, {series['d'][0]} - {series['d'][-1]}", file=sys.stderr)
        except Exception as e:
            print(f"    failed: {e}", file=sys.stderr)
    return results


def merge_extension(pegelonline_series, gkd_series):
    """Prepend GKD days strictly before PEGELONLINE's first date."""
    if not gkd_series or not gkd_series["d"]:
        return pegelonline_series
    cutoff = pegelonline_series["d"][0] if pegelonline_series and pegelonline_series["d"] else "9999-99-99"
    keep = [i for i, d in enumerate(gkd_series["d"]) if d < cutoff]
    if not keep:
        return pegelonline_series
    prefix = {
        "d": [gkd_series["d"][i] for i in keep],
        "mean": [gkd_series["mean"][i] for i in keep],
        "max": [gkd_series["max"][i] for i in keep],
        "min": [gkd_series["min"][i] for i in keep],
    }
    if not pegelonline_series or not pegelonline_series["d"]:
        return prefix
    return {
        "d": prefix["d"] + pegelonline_series["d"],
        "mean": prefix["mean"] + pegelonline_series["mean"],
        "max": prefix["max"] + pegelonline_series["max"],
        "min": prefix["min"] + pegelonline_series["min"],
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    stations = fetch_pegel_stations()

    matches = json.loads(MATCHES_FILE.read_text()) if MATCHES_FILE.exists() else []
    gkd_extensions = fetch_gkd_extensions(matches)

    pegelonline_history = fetch_all_pegelonline_history(stations)

    fetch_all_live(stations)

    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    out_stations = []
    n_with_history = 0
    n_extended = 0
    for s in stations:
        uuid = s["uuid"]
        series = merge_extension(pegelonline_history.get(uuid), gkd_extensions.get(uuid))
        hist_meta = None
        if series and series["d"]:
            (HISTORY_DIR / f"{uuid}.json").write_text(json.dumps(series, separators=(",", ":")))
            hist_meta = {"from": series["d"][0], "to": series["d"][-1]}
            n_with_history += 1
            if uuid in gkd_extensions:
                n_extended += 1
        out_stations.append({
            "uuid": uuid,
            "name": s["shortname"],
            "water": s["water"]["longname"],
            "km": s.get("km"),
            "lat": s.get("latitude"),
            "lon": s.get("longitude"),
            "agency": s.get("agency"),
            "history": hist_meta,
        })
    out_stations.sort(key=lambda s: (s["water"], s["name"]))

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "stations.json").write_text(
        json.dumps(out_stations, ensure_ascii=False, separators=(",", ":"))
    )
    (DATA_DIR / "meta.json").write_text(
        json.dumps({"updated": datetime.utcnow().isoformat() + "Z"}, separators=(",", ":"))
    )
    print(
        f"Done. {len(out_stations)} stations, {n_with_history} with long-term history "
        f"({n_extended} extended further back via GKD Bayern).",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
