"""Temporary probe: what do the GeoSphere grids look like, how big may a request be,
and do the tile servers send CORS headers? Output goes to the skitour-probe-out branch."""
import io, json, os, sys, tempfile, time, traceback
import numpy as np
import requests
import xarray as xr

API = "https://dataset.api.hub.geosphere.at/v1"
BBOX = (46.2, 9.3, 48.0, 16.2)  # S, W, N, E
UA = {"User-Agent": "protofox.at/skitour probe (GitHub Actions)"}
OUT = sys.argv[1]
os.makedirs(OUT, exist_ok=True)
log = open(os.path.join(OUT, "probe.txt"), "w")


def p(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    log.write(s + "\n")
    log.flush()


def get(url, params=None):
    t = time.time()
    r = requests.get(url, params=params, headers=UA, timeout=300)
    p(f"GET {r.url[:220]} -> {r.status_code}, {len(r.content)/1e6:.2f} MB, {time.time()-t:.1f} s")
    if r.status_code != 200:
        p("   body:", r.text[:600])
    return r


def describe(raw, name):
    with tempfile.NamedTemporaryFile(suffix=".nc", delete=False) as f:
        f.write(raw)
        path = f.name
    with xr.open_dataset(path) as ds:
        p(f"--- {name}")
        p(ds)
        for v in ds.data_vars:
            a = ds[v].values
            p(f"   {v}: dims={ds[v].dims} shape={a.shape} dtype={a.dtype} attrs={dict(ds[v].attrs)}")
            p(f"      min={np.nanmin(a)} max={np.nanmax(a)} mean={np.nanmean(a)} nan={np.isnan(a).sum() if a.dtype.kind=='f' else 0}")
        for c in ds.coords:
            a = ds[c].values
            p(f"   coord {c}: shape={a.shape} first={a.flat[0]} last={a.flat[-1]}")
    os.unlink(path)


def bbox():
    return ",".join(str(v) for v in BBOX)


try:
    # ---------------- NWP v2 1 km forecast
    meta = get(f"{API}/grid/forecast/nwp-v2-1h-1km/metadata").json()
    p(json.dumps({k: v for k, v in meta.items() if k != "parameters"}, indent=1)[:3000])
    ref = meta.get("last_forecast_reftime")
    p("reftime", ref)
    params = "sf,tp,snowlmt,10u,10v,10fg,2t"
    r = get(f"{API}/grid/forecast/nwp-v2-1h-1km", {
        "parameters": params, "bbox": bbox(), "output_format": "netcdf",
        "forecast_offset": 0, "start": None, "end": None})
    if r.status_code == 200:
        describe(r.content, "nwp-v2 all hours?")
    # explicit short time window
    for hours in (3, 12, 24):
        t0 = ref[:16]
        import datetime as dt
        t0d = dt.datetime.fromisoformat(ref.replace("Z", "+00:00"))
        t1 = (t0d + dt.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M")
        r = get(f"{API}/grid/forecast/nwp-v2-1h-1km", {
            "parameters": params, "bbox": bbox(), "output_format": "netcdf",
            "start": t0d.strftime("%Y-%m-%dT%H:%M"), "end": t1})
        if r.status_code == 200 and hours == 3:
            describe(r.content, f"nwp-v2 {hours}h")
    # older reference time
    r = get(f"{API}/grid/forecast/nwp-v2-1h-1km", {
        "parameters": "sf,snowlmt", "bbox": bbox(), "output_format": "netcdf", "forecast_offset": 3,
        "start": None})
    if r.status_code == 200:
        describe(r.content, "nwp-v2 offset 3")
except Exception:
    p(traceback.format_exc())

try:
    # ---------------- INCA analysis
    meta = get(f"{API}/grid/historical/inca-v1-1h-1km/metadata").json()
    end = meta["end_time"][:16]
    p("inca end", end)
    import datetime as dt
    e = dt.datetime.fromisoformat(end)
    for hours in (2, 24, 72):
        s = (e - dt.timedelta(hours=hours - 1)).strftime("%Y-%m-%dT%H:%M")
        r = get(f"{API}/grid/historical/inca-v1-1h-1km", {
            "parameters": "RR,T2M,TD2M,UU,VV", "bbox": bbox(), "output_format": "netcdf",
            "start": s, "end": end})
        if r.status_code == 200 and hours == 2:
            describe(r.content, "inca 2h")
except Exception:
    p(traceback.format_exc())

try:
    # ---------------- SNOWGRID
    meta = get(f"{API}/grid/historical/snowgrid_cl-v2-1d-1km/metadata").json()
    end = meta["end_time"][:16]
    p("snowgrid end", end)
    r = get(f"{API}/grid/historical/snowgrid_cl-v2-1d-1km", {
        "parameters": "snow_depth,swe_tot", "bbox": bbox(), "output_format": "netcdf",
        "start": end, "end": end})
    if r.status_code == 200:
        describe(r.content, "snowgrid")
except Exception:
    p(traceback.format_exc())

# ---------------- tile servers / CORS
for url in [
    "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/12/2170/1435.png",
    "https://elevation-tiles-prod.s3.amazonaws.com/terrarium/14/8682/5742.png",
    "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/15/17364/11484.png",
    "https://tile.opentopomap.org/12/2170/1435.png",
    "https://a.tile.opentopomap.org/12/2170/1435.png",
    "https://mapsneu.wien.gv.at/basemap/bmaphidpi/normal/google3857/12/1435/2170.jpeg",
    "https://mapsneu.wien.gv.at/basemap/bmaporthofoto30cm/normal/google3857/12/1435/2170.jpeg",
    "https://mapsneu.wien.gv.at/basemap/bmapgelaende/grau/google3857/12/1435/2170.jpeg",
    "https://mapsneu.wien.gv.at/basemap/bmapoberflaeche/grau/google3857/12/1435/2170.jpeg",
    "https://wmts.geo.admin.ch/1.0.0/ch.swisstopo.pixelkarte-farbe/default/current/3857/12/2170/1435.jpeg",
    "https://wmts.geo.admin.ch/1.0.0/ch.swisstopo.pixelkarte-farbe/default/current/3857/12/2140/1440.jpeg",
    "https://tiles.bergfex.at/styles/bergfex-osm/12/2170/1435.jpg",
]:
    try:
        r = requests.get(url, headers={**UA, "Origin": "https://protofox.at"}, timeout=30)
        p(url, r.status_code, r.headers.get("content-type"), len(r.content),
          "ACAO=", r.headers.get("access-control-allow-origin"))
    except Exception as ex:
        p(url, "ERR", ex)

# GeoSphere CORS
r = requests.get(f"{API}/grid/forecast/nwp-v2-1h-1km/metadata", headers={**UA, "Origin": "https://protofox.at"})
p("geosphere ACAO", r.headers.get("access-control-allow-origin"))
log.close()
