#!/usr/bin/env python3
"""
precompute.py - builds the data behind protofox.at/skitour

Runs in GitHub Actions (.github/workflows/skitour-data.yml):

    python precompute.py OUT_DIR [PREVIOUS_DATA_DIR] [CACHE_DIR]

All sources are GeoSphere Austria Data Hub grids (CC BY 4.0), 1 km:

  past 24/48/72 h   INCA analysis inca-v1-1h-1km (RR, T2M, TD2M, UU, VV)
                    - precipitation is split into snow and rain with the
                      wet-bulb temperature (all snow at Tw <= 0 C, all rain
                      at Tw >= 2 C), and the snow line is estimated from Tw
                      and the terrain height of each cell
  next 24/48 h      C-LAEF/AROME forecast nwp-v2-1h-1km (sf, tp, snowlmt,
                    10u, 10v, 10fg, 2t)
  snow depth        SNOWGRID snowgrid_cl-v2-1d-1km (snow_depth, newest day)

New snow in cm = snowfall water equivalent / fresh-snow density, the density
from air temperature (Hedstrom & Pomeroy 1998, 68-180 kg/m3).

Output: per time window two small RGB PNGs on a raster that is linear in Web
Mercator over BOUNDS (the page decodes and colours them itself):

  a_<win>.png   R new snow [cm] (255 = no data), G snow line [20 m] (0 = no
                precipitation), B precipitation [mm]
  b_<win>.png   R max hourly mean wind [km/h], G max gust [km/h] (255 = none),
                B drift direction the wind comes FROM [2 deg] (255 = calm)
  snowdepth.png R*256+G snow depth [cm], B 255 = no data
  meta.json     windows, times, sources, errors

Hourly fields are cached in CACHE_DIR (actions/cache), so a run only
downloads the hours it doesn't have yet.
"""

import datetime as dt
import glob
import json
import math
import os
import re
import shutil
import sys
import tempfile
import time
import traceback

import numpy as np

API = "https://dataset.api.hub.geosphere.at/v1"
INCA = "inca-v1-1h-1km"
NWP = "nwp-v2-1h-1km"
SNOWGRID = "snowgrid_cl-v2-1d-1km"
TERRARIUM = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"

BOUNDS = (46.2, 9.3, 48.0, 16.2)     # south, west, north, east: the Eastern Alps
WIDTH = 690                          # px, ~0.75 km per px
PAST = (24, 48, 72)                  # hours before the newest analysis hour
FUTURE = (24, 48)                    # hours after it
MAX_POINTS = 9_000_000               # API limit is 10 M values per request
USER_AGENT = "protofox.at/skitour precompute (GitHub Actions)"

TW_SNOW, TW_RAIN = 0.0, 2.0          # wet-bulb range of the snow/rain mix [C]
TW_LINE = 1.0                        # 50 % snow -> snow line
LAPSE_W = 0.0055                     # wet-bulb lapse rate in precipitation [K/m]
DRIFT_MIN = 4.2                      # m/s (15 km/h): loose snow starts to move

NWP_SNOWLMT_ABOVE_GROUND = None      # decided from the data in nwp_diag(), see there


# ---------------------------------------------------------------- utils ----

def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%MZ")


def now_iso():
    return iso(dt.datetime.now(dt.timezone.utc))


def parse_time(s):
    """'2026-10-14T06:00+00:00' -> naive UTC datetime."""
    s = str(s).replace("Z", "+00:00")
    t = dt.datetime.fromisoformat(s)
    if t.tzinfo:
        t = t.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return t


def hours(t0, t1):
    """Hourly datetimes t0..t1 inclusive."""
    out, t = [], t0
    while t <= t1:
        out.append(t)
        t += dt.timedelta(hours=1)
    return out


class ApiError(RuntimeError):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status = status


_last = 0.0


def api_get(url, params=None, as_json=True, timeout=300):
    """GeoSphere GET, paced below 4 req/s, backs off on 429."""
    import requests
    global _last
    for attempt in range(4):
        wait = 0.3 - (time.time() - _last)
        if wait > 0:
            time.sleep(wait)
        _last = time.time()
        try:
            r = requests.get(url, params=params, timeout=timeout, headers={"User-Agent": USER_AGENT})
        except requests.RequestException:
            if attempt == 3:
                raise
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code == 429 and attempt < 3:
            time.sleep(65)
            continue
        if r.status_code >= 500 and attempt < 3:
            time.sleep(10 * (attempt + 1))
            continue
        break
    if r.status_code != 200:
        try:
            msg = r.json().get("detail") or r.text[:300]
        except Exception:
            msg = r.text[:300]
        raise ApiError(r.status_code, f"{r.status_code}: {msg}")
    return r.json() if as_json else r.content


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


