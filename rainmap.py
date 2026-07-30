#!/usr/bin/env python3
"""
rainmap.py — Klimamonitoring-style precipitation map with a free time range

Data: GeoSphere Datahub gridded dataset SPARTACUS v2 (1 km, daily),
parameter RR = daily precipitation sum [mm].
    https://dataset.api.hub.geosphere.at/v1/grid/historical/spartacus-v2-1d-1km

The grids come in the Austria Lambert projection (metres). This script
reprojects them to a regular lat/lon grid, so both the static PNG and the
Leaflet overlay line up with real geography.

Install:
    pip install streamlit requests numpy xarray netcdf4 matplotlib folium pillow pyproj

Run:
    streamlit run rainmap.py                       # interactive app
    python rainmap.py --days 14 --region muehlviertel
    python rainmap.py --start 2026-07-01 --end 2026-07-25 --region austria
"""

import argparse
import datetime as dt
import io
import json
import os
import sys
import tempfile

import numpy as np
import requests

API = "https://dataset.api.hub.geosphere.at/v1/grid/historical/spartacus-v2-1d-1km"
API_INCA = "https://dataset.api.hub.geosphere.at/v1/grid/historical/inca-v1-1h-1km"
PARAM = "RR"
TIMEOUT = 180
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "rainmap")

# Named regions: (lat_min, lon_min, lat_max, lon_max)
REGIONS = {
    "austria":       (46.2, 9.4, 49.1, 17.3),
    "muehlviertel":  (48.20, 13.70, 48.80, 15.00),   # north of Linz
    "linz50":        (47.85, 13.60, 48.75, 14.95),   # ~50 km around Linz
}

# Klimamonitoring colour classes (mm)
BOUNDS = [0, 2, 15, 30, 50, 75, 100, 150, 200, 250, 350, 450, 600]
COLORS = ["#ffffff", "#fdfdd0", "#e8f4a5", "#b8e28d", "#7fcdbb", "#41b6c4",
          "#1d91c0", "#225ea8", "#0c2c84", "#6a51a3", "#88419d", "#e31a1c",
          "#fc8d59"]  # last colour = >600 mm

CITIES = [
    ("Wien", 48.2082, 16.3738), ("Graz", 47.0707, 15.4395),
    ("Linz", 48.3069, 14.2858), ("Salzburg", 47.8095, 13.0550),
    ("Innsbruck", 47.2692, 11.4041), ("Klagenfurt", 46.6247, 14.3053),
    ("Bregenz", 47.5031, 9.7471), ("St. Pölten", 48.2047, 15.6256),
    ("Eisenstadt", 47.8456, 16.5219), ("Freistadt", 48.5112, 14.5050),
    ("Wels", 48.1575, 14.0250), ("Steyr", 48.0500, 14.4167),
    ("Rohrbach", 48.5731, 13.9903), ("Bad Zell", 48.3486, 14.6664),
    ("Enns", 48.2128, 14.4753), ("Perg", 48.2500, 14.6333),
]

# Overlay line data (fetched once, cached on disk)
LAENDER_URL = ("https://raw.githubusercontent.com/ginseng666/"
               "GeoJSON-TopoJSON-Austria/master/2021/simplified-95/"
               "laender_95_geo.json")
RIVERS_URL = ("https://raw.githubusercontent.com/nvkelso/"
              "natural-earth-vector/master/geojson/"
              "ne_10m_rivers_lake_centerlines.geojson")


# ----------------------------------------------------------------------
# Data download
# ----------------------------------------------------------------------

def _iso(t):
    """date -> 'YYYY-MM-DDT00:00', datetime -> 'YYYY-MM-DDTHH:MM'."""
    if isinstance(t, dt.datetime):
        return t.strftime("%Y-%m-%dT%H:%M")
    return f"{t.isoformat()}T00:00"


def fetch_grid(start, end, bbox, api=API):
    """Download RR grids as NetCDF and return an xarray.Dataset.
    `api` selects the dataset: API (SPARTACUS daily) or API_INCA (hourly)."""
    import xarray as xr
    params = {
        "parameters": PARAM,
        "start": _iso(start),
        "end": _iso(end),
        "bbox": ",".join(f"{v:.4f}" for v in bbox),
        "output_format": "netcdf",
    }
    r = requests.get(api, params=params, timeout=TIMEOUT)
    if r.status_code != 200:
        try:
            msg = r.json().get("message", r.text[:300])
        except Exception:
            msg = r.text[:300]
        raise RuntimeError(f"API error {r.status_code}: {msg}")
    with tempfile.NamedTemporaryFile(suffix=".nc", delete=False) as f:
        f.write(r.content)
        path = f.name
    return xr.open_dataset(path)


