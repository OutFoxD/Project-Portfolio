#!/usr/bin/env python3
"""
precompute.py - builds the cached data behind protofox.at/shroomzn

Runs in GitHub Actions (.github/workflows/shroomzn-data.yml), a few times a day:

    python precompute.py OUT_DIR [PREVIOUS_DATA_DIR]

Austria (GeoSphere Austria Data Hub, CC BY 4.0):
  stations.json      every active klima-v2-1d station with its Bundesland and
                     the last ~46 daily precipitation sums [mm]
  grid_{7,14,30,45}d.png + grid.json
                     SPARTACUS v3 1 km precipitation sums as colour-classed,
                     Web-Mercator-aligned overlays; area mean per Bundesland

Switzerland (MeteoSwiss Open Data, "Source: MeteoSwiss"):
  ch_stations.json   SwissMetNet weather + precipitation stations with canton
                     and the last ~46 daily sums (rre150d0, 06-06 UTC)
  ch_grid_{7,14,30,45}d.png + ch_grid.json
                     RprelimD 1 km daily precipitation analysis, same format

  meta.json          when it ran, what each part produced, and any errors

The four parts are independent: if one fails, the others are still written,
and the workflow keeps the previous files for the failed one.
"""

import concurrent.futures as cf
import csv
import datetime as dt
import io
import json
import math
import os
import sys
import tempfile
import threading
import time

import numpy as np

API = "https://dataset.api.hub.geosphere.at/v1"
STATION_DATASET = "klima-v2-1d"
GRID_DATASET = "spartacus-v3-1d-1km"
STATION_DAYS = 46            # 45 full days + today
PERIODS = (7, 14, 30, 45)    # grid overlay presets, days
OVERLAY_WIDTH = 800          # px across Austria (~0.75 km per px)
USER_AGENT = "protofox.at/shroomzn precompute (GitHub Actions)"
HERE = os.path.dirname(os.path.abspath(__file__))

# Klimamonitoring colour classes: upper bound [mm], colour
BINS = [
    (2, "#ffffff"), (15, "#fdf6c3"), (30, "#e2ef9f"), (50, "#a5d383"),
    (75, "#7fc8b4"), (100, "#4fb3c6"), (150, "#3f8fd2"), (200, "#2f5fc4"),
    (250, "#1f3a9e"), (350, "#6f4fb3"), (450, "#9c4fb3"), (600, "#d23b3b"),
    (math.inf, "#e88a4f"),
]


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
    """Plain GET for static files (MeteoSwiss), one session per thread, with
    retries and backoff. Returns None for 404 when ok404 is set."""
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
    stations = [s for s in stations if any(v is not None for v in s["rr"])]
    stations = drop_twins(stations)
    doc = {
        "generated": now_iso(),
        "source": STATION_DATASET,
        "dates": dates or [],
        "through": through_date(dates or [], {s["id"]: s["rr"] for s in stations}),
        "stations": stations,
    }
    write_json(out_dir, "stations.json", doc)
    return {"stations": len(stations), "through": doc["through"]}


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
    with open(os.path.join(HERE, fname), encoding="utf-8") as f:
        gj = json.load(f)
    states = {}
    for feat in gj["features"]:
        rings = []
        for poly in feat["geometry"]["coordinates"]:
            rings.extend(poly)       # holes are negligible at this scale
        states[feat["properties"][key]] = rings
    return states


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


