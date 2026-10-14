#!/usr/bin/env python3
"""
precompute.py - builds the cached data behind protofox.at/shroomzn

Runs in GitHub Actions (.github/workflows/shroomzn-data.yml):

    python precompute.py OUT_DIR [PREVIOUS_DATA_DIR] [full|now]

"full" (twice a day) writes everything below; "now" (every few hours) only
refreshes the last-24-hours files for Germany and Czechia, whose servers
can't be read from a visitor's browser.

Daily station sums, last ~46 days, one file per country:
  stations.json      Austria   GeoSphere klima-v2-1d "rr"        (CC BY 4.0)
  ch_stations.json   Switzerland MeteoSwiss SwissMetNet "rre150d0" (Source: MeteoSwiss)
  de_stations.json   Germany   DWD daily "more_precip" RS       (Source: Deutscher Wetterdienst)
  cz_stations.json   Czechia   CHMI daily "SRA"                 (Source: CHMI)
All four define a day the same way: 06:00 UTC that day to 06:00 UTC the next
(checked against each other in verify.py). After a full run every file gets
the same "through" date, so a preset covers the same days in every country.

Rain grids (1 km analyses), 7/14/30/45-day sums as Web-Mercator overlays,
clipped to the country, with area means per region; all end on the same day:
  grid*.json/png     Austria   SPARTACUS v3
  ch_grid*.json/png  Switzerland RprelimD (+ RhiresD for older days)
  de_grid*.json/png  Germany   HYRAS-DE-PR
  (Czechia publishes no daily grid.)

Last 24 hours, Germany and Czechia (hourly values):
  de_now.json        DWD 10-minute precipitation "now"
  cz_now.json        CHMI hourly "SRA1H" "now"

  meta.json          what each part produced, and any errors

Every part is independent: if one fails, the others are still written, and
the workflow keeps the previous files for the failed one.
"""

import concurrent.futures as cf
import csv
import datetime as dt
import io
import json
import math
import os
import re
import sys
import tempfile
import threading
import time
import zipfile

import numpy as np

API = "https://dataset.api.hub.geosphere.at/v1"
STATION_DATASET = "klima-v2-1d"
GRID_DATASET = "spartacus-v3-1d-1km"
STATION_DAYS = 46            # 45 full days + today
PERIODS = (7, 14, 30, 45)    # grid overlay presets, days
GRID_DAYS = max(PERIODS) + 3 # fetched per grid, so a common end day up to 3 days earlier still works
OVERLAY_WIDTH = 800          # px across Austria (~0.75 km per px)
USER_AGENT = "protofox.at/shroomzn precompute (GitHub Actions)"
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.environ.get("SHROOMZN_CACHE")    # verify.py reuses big downloads from here

STATION_FILES = {"AT": "stations.json", "CH": "ch_stations.json",
                 "DE": "de_stations.json", "CZ": "cz_stations.json"}

# Klimamonitoring colour classes: upper bound [mm], colour
BINS = [
    (2, "#ffffff"), (15, "#fdf6c3"), (30, "#e2ef9f"), (50, "#a5d383"),
    (75, "#7fc8b4"), (100, "#4fb3c6"), (150, "#3f8fd2"), (200, "#2f5fc4"),
    (250, "#1f3a9e"), (350, "#6f4fb3"), (450, "#9c4fb3"), (600, "#d23b3b"),
    (math.inf, "#e88a4f"),
]


def utc_today():
    return dt.datetime.now(dt.timezone.utc).date()


def window_dates(end=None):
    end = end or utc_today()
    return [(end - dt.timedelta(days=STATION_DAYS - 1 - i)).isoformat() for i in range(STATION_DAYS)]


# ---------------------------------------------------------------- HTTP ----

_last_request = 0.0


def api_get(url, params=None, as_json=True, timeout=180):
    """GeoSphere GET with a polite pace (<4 req/s) and a readable error message."""
    import requests
    global _last_request
    wait = 0.3 - (time.time() - _last_request)
    if wait > 0:
        time.sleep(wait)
    for attempt in range(3):
        _last_request = time.time()
        r = requests.get(url, params=params, timeout=timeout,
                         headers={"User-Agent": USER_AGENT})
        if r.status_code == 429 and attempt < 2:
            time.sleep(65)            # per-second / per-hour limit: back off
            continue
        break
    if r.status_code != 200:
        try:
            j = r.json()
            msg = j.get("detail") or j.get("message") or str(j)[:300]
        except Exception:
            msg = r.text[:300]
        raise ApiError(r.status_code, f"{r.status_code}: {msg}")
    return r.json() if as_json else r.content


class ApiError(RuntimeError):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status = status


_tls = threading.local()


def http_get(url, timeout=120, ok404=False):
    """Plain GET for static files, one session per thread, with retries and
    backoff. Returns None for 403/404 when ok404 is set."""
    import requests
    if not hasattr(_tls, "s"):
        _tls.s = requests.Session()
        _tls.s.headers["User-Agent"] = USER_AGENT
    for attempt in range(4):
        try:
            r = _tls.s.get(url, timeout=timeout)
        except requests.RequestException:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
            continue
        if r.status_code in (403, 404) and ok404:
            return None
        if r.status_code in (429, 500, 502, 503, 504) and attempt < 3:
            time.sleep(2 ** attempt + 1)
            continue
        r.raise_for_status()
        return r
    return None


def dir_listing(url):
    """File names linked from an nginx/Apache directory index."""
    r = http_get(url)
    return [n for n in re.findall(r'href="([^"?#]+)"', r.text) if not n.startswith(("/", "../"))]


def pmap(fn, items, workers=8):
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(fn, items))


# ------------------------------------------------------ station helpers ----

def drop_twins(stations, max_deg=0.01):
    """Same name at (almost) the same spot under two ids: keep the one with
    the most values. Seen e.g. for Hochfilzen, Reutte, Warth."""
    def filled(s):
        return sum(v is not None for v in s["rr"])
    keep = []
    for s in sorted(stations, key=filled, reverse=True):
        if any(k["name"] == s["name"] and abs(k["lat"] - s["lat"]) < max_deg
               and abs(k["lon"] - s["lon"]) < max_deg for k in keep):
            continue
        keep.append(s)
    keep.sort(key=lambda s: (s["state"], s["name"]))
    return keep


