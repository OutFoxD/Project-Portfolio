#!/usr/bin/env python3
"""
verify.py - independent checks of the precomputed shroomzn data.

    python verify.py DATA_DIR [CACHE_DIR]      ->  DATA_DIR/verify.json

1. raw_spotcheck   For a few stations per country, download the original
                   files again with separate, minimal code (different formats
                   where possible) and compare every daily value with what the
                   page will show. Also lists their 7/14/30/45-day sums, so the
                   rendered page can be checked against them.
2. cross_border    Stations less than 15 km apart in different countries:
                   correlation of their daily series at a shift of -1, 0, +1
                   days. If a country booked its days differently, the best
                   shift would be +-1 instead of 0.
3. grid_alignment  Stations outside Austria sampled in Austria's SPARTACUS
                   grid, and stations outside Germany in Germany's HYRAS grid
                   (both 1 km, both reach a bit past the border): same shift
                   test, against an independent gridded analysis.
4. grid_vs_station Each country's published grid image against its own
                   stations' sums (share within one colour class).

Nothing here changes the data; it only reports.
"""

import csv
import datetime as dt
import io
import json
import math
import os
import re
import sys
import zipfile

import numpy as np
import requests

UA = {"User-Agent": "protofox.at/shroomzn verify (GitHub Actions)"}
FILES = {"AT": "stations.json", "CH": "ch_stations.json", "DE": "de_stations.json", "CZ": "cz_stations.json"}
GRIDS = {"AT": "grid.json", "CH": "ch_grid.json", "DE": "de_grid.json"}
EDGES = [2, 15, 30, 50, 75, 100, 150, 200, 250, 350, 450, 600]
S = requests.Session()
S.headers.update(UA)


def get(url, **kw):
    r = S.get(url, timeout=120, **kw)
    r.raise_for_status()
    return r


def km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def load(data_dir):
    out = {}
    for c, f in FILES.items():
        p = os.path.join(data_dir, f)
        if os.path.exists(p):
            d = json.load(open(p, encoding="utf-8"))
            for s in d["stations"]:
                s["country"] = c
                s["by_date"] = dict(zip(d["dates"], s["rr"]))
            out[c] = d
    return out


def sums(by_date, through, days=(7, 14, 30, 45)):
    end = dt.date.fromisoformat(through)
    res = {}
    for n in days:
        vals = [by_date.get((end - dt.timedelta(days=k)).isoformat()) for k in range(n)]
        have = [v for v in vals if v is not None]
        res[str(n)] = round(sum(have), 1) if have else None
    return res


# ------------------------------------------------- 1. raw spot check ----

def raw_at(ids, start, end):
    """GeoSphere API as CSV (precompute reads GeoJSON)."""
    r = get("https://dataset.api.hub.geosphere.at/v1/station/historical/klima-v2-1d",
            params={"parameters": "rr", "station_ids": ",".join(map(str, ids)), "start": start, "end": end,
                    "output_format": "csv"})
    out = {}
    for row in csv.DictReader(io.StringIO(r.text)):
        v = row.get("rr", "")
        out.setdefault(str(row["station"]), {})[row["time"][:10]] = None if v in ("", "nan") else max(0.0, float(v))
    return out


def raw_ch(st):
    sets = {"smn": ("ch.meteoschweiz.ogd-smn", "ogd-smn"), "smn-precip": ("ch.meteoschweiz.ogd-smn-precip", "ogd-smn-precip")}
    coll, pre = sets.get(st.get("src"), sets["smn"])
    a = st["id"].lower()
    text = get(f"https://data.geo.admin.ch/{coll}/{a}/{pre}_{a}_d_recent.csv").content.decode("cp1252")
    lines = text.splitlines()
    head = lines[0].split(";")
    it, iv = head.index("reference_timestamp"), head.index("rre150d0")
    out = {}
    for ln in lines[1:]:
        c = ln.split(";")
        d = c[it][6:10] + "-" + c[it][3:5] + "-" + c[it][0:2]
        out[d] = None if c[iv] == "" else max(0.0, float(c[iv]))
    return out