def build_grid(out_dir, prev_dir=None):
    from pyproj import Transformer
    meta = api_get(f"{API}/grid/historical/{GRID_DATASET}/metadata")
    last = dt.date.fromisoformat(str(meta["end_time"])[:10])
    first = last - dt.timedelta(days=max(PERIODS) - 1)
    bbox = meta["bbox"]                          # south, west, north, east
    xs, ys, days = fetch_grid_days(first, last, bbox)

    LON, LAT = mercator_target(bbox, OVERLAY_WIDTH)
    tr = Transformer.from_crs("EPSG:4326", meta.get("crs") or "EPSG:3416", always_xy=True)
    X, Y = tr.transform(LON, LAT)
    masks = state_masks(LON, LAT, load_state_polygons())
    # SPARTACUS v3 reaches ~20 % beyond the border; show Austria only, like Klimamonitoring
    clip = clip_mask(masks)

    doc = {
        "generated": now_iso(),
        "source": GRID_DATASET,
        "bounds": [[bbox[0], bbox[1]], [bbox[2], bbox[3]]],
        "last_day": last.isoformat(),
        "periods": overlay_periods(days, last, xs, ys, X, Y, LAT, masks, clip,
                                   out_dir, "", "Österreich"),
    }
    write_json(out_dir, "grid.json", doc, compact=False)
    return {"periods": list(doc["periods"]), "last_day": doc["last_day"]}


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

    end = dt.datetime.now(dt.timezone.utc).date()
    dates = [(end - dt.timedelta(days=STATION_DAYS - 1 - i)).isoformat() for i in range(STATION_DAYS)]

    def fetch(s):
        a = s["abbr"].lower()
        r = http_get(f"{CH_BASE}/{s['coll']}/{a}/{s['prefix']}_{a}_d_recent.csv", ok404=True)
        return s, (parse_ch_daily(r.content.decode("cp1252")) if r is not None else None)

    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(fetch, stations))

    prev = load_prev_series(prev_dir, "ch_stations.json") if prev_dir else {}
    out, no_file = [], 0
    for s, series in results:
        if series is None:
            no_file += 1
        old = prev.get(s["abbr"], {})
        rr = []
        for d in dates:
            v = series.get(d) if series else None
            rr.append(old.get(d) if v is None else v)
        if not any(v is not None for v in rr):
            continue
        out.append({"id": s["abbr"], "name": s["name"], "state": s["canton"],
                    "lat": s["lat"], "lon": s["lon"], "alt": s["alt"], "src": s["src"], "rr": rr})
    out = drop_twins(out)
    doc = {
        "generated": now_iso(),
        "source": "MeteoSwiss SwissMetNet, rre150d0",
        "dates": dates,
        "through": through_date(dates, {s["id"]: s["rr"] for s in out}),
        "stations": out,
    }
    write_json(out_dir, "ch_stations.json", doc)

    # does the browser get CORS access to the live hourly files? (for the 24 h view)
    report["cors"] = None
    if out:
        try:
            import requests
            s0 = out[0]
            coll, prefix = next((c, p) for c, p, t in CH_STATION_SETS if t == s0["src"])
            a = s0["id"].lower()
            r = requests.get(f"{CH_BASE}/{coll}/{a}/{prefix}_{a}_h_now.csv", timeout=30,
                             headers={"Origin": "https://protofox.at", "User-Agent": USER_AGENT})
            report["cors"] = r.headers.get("Access-Control-Allow-Origin")
            report["h_now_has_rre150h0"] = "rre150h0" in r.text.splitlines()[0] if r.ok else None
        except Exception as e:
            report["cors"] = f"probe failed: {e}"
    report.update({"stations": len(out), "through": doc["through"], "without_file": no_file})
    return report


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


def build_ch_grid(out_dir, prev_dir=None):
    from pyproj import Transformer
    today = dt.datetime.now(dt.timezone.utc).date()
    # newest preliminary day that is already published
    last, first_raw = None, None
    for k in range(1, 6):
        r = http_get(ch_daily_url(today - dt.timedelta(days=k)), ok404=True)
        if r is not None:
            last, first_raw = today - dt.timedelta(days=k), r.content
            break
    if last is None:
        raise RuntimeError("no preliminary daily precipitation grid found for the last 5 days")
    wanted = [last - dt.timedelta(days=k) for k in range(max(PERIODS))]

    def fetch(day):
        if day == last:
            return day, first_raw
        r = http_get(ch_daily_url(day), ok404=True)
        return day, (r.content if r is not None else None)

    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        raws = dict(ex.map(fetch, wanted))

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
    still_missing = [d.isoformat() for d in wanted if d.isoformat() not in days]

    LON, LAT = mercator_target(CH_BOUNDS, CH_OVERLAY_WIDTH)
    X, Y = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform(LON, LAT)
    masks = state_masks(LON, LAT, load_state_polygons("kantone.geojson", "code"))
    clip = clip_mask(masks)
    s, w, n, e = CH_BOUNDS
    doc = {
        "generated": now_iso(),
        "source": "MeteoSwiss RprelimD (preliminary daily precipitation analysis)",
        "bounds": [[s, w], [n, e]],
        "last_day": last.isoformat(),
        "periods": overlay_periods(days, last, xs, ys, X, Y, LAT, masks, clip,
                                   out_dir, "ch_", "Schweiz"),
    }
    write_json(out_dir, "ch_grid.json", doc, compact=False)
    return {"periods": list(doc["periods"]), "last_day": doc["last_day"],
            "monthly_used": monthly_used, "missing_days": still_missing, "file": debug}


# ---------------------------------------------------------------- main ----

def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.getcwd(), "out")
    prev_dir = sys.argv[2] if len(sys.argv) > 2 else None
    os.makedirs(out_dir, exist_ok=True)
    report = {"ran": now_iso()}
    ok = 0
    parts = (("stations", build_stations), ("grid", build_grid),
             ("ch_stations", build_ch_stations), ("ch_grid", build_ch_grid))
    for name, fn in parts:
        try:
            report[name] = fn(out_dir, prev_dir)
            ok += 1
            print(f"{name}: {report[name]}")
        except Exception as e:                       # keep going, report it
            report[name] = {"error": f"{type(e).__name__}: {e}"}
            print(f"{name} FAILED: {e}", file=sys.stderr)
    write_json(out_dir, "meta.json", report, compact=False)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