def _grid_crs(ds, var):
    """CRS of the SPARTACUS grid (Lambert conformal). Read from the file's
    CF grid_mapping if possible, otherwise fall back to EPSG:3416."""
    from pyproj import CRS
    gm_name = var.attrs.get("grid_mapping")
    if gm_name and gm_name in ds:
        try:
            return CRS.from_cf(dict(ds[gm_name].attrs))
        except Exception:
            pass
    for v in ds.variables:  # sometimes the mapping var isn't referenced
        if "grid_mapping_name" in ds[v].attrs:
            try:
                return CRS.from_cf(dict(ds[v].attrs))
            except Exception:
                pass
    return CRS.from_epsg(3416)  # ETRS89 / Austria Lambert


def total_precip_latlon(ds, bbox, res=0.012):
    """Sum RR over time and return (lat_1d, lon_1d, arr_2d, area_mean)
    on a regular lat/lon grid, reprojecting from Lambert if needed."""
    var = None
    for cand in ("RR", "rr"):
        if cand in ds:
            var = ds[cand]
            break
    if var is None:
        var = ds[list(ds.data_vars)[0]]
    total = var.sum(dim="time", skipna=True)
    A = np.asarray(total.values, dtype=float)

    ycoord = "y" if "y" in ds.coords else "lat"
    xcoord = "x" if "x" in ds.coords else "lon"
    y = np.asarray(ds[ycoord].values, dtype=float)
    x = np.asarray(ds[xcoord].values, dtype=float)

    if np.nanmax(np.abs(y)) <= 90 and np.nanmax(np.abs(x)) <= 360:
        # already geographic
        mean = float(np.nanmean(A))
        return y, x, A, mean

    # --- projected (metres) -> resample to regular lat/lon ---------------
    try:
        from pyproj import Transformer
    except ImportError:
        raise RuntimeError("The grid is in a projected CRS — please "
                           "`pip install pyproj` to reproject it.")
    crs = _grid_crs(ds, var)
    lat = np.arange(bbox[0], bbox[2] + res / 2, res)
    lon = np.arange(bbox[1], bbox[3] + res / 2, res)
    LO, LA = np.meshgrid(lon, lat)
    tr = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    X, Y = tr.transform(LO, LA)

    dx = np.median(np.diff(x))
    dy = np.median(np.diff(y))
    ix = np.round((X - x[0]) / dx).astype(int)
    iy = np.round((Y - y[0]) / dy).astype(int)
    ok = (ix >= 0) & (ix < len(x)) & (iy >= 0) & (iy < len(y))
    out = np.full(LA.shape, np.nan)
    out[ok] = A[iy[ok], ix[ok]]
    mean = float(np.nanmean(A))          # area mean from native grid
    return lat, lon, out, mean


# ----------------------------------------------------------------------
# Reference geometry (borders, rivers)
# ----------------------------------------------------------------------

def _cached_geojson(url, fname):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, fname)
    if not os.path.exists(path):
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        with open(path, "wb") as f:
            f.write(r.content)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _geojson_lines(gj):
    """Yield (lons, lats) arrays for every line/ring in a GeoJSON."""
    def rings(geom):
        t, c = geom.get("type"), geom.get("coordinates", [])
        if t == "LineString":
            yield c
        elif t in ("MultiLineString", "Polygon"):
            yield from c
        elif t == "MultiPolygon":
            for poly in c:
                yield from poly
    for feat in gj.get("features", []):
        for ring in rings(feat.get("geometry") or {}):
            arr = np.asarray(ring, dtype=float)
            if arr.ndim == 2 and arr.shape[0] > 1:
                yield arr[:, 0], arr[:, 1]