def raw_de(st):
    z = zipfile.ZipFile(io.BytesIO(get(
        f"https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/daily/more_precip/recent/"
        f"tageswerte_RR_{int(st['id']):05d}_akt.zip").content))
    text = z.read([n for n in z.namelist() if n.startswith("produkt")][0]).decode("latin-1")
    out = {}
    for row in csv.reader(io.StringIO(text), delimiter=";"):
        if not row or not row[1].strip().isdigit():
            continue
        d, v = row[1].strip(), float(row[3])
        out[f"{d[:4]}-{d[4:6]}-{d[6:]}"] = None if v < -900 else max(0.0, v)
    return out


def raw_cz(st, months):
    out = {}
    for ym in months:
        for sub in ("", f"{ym[4:]}/"):
            r = S.get(f"https://opendata.chmi.cz/meteorology/climate/recent/data/daily/{sub}dly-{st['id']}-{ym}.json", timeout=60)
            if r.ok:
                for row in r.json()["data"]["data"]["values"]:
                    if row[1] == "SRA":
                        out[row[3][:10]] = None if row[4] in ("", None) else max(0.0, float(row[4]))
                break
    return out


def pick(stations, n=6):
    s = sorted(stations, key=lambda x: str(x["id"]))
    if len(s) <= n:
        return s
    return [s[int(i * (len(s) - 1) / (n - 1))] for i in range(n)]


def raw_spotcheck(data):
    res = {}
    for c, doc in data.items():
        chosen = pick([s for s in doc["stations"] if sum(v is not None for v in s["rr"]) > 30])
        dates = doc["dates"]
        try:
            if c == "AT":
                raw = raw_at([s["id"] for s in chosen], dates[0], dates[-1])
                raws = [raw.get(str(s["id"]), {}) for s in chosen]
            elif c == "CH":
                raws = [raw_ch(s) for s in chosen]
            elif c == "DE":
                raws = [raw_de(s) for s in chosen]
            else:
                months = sorted({d[:4] + d[5:7] for d in dates})
                raws = [raw_cz(s, months) for s in chosen]
        except Exception as e:
            res[c] = {"error": f"{type(e).__name__}: {e}"}
            continue
        rows, compared, worst, mism = [], 0, 0.0, []
        for s, raw in zip(chosen, raws):
            for d in dates:
                a, b = s["by_date"].get(d), raw.get(d)
                if a is None and b is None:
                    continue
                if (a is None) != (b is None):
                    mism.append(f"{s['name']} {d}: page {a} vs source {b}")
                    continue
                compared += 1
                diff = abs(a - b)
                worst = max(worst, diff)
                if diff > 0.05:
                    mism.append(f"{s['name']} {d}: page {a} vs source {b}")
            rows.append({"id": s["id"], "name": s["name"], "state": s["state"],
                         "sums_page": sums(s["by_date"], doc["through"]), "sums_source": sums(raw, doc["through"])})
        res[c] = {"stations": len(chosen), "values_compared": compared, "max_abs_diff": round(worst, 2),
                  "mismatches": len(mism), "examples": mism[:6], "through": doc["through"], "detail": rows}
    return res


# ------------------------------------------------- 2./3. shift tests ----

def lag_corr(a, b, dates):
    """Pearson r of a[d] vs b[d + lag] for lag -1, 0, +1."""
    out = {}
    for lag in (-1, 0, 1):
        xs, ys = [], []
        for i, d in enumerate(dates):
            j = i + lag
            if 0 <= j < len(dates):
                x, y = a.get(d), b.get(dates[j])
                if x is not None and y is not None and not (isinstance(y, float) and math.isnan(y)):
                    xs.append(x); ys.append(y)
        if len(xs) >= 20 and np.std(xs) > 0.2 and np.std(ys) > 0.2:
            out[lag] = float(np.corrcoef(xs, ys)[0, 1])
    return out


def summarise(results):
    """results: list of {lag: r}. Share of pairs whose best shift is 0, median r per shift."""
    ok = [r for r in results if len(r) == 3]
    if not ok:
        return {"pairs": 0}
    best = [max(r, key=r.get) for r in ok]
    return {"pairs": len(ok), "best_shift_0": round(best.count(0) / len(ok), 3),
            "best_shift_minus1": round(best.count(-1) / len(ok), 3), "best_shift_plus1": round(best.count(1) / len(ok), 3),
            "median_r": {str(l): round(float(np.median([r[l] for r in ok])), 3) for l in (-1, 0, 1)}}


