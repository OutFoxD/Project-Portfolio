#!/usr/bin/env python3
"""
precompute.py - builds the cached data behind protofox.at/shroomzn

Runs in GitHub Actions (.github/workflows/shroomzn-data.yml), a few times a day.
Writes into OUT_DIR (default: ./out):

  stations.json   every active Austrian klima-v2-1d station with its Bundesland
                  and the last ~46 daily precipitation sums [mm]
  grid_7d.png     SPARTACUS v3 1 km precipitation sums, colour-classed with the
  grid_14d.png    Klimamonitoring scale, as transparent overlays aligned to
  grid_30d.png    Web Mercator so Leaflet can drop them straight onto the map
  grid_45d.png
  grid.json       bounds + period per overlay, area mean per Bundesland
  meta.json       when it ran and what went wrong, if anything

The station part and the grid part are independent: if one fails, the other
is still written, and the workflow keeps the previous files for the failed one.

Data: GeoSphere Austria Data Hub, CC BY 4.0.
"""

import datetime as dt
import io
import json
import math
import os
import sys
import tempfile
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
    """GET with a polite pace (<4 req/s) and a readable error message."""
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


# ------------------------------------------------------------ stations ----

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


def through_date(dates, series, min_share=0.3):
    """Last date on which at least min_share of stations reported."""
    n = max(1, len(series))
    for i in range(len(dates) - 1, -1, -1):
        have = sum(1 for v in series.values() if i < len(v) and v[i] is not None)
        if have / n >= min_share:
            return dates[i]
    return None


def build_stations(out_dir):
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
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "source": STATION_DATASET,
        "dates": dates or [],
        "through": through_date(dates or [], {s["id"]: s["rr"] for s in stations}),
        "stations": stations,
    }
    with open(os.path.join(out_dir, "stations.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
    return {"stations": len(stations), "through": doc["through"]}


# ---------------------------------------------------------------- grid ----

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


def load_state_polygons():
    with open(os.path.join(HERE, "bundeslaender.geojson"), encoding="utf-8") as f:
        gj = json.load(f)
    states = {}
    for feat in gj["features"]:
        rings = []
        for poly in feat["geometry"]["coordinates"]:
            rings.extend(poly)       # holes are negligible at this scale
        states[feat["properties"]["name"]] = rings
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


def area_means(values, LAT, masks):
    """Area-weighted means per state (Mercator pixel area ~ cos^2 lat)."""
    wgt = np.cos(np.radians(LAT)) ** 2
    out = {}
    union = np.zeros(values.shape, dtype=bool)
    for name, m in masks.items():
        sel = m & np.isfinite(values)
        union |= sel
        if sel.any():
            out[name] = round(float((values[sel] * wgt[sel]).sum() / wgt[sel].sum()), 1)
    if union.any():
        out["Österreich"] = round(float((values[union] * wgt[union]).sum() / wgt[union].sum()), 1)
    return out


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


def build_grid(out_dir):
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
    austria = np.zeros(LON.shape, dtype=bool)
    for m in masks.values():
        austria |= m
    # close the hairline gaps the simplified borders leave between neighbouring states
    near = lambda a, ax, k: np.roll(a, k, ax) | np.roll(a, 2 * k, ax)
    austria |= (near(austria, 1, 1) & near(austria, 1, -1)) | (near(austria, 0, 1) & near(austria, 0, -1))

    doc = {
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "source": GRID_DATASET,
        "bounds": [[bbox[0], bbox[1]], [bbox[2], bbox[3]]],
        "last_day": last.isoformat(),
        "periods": {},
    }
    for p in PERIODS:
        want = [(last - dt.timedelta(days=k)).isoformat() for k in range(p)]
        if not all(w in days for w in want):
            continue
        stack = np.stack([days[w] for w in want])
        total = np.nansum(stack, axis=0)
        total[np.all(np.isnan(stack), axis=0)] = np.nan     # outside Austria
        vals = sample_nearest(total, xs, ys, X, Y)
        vals[~austria] = np.nan
        fname = f"grid_{p}d.png"
        write_overlay_png(classify(vals), os.path.join(out_dir, fname))
        doc["periods"][str(p)] = {
            "start": want[-1], "end": want[0], "file": fname,
            "mean": area_means(vals, LAT, masks),
            "max": round(float(np.nanmax(vals)), 1),
        }
    with open(os.path.join(out_dir, "grid.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    return {"periods": list(doc["periods"]), "last_day": doc["last_day"]}


# ---------------------------------------------------------------- main ----

def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.getcwd(), "out")
    os.makedirs(out_dir, exist_ok=True)
    report = {"ran": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    ok = 0
    for name, fn in (("stations", build_stations), ("grid", build_grid)):
        try:
            report[name] = fn(out_dir)
            ok += 1
            print(f"{name}: {report[name]}")
        except Exception as e:                       # keep going, report it
            report[name] = {"error": f"{type(e).__name__}: {e}"}
            print(f"{name} FAILED: {e}", file=sys.stderr)
    with open(os.path.join(out_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