def draw_reference(ax, bbox):
    """Bundesländer borders, major rivers, cities — best effort."""
    lat_min, lon_min, lat_max, lon_max = bbox
    try:
        for lons, lats in _geojson_lines(
                _cached_geojson(LAENDER_URL, "laender.json")):
            ax.plot(lons, lats, color="#555555", lw=0.7, zorder=4)
    except Exception as e:
        print(f"(borders unavailable: {e})", file=sys.stderr)
    try:
        for lons, lats in _geojson_lines(
                _cached_geojson(RIVERS_URL, "rivers.json")):
            if (lons.max() < lon_min or lons.min() > lon_max or
                    lats.max() < lat_min or lats.min() > lat_max):
                continue
            ax.plot(lons, lats, color="#4a90d9", lw=0.6, alpha=0.8, zorder=3)
    except Exception as e:
        print(f"(rivers unavailable: {e})", file=sys.stderr)
    for name, la, lo in CITIES:
        if lat_min <= la <= lat_max and lon_min <= lo <= lon_max:
            ax.plot(lo, la, "o", color="#222222", ms=3, zorder=5)
            ax.annotate(name, (lo, la), xytext=(4, 3),
                        textcoords="offset points", fontsize=7, zorder=5)


# ----------------------------------------------------------------------
# Static map
# ----------------------------------------------------------------------

def _fmt(t):
    if isinstance(t, dt.datetime):
        return t.strftime("%d.%m.%Y %H:%M")
    return t.strftime("%d.%m.%Y")


def render_map(lat, lon, arr, mean, start, end, title_extra="", bbox=None):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import BoundaryNorm, ListedColormap

    cmap = ListedColormap(COLORS[:-1])
    cmap.set_over(COLORS[-1])
    norm = BoundaryNorm(BOUNDS, cmap.N)

    # true geographic proportions: 1° lon shrinks with cos(lat)
    coslat = np.cos(np.deg2rad(np.mean(lat)))
    dlat = float(lat.max() - lat.min())
    dlon = float(lon.max() - lon.min())
    width = 12.0
    height = np.clip(width * dlat / (dlon * coslat) + 0.8, 3.0, 10.0)

    fig, ax = plt.subplots(figsize=(width, height))
    mesh = ax.pcolormesh(lon, lat, arr, cmap=cmap, norm=norm, shading="auto",
                         zorder=2)
    ax.set_aspect(1.0 / coslat)
    if bbox is None:
        bbox = (float(lat.min()), float(lon.min()),
                float(lat.max()), float(lon.max()))
    draw_reference(ax, bbox)
    ax.set_xlim(bbox[1], bbox[3])
    ax.set_ylim(bbox[0], bbox[2])
    ax.set_title(
        f"Precipitation sum {_fmt(start)} – {_fmt(end)}"
        f"{title_extra}   |   Flächenmittel: {mean:.0f} mm",
        loc="left", fontsize=11,
    )
    ax.tick_params(labelsize=8)
    cb = fig.colorbar(mesh, ax=ax, ticks=BOUNDS, extend="max",
                      shrink=0.85, pad=0.02)
    cb.set_label("mm")
    fig.tight_layout()
    return fig


# ----------------------------------------------------------------------
# Interactive map (Leaflet / folium)
# ----------------------------------------------------------------------