def cross_border(data, max_km=15):
    stations = [s for d in data.values() for s in d["stations"]]
    dates = sorted({d for doc in data.values() for d in doc["dates"]})
    groups = {}
    for i, a in enumerate(stations):
        for b in stations[i + 1:]:
            if a["country"] == b["country"] or abs(a["lat"] - b["lat"]) > 0.2 or abs(a["lon"] - b["lon"]) > 0.3:
                continue
            if km(a, b) > max_km:
                continue
            key = "-".join(sorted((a["country"], b["country"])))
            first, second = (a, b) if a["country"] == key[:2] else (b, a)
            groups.setdefault(key, []).append(lag_corr(first["by_date"], second["by_date"], dates))
    return {k: summarise(v) for k, v in sorted(groups.items())}


def near(stations, ref, max_km):
    return [s for s in stations if any(abs(s["lat"] - r["lat"]) < 0.4 and abs(s["lon"] - r["lon"]) < 0.6 and km(s, r) <= max_km for r in ref)]


def grid_alignment(data, cache_dir, data_dir):
    res = {}
    # a) SPARTACUS (Austria) at nearby stations of the other countries, via GeoSphere's point API
    if "AT" in data:
        through = min(d["through"] for d in data.values() if d.get("through"))
        dates = [d for d in data["AT"]["dates"] if d <= through]     # SPARTACUS ends ~1-2 days back
        others = [s for c, d in data.items() if c != "AT" for s in d["stations"]]
        # a point outside the grid's box makes the API reject the whole request
        (s0, w0), (n0, e0) = json.load(open(os.path.join(data_dir, "grid.json"), encoding="utf-8"))["bounds"]
        inside = [s for s in others if s0 + 0.03 < s["lat"] < n0 - 0.03 and w0 + 0.03 < s["lon"] < e0 - 0.03]
        cand = near(inside, data["AT"]["stations"], 25)[:240]
        try:
            series = {}
            for i in range(0, len(cand), 120):
                part = cand[i:i + 120]
                r = get("https://dataset.api.hub.geosphere.at/v1/timeseries/historical/spartacus-v3-1d-1km",
                        params=[("parameters", "RR"), ("start", dates[0]), ("end", dates[-1])] +
                               [("lat_lon", f"{s['lat']},{s['lon']}") for s in part])
                js = r.json()
                ts = [t[:10] for t in js["timestamps"]]
                for s, f in zip(part, js["features"]):
                    vals = f["properties"]["parameters"]["RR"]["data"]
                    series[id(s)] = (s, {d: (None if v is None else float(v)) for d, v in zip(ts, vals)})
            by_c = {}
            for s, g in series.values():
                by_c.setdefault(s["country"], []).append(lag_corr(s["by_date"], g, dates))
            res["SPARTACUS_vs_" + "_".join(sorted(by_c))] = {c: summarise(v) for c, v in sorted(by_c.items())}
        except Exception as e:
            body = getattr(getattr(e, "response", None), "text", "")[:300]
            res["SPARTACUS"] = {"error": f"{type(e).__name__}: {str(e)[:200]} {body}"}
    # b) HYRAS (Germany) at nearby stations of the other countries, from the file precompute downloaded
    if "DE" in data and cache_dir:
        try:
            import xarray as xr
            from pyproj import Transformer
            files = sorted(f for f in os.listdir(cache_dir) if f.startswith("pr_hyras_1_"))
            others = [s for c, d in data.items() if c != "DE" for s in d["stations"]]
            cand = near(others, data["DE"]["stations"], 25)
            tr = Transformer.from_crs("EPSG:4326", "EPSG:3035", always_xy=True)
            grid = {}
            for fn in files:
                with xr.open_dataset(os.path.join(cache_dir, fn)) as ds:
                    xs, ys = ds["x"].values, ds["y"].values
                    t = [str(v)[:10] for v in ds["time"].values]
                    keep = [i for i, d in enumerate(t) if d in set(data["DE"]["dates"])]
                    if not keep:
                        continue
                    X, Y = tr.transform([s["lon"] for s in cand], [s["lat"] for s in cand])
                    ix = np.rint((np.asarray(X) - xs[0]) / (xs[1] - xs[0])).astype(int)
                    iy = np.rint((np.asarray(Y) - ys[0]) / (ys[1] - ys[0])).astype(int)
                    block = ds["pr"].isel(time=keep).transpose("time", "y", "x").values
                    for k, s in enumerate(cand):
                        if 0 <= ix[k] < len(xs) and 0 <= iy[k] < len(ys):
                            col = block[:, iy[k], ix[k]]
                            grid.setdefault(k, {}).update({t[i]: (None if np.isnan(col[n]) else float(col[n])) for n, i in enumerate(keep)})
            by_c = {}
            dates = data["DE"]["dates"]
            for k, g in grid.items():
                if sum(v is not None for v in g.values()) >= 20:
                    by_c.setdefault(cand[k]["country"], []).append(lag_corr(cand[k]["by_date"], g, dates))
            res["HYRAS_vs_" + "_".join(sorted(by_c))] = {c: summarise(v) for c, v in sorted(by_c.items())}
        except Exception as e:
            res["HYRAS"] = {"error": f"{type(e).__name__}: {e}"}
    return res


