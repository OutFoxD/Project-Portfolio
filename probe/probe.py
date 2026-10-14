"""Probe 2: exact precipitation fields (DWD parameter metadata, CHMI SRA, HYRAS file)."""
import os, re, io, json, zipfile, datetime as dt, traceback
import requests
OUT = "out"; os.makedirs(OUT, exist_ok=True)
S = requests.Session(); S.headers["User-Agent"] = "protofox.at/shroomzn probe"
def save(name, text): open(os.path.join(OUT, name), "w", encoding="utf-8").write(text)
def get(url, **kw):
    r = S.get(url, timeout=120, **kw); r.raise_for_status(); return r
def step(name, fn):
    try:
        res = fn(); save(name + ".txt", res if isinstance(res, str) else json.dumps(res, ensure_ascii=False, indent=1, default=str))
    except Exception:
        save(name + ".ERR.txt", traceback.format_exc())
DWD = "https://opendata.dwd.de/climate_environment/CDC"
CH = "https://opendata.chmi.cz/meteorology/climate"

def dwd_param():
    z = zipfile.ZipFile(io.BytesIO(get(f"{DWD}/observations_germany/climate/daily/more_precip/recent/tageswerte_RR_03878_akt.zip").content))
    out = []
    for n in z.namelist():
        if n.startswith("Metadaten_Parameter") and n.endswith(".txt"):
            out.append(f"--- {n}\n" + z.read(n).decode("latin-1"))
    z2 = zipfile.ZipFile(io.BytesIO(get(f"{DWD}/observations_germany/climate/10_minutes/precipitation/now/10minutenwerte_nieder_03878_now.zip").content))
    for n in z2.namelist():
        t = z2.read(n).decode("latin-1").splitlines()
        out.append(f"--- 10min now {n}: {len(t)} lines\n" + "\n".join(t[:3]) + "\n...\n" + "\n".join(t[-3:]))
    return "\n".join(out)
step("dwd_param", dwd_param)

def chmi_sra():
    out = []
    j = get(f"{CH}/recent/data/daily/dly-0-20000-0-11546-202610.json").json()
    vals = j["data"]["data"]["values"]
    out.append("header: " + j["data"]["data"]["header"])
    els = sorted({(v[1], v[2]) for v in vals}); out.append("elements: " + str(els))
    out += [json.dumps(v, ensure_ascii=False) for v in vals if v[1].startswith("SRA") or v[1].startswith("SNO") or v[1] == "T" and False]
    j2 = get(f"{CH}/recent/data/daily/dly-0-203-0-42106052001-202610.json").json()
    v2 = j2["data"]["data"]["values"]
    out.append("--- precip-only station elements: " + str(sorted({(v[1], v[2]) for v in v2})))
    out += [json.dumps(v, ensure_ascii=False) for v in v2 if v[1].startswith("SRA")][:20]
    return "\n".join(out)
step("chmi_sra", chmi_sra)

def chmi_meta2():
    j = get(f"{CH}/now/metadata/meta2-{dt.date.today():%Y%m%d}.json").json()
    d = j["data"]["data"]
    rows = d["values"]
    out = ["header: " + d["header"], f"rows: {len(rows)}"]
    out += [json.dumps(r, ensure_ascii=False) for r in rows[:5]]
    out += [json.dumps(r, ensure_ascii=False) for r in rows if "SRA" in json.dumps(r)][:15]
    for k in ("meta3", "meta4"):
        jj = get(f"{CH}/now/metadata/{k}-{dt.date.today():%Y%m%d}.json").json()
        out.append(f"--- {k}: " + json.dumps(jj["data"], ensure_ascii=False)[:1800])
    return "\n".join(out)
step("chmi_meta2", chmi_meta2)

def chmi_now():
    out = []
    t = get(f"{CH}/now/data/").text
    names = re.findall(r'href="(1h-[^"]+)"', t)
    out.append(f"1h files: {len(names)}; stations: {len({n[:-14] for n in names})}")
    j = get(f"{CH}/now/data/1h-0-20000-0-11546-{dt.date.today() - dt.timedelta(days=1):%Y%m%d}.json").json()
    vals = j["data"]["data"]["values"]
    out.append("header " + j["data"]["data"]["header"]); out.append("elements " + str(sorted({v[1] for v in vals})))
    out += [json.dumps(v) for v in vals if v[1].startswith("SRA")][:30]
    return "\n".join(out)
step("chmi_now", chmi_now)

def chmi_products():
    out = []
    for u in ["https://opendata.chmi.cz/meteorology/products/", "https://opendata.chmi.cz/meteorology/weather/radar/"]:
        t = get(u).text
        lines = [l for l in re.sub(r"<[^>]+>", "", t).splitlines() if l.strip()]
        out.append(f"=== {u}\n" + "\n".join(lines[:40]))
        for sub in re.findall(r'href="([^"./][^"]*/)"', t)[:12]:
            try:
                t2 = get(u + sub).text
                l2 = [l for l in re.sub(r"<[^>]+>", "", t2).splitlines() if l.strip()]
                out.append(f"  --- {u}{sub}\n" + "\n".join("    " + x for x in l2[2:14]))
            except Exception as e:
                out.append(f"  --- {u}{sub} {e}")
    return "\n".join(out)
step("chmi_products", chmi_products)

def hyras():
    import xarray as xr, numpy as np
    url = f"{DWD}/grids_germany/daily/hyras_de/precipitation/pr_hyras_1_2026_v6-1_de.nc"
    r = get(url); open("/tmp/h.nc", "wb").write(r.content)
    ds = xr.open_dataset("/tmp/h.nc")
    out = [f"size {len(r.content)}", str(ds)[:3500]]
    for v in ds.variables:
        out.append(f"{v}: {dict(ds[v].attrs)}"[:600])
    out.append(f"global attrs: {dict(ds.attrs)}"[:3000])
    t = ds["time"].values; out.append(f"time: {t[:2]} ... {t[-3:]} n={len(t)}")
    try:
        r2 = get(f"{DWD}/grids_germany/daily/hyras_de/precipitation/DESCRIPTION_HYRAS-DE-PR_V6-1_en.pdf")
        open(os.path.join(OUT, "HYRAS_en.pdf"), "wb").write(r2.content)
    except Exception as e:
        out.append(str(e))
    return "\n".join(out)
step("hyras", hyras)
print(os.listdir(OUT))