def make_overlay_map(lat, lon, arr, mean, start, end, opacity=0.65):
    import folium
    import matplotlib.cm as cm
    from matplotlib.colors import BoundaryNorm, ListedColormap

    cmap = ListedColormap(COLORS[:-1])
    cmap.set_over(COLORS[-1])
    norm = BoundaryNorm(BOUNDS, cmap.N)

    rgba = cm.ScalarMappable(norm=norm, cmap=cmap).to_rgba(
        np.ma.masked_invalid(arr), bytes=True)
    alpha = np.where(np.isnan(arr), 0, int(opacity * 255)).astype(np.uint8)
    alpha[np.nan_to_num(arr) < 1] = 0
    rgba[..., 3] = alpha
    if lat[0] < lat[-1]:                       # Leaflet wants north as row 0
        rgba = rgba[::-1, :, :]

    lat_min, lat_max = float(np.min(lat)), float(np.max(lat))
    lon_min, lon_max = float(np.min(lon)), float(np.max(lon))
    m = folium.Map(tiles="OpenStreetMap")
    m.fit_bounds([[lat_min, lon_min], [lat_max, lon_max]])
    folium.raster_layers.ImageOverlay(
        image=rgba, bounds=[[lat_min, lon_min], [lat_max, lon_max]],
        opacity=1.0, name=f"Σ rain {start}–{end}", zindex=5,
    ).add_to(m)

    for name, la, lo in CITIES:
        if lat_min <= la <= lat_max and lon_min <= lo <= lon_max:
            iy = int(np.argmin(np.abs(np.asarray(lat) - la)))
            ix = int(np.argmin(np.abs(np.asarray(lon) - lo)))
            val = arr[iy, ix]
            txt = f"{name}: {val:.0f} mm" if np.isfinite(val) else name
            folium.CircleMarker([la, lo], radius=4, color="#222", fill=True,
                                fill_opacity=1, tooltip=txt).add_to(m)

    rows = "".join(
        f'<span style="background:{c};display:inline-block;width:22px;'
        f'height:11px;margin-right:4px;border:1px solid #999"></span>'
        f'{BOUNDS[i]}–{BOUNDS[i+1]} mm<br>'
        for i, c in enumerate(COLORS[:-1]))
    legend = (f'<div style="position:fixed;bottom:20px;left:20px;z-index:9999;'
              f'background:rgba(255,255,255,.92);color:#111;'
              f'padding:10px 12px;border-radius:8px;font:12px sans-serif;'
              f'box-shadow:0 1px 4px rgba(0,0,0,.3)">'
              f'<b>Σ {start} – {end}</b><br>Flächenmittel {mean:.0f} mm<br>'
              f'<div style="margin-top:6px">{rows}</div></div>')
    m.get_root().html.add_child(folium.Element(legend))
    folium.LayerControl().add_to(m)
    return m


# ----------------------------------------------------------------------
# Streamlit app
# ----------------------------------------------------------------------

