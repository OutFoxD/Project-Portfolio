"""One-off probe of DWD + CHMI open data. Writes small samples to ./out."""
import os, re, io, json, zipfile, datetime as dt, traceback
import requests
OUT = "out"; os.makedirs(OUT, exist_ok=True)
S = requests.Session(); S.headers["User-Agent"] = "protofox.at/shroomzn probe"
def save(name, text): open(os.path.join(OUT, name), "w", encoding="utf-8").write(text)
def get(url, **kw):
    r = S.get(url, timeout=60, **kw); r.raise_for_status(); return r
def listing(url, n_head=40, n_tail=15, pat=None):
    t = get(url).text
    names = re.findall(r'href="([^"?]+)"', t)
    names = [x for x in names if not x.startswith("/") and x != "../"]
    if pat: names = [x for x in names if re.search(pat, x)]
    # include sizes / dates lines for the tail
    lines = [l for l in re.sub(r"<[^>]+>", "", t).splitlines() if l.strip()]
    return names, lines
def step(name, fn):
    try:
        res = fn(); save(name + ".txt", res if isinstance(res, str) else json.dumps(res, ensure_ascii=False, indent=1, default=str))
    except Exception:
        save(name + ".ERR.txt", traceback.format_exc())

DWD = "https://opendata.dwd.de/climate_environment/CDC"
def dwd_dirs():
    out = []
    for u in [f"{DWD}/observations_germany/climate/daily/more_precip/recent/",
              f"{DWD}/observations_germany/climate/daily/kl/recent/",
              f"{DWD}/grids_germany/daily/", f"{DWD}/grids_germany/daily/radolan/",
              f"{DWD}/grids_germany/daily/radolan/recent/", f"{DWD}/grids_germany/daily/radolan/recent/bin/",
              f"{DWD}/grids_germany/daily/regnie/", f"{DWD}/grids_germany/daily/hyras_de/",
              f"{DWD}/grids_germany/daily/hyras_de/precipitation/",
              f"{DWD}/observations_germany/climate/hourly/precipitation/now/",
              f"{DWD}/observations_germany/climate/hourly/precipitation/recent/",
              f"{DWD}/observations_germany/climate/10_minutes/precipitation/now/",
              "https://opendata.dwd.de/weather/radar/radolan/", "https://opendata.dwd.de/weather/radar/radolan/rw/",
              "https://opendata.dwd.de/weather/radar/radolan/sf/"]:
        try:
            names, lines = listing(u)
            out.append(f"=== {u}  ({len(names)} entries)\n" + "\n".join(lines[:25]) + "\n ...\n" + "\n".join(lines[-12:]))
        except Exception as e:
            out.append(f"=== {u}  ERROR {e}")
    return "\n\n".join(out)
step("dwd_dirs", dwd_dirs)

def dwd_stations():
    t = get(f"{DWD}/observations_germany/climate/daily/more_precip/recent/RR_Tageswerte_Beschreibung_Stationen.txt").content.decode("latin-1")
    lines = t.splitlines()
    active = [l for l in lines[2:] if re.match(r"\s*\d+\s+\d{8}\s+(2026\d{4})", l)]
    recent = [l for l in active if re.match(r"\s*\d+\s+\d{8}\s+202610", l)]
    return "\n".join(lines[:6]) + f"\n... total rows {len(lines)-2}, bis 2026: {len(active)}, bis 2026-10: {len(recent)}\n" + "\n".join(recent[:5]) + "\n...\n" + "\n".join(l for l in lines if re.search(r"Passau|Freilassing|Reichenhall|Lindau|Konstanz|Berchtesgaden|Zwiesel|Hof\b|Oberstdorf", l))
step("dwd_stations", dwd_stations)

def dwd_sample():
    out = []
    names, _ = listing(f"{DWD}/observations_germany/climate/daily/more_precip/recent/", pat=r"\.zip$")
    out.append(f"zip files: {len(names)}; first: {names[:3]}")
    t = get(f"{DWD}/observations_germany/climate/daily/more_precip/recent/RR_Tageswerte_Beschreibung_Stationen.txt").content.decode("latin-1")
    ids = []
    for l in t.splitlines():
        if re.search(r"Passau|Lindau|Konstanz|Berchtesgaden|Bad Reichenhall", l) and re.match(r"\s*\d+\s+\d{8}\s+202610", l):
            ids.append(int(l.split()[0]))
    for sid in ids[:4]:
        fn = f"tageswerte_RR_{sid:05d}_akt.zip"
        r = get(f"{DWD}/observations_germany/climate/daily/more_precip/recent/{fn}")
        z = zipfile.ZipFile(io.BytesIO(r.content))
        prod = [n for n in z.namelist() if n.startswith("produkt")]
        txt = z.read(prod[0]).decode("latin-1").splitlines()
        out.append(f"--- {fn} ({len(r.content)} B) files={z.namelist()}\n" + "\n".join(txt[:2]) + "\n...\n" + "\n".join(txt[-50:]))
        hdrs = {k: v for k, v in r.headers.items() if k.lower() in ("last-modified", "access-control-allow-origin", "cache-control")}
        out.append(f"headers: {hdrs}")
    return "\n".join(out)
