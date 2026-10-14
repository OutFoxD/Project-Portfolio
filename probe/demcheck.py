"""Same area, different zoom levels: does the slope distribution agree?"""
import io, math, os, sys
import numpy as np
import requests
from PIL import Image

OUT = sys.argv[1]
os.makedirs(OUT, exist_ok=True)
rep = open(os.path.join(OUT, "demcheck.txt"), "w")
def p(*a):
    s = " ".join(map(str, a)); print(s); rep.write(s + "\n"); rep.flush()

URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
S = requests.Session()

def tile(z, x, y):
    t = np.asarray(Image.open(io.BytesIO(S.get(URL.format(z=z, x=x, y=y)).content)).convert("RGB"), dtype=np.float64)
    return t[..., 0] * 256 + t[..., 1] + t[..., 2] / 256 - 32768

def mosaic(z, x0, y0, nx, ny):
    return np.vstack([np.hstack([tile(z, x0 + i, y0 + j) for i in range(nx)]) for j in range(ny)])

def slope(e, z, y0px):
    n = 256 * 2 ** z
    rows = (y0px + np.arange(1, e.shape[0] - 1) + 0.5) / n
    lat = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * rows))))
    res = 40075016.686 * np.cos(np.radians(lat)) / n
    A = e
    dzdx = ((A[:-2, 2:] + 2 * A[1:-1, 2:] + A[2:, 2:]) - (A[:-2, :-2] + 2 * A[1:-1, :-2] + A[2:, :-2])) / (8 * res[:, None])
    dzdy = ((A[2:, :-2] + 2 * A[2:, 1:-1] + A[2:, 2:]) - (A[:-2, :-2] + 2 * A[:-2, 1:-1] + A[:-2, 2:])) / (8 * res[:, None])
    return np.degrees(np.arctan(np.hypot(dzdx, dzdy))), res.mean()

def stats(s):
    return " ".join(f">{t}:{np.mean(s > t):.2f}" for t in (30, 35, 40, 45)) + f" median {np.median(s):.1f}"

def tile_xy(lat, lon, z):
    n = 2 ** z
    return int((lon + 180) / 360 * n), int((1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n)

for name, lat, lon in [("Kuehtai S", 47.195, 11.02), ("Stubai Schaufel", 47.0, 11.12), ("Swiss Engadin", 46.5, 9.9), ("Rax", 47.70, 15.73)]:
    x13, y13 = tile_xy(lat, lon, 13)
    p(f"--- {name}: z13 tile {x13}/{y13}")
    for z in (12, 13, 14, 15):
        if z == 12:
            e = tile(12, x13 // 2, y13 // 2)
            ox, oy = (x13 % 2) * 128, (y13 % 2) * 128
            e = e[oy:oy + 128, ox:ox + 128]
            s, r = slope(e, 12, (y13 // 2) * 256 + oy - 1)
        else:
            k = 2 ** (z - 13)
            e = mosaic(z, x13 * k, y13 * k, k, k)
            s, r = slope(e, z, y13 * k * 256 - 1)
        # slope on a ~40 m baseline too (smooth first)
        p(f"  z{z} ({r:.1f} m/px): {stats(s)}   elev {e.min():.0f}-{e.max():.0f}")
        if z == 14:
            # quantisation / terracing check: share of exact equal neighbours, histogram of fractional part
            d = np.diff(e, axis=1)
            p(f"     z14 equal-neighbour share {np.mean(np.abs(d) < 1e-6):.3f}; distinct fractional values {len(np.unique(np.round(e % 1, 3)))}")
            from scipy.ndimage import uniform_filter
            s3, _ = slope(uniform_filter(e, 3), z, y13 * k * 256 - 1)
            p(f"     z14 after 3x3 mean: {stats(s3)}")
            col = np.full(s.shape + (3,), 255, np.uint8)
            for lo, c in [(30, (245, 228, 0)), (35, (243, 146, 0)), (40, (226, 0, 26)), (45, (139, 26, 143))]:
                col[s >= lo] = c
            Image.fromarray(col).save(os.path.join(OUT, f"slope_{name.replace(' ', '_')}_z14.png"))
            hs = np.clip(128 + (e[1:-1, 2:] - e[1:-1, :-2]) * 2, 0, 255).astype(np.uint8)
            Image.fromarray(hs).save(os.path.join(OUT, f"shade_{name.replace(' ', '_')}_z14.png"))