def run_app():
    import streamlit as st

    st.set_page_config(page_title="Pilzwetter — rain map", layout="wide")
    st.title("Precipitation map (GeoSphere SPARTACUS, 1 km)")
    st.caption("Pick any time range — e.g. the last 14 days before your foraging trip 🍄")

    with st.sidebar:
        st.header("Time range")
        preset = st.radio("Preset",
                          ["Last 24 hours", "Last 7 days", "Last 14 days",
                           "Last 30 days", "This month", "Custom"],
                          index=2)  # default: Last 14 days
        today = dt.date.today()
        latest = today - dt.timedelta(days=2)  # SPARTACUS lag
        api = API
        if preset == "Last 24 hours":
            # near-real-time INCA hourly data (~1 h lag)
            end = dt.datetime.now().replace(minute=0, second=0,
                                            microsecond=0) - dt.timedelta(hours=1)
            start = end - dt.timedelta(hours=24)
            api = API_INCA
        elif preset == "Last 7 days":
            start, end = latest - dt.timedelta(days=6), latest
        elif preset == "Last 14 days":
            start, end = latest - dt.timedelta(days=13), latest
        elif preset == "Last 30 days":
            start, end = latest - dt.timedelta(days=29), latest
        elif preset == "This month":
            start, end = latest.replace(day=1), latest
        else:
            start = st.date_input("Start", latest - dt.timedelta(days=13),
                                  max_value=latest)
            end = st.date_input("End", latest, max_value=latest)
        if preset == "Last 24 hours":
            st.write(f"**{start:%d.%m. %H:%M} → {end:%d.%m. %H:%M}** (INCA, hourly)")
        else:
            st.write(f"**{start} → {end}** ({(end - start).days + 1} days)")

        st.header("Region")
        region = st.selectbox("Area", list(REGIONS.keys()), index=1,
                              format_func=lambda k: {
                                  "austria": "Austria (whole)",
                                  "muehlviertel": "Mühlviertel",
                                  "linz50": "Linz ±50 km"}[k])
        bbox = REGIONS[region]
        if st.checkbox("Custom bounding box"):
            c1, c2 = st.columns(2)
            lat_min = c1.number_input("lat min", value=bbox[0], format="%.3f")
            lon_min = c1.number_input("lon min", value=bbox[1], format="%.3f")
            lat_max = c2.number_input("lat max", value=bbox[2], format="%.3f")
            lon_max = c2.number_input("lon max", value=bbox[3], format="%.3f")
            bbox = (lat_min, lon_min, lat_max, lon_max)

        opacity = st.slider("Overlay opacity", 0.2, 1.0, 0.75, 0.05)

    if _iso(start) > _iso(end):
        st.error("Start must be before end."); st.stop()

    @st.cache_data(show_spinner="Downloading grids from GeoSphere …", ttl=3600)
    def cached(start_iso, end_iso, bbox_t, api_url):
        ds = fetch_grid(dt.datetime.fromisoformat(start_iso),
                        dt.datetime.fromisoformat(end_iso), bbox_t, api_url)
        return total_precip_latlon(ds, bbox_t)

    try:
        lat, lon, arr, mean = cached(_iso(start), _iso(end), tuple(bbox), api)
    except Exception as e:
        st.error(f"Download failed: {e}")
        st.info("For long periods choose a smaller region — the API limits "
                "very large requests.")
        st.stop()

    c1, c2, c3 = st.columns(3)
    c1.metric("Flächenmittel", f"{mean:.0f} mm")
    c2.metric("Max cell", f"{np.nanmax(arr):.0f} mm")
    verdict = ("🍄 excellent" if mean >= 60 else "good" if mean >= 40
               else "marginal" if mean >= 20 else "too dry")
    if preset == "Last 24 hours":
        c3.metric("Foraging verdict", "—",
                  help="Verdict thresholds are tuned for ~14-day sums; "
                       "a 24 h window says little about soil moisture.")
    else:
        c3.metric("Foraging verdict", verdict)

    tab1, tab2 = st.tabs(["🗺 Interactive map", "🖼 Static PNG"])
    with tab1:
        m = make_overlay_map(lat, lon, arr, mean, start, end, opacity)
        import streamlit.components.v1 as components
        components.html(m.get_root().render(), height=650)
        st.download_button("Download interactive map (HTML)",
                           m.get_root().render().encode(),
                           file_name=f"rainmap_{start}_{end}.html",
                           mime="text/html")
    with tab2:
        fig = render_map(lat, lon, arr, mean, start, end, bbox=tuple(bbox))
        st.pyplot(fig, use_container_width=True)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=150)
        st.download_button("Download PNG", buf.getvalue(),
                           file_name=f"rain_{start}_{end}.png",
                           mime="image/png")


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def run_cli():
    p = argparse.ArgumentParser(description="Precipitation-sum map (SPARTACUS 1 km)")
    p.add_argument("--start", type=dt.date.fromisoformat)
    p.add_argument("--end", type=dt.date.fromisoformat)
    p.add_argument("--days", type=int, default=14,
                   help="used when --start/--end are omitted")
    p.add_argument("--hours", type=int,
                   help="e.g. --hours 24: use near-real-time INCA hourly data "
                        "instead of daily SPARTACUS")
    p.add_argument("--region", choices=REGIONS.keys(), default="muehlviertel")
    p.add_argument("--bbox", type=str,
                   help="lat_min,lon_min,lat_max,lon_max (overrides --region)")
    p.add_argument("-o", "--out", default="rainmap.png")
    a = p.parse_args()

    api = API
    if a.hours:
        end = dt.datetime.now().replace(minute=0, second=0,
                                        microsecond=0) - dt.timedelta(hours=1)
        start = end - dt.timedelta(hours=a.hours)
        api = API_INCA
    else:
        end = a.end or (dt.date.today() - dt.timedelta(days=2))
        start = a.start or (end - dt.timedelta(days=a.days - 1))
    bbox = tuple(float(x) for x in a.bbox.split(",")) if a.bbox else REGIONS[a.region]

    print(f"Fetching RR {_iso(start)} → {_iso(end)}, bbox {bbox} …")
    ds = fetch_grid(start, end, bbox, api)
    lat, lon, arr, mean = total_precip_latlon(ds, bbox)
    print(f"Flächenmittel: {mean:.1f} mm | max cell: {np.nanmax(arr):.1f} mm")

    html_out = a.out.rsplit(".", 1)[0] + ".html"
    make_overlay_map(lat, lon, arr, mean, start, end).save(html_out)
    print(f"✔ wrote {html_out}  (interactive — open in browser)")
    fig = render_map(lat, lon, arr, mean, start, end, f" — {a.region}",
                     bbox=bbox)
    fig.savefig(a.out, dpi=150)
    print(f"✔ wrote {a.out}")


def _in_streamlit():
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        return get_script_run_ctx() is not None
    except ImportError:
        return False


if __name__ == "__main__":
    if _in_streamlit():
        run_app()
    else:
        run_cli()