def through_date(dates, series, min_share=0.3):
    """Last date on which at least min_share of stations reported."""
    n = max(1, len(series))
    for i in range(len(dates) - 1, -1, -1):
        have = sum(1 for v in series.values() if i < len(v) and v[i] is not None)
        if have / n >= min_share:
            return dates[i]
    return None


def write_json(out_dir, name, doc, compact=True):
    with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
        if compact:
            json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(doc, f, ensure_ascii=False, indent=1)


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def station_doc(source, dates, stations):
    stations = drop_twins([s for s in stations if any(v is not None for v in s["rr"])])
    return {
        "generated": now_iso(),
        "source": source,
        "dates": dates,
        "through": through_date(dates, {s["id"]: s["rr"] for s in stations}),
        "stations": stations,
    }


def load_regions(fname, key):
    """{region key: [rings]} from one of the border files next to this script."""
    with open(os.path.join(HERE, fname), encoding="utf-8") as f:
        gj = json.load(f)
    regions = {}
    for feat in gj["features"]:
        rings = []
        for poly in feat["geometry"]["coordinates"]:
            rings.extend(poly)       # holes are negligible at this scale
        regions[feat["properties"][key]] = rings
    return regions


def region_of(lat, lon, regions):
    from matplotlib.path import Path
    for name, rings in regions.items():
        for ring in rings:
            if Path(np.asarray(ring)).contains_point((lon, lat)):
                return name
    return None


# ------------------------------------------------------ Austria: stations ----

def select_stations(meta):
    """Active stations with coordinates; drop INDIVIDUAL series that belong to
    an active COMBINED station (same place, would show up twice)."""
    today = dt.date.today().isoformat()
    active = []
    for s in meta.get("stations", []):
        if s.get("lat") is None or s.get("lon") is None:
            continue
        if s.get("is_active") is False:
            continue
        if s.get("valid_to") and str(s["valid_to"])[:10] < today:
            continue
        active.append(s)
    combined_ids = {s["id"] for s in active if s.get("type") == "COMBINED"}
    out = []
    for s in active:
        if s.get("type") == "INDIVIDUAL" and s.get("group_id") in combined_ids:
            continue
        out.append({
            "id": s["id"],
            "name": (s.get("name") or "?").strip(),
            "state": s.get("state") or "",
            "lat": round(float(s["lat"]), 4),
            "lon": round(float(s["lon"]), 4),
            "alt": None if s.get("altitude") is None else round(float(s["altitude"])),
        })
    out.sort(key=lambda s: (s["state"], s["name"]))
    return out


def parse_station_series(js, param="rr"):
    """Station GeoJSON response -> (dates, {station_id: [mm|None, ...]}).
    Negative values are trace amounts (<0.1 mm) and count as 0."""
    dates = [t[:10] for t in js.get("timestamps", [])]
    series = {}
    for f in js.get("features", []):
        props = f.get("properties", {})
        p = props.get("parameters", {})
        data = (p.get(param) or p.get(param.upper()) or {}).get("data", [])
        vals = []
        for v in data:
            if v is None or (isinstance(v, float) and math.isnan(v)):
                vals.append(None)
            else:
                vals.append(round(max(0.0, float(v)), 1))
        series[props.get("station")] = vals
    return dates, series


def build_stations(out_dir, prev_dir=None):
    meta = api_get(f"{API}/station/historical/{STATION_DATASET}/metadata")
    stations = select_stations(meta)
    end = dt.date.today()
    start = end - dt.timedelta(days=STATION_DAYS - 1)
    dates, series = None, {}
    ids = [s["id"] for s in stations]
    for i in range(0, len(ids), 80):
        chunk = ids[i:i + 80]
        js = api_get(f"{API}/station/historical/{STATION_DATASET}", params={
            "parameters": "rr",
            "station_ids": ",".join(str(x) for x in chunk),
            "start": start.isoformat(),
            "end": end.isoformat(),
        })
        d, s = parse_station_series(js)
        dates = dates or d
        series.update(s)
    for st in stations:
        st["rr"] = series.get(st["id"]) or series.get(str(st["id"])) or [None] * len(dates or [])
    doc = station_doc(STATION_DATASET, dates or [], stations)
    write_json(out_dir, STATION_FILES["AT"], doc)
    return {"stations": len(doc["stations"]), "through": doc["through"]}


# ------------------------------------------------- Switzerland: stations ----

CH_BASE = "https://data.geo.admin.ch"
CH_STAC = f"{CH_BASE}/api/stac/v1/collections"
# (STAC collection, file prefix, short source tag used by the page for live 24 h data)
CH_STATION_SETS = (
    ("ch.meteoschweiz.ogd-smn", "ogd-smn", "smn"),
    ("ch.meteoschweiz.ogd-smn-precip", "ogd-smn-precip", "smn-precip"),
)
CH_PARAM = "rre150d0"        # daily precipitation, 06 UTC - 06 UTC next day


def parse_ch_meta(text):
    """MeteoSwiss *_meta_stations.csv (semicolon, cp1252) -> station dicts."""
    out = []
    for r in csv.DictReader(io.StringIO(text), delimiter=";"):
        try:
            lat = float(r["station_coordinates_wgs84_lat"])
            lon = float(r["station_coordinates_wgs84_lon"])
        except (KeyError, TypeError, ValueError):
            continue
        alt = r.get("station_height_masl") or ""
        out.append({
            "abbr": (r.get("station_abbr") or "").strip(),
            "name": (r.get("station_name") or "?").strip(),
            "canton": (r.get("station_canton") or "").strip(),
            "lat": round(lat, 4), "lon": round(lon, 4),
            "alt": round(float(alt)) if alt.strip() else None,
        })
    return [s for s in out if s["abbr"]]


def parse_ch_daily(text, param=CH_PARAM):
    """Station CSV -> {YYYY-MM-DD: mm or None}. Timestamps are DD.MM.YYYY HH:MM (UTC)."""
    out = {}
    for r in csv.DictReader(io.StringIO(text), delimiter=";"):
        ts = (r.get("reference_timestamp") or "").strip()
        if len(ts) < 10:
            continue
        try:
            day = dt.datetime.strptime(ts[:10], "%d.%m.%Y").date().isoformat()
        except ValueError:
            continue
        v = (r.get(param) or "").strip()
        try:
            out[day] = None if v == "" else round(max(0.0, float(v)), 1)
        except ValueError:
            out[day] = None
    return out