# ------------------------------------------- 4. grid image vs stations ----

def grid_vs_station(data, data_dir):
    from PIL import Image
    res = {}
    merc = lambda la: math.log(math.tan(math.pi / 4 + math.radians(la) / 2))
    for c, gfile in GRIDS.items():
        p = os.path.join(data_dir, gfile)
        if c not in data or not os.path.exists(p):
            continue
        g = json.load(open(p, encoding="utf-8"))
        (s0, w0), (n0, e0) = g["bounds"]
        out = {}
        for per, info in g["periods"].items():
            img = np.array(Image.open(os.path.join(data_dir, info["file"])))
            H, W = img.shape
            end = dt.date.fromisoformat(info["end"])
            days = [(end - dt.timedelta(days=k)).isoformat() for k in range(int(per))]
            tot = within1 = exact = 0
            for s in data[c]["stations"]:
                vals = [s["by_date"].get(d) for d in days]
                if sum(v is None for v in vals) > 1:
                    continue
                x = int((s["lon"] - w0) / (e0 - w0) * W)
                y = int((merc(n0) - merc(s["lat"])) / (merc(n0) - merc(s0)) * H)
                if not (0 <= x < W and 0 <= y < H) or img[y, x] >= 13:
                    continue
                sc = int(np.digitize(sum(v for v in vals if v is not None), EDGES))
                tot += 1
                within1 += abs(sc - int(img[y, x])) <= 1
                exact += sc == int(img[y, x])
            if tot:
                out[per] = {"stations": tot, "same_class": round(exact / tot, 3), "within_one_class": round(within1 / tot, 3)}
        res[c] = {"window_end": g.get("last_day"), "periods": out}
    return res


def main():
    data_dir = sys.argv[1]
    cache_dir = sys.argv[2] if len(sys.argv) > 2 else None
    data = load(data_dir)
    report = {"ran": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
              "through": {c: d["through"] for c, d in data.items()},
              "stations": {c: len(d["stations"]) for c, d in data.items()}}
    for name, fn in (("raw_spotcheck", lambda: raw_spotcheck(data)), ("cross_border", lambda: cross_border(data)),
                     ("grid_alignment", lambda: grid_alignment(data, cache_dir, data_dir)),
                     ("grid_vs_station", lambda: grid_vs_station(data, data_dir))):
        try:
            report[name] = fn()
        except Exception as e:
            report[name] = {"error": f"{type(e).__name__}: {e}"}
    with open(os.path.join(data_dir, "verify.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    short = {k: v for k, v in report.items() if k != "raw_spotcheck"}
    short["raw_spotcheck"] = {c: {k: v for k, v in r.items() if k != "detail"} for c, r in report.get("raw_spotcheck", {}).items()
                              if isinstance(r, dict)}
    print(json.dumps(short, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