def fetch_hours(kind, dataset, params, t0, t1, cells, extra=None):
    """Download a grid for hours t0..t1 in chunks under the size limit.
    Yields xarray datasets, oldest first."""
    per_hour = len(params) * cells
    step = max(1, MAX_POINTS // per_hour)
    t = t0
    while t <= t1:
        e = min(t1, t + dt.timedelta(hours=step - 1))
        q = {
            "parameters": ",".join(params),
            "bbox": ",".join(f"{v:.4f}" for v in BOUNDS),
            "output_format": "netcdf",
            "start": t.strftime("%Y-%m-%dT%H:%M"),
            "end": e.strftime("%Y-%m-%dT%H:%M"),
        }
        q.update(extra or {})
        try:
            raw = api_get(f"{API}/grid/{kind}/{dataset}", q, as_json=False)
        except ApiError as err:
            m = re.search(r"You requested (\d+)", str(err))
            if err.status == 400 and m and step > 1:
                n = (e - t).total_seconds() / 3600 + 1
                step = max(1, int(n * MAX_POINTS / int(m.group(1))))
                continue
            raise
        log(f"  {dataset} {q['start']}..{q['end']}: {len(raw) / 1e6:.1f} MB")
        yield open_nc(raw)
        t = e + dt.timedelta(hours=1)


# ------------------------------------------------------------ geometry ----

def merc_y(lat_deg):
    return np.log(np.tan(np.pi / 4 + np.radians(lat_deg) / 2))


def inv_merc_y(y):
    return np.degrees(2 * np.arctan(np.exp(y)) - np.pi / 2)


def target_grid():
    """Pixel-centre LON, LAT of the output raster (row 0 = north), linear in Web Mercator."""
    s, w, n, e = BOUNDS
    lon = w + (np.arange(WIDTH) + 0.5) * (e - w) / WIDTH
    y_n, y_s = merc_y(n), merc_y(s)
    height = int(round(WIDTH * (y_n - y_s) / np.radians(e - w)))
    y = y_n - (np.arange(height) + 0.5) * (y_n - y_s) / height
    return np.meshgrid(lon, inv_merc_y(y))


def nearest_index(coord, values):
    """Index of the nearest entry of a regular 1-D coordinate array (either order); -1 outside."""
    c0, dc = float(coord[0]), float(coord[1] - coord[0])
    i = np.rint((values - c0) / dc).astype(np.int64)
    i[(i < 0) | (i >= len(coord))] = -1
    return i


class Sampler:
    """Nearest-neighbour lookup from a source grid (1-D x/y in some CRS) onto the target raster."""

    def __init__(self, xs, ys, LON, LAT, crs=None):
        if crs and crs != "EPSG:4326":
            from pyproj import Transformer
            X, Y = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform(LON, LAT)
        else:
            X, Y = LON, LAT
        self.ix = nearest_index(np.asarray(xs, dtype=np.float64), X)
        self.iy = nearest_index(np.asarray(ys, dtype=np.float64), Y)
        self.ok = (self.ix >= 0) & (self.iy >= 0)

    def __call__(self, arr):
        out = np.full(self.ix.shape, np.nan, dtype=np.float32)
        out[self.ok] = arr[self.iy[self.ok], self.ix[self.ok]]
        return out


# --------------------------------------------------------------- terrain ----

def lonlat_to_global_px(lon, lat, z):
    n = 256 * 2 ** z
    x = (np.asarray(lon) + 180.0) / 360.0 * n
    y = (1 - merc_y(np.asarray(lat)) / np.pi) / 2 * n
    return x, y


class Dem:
    """Terrain heights from the AWS terrain tiles (Terrarium PNG), smoothed to ~1 km,
    so they compare with the 1 km weather grids. Cached as one mosaic."""

    Z = 9

    def __init__(self, cache_dir):
        path = os.path.join(cache_dir, f"dem_z{self.Z}.npz") if cache_dir else None
        if path and os.path.exists(path):
            d = np.load(path)
            self.arr, self.x0, self.y0 = d["arr"], int(d["x0"]), int(d["y0"])
            return
        import requests
        from PIL import Image
        from scipy.ndimage import gaussian_filter
        s, w, n, e = BOUNDS
        gx0, gy0 = lonlat_to_global_px(w - 0.2, n + 0.2, self.Z)
        gx1, gy1 = lonlat_to_global_px(e + 0.2, s - 0.2, self.Z)
        tx0, ty0, tx1, ty1 = int(gx0 // 256), int(gy0 // 256), int(gx1 // 256), int(gy1 // 256)
        arr = np.zeros(((ty1 - ty0 + 1) * 256, (tx1 - tx0 + 1) * 256), dtype=np.float32)
        sess = requests.Session()
        sess.headers["User-Agent"] = USER_AGENT
        for ty in range(ty0, ty1 + 1):
            for tx in range(tx0, tx1 + 1):
                url = TERRARIUM.format(z=self.Z, x=tx, y=ty)
                for attempt in range(4):
                    try:
                        r = sess.get(url, timeout=60)
                        r.raise_for_status()
                        break
                    except Exception:
                        if attempt == 3:
                            raise
                        time.sleep(2 ** attempt)
                from io import BytesIO
                px = np.asarray(Image.open(BytesIO(r.content)).convert("RGB"), dtype=np.float32)
                elev = px[..., 0] * 256 + px[..., 1] + px[..., 2] / 256 - 32768
                arr[(ty - ty0) * 256:(ty - ty0 + 1) * 256, (tx - tx0) * 256:(tx - tx0 + 1) * 256] = elev
        arr = np.maximum(arr, 0)
        self.arr = gaussian_filter(arr, 2.5).astype(np.float32)    # ~200 m px -> ~1 km footprint
        self.x0, self.y0 = tx0 * 256, ty0 * 256
        if path:
            np.savez_compressed(path, arr=self.arr, x0=self.x0, y0=self.y0)
        log(f"terrain mosaic {self.arr.shape}, {(tx1 - tx0 + 1) * (ty1 - ty0 + 1)} tiles")

    def at(self, lon, lat):
        x, y = lonlat_to_global_px(lon, lat, self.Z)
        ix = np.clip(np.rint(x - self.x0).astype(np.int64), 0, self.arr.shape[1] - 1)
        iy = np.clip(np.rint(y - self.y0).astype(np.int64), 0, self.arr.shape[0] - 1)
        return self.arr[iy, ix]


# ---------------------------------------------------------------- physics ----

def wet_bulb(T, Td):
    """Wet-bulb temperature [C] from air and dew-point temperature (Stull 2011)."""
    Td = np.minimum(Td, T)
    rh = 100 * np.exp(17.625 * Td / (243.04 + Td) - 17.625 * T / (243.04 + T))
    rh = np.clip(rh, 1, 100)
    return (T * np.arctan(0.151977 * np.sqrt(rh + 8.313659)) + np.arctan(T + rh)
            - np.arctan(rh - 1.676331) + 0.00391838 * rh ** 1.5 * np.arctan(0.023101 * rh) - 4.686035)


def snow_density(T):
    """Fresh-snow density [kg/m3] from air temperature (Hedstrom & Pomeroy 1998)."""
    return 67.9 + 51.3 * np.exp(np.clip(T, -20, 2) / 2.6)


# ------------------------------------------------------- hourly fields ----
# One hour on the target raster: snow [cm], p [mm], sl [m] (NaN = no precipitation),
# u, v [m/s], g gust [m/s] (NaN = not available). Stored quantised in the cache.

def save_hour(path, f):
    def q(a, scale, dtype, nan):
        b = np.where(np.isfinite(a), np.rint(a * scale), nan)
        info = np.iinfo(dtype)
        return np.clip(b, info.min, info.max).astype(dtype)
    np.savez_compressed(
        path,
        snow=q(f["snow"], 10, np.uint16, 65535), p=q(f["p"], 10, np.uint16, 65535),
        sl=q(f["sl"], 1, np.uint16, 0), u=q(f["u"], 2, np.int8, -128), v=q(f["v"], 2, np.int8, -128),
        g=q(f["g"], 2, np.uint8, 255))


def load_hour(path):
    d = np.load(path)

    def uq(a, scale, nan):
        out = a.astype(np.float32) / scale
        out[a == nan] = np.nan
        return out
    return {"snow": uq(d["snow"], 10, 65535), "p": uq(d["p"], 10, 65535), "sl": uq(d["sl"], 1, 0),
            "u": uq(d["u"], 2, -128), "v": uq(d["v"], 2, -128), "g": uq(d["g"], 2, 255)}


def hour_name(t):
    return t.strftime("%Y%m%d%H") + ".npz"


# -------------------------------------------------------------- INCA ----

INCA_PARAMS = ["RR", "T2M", "TD2M", "UU", "VV"]


def inca_hours(cache, LON, LAT, dem, need, refresh_after):
    """Make sure every hour in `need` is in cache/inca; re-download hours after refresh_after."""
    d = os.path.join(cache, "inca")
    os.makedirs(d, exist_ok=True)
    missing = [t for t in need if t > refresh_after or not os.path.exists(os.path.join(d, hour_name(t)))]
    if not missing:
        return
    # consecutive runs -> one download each
    runs, cur = [], [missing[0]]
    for t in missing[1:]:
        if t - cur[-1] == dt.timedelta(hours=1):
            cur.append(t)
        else:
            runs.append(cur)
            cur = [t]
    runs.append(cur)
    sampler = None
    z = None
    for run in runs:
        for ds in fetch_hours("historical", INCA, INCA_PARAMS, run[0], run[-1], 208 * 533):
            if sampler is None:
                sampler = Sampler(ds["x"].values, ds["y"].values, LON, LAT, "EPSG:31287")
                z = dem.at(ds["lon"].values, ds["lat"].values)
            for i, tv in enumerate(ds["time"].values):
                t = parse_time(str(tv)[:19])
                rr = np.maximum(np.nan_to_num(ds["RR"].values[i]), 0)
                T = ds["T2M"].values[i]
                tw = wet_bulb(T, ds["TD2M"].values[i])
                frac = np.clip((TW_RAIN - tw) / (TW_RAIN - TW_SNOW), 0, 1)
                snow = rr * frac / snow_density(T) * 100
                sl = np.where(rr >= 0.1, np.clip(z + (tw - TW_LINE) / LAPSE_W, 0, 4800), np.nan)
                f = {"snow": sampler(snow), "p": sampler(rr), "sl": sampler(sl),
                     "u": sampler(ds["UU"].values[i]), "v": sampler(ds["VV"].values[i]),
                     "g": np.full(LON.shape, np.nan, np.float32)}
                save_hour(os.path.join(d, hour_name(t)), f)


# --------------------------------------------------------------- NWP ----

NWP_PARAMS = ["sf", "tp", "snowlmt", "10u", "10v", "10fg", "2t"]


def nwp_hours(cache, LON, LAT, dem, ref, diag):
    """Cache all hours ref+1..end of one forecast run (once per run)."""
    d = os.path.join(cache, "nwp", ref.strftime("%Y%m%d%H"))
    done = os.path.join(d, "complete")
    if os.path.exists(done):
        return d, json.load(open(done))
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    meta = api_get(f"{API}/grid/forecast/{NWP}/metadata")
    length = int(meta.get("forecast_length") or 61)
    t0, t1 = ref + dt.timedelta(hours=1), ref + dt.timedelta(hours=length - 1)
    sampler = None
    first, last = None, None
    for ds in fetch_hours("forecast", NWP, NWP_PARAMS, t0, t1, 200 * 511):
        if sampler is None:
            sampler = Sampler(ds["lon"].values, ds["lat"].values, LON, LAT)
            LON2, LAT2 = np.meshgrid(ds["lon"].values, ds["lat"].values)
            znwp = dem.at(LON2, LAT2)
            above_ground = nwp_diag(ds, znwp, diag)
        for i, tv in enumerate(ds["time"].values):
            t = parse_time(str(tv)[:19])
            if t <= ref:
                continue
            tp = np.maximum(np.nan_to_num(ds["tp"].values[i]), 0)
            sf = np.maximum(np.nan_to_num(ds["sf"].values[i]), 0)
            T = ds["2t"].values[i]
            lmt = ds["snowlmt"].values[i] + (znwp if above_ground else 0)
            snow = sf / snow_density(T) * 100
            sl = np.where(tp >= 0.1, np.clip(lmt, 0, 4800), np.nan)
            f = {"snow": sampler(snow), "p": sampler(tp), "sl": sampler(sl),
                 "u": sampler(ds["10u"].values[i]), "v": sampler(ds["10v"].values[i]),
                 "g": sampler(ds["10fg"].values[i])}
            save_hour(os.path.join(d, hour_name(t)), f)
            first = first or t
            last = t
    info = {"ref": iso(ref), "first": iso(first), "last": iso(last), "snowlmt_above_ground": bool(above_ground)}
    json.dump(info, open(done, "w"))
    return d, info


def nwp_diag(ds, znwp, diag):
    """Is snowlmt metres above sea level or above ground? The attribute says 'height above
    ground where falling snow melts', but the values look like a smooth free-atmosphere
    height. The one that's smooth in space is the right reading: a field above ground
    carries the whole terrain (with a minus sign) in it."""
    from scipy.ndimage import gaussian_filter
    i = min(3, ds.sizes["time"] - 1)
    a = np.asarray(ds["snowlmt"].values[i], dtype=np.float64)

    def rough(x):
        return float(np.nanstd(x - gaussian_filter(x, 3)))
    r_asl, r_agl = rough(a), rough(a + znwp)
    corr = float(np.corrcoef(a.ravel(), znwp.ravel())[0, 1])
    above_ground = r_agl < 0.6 * r_asl
    diag["snowlmt"] = {"roughness_as_is": round(r_asl, 1), "roughness_plus_terrain": round(r_agl, 1),
                       "corr_with_terrain": round(corr, 3), "treated_as_above_ground": bool(above_ground),
                       "min": round(float(np.nanmin(a))), "max": round(float(np.nanmax(a)))}
    log("snowlmt check:", diag["snowlmt"])
    global NWP_SNOWLMT_ABOVE_GROUND
    NWP_SNOWLMT_ABOVE_GROUND = above_ground
    return above_ground


# ------------------------------------------------------------- windows ----

def aggregate(files):
    """Sum/mix the hourly fields of one window."""
    from scipy.ndimage import gaussian_filter
    snow = p = psl = pw = None
    smax = gmax = None
    U = V = W2u = W2v = None
    for path in files:
        h = load_hour(path)
        if snow is None:
            shape = h["snow"].shape
            snow, p, psl, pw = (np.zeros(shape, np.float64) for _ in range(4))
            smax, gmax = np.zeros(shape, np.float64), np.full(shape, np.nan)
            U, V, W2u, W2v = (np.zeros(shape, np.float64) for _ in range(4))
            valid = np.isfinite(h["snow"])
        snow += np.nan_to_num(h["snow"])
        hp = np.nan_to_num(h["p"])
        p += hp
        ok = np.isfinite(h["sl"]) & (hp >= 0.1)
        psl[ok] += hp[ok] * h["sl"][ok]
        pw[ok] += hp[ok]
        u, v = np.nan_to_num(h["u"]), np.nan_to_num(h["v"])
        s = np.hypot(u, v)
        smax = np.maximum(smax, s)
        if np.isfinite(h["g"]).any():
            gmax = np.fmax(gmax, h["g"])
        # direction: hours with drifting wind count with the cube of the excess speed
        # (transport rate ~ u^3); calm windows fall back to a speed^2 weighting
        w = np.maximum(s - DRIFT_MIN, 0) ** 3
        su = np.where(s > 0, u / np.maximum(s, 1e-6), 0)
        sv = np.where(s > 0, v / np.maximum(s, 1e-6), 0)
        U += w * su
        V += w * sv
        W2u += s ** 2 * su
        W2v += s ** 2 * sv
    if snow is None:
        return None
    calm = (U == 0) & (V == 0)
    U = np.where(calm, W2u, U)
    V = np.where(calm, W2v, V)
    # smooth the direction field (~2 km) so the arrows show the flow, not every gully
    U, V = gaussian_filter(U, 2.5), gaussian_filter(V, 2.5)
    direction = (np.degrees(np.arctan2(-U, -V)) + 360) % 360        # where it comes FROM
    sl = np.where((pw > 0) & (p >= 0.5), psl / np.maximum(pw, 1e-9), np.nan)
    sl = smooth_nan(sl, 2.0)
    return {"snow": snow, "p": p, "sl": sl, "smax": smax * 3.6, "gmax": gmax * 3.6,
            "dir": direction, "valid": valid}


def smooth_nan(a, sigma):
    """Gaussian smoothing that ignores NaN and keeps them NaN."""
    from scipy.ndimage import gaussian_filter
    m = np.isfinite(a)
    num = gaussian_filter(np.where(m, a, 0), sigma)
    den = gaussian_filter(m.astype(np.float64), sigma)
    out = num / np.maximum(den, 1e-9)
    out[~m] = np.nan
    return out


def write_rgb(path, R, G, B):
    from PIL import Image
    img = Image.fromarray(np.dstack([R, G, B]).astype(np.uint8), "RGB")
    img.save(path, optimize=True)


def u8(a, lo=0, hi=254):
    return np.clip(np.rint(np.nan_to_num(a)), lo, hi).astype(np.uint8)


def write_window(out_dir, wid, agg):
    valid = agg["valid"]
    R = u8(agg["snow"])
    R[~valid] = 255
    G = np.where(np.isfinite(agg["sl"]), u8(agg["sl"] / 20, 1, 254), 0).astype(np.uint8)
    B = u8(agg["p"])
    write_rgb(os.path.join(out_dir, f"a_{wid}.png"), R, G, B)
    R = u8(agg["smax"])
    R[~valid] = 255
    G = np.where(np.isfinite(agg["gmax"]), u8(agg["gmax"]), 255).astype(np.uint8)
    B = (np.rint(agg["dir"] / 2).astype(np.int64) % 180).astype(np.uint8)
    B[agg["smax"] < 3] = 255
    write_rgb(os.path.join(out_dir, f"b_{wid}.png"), R, G, B)
    v = valid
    q = lambda a, p: round(float(np.nanpercentile(a[v], p)), 1) if v.any() else None
    return {"a": f"a_{wid}.png", "b": f"b_{wid}.png",
            "stats": {"snow_max": q(agg["snow"], 100), "snow_p99": q(agg["snow"], 99),
                      "wind_max": q(agg["smax"], 100), "wind_p95": q(agg["smax"], 95),
                      "sl_median": round(float(np.nanmedian(agg["sl"]))) if np.isfinite(agg["sl"]).any() else None}}


# ----------------------------------------------------------- snow depth ----

def build_snowdepth(out_dir, LON, LAT):
    meta = api_get(f"{API}/grid/historical/{SNOWGRID}/metadata")
    day = str(meta["end_time"])[:10]
    raw = api_get(f"{API}/grid/historical/{SNOWGRID}", {
        "parameters": "snow_depth", "bbox": ",".join(f"{v:.4f}" for v in BOUNDS),
        "output_format": "netcdf", "start": f"{day}T00:00", "end": f"{day}T00:00"}, as_json=False)
    ds = open_nc(raw)
    sampler = Sampler(ds["x"].values, ds["y"].values, LON, LAT, "EPSG:3416")
    cm = sampler(ds["snow_depth"].values[0] * 100)
    ok = np.isfinite(cm)
    c = np.clip(np.rint(np.nan_to_num(cm)), 0, 65535).astype(np.int64)
    R, G = (c >> 8).astype(np.uint8), (c & 255).astype(np.uint8)
    B = np.where(ok, 0, 255).astype(np.uint8)
    R[~ok] = 0
    G[~ok] = 0
    write_rgb(os.path.join(out_dir, "snowdepth.png"), R, G, B)
    return {"file": "snowdepth.png", "day": day, "source": SNOWGRID,
            "max_cm": int(c[ok].max()) if ok.any() else 0}


# ------------------------------------------------------------------ main ----

def main():
    out_dir = sys.argv[1]
    prev_dir = sys.argv[2] if len(sys.argv) > 2 else None
    cache = sys.argv[3] if len(sys.argv) > 3 else tempfile.mkdtemp()
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(cache, exist_ok=True)
    LON, LAT = target_grid()
    H, W = LON.shape
    s, w, n, e = BOUNDS
    meta = {"generated": now_iso(), "bounds": [[s, w], [n, e]], "size": [W, H],
            "windows": [], "errors": {}, "diag": {}, "attribution": "GeoSphere Austria (CC BY 4.0)"}
    prev = {}
    if prev_dir and os.path.exists(os.path.join(prev_dir, "meta.json")):
        try:
            prev = json.load(open(os.path.join(prev_dir, "meta.json")))
        except Exception:
            prev = {}

    dem = Dem(cache)

    # newest analysis hour = the common "now" of all windows
    t0 = None
    try:
        im = api_get(f"{API}/grid/historical/{INCA}/metadata")
        t0 = parse_time(im["end_time"])
        need = hours(t0 - dt.timedelta(hours=max(PAST) - 1), t0)
        inca_hours(cache, LON, LAT, dem, need, refresh_after=t0 - dt.timedelta(hours=3))
        d = os.path.join(cache, "inca")
        for hrs in PAST:
            ts = hours(t0 - dt.timedelta(hours=hrs - 1), t0)
            files = [os.path.join(d, hour_name(t)) for t in ts if os.path.exists(os.path.join(d, hour_name(t)))]
            agg = aggregate(files)
            if agg is None:
                continue
            wid = f"p{hrs}"
            win = write_window(out_dir, wid, agg)
            win.update({"id": wid, "kind": "past", "hours": hrs, "source": INCA,
                        "start": iso(ts[0] - dt.timedelta(hours=1)), "end": iso(t0), "hours_ok": len(files)})
            meta["windows"].append(win)
            log(wid, win["stats"])
        # prune old hours
        for path in glob.glob(os.path.join(d, "*.npz")):
            t = dt.datetime.strptime(os.path.basename(path)[:10], "%Y%m%d%H")
            if t < t0 - dt.timedelta(hours=max(PAST) + 6):
                os.unlink(path)
        meta["inca_end"] = iso(t0)
    except Exception as ex:
        traceback.print_exc()
        meta["errors"]["inca"] = str(ex)[:300]

    try:
        nm = api_get(f"{API}/grid/forecast/{NWP}/metadata")
        ref = parse_time(nm["last_forecast_reftime"])
        d, info = nwp_hours(cache, LON, LAT, dem, ref, meta["diag"])
        if t0 is None:
            t0 = parse_time(info["first"]) - dt.timedelta(hours=1)
        for hrs in FUTURE:
            ts = hours(t0 + dt.timedelta(hours=1), t0 + dt.timedelta(hours=hrs))
            files = [os.path.join(d, hour_name(t)) for t in ts if os.path.exists(os.path.join(d, hour_name(t)))]
            agg = aggregate(files)
            if agg is None:
                continue
            wid = f"f{hrs}"
            win = write_window(out_dir, wid, agg)
            got = [t for t in ts if os.path.exists(os.path.join(d, hour_name(t)))]
            win.update({"id": wid, "kind": "forecast", "hours": hrs, "source": NWP, "ref": info["ref"],
                        "start": iso(t0), "end": iso(got[-1]), "hours_ok": len(files)})
            meta["windows"].append(win)
            log(wid, win["stats"])
        meta["nwp_ref"] = info["ref"]
        meta["diag"].setdefault("snowlmt_above_ground", info.get("snowlmt_above_ground"))
        for old in glob.glob(os.path.join(cache, "nwp", "*")):
            if os.path.basename(old) < (ref - dt.timedelta(hours=6)).strftime("%Y%m%d%H"):
                shutil.rmtree(old, ignore_errors=True)
    except Exception as ex:
        traceback.print_exc()
        meta["errors"]["nwp"] = str(ex)[:300]

    try:
        meta["snowdepth"] = build_snowdepth(out_dir, LON, LAT)
    except Exception as ex:
        traceback.print_exc()
        meta["errors"]["snowdepth"] = str(ex)[:300]
        if prev.get("snowdepth"):
            meta["snowdepth"] = {**prev["snowdepth"], "stale": True}

    # a part that failed keeps the previous run's windows (the workflow keeps their files)
    have = {w["id"] for w in meta["windows"]}
    for w in prev.get("windows", []):
        if w["id"] not in have:
            meta["windows"].append({**w, "stale": True})
    order = {f"p{h}": i for i, h in enumerate(sorted(PAST, reverse=True))}
    order.update({f"f{h}": 10 + i for i, h in enumerate(FUTURE)})
    meta["windows"].sort(key=lambda w: order.get(w["id"], 99))
    meta["t0"] = iso(t0) if t0 else prev.get("t0")

    with open(os.path.join(out_dir, "meta.json"), "w") as f:
        json.dump(meta, f, indent=1)
    log("done:", [w["id"] for w in meta["windows"]], "errors:", meta["errors"])
    return 1 if not meta["windows"] else 0


if __name__ == "__main__":
    sys.exit(main())