def load_prev_series(prev_dir, name):
    """{station id: {date: value}} from a previous run's file, to bridge gaps
    (e.g. early January, when the 'recent' files restart on 1 January)."""
    try:
        with open(os.path.join(prev_dir, name), encoding="utf-8") as f:
            doc = json.load(f)
        return {s["id"]: dict(zip(doc["dates"], s["rr"])) for s in doc["stations"]}
    except Exception:
        return {}


def build_ch_stations(out_dir, prev_dir=None):
    stations, seen, report = [], set(), {}
    for coll, prefix, src in CH_STATION_SETS:
        r = http_get(f"{CH_BASE}/{coll}/{prefix}_meta_stations.csv", ok404=True)
        if r is None:
            report[f"{src}_meta"] = "missing"
            continue
        meta = parse_ch_meta(r.content.decode("cp1252"))
        report[f"{src}_meta"] = len(meta)
        for s in meta:
            if s["abbr"] in seen:        # the same station in both sets: keep the first
                continue
            seen.add(s["abbr"])
            stations.append({**s, "coll": coll, "prefix": prefix, "src": src})

    dates = window_dates()

    def fetch(s):
        a = s["abbr"].lower()
        r = http_get(f"{CH_BASE}/{s['coll']}/{a}/{s['prefix']}_{a}_d_recent.csv", ok404=True)
        return s, (parse_ch_daily(r.content.decode("cp1252")) if r is not None else None)

    results = pmap(fetch, stations, 6)
    prev = load_prev_series(prev_dir, STATION_FILES["CH"]) if prev_dir else {}
    out, no_file = [], 0
    for s, series in results:
        if series is None:
            no_file += 1
        old = prev.get(s["abbr"], {})
        rr = []
        for d in dates:
            v = series.get(d) if series else None
            rr.append(old.get(d) if v is None else v)
        out.append({"id": s["abbr"], "name": s["name"], "state": s["canton"],
                    "lat": s["lat"], "lon": s["lon"], "alt": s["alt"], "src": s["src"], "rr": rr})
    doc = station_doc("MeteoSwiss SwissMetNet, rre150d0", dates, out)
    write_json(out_dir, STATION_FILES["CH"], doc)
    report.update({"stations": len(doc["stations"]), "through": doc["through"], "without_file": no_file})
    return report


# ----------------------------------------------------- Germany: stations ----

DWD = "https://opendata.dwd.de/climate_environment/CDC"
DE_DAILY = f"{DWD}/observations_germany/climate/daily/more_precip/recent"
DE_NOW = f"{DWD}/observations_germany/climate/10_minutes/precipitation/now"
DE_STATES = {"Baden-Württemberg": "BW", "Bayern": "BY", "Berlin": "BE", "Brandenburg": "BB",
             "Bremen": "HB", "Hamburg": "HH", "Hessen": "HE", "Mecklenburg-Vorpommern": "MV",
             "Niedersachsen": "NI", "Nordrhein-Westfalen": "NW", "Rheinland-Pfalz": "RP",
             "Saarland": "SL", "Sachsen": "SN", "Sachsen-Anhalt": "ST", "Schleswig-Holstein": "SH",
             "Thüringen": "TH"}


def decode_dwd(raw):
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def parse_dwd_stations(text):
    """DWD *_Beschreibung_Stationen.txt -> {id: station}. Columns: id, from, to,
    height, lat, lon, name (may contain spaces), Bundesland, Abgabe."""
    out = {}
    for line in text.splitlines()[2:]:
        t = line.split()
        if len(t) < 9 or not t[0].isdigit():
            continue
        state = DE_STATES.get(t[-2])
        if not state:
            continue
        try:
            out[int(t[0])] = {"id": str(int(t[0])), "to": t[2], "alt": round(float(t[3])),
                              "lat": round(float(t[4]), 4), "lon": round(float(t[5]), 4),
                              "name": " ".join(t[6:-2]), "state": state}
        except ValueError:
            continue
    return out


def parse_dwd_daily(text):
    """produkt_nieder_tag_*.txt -> {YYYY-MM-DD: RS mm or None}. -999 = missing."""
    out = {}
    lines = text.splitlines()
    head = [h.strip() for h in lines[0].split(";")]
    i_d, i_rs = head.index("MESS_DATUM"), head.index("RS")
    for line in lines[1:]:
        c = line.split(";")
        if len(c) <= i_rs:
            continue
        d = c[i_d].strip()
        try:
            v = float(c[i_rs])
        except ValueError:
            continue
        out[f"{d[:4]}-{d[4:6]}-{d[6:8]}"] = None if v <= -998 else round(max(0.0, v), 1)
    return out


def read_zip_product(raw, prefix="produkt"):
    z = zipfile.ZipFile(io.BytesIO(raw))
    name = next(n for n in z.namelist() if n.startswith(prefix))
    return decode_dwd(z.read(name))


def build_de_stations(out_dir, prev_dir=None):
    meta = parse_dwd_stations(decode_dwd(http_get(f"{DE_DAILY}/RR_Tageswerte_Beschreibung_Stationen.txt").content))
    zips = {int(m.group(1)): n for n in dir_listing(f"{DE_DAILY}/")
            if (m := re.match(r"tageswerte_RR_(\d+)_akt\.zip$", n))}
    recent = (utc_today() - dt.timedelta(days=7)).strftime("%Y%m%d")
    todo = [(sid, zips[sid]) for sid, s in meta.items() if sid in zips and s["to"] >= recent]
    dates = window_dates()

    def fetch(item):
        sid, fn = item
        r = http_get(f"{DE_DAILY}/{fn}", ok404=True)
        if r is None:
            return sid, None
        try:
            return sid, parse_dwd_daily(read_zip_product(r.content))
        except Exception:
            return sid, None

    out, failed = [], 0
    for sid, series in pmap(fetch, todo, 12):
        if series is None:
            failed += 1
            continue
        s = meta[sid]
        out.append({"id": s["id"], "name": s["name"], "state": s["state"], "lat": s["lat"],
                    "lon": s["lon"], "alt": s["alt"], "rr": [series.get(d) for d in dates]})
    doc = station_doc("DWD daily precipitation (more_precip, RS)", dates, out)
    write_json(out_dir, STATION_FILES["DE"], doc)
    return {"stations": len(doc["stations"]), "through": doc["through"], "listed": len(todo), "failed": failed}