step("dwd_sample", dwd_sample)

def dwd_docs():
    got = []
    for u in [f"{DWD}/observations_germany/climate/daily/more_precip/recent/DESCRIPTION_obsgermany-climate-daily-more_precip-recent_en.pdf",
              f"{DWD}/observations_germany/climate/daily/more_precip/DESCRIPTION_obsgermany-climate-daily-more_precip_en.pdf",
              f"{DWD}/grids_germany/daily/radolan/recent/bin/DESCRIPTION_gridsgermany-daily-radolan-recent-bin_en.pdf"]:
        try:
            r = get(u); fn = u.rsplit("/", 1)[-1]; open(os.path.join(OUT, fn), "wb").write(r.content); got.append(fn)
        except Exception as e:
            got.append(f"{u}: {e}")
    return "\n".join(got)
step("dwd_docs", dwd_docs)

def cors():
    res = {}
    for u in ["https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/daily/more_precip/recent/RR_Tageswerte_Beschreibung_Stationen.txt",
              "https://opendata.chmi.cz/meteorology/climate/now/metadata/"]:
        try:
            r = S.get(u, headers={"Origin": "https://protofox.at"}, timeout=30)
            res[u] = {k: v for k, v in r.headers.items() if "access-control" in k.lower() or k.lower() in ("server", "last-modified")}
            res[u]["status"] = r.status_code
        except Exception as e:
            res[u] = str(e)
    return res
step("cors", cors)

CH = "https://opendata.chmi.cz/meteorology/climate"
def chmi_dirs():
    out = []
    for u in ["https://opendata.chmi.cz/", "https://opendata.chmi.cz/meteorology/", f"{CH}/", f"{CH}/now/", f"{CH}/now/metadata/",
              f"{CH}/now/data/", f"{CH}/recent/data/", f"{CH}/recent/data/daily/", f"{CH}/recent/data/daily/10/",
              f"{CH}/recent/data/1hour/", "https://opendata.chmi.cz/meteorology/weather/"]:
        try:
            names, lines = listing(u)
            out.append(f"=== {u}  ({len(names)} entries)\n" + "\n".join(lines[:30]) + "\n ...\n" + "\n".join(lines[-10:]))
        except Exception as e:
            out.append(f"=== {u}  ERROR {e}")
    return "\n\n".join(out)
step("chmi_dirs", chmi_dirs)

def chmi_meta():
    names, _ = listing(f"{CH}/now/metadata/")
    metas = sorted(n for n in names if n.startswith("meta1"))
    r = get(f"{CH}/now/metadata/{metas[-1]}")
    j = r.json()
    s = json.dumps(j, ensure_ascii=False)
    return f"files: {names}\nlatest: {metas[-1]} ({len(r.content)} B)\nkeys: {list(j.keys()) if isinstance(j, dict) else type(j)}\nhead: {s[:3000]}"
step("chmi_meta", chmi_meta)

def chmi_daily():
    out = []
    m = dt.date.today().strftime("%m"); ym = dt.date.today().strftime("%Y%m")
    names, _ = listing(f"{CH}/recent/data/daily/{m}/", pat=r"\.json$")
    out.append(f"daily/{m}: {len(names)} files; first {names[:5]}")
    for n in names[:2]:
        r = get(f"{CH}/recent/data/daily/{m}/{n}")
        out.append(f"--- {n} ({len(r.content)} B)\n{r.text[:4000]}")
    # previous month too
    pm = (dt.date.today().replace(day=1) - dt.timedelta(days=1)).strftime("%m")
    names2, _ = listing(f"{CH}/recent/data/daily/{pm}/", pat=r"\.json$")
    out.append(f"daily/{pm}: {len(names2)} files")
    return "\n".join(out)
step("chmi_daily", chmi_daily)

def chmi_now():
    out = []
    names, lines = listing(f"{CH}/now/data/")
    out.append(f"now/data: {len(names)} entries: {names[:20]}")
    for n in names[:3]:
        if n.endswith("/"):
            sub, _ = listing(f"{CH}/now/data/{n}")
            out.append(f"  {n}: {len(sub)} -> {sub[:6]}")
            js = [x for x in sub if x.endswith(".json")]
            if js:
                r = get(f"{CH}/now/data/{n}{js[0]}"); out.append(f"--- {js[0]}\n{r.text[:2500]}")
        elif n.endswith(".json"):
            r = get(f"{CH}/now/data/{n}"); out.append(f"--- {n}\n{r.text[:2500]}")
    return "\n".join(out)
step("chmi_now", chmi_now)
print(os.listdir(OUT))