# ----------------------------------------------------- Czechia: stations ----

CHMI = "https://opendata.chmi.cz/meteorology/climate"


def chmi_values(j):
    """CHMI open-data JSON -> (header list, rows)."""
    d = j["data"]["data"]
    return d["header"].split(","), d["values"]


def chmi_meta():
    """Station list (WSI, name, lon, lat, elevation) from the newest meta1 file."""
    for k in range(0, 4):
        day = utc_today() - dt.timedelta(days=k)
        r = http_get(f"{CHMI}/now/metadata/meta1-{day:%Y%m%d}.json", ok404=True)
        if r is not None:
            head, rows = chmi_values(r.json())
            ix = {h: i for i, h in enumerate(head)}
            out = {}
            for row in rows:
                try:
                    out[row[ix["WSI"]]] = {"name": row[ix["FULL_NAME"]].strip(), "lon": round(float(row[ix["GEOGR1"]]), 4),
                                           "lat": round(float(row[ix["GEOGR2"]]), 4),
                                           "alt": None if row[ix["ELEVATION"]] in ("", None) else round(float(row[ix["ELEVATION"]]))}
                except (TypeError, ValueError, KeyError):
                    continue
            return out
    raise RuntimeError("no CHMI station metadata (meta1) for the last 4 days")


def parse_chmi_daily(j, element="SRA"):
    """{YYYY-MM-DD: mm or None}. DT is 06:00 UTC on the day the 24 h period starts."""
    head, rows = chmi_values(j)
    ix = {h: i for i, h in enumerate(head)}
    out = {}
    for row in rows:
        if row[ix["ELEMENT"]] != element:
            continue
        v = row[ix["VAL"]]
        try:
            out[row[ix["DT"]][:10]] = None if v in ("", None) else round(max(0.0, float(v)), 1)
        except (TypeError, ValueError):
            continue
    return out


def build_cz_stations(out_dir, prev_dir=None):
    meta = chmi_meta()
    kraje = load_regions("kraje_cz.geojson", "code")
    dates = window_dates()
    months = sorted({d[:4] + d[5:7] for d in dates})
    # the current month's files sit in daily/, earlier months in daily/MM/ (MM/ may still
    # hold last year's files, so look for the exact YYYYMM in both places)
    listings = {"": set(dir_listing(f"{CHMI}/recent/data/daily/"))}
    where = {}
    for ym in months:
        for sub in ("", f"{ym[4:]}/"):
            if sub not in listings:
                try:
                    listings[sub] = set(dir_listing(f"{CHMI}/recent/data/daily/{sub}"))
                except Exception:
                    listings[sub] = set()
            if any(n.endswith(f"-{ym}.json") for n in listings[sub]):
                where[ym] = (sub, listings[sub])
                break
    stations = {}
    for wsi, m in meta.items():
        if not any(f"dly-{wsi}-{ym}.json" in where.get(ym, ("", set()))[1] for ym in months):
            continue
        st = region_of(m["lat"], m["lon"], kraje)
        if st:                                # only stations in Czechia
            stations[wsi] = {**m, "state": st}

    def fetch(job):
        wsi, ym = job
        sub, names = where[ym]
        fn = f"dly-{wsi}-{ym}.json"
        if fn not in names:
            return wsi, {}
        r = http_get(f"{CHMI}/recent/data/daily/{sub}{fn}", ok404=True)
        try:
            return wsi, parse_chmi_daily(r.json()) if r is not None else {}
        except Exception:
            return wsi, {}

    jobs = [(w, ym) for w in stations for ym in months if ym in where]
    series = {}
    for wsi, part in pmap(fetch, jobs, 8):
        series.setdefault(wsi, {}).update(part)
    out = [{"id": w, "name": s["name"], "state": s["state"], "lat": s["lat"], "lon": s["lon"], "alt": s["alt"],
            "rr": [series.get(w, {}).get(d) for d in dates]} for w, s in stations.items()]
    doc = station_doc("CHMI daily precipitation (SRA)", dates, out)
    write_json(out_dir, STATION_FILES["CZ"], doc)
    return {"stations": len(doc["stations"]), "through": doc["through"], "months": {k: v[0] or "root" for k, v in where.items()}}


# --------------------------------------------- Germany + Czechia: 24 hours ----

def hourly_doc(source, end, stations):
    hours = [(end - dt.timedelta(hours=23 - i)).strftime("%Y-%m-%dT%H:%MZ") for i in range(24)]
    return {"generated": now_iso(), "source": source, "end": end.strftime("%Y-%m-%dT%H:%MZ"),
            "hours": hours, "stations": stations}


def to_hourly(values, end):
    """{datetime (end of interval, UTC): mm} -> 24 hourly sums for the hours ending
    end-23h .. end; None for an hour without any value."""
    out = [None] * 24
    for t, v in values.items():
        # t in (end - 24h, end]  ->  hour index 0..23 (the hour that ends at or after t)
        if not (end - dt.timedelta(hours=24) < t <= end):
            continue
        hour_end = t if (t.minute == 0 and t.second == 0) else (t.replace(minute=0, second=0) + dt.timedelta(hours=1))
        k = 23 - int((end - hour_end).total_seconds() // 3600)
        if 0 <= k < 24 and v is not None:
            out[k] = round((out[k] or 0) + v, 2)
    return [None if x is None else round(x, 1) for x in out]


def parse_dwd_10min(text):
    """produkt_zehn_now_rr_*.txt -> {datetime UTC (end of the 10 min): mm}."""
    lines = text.splitlines()
    head = [h.strip() for h in lines[0].split(";")]
    i_d, i_r = head.index("MESS_DATUM"), head.index("RWS_10")
    out = {}
    for line in lines[1:]:
        c = line.split(";")
        if len(c) <= i_r:
            continue
        try:
            v = float(c[i_r])
            t = dt.datetime.strptime(c[i_d].strip(), "%Y%m%d%H%M").replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        out[t] = None if v <= -998 else max(0.0, v)
    return out


def build_de_now(out_dir, prev_dir=None):
    meta = parse_dwd_stations(decode_dwd(http_get(f"{DE_NOW}/zehn_now_rr_Beschreibung_Stationen.txt").content))
    zips = {int(m.group(1)): n for n in dir_listing(f"{DE_NOW}/")
            if (m := re.match(r"10minutenwerte_nieder_(\d+)_now\.zip$", n))}

    def fetch(item):
        sid, fn = item
        r = http_get(f"{DE_NOW}/{fn}", ok404=True)
        try:
            return sid, parse_dwd_10min(read_zip_product(r.content)) if r is not None else None
        except Exception:
            return sid, None

    data = {sid: v for sid, v in pmap(fetch, [(s, f) for s, f in zips.items() if s in meta], 12) if v}
    latest = [max(t for t, v in d.items() if v is not None) for d in data.values() if any(v is not None for v in d.values())]
    if not latest:
        raise RuntimeError("no 10-minute precipitation values")
    end = sorted(latest)[len(latest) // 2].replace(minute=0, second=0)   # median station's last full hour
    out = []
    for sid, d in data.items():
        h = to_hourly(d, end)
        if sum(x is not None for x in h) < 20:
            continue
        s = meta[sid]
        out.append({"id": s["id"], "name": s["name"], "state": s["state"], "lat": s["lat"], "lon": s["lon"],
                    "alt": s["alt"], "h": h})
    doc = hourly_doc("DWD 10-minute precipitation (now)", end, out)
    write_json(out_dir, "de_now.json", doc)
    return {"stations": len(out), "end": doc["end"]}


def build_cz_now(out_dir, prev_dir=None):
    meta = chmi_meta()
    kraje = load_regions("kraje_cz.geojson", "code")
    names = [n for n in dir_listing(f"{CHMI}/now/data/") if n.startswith("1h-") and n.endswith(".json")]
    today = utc_today()
    keep_days = {(today - dt.timedelta(days=k)).strftime("%Y%m%d") for k in range(2)}
    by_station = {}
    for n in names:
        m = re.match(r"1h-(.+)-(\d{8})\.json$", n)
        if m and m.group(2) in keep_days and m.group(1) in meta:
            by_station.setdefault(m.group(1), []).append(n)
    regions = {w: region_of(meta[w]["lat"], meta[w]["lon"], kraje) for w in by_station}
    jobs = [(w, n) for w, ns in by_station.items() if regions.get(w) for n in ns]

    def fetch(job):
        w, n = job
        r = http_get(f"{CHMI}/now/data/{n}", ok404=True)
        vals = {}
        if r is not None:
            try:
                head, rows = chmi_values(r.json())
                ix = {h: i for i, h in enumerate(head)}
                for row in rows:
                    if row[ix["ELEMENT"]] != "SRA1H" or row[ix["VAL"]] in ("", None):
                        continue
                    t = dt.datetime.strptime(row[ix["DT"]][:16], "%Y-%m-%dT%H:%M").replace(tzinfo=dt.timezone.utc)
                    vals[t] = max(0.0, float(row[ix["VAL"]]))
            except Exception:
                pass
        return w, vals

    data = {}
    for w, vals in pmap(fetch, jobs, 8):
        data.setdefault(w, {}).update(vals)
    latest = [max(d) for d in data.values() if d]
    if not latest:
        raise RuntimeError("no hourly CHMI precipitation values")
    end = sorted(latest)[len(latest) // 2]          # hourly values are stamped with the end of the hour
    out = []
    for w, d in data.items():
        h = to_hourly(d, end)
        if sum(x is not None for x in h) < 20:
            continue
        m = meta[w]
        out.append({"id": w, "name": m["name"], "state": regions[w], "lat": m["lat"], "lon": m["lon"],
                    "alt": m["alt"], "h": h})
    doc = hourly_doc("CHMI hourly precipitation (SRA1H, now)", end, out)
    write_json(out_dir, "cz_now.json", doc)
    return {"stations": len(out), "end": doc["end"]}


# ---------------------------------------------------------- grid helpers ----

def merc_y(lat_deg):
    return np.log(np.tan(np.pi / 4 + np.radians(lat_deg) / 2))


def inv_merc_y(y):
    return np.degrees(2 * np.arctan(np.exp(y)) - np.pi / 2)


def mercator_target(bounds, width):
    """Pixel-centre lat/lon of a raster that is linear in Web Mercator over
    bounds = (south, west, north, east). Row 0 is north."""
    s, w, n, e = bounds
    lon = w + (np.arange(width) + 0.5) * (e - w) / width
    y_n, y_s = merc_y(n), merc_y(s)
    height = int(round(width * (y_n - y_s) / np.radians(e - w)))
    y = y_n - (np.arange(height) + 0.5) * (y_n - y_s) / height
    lat = inv_merc_y(y)
    return np.meshgrid(lon, lat)          # LON, LAT, shape (height, width)


def sample_nearest(arr, xs, ys, X, Y):
    """Nearest-neighbour lookup of arr[y, x] (regular 1-D coords, either
    order) at projected points X, Y. Outside the grid -> NaN."""
    dx = xs[1] - xs[0]
    dy = ys[1] - ys[0]
    ix = np.rint((X - xs[0]) / dx).astype(np.int64)
    iy = np.rint((Y - ys[0]) / dy).astype(np.int64)
    ok = (ix >= 0) & (ix < len(xs)) & (iy >= 0) & (iy < len(ys))
    out = np.full(X.shape, np.nan, dtype=np.float32)
    out[ok] = arr[iy[ok], ix[ok]]
    return out


def classify(values):
    """mm -> class index 0..len(BINS)-1; NaN -> len(BINS) (transparent)."""
    edges = np.array([b for b, _ in BINS[:-1]], dtype=np.float64)
    cls = np.digitize(np.nan_to_num(values, nan=0.0), edges, right=False)
    cls = cls.astype(np.uint8)
    cls[np.isnan(values)] = len(BINS)
    return cls


def write_overlay_png(cls, path):
    """Palette PNG: one entry per class + a fully transparent one.
    The 0-2 mm class is half transparent so the basemap stays readable."""
    from PIL import Image
    pal, alpha = [], []
    for i, (_, hexcol) in enumerate(BINS):
        pal += [int(hexcol[k:k + 2], 16) for k in (1, 3, 5)]
        alpha.append(110 if i == 0 else 255)
    pal += [0, 0, 0]
    alpha.append(0)
    img = Image.fromarray(cls, mode="P")
    img.putpalette(pal)
    img.info["transparency"] = bytes(alpha)
    img.save(path, optimize=True, transparency=bytes(alpha))


def load_state_polygons(fname="bundeslaender.geojson", key="name"):
    return load_regions(fname, key)


def state_masks(LON, LAT, states):
    from matplotlib.path import Path
    pts = np.column_stack([LON.ravel(), LAT.ravel()])
    masks = {}
    for name, rings in states.items():
        m = np.zeros(len(pts), dtype=bool)
        for ring in rings:
            m |= Path(np.asarray(ring)).contains_points(pts)
        masks[name] = m.reshape(LON.shape)
    return masks


def clip_mask(masks):
    """Union of all region masks, with the hairline gaps the simplified
    borders leave between neighbours closed."""
    m = np.zeros(next(iter(masks.values())).shape, dtype=bool)
    for v in masks.values():
        m |= v
    near = lambda a, ax, k: np.roll(a, k, ax) | np.roll(a, 2 * k, ax)
    return m | (near(m, 1, 1) & near(m, 1, -1)) | (near(m, 0, 1) & near(m, 0, -1))


def area_means(values, LAT, masks, total_name="Österreich"):
    """Area-weighted means per region (Mercator pixel area ~ cos^2 lat)."""
    wgt = np.cos(np.radians(LAT)) ** 2
    out = {}
    union = np.zeros(values.shape, dtype=bool)
    for name, m in masks.items():
        sel = m & np.isfinite(values)
        union |= sel
        if sel.any():
            out[name] = round(float((values[sel] * wgt[sel]).sum() / wgt[sel].sum()), 1)
    if union.any():
        out[total_name] = round(float((values[union] * wgt[union]).sum() / wgt[union].sum()), 1)
    return out


def overlay_periods(days, last, xs, ys, X, Y, LAT, masks, clip, out_dir, prefix, total_name):
    """Sum the daily grids for each preset, resample, clip and write PNGs."""
    periods = {}
    for p in PERIODS:
        want = [(last - dt.timedelta(days=k)).isoformat() for k in range(p)]
        if not all(w in days for w in want):
            continue
        stack = np.stack([days[w] for w in want])
        total = np.nansum(stack, axis=0)
        total[np.all(np.isnan(stack), axis=0)] = np.nan     # outside the analysis domain
        vals = sample_nearest(total, xs, ys, X, Y)
        vals[~clip] = np.nan
        fname = f"{prefix}grid_{p}d.png"
        write_overlay_png(classify(vals), os.path.join(out_dir, fname))
        periods[str(p)] = {
            "start": want[-1], "end": want[0], "file": fname,
            "mean": area_means(vals, LAT, masks, total_name),
            "max": round(float(np.nanmax(vals)), 1),
        }
    return periods


def render_grid(g, last, out_dir):
    """g: dict from one of the fetch_*_grid functions; writes PNGs + json."""
    from pyproj import Transformer
    LON, LAT = mercator_target(g["bounds"], g["width"])
    X, Y = Transformer.from_crs("EPSG:4326", g["crs"], always_xy=True).transform(LON, LAT)
    masks = state_masks(LON, LAT, load_regions(*g["borders"]))
    clip = clip_mask(masks)
    s, w, n, e = g["bounds"]
    doc = {
        "generated": now_iso(),
        "source": g["source"],
        "bounds": [[s, w], [n, e]],
        "last_day": last.isoformat(),
        "own_last_day": g["last"].isoformat(),
        "periods": overlay_periods(g["days"], last, g["xs"], g["ys"], X, Y, LAT, masks, clip,
                                   out_dir, g["prefix"], g["total"]),
    }
    write_json(out_dir, g["json"], doc, compact=False)
    return {"periods": list(doc["periods"]), "last_day": doc["last_day"], "own_last_day": doc["own_last_day"],
            **{k: v for k, v in g.items() if k in ("monthly_used", "missing_days", "file")}}


# ---------------------------------------------------------- Austria: grid ----

def fetch_grid_days(first, last, bbox):
    """Download daily RR grids in adaptive chunks -> (xs, ys, {date: 2-D})."""
    import xarray as xr
    days = {}
    xs = ys = None
    chunk = 5
    d = first
    while d <= last:
        e = min(last, d + dt.timedelta(days=chunk - 1))
        try:
            raw = api_get(f"{API}/grid/historical/{GRID_DATASET}", as_json=False, params={
                "parameters": "RR",
                "start": f"{d.isoformat()}T00:00",
                "end": f"{e.isoformat()}T00:00",
                "bbox": ",".join(f"{v:.5f}" for v in bbox),
                "output_format": "netcdf",
            })
        except ApiError as err:
            if err.status in (400, 413, 422) and chunk > 1:
                chunk = max(1, chunk // 2)      # request too large: smaller bites
                continue
            raise
        with tempfile.NamedTemporaryFile(suffix=".nc", delete=False) as f:
            f.write(raw)
            path = f.name
        try:
            with xr.open_dataset(path) as ds:
                var = ds["RR"] if "RR" in ds else ds[list(ds.data_vars)[0]]
                cx = np.asarray(ds["x"].values, dtype=np.float64)
                cy = np.asarray(ds["y"].values, dtype=np.float64)
                if xs is None:
                    xs, ys = cx, cy
                elif cx.shape != xs.shape or cy.shape != ys.shape:
                    raise RuntimeError("grid shape changed between requests")
                for i, t in enumerate(var["time"].values):
                    day = str(t)[:10]
                    a = np.asarray(var.isel(time=i).values, dtype=np.float32)
                    days[day] = np.where(a < 0, 0, a)
        finally:
            os.unlink(path)
        d = e + dt.timedelta(days=1)
    return xs, ys, days


def fetch_at_grid():
    meta = api_get(f"{API}/grid/historical/{GRID_DATASET}/metadata")
    last = dt.date.fromisoformat(str(meta["end_time"])[:10])
    first = last - dt.timedelta(days=GRID_DAYS - 1)
    bbox = meta["bbox"]                          # south, west, north, east
    xs, ys, days = fetch_grid_days(first, last, bbox)
    # SPARTACUS v3 reaches ~20 % beyond the border; the overlay shows Austria only
    return {"country": "AT", "source": GRID_DATASET, "xs": xs, "ys": ys, "days": days, "last": last,
            "crs": meta.get("crs") or "EPSG:3416", "bounds": tuple(bbox), "width": OVERLAY_WIDTH,
            "borders": ("bundeslaender.geojson", "name"), "prefix": "", "total": "Österreich", "json": "grid.json"}


# ----------------------------------------------------- Switzerland: grid ----

CH_GRID = "ch.meteoschweiz.ogd-surface-derived-grid"
CH_BOUNDS = (45.78, 5.90, 47.84, 10.53)      # south, west, north, east
CH_OVERLAY_WIDTH = 560                       # ~0.7 km per px


def ch_daily_url(day):
    s = day.strftime("%Y%m%d")
    return (f"{CH_BASE}/{CH_GRID}/{s}-ch/"
            f"ogd-surface-derived-grid.rprelimd_ch01h.swiss.lv95_{s}000000_{s}000000.nc")


def open_nc(raw):
    import xarray as xr
    with tempfile.NamedTemporaryFile(suffix=".nc", delete=False) as f:
        f.write(raw)
        path = f.name
    try:
        with xr.open_dataset(path) as ds:
            return ds.load()
    finally:
        os.unlink(path)


def ch_fields(ds):
    """-> (xs, ys, crs, [(date, 2-D array)], debug) for a MeteoSwiss grid file,
    whatever the coordinate names are."""
    names = [v for v in ds.data_vars if ds[v].ndim >= 2]
    pick = next((v for v in names if v.lower().startswith(("rprelim", "rhires"))), names[0])
    var = ds[pick]
    xn = next(c for c in ("E", "x", "chx", "X", "east", "lon", "longitude") if c in var.dims)
    yn = next(c for c in ("N", "y", "chy", "Y", "north", "lat", "latitude") if c in var.dims)
    xs = np.asarray(ds[xn].values, dtype=np.float64)
    ys = np.asarray(ds[yn].values, dtype=np.float64)
    big = max(abs(xs).max(), abs(ys).max())
    crs = "EPSG:2056" if big > 1e6 else "EPSG:21781" if big > 1e4 else "EPSG:4326"
    other = [d for d in var.dims if d not in (xn, yn)]
    var = var.transpose(*other, yn, xn)
    fields = []
    if not other:
        fields.append((None, np.asarray(var.values, dtype=np.float32)))
    else:
        tdim = other[0]
        tvals = ds[tdim].values if tdim in ds.coords else [None] * var.sizes[tdim]
        for i in range(var.sizes[tdim]):
            a = np.asarray(var.isel({tdim: i}).values, dtype=np.float32)
            while a.ndim > 2:
                a = a[0]
            day = None if tvals[i] is None else str(np.datetime_as_string(np.datetime64(tvals[i], "D")))
            fields.append((day, a))
    debug = {"var": pick, "dims": list(var.dims), "crs": crs, "shape": list(var.shape[-2:]),
             "x0": float(xs[0]), "dx": float(xs[1] - xs[0]), "y0": float(ys[0]), "dy": float(ys[1] - ys[0])}
    return xs, ys, crs, fields, debug


def ch_monthly_asset(month):
    """href of the consolidated monthly RhiresD file (daily values) for YYYYMM."""
    r = http_get(f"{CH_STAC}/{CH_GRID}/items/{month}-ch", ok404=True)
    if r is None:
        return None
    for key, a in (r.json().get("assets") or {}).items():
        if "rhiresd" in key.lower():
            return a.get("href")
    return None


def fetch_ch_grid():
    today = utc_today()
    # newest preliminary day that is already published
    last, first_raw = None, None
    for k in range(1, 6):
        r = http_get(ch_daily_url(today - dt.timedelta(days=k)), ok404=True)
        if r is not None:
            last, first_raw = today - dt.timedelta(days=k), r.content
            break
    if last is None:
        raise RuntimeError("no preliminary daily precipitation grid found for the last 5 days")
    wanted = [last - dt.timedelta(days=k) for k in range(GRID_DAYS)]

    def fetch(day):
        if day == last:
            return day, first_raw
        r = http_get(ch_daily_url(day), ok404=True)
        return day, (r.content if r is not None else None)

    raws = dict(pmap(fetch, wanted, 4))
    days, xs, ys, crs, debug = {}, None, None, None, None
    for day, raw in raws.items():
        if raw is None:
            continue
        gx, gy, gcrs, fields, dbg = ch_fields(open_nc(raw))
        if xs is None:
            xs, ys, crs, debug = gx, gy, gcrs, dbg
        elif gx.shape != xs.shape or gy.shape != ys.shape:
            continue
        a = fields[0][1]
        days[day.isoformat()] = np.where(a < 0, 0, a)

    # older days the preliminary series no longer holds: consolidated monthly files
    missing = sorted(d for d in wanted if d.isoformat() not in days)
    monthly_used = []
    for month in sorted({d.strftime("%Y%m") for d in missing}):
        href = ch_monthly_asset(month)
        r = http_get(href, timeout=300, ok404=True) if href else None
        if r is None:
            continue
        gx, gy, gcrs, fields, _ = ch_fields(open_nc(r.content))
        if xs is not None and (gx.shape != xs.shape or gy.shape != ys.shape):
            continue
        xs, ys, crs = (xs, ys, crs) if xs is not None else (gx, gy, gcrs)
        monthly_used.append(month)
        for day, a in fields:
            if day and day not in days and dt.date.fromisoformat(day) in missing:
                days[day] = np.where(a < 0, 0, a)
    return {"country": "CH", "source": "MeteoSwiss RprelimD (preliminary daily precipitation analysis)",
            "xs": xs, "ys": ys, "days": days, "last": last, "crs": crs, "bounds": CH_BOUNDS,
            "width": CH_OVERLAY_WIDTH, "borders": ("kantone.geojson", "code"), "prefix": "ch_",
            "total": "Schweiz", "json": "ch_grid.json", "monthly_used": monthly_used,
            "missing_days": [d.isoformat() for d in wanted if d.isoformat() not in days], "file": debug}


# --------------------------------------------------------- Germany: grid ----

DE_HYRAS = f"{DWD}/grids_germany/daily/hyras_de/precipitation"
DE_BOUNDS = (47.20, 5.80, 55.10, 15.10)
DE_OVERLAY_WIDTH = 760                       # ~0.85 km per px


def hyras_file(year, names):
    """Newest version of pr_hyras_1_{year}_v*_de.nc in the directory listing."""
    cands = []
    for n in names:
        m = re.match(rf"pr_hyras_1_{year}_v(\d+)-(\d+)_de\.nc$", n)
        if m:
            cands.append(((int(m.group(1)), int(m.group(2))), n))
    return max(cands)[1] if cands else None


def fetch_de_grid():
    import xarray as xr
    names = dir_listing(f"{DE_HYRAS}/")
    today = utc_today()
    years = sorted({today.year, (today - dt.timedelta(days=GRID_DAYS + 5)).year})
    days, xs, ys, used = {}, None, None, []
    for year in years:
        fn = hyras_file(year, names)
        if not fn:
            continue
        r = http_get(f"{DE_HYRAS}/{fn}", timeout=600)
        path = os.path.join(CACHE_DIR, fn) if CACHE_DIR else tempfile.NamedTemporaryFile(suffix=".nc", delete=False).name
        with open(path, "wb") as f:
            f.write(r.content)
        used.append(fn)
        with xr.open_dataset(path) as ds:
            var = ds["pr"]
            t = [str(v)[:10] for v in ds["time"].values]
            first = (today - dt.timedelta(days=GRID_DAYS + 5)).isoformat()
            idx = [i for i, d in enumerate(t) if d >= first]
            if not idx:
                continue
            block = np.asarray(var.isel(time=idx).transpose("time", "y", "x").values, dtype=np.float32)
            gx = np.asarray(ds["x"].values, dtype=np.float64)
            gy = np.asarray(ds["y"].values, dtype=np.float64)
            xs, ys = (gx, gy) if xs is None else (xs, ys)
            for k, i in enumerate(idx):
                a = block[k]
                days[t[i]] = np.where(a < 0, 0, a)
        if not CACHE_DIR:
            os.unlink(path)
    if not days:
        raise RuntimeError("no HYRAS days found")
    last = dt.date.fromisoformat(max(days))
    return {"country": "DE", "source": "DWD HYRAS-DE-PR (daily precipitation, 1 km)", "xs": xs, "ys": ys,
            "days": days, "last": last, "crs": "EPSG:3035", "bounds": DE_BOUNDS, "width": DE_OVERLAY_WIDTH,
            "borders": ("laender_de.geojson", "code"), "prefix": "de_", "total": "Deutschland",
            "json": "de_grid.json", "file": {"files": used}}


# ------------------------------------------------------------ alignment ----

def align_station_files(out_dir, prev_dir):
    """Give every country's station file the same 'through' date (the earliest
    of the countries), so a preset covers the same days everywhere."""
    docs = {}
    for c, name in STATION_FILES.items():
        for d in (out_dir, prev_dir):
            p = os.path.join(d, name) if d else None
            if p and os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    docs[c] = json.load(f)
                break
    own = {c: d.get("through_own") or d.get("through") for c, d in docs.items()}
    fresh = [t for t in own.values() if t and t >= (utc_today() - dt.timedelta(days=6)).isoformat()]
    if not fresh:
        return {}
    common = min(fresh)
    for c, d in docs.items():
        d["through_own"] = own[c]
        d["through"] = common if own[c] and own[c] >= common else own[c]
        write_json(out_dir, STATION_FILES[c], d)
    # stations per region, so the page's menu can leave out regions without any
    counts = {}
    for c, d in docs.items():
        for s in d["stations"]:
            counts.setdefault(c, {}).setdefault(s["state"], 0)
            counts[c][s["state"]] += 1
    write_json(out_dir, "regions.json", counts)
    return {"common_through": common, "own": own}


# ---------------------------------------------------------------- main ----

def run(report, name, fn, *args):
    try:
        report[name] = fn(*args)
        print(f"{name}: {report[name]}")
        return report[name]
    except Exception as e:                       # keep going, report it
        report[name] = {"error": f"{type(e).__name__}: {e}"}
        print(f"{name} FAILED: {e}", file=sys.stderr)
        return None


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.getcwd(), "out")
    prev_dir = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None
    mode = sys.argv[3] if len(sys.argv) > 3 else "full"
    os.makedirs(out_dir, exist_ok=True)
    if CACHE_DIR:
        os.makedirs(CACHE_DIR, exist_ok=True)
    report = {"ran": now_iso(), "mode": mode}

    if mode == "full":
        for name, fn in (("stations", build_stations), ("ch_stations", build_ch_stations),
                         ("de_stations", build_de_stations), ("cz_stations", build_cz_stations)):
            run(report, name, fn, out_dir, prev_dir)
        run(report, "alignment", align_station_files, out_dir, prev_dir)

        grids = {}
        for c, fn in (("AT", fetch_at_grid), ("CH", fetch_ch_grid), ("DE", fetch_de_grid)):
            try:
                grids[c] = fn()
                report[f"fetch_{c}_grid"] = {"last": grids[c]["last"].isoformat(), "days": len(grids[c]["days"])}
            except Exception as e:
                report[f"fetch_{c}_grid"] = {"error": f"{type(e).__name__}: {e}"}
            print(f"fetch_{c}_grid: {report[f'fetch_{c}_grid']}")
        if grids:
            common = min(g["last"] for g in grids.values())
            report["grid_common_last_day"] = common.isoformat()
            for c, g in grids.items():
                run(report, {"AT": "grid", "CH": "ch_grid", "DE": "de_grid"}[c], render_grid, g, common, out_dir)

    for name, fn in (("de_now", build_de_now), ("cz_now", build_cz_now)):
        run(report, name, fn, out_dir, prev_dir)

    write_json(out_dir, "meta.json" if mode == "full" else "meta_now.json", report, compact=False)
    ok = any(isinstance(v, dict) and "error" not in v for k, v in report.items() if k not in ("ran", "mode"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
