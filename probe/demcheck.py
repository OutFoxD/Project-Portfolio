"""Is the browser decoding Terrarium PNGs exactly? Compare with PIL, list PNG chunks,
and render a reference slope map in numpy."""
import io, json, math, os, struct, sys, functools, http.server, threading
import numpy as np
import requests
from PIL import Image

OUT = sys.argv[1]
os.makedirs(OUT, exist_ok=True)
rep = open(os.path.join(OUT, "demcheck.txt"), "w")
def p(*a):
    s = " ".join(map(str, a)); print(s); rep.write(s + "\n"); rep.flush()

def tile_xy(lat, lon, z):
    n = 2 ** z
    x = (lon + 180) / 360 * n
    y = (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n
    return int(x), int(y)

URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
z = 14
x, y = tile_xy(47.2130, 11.0270, z)
raw = requests.get(URL.format(z=z, x=x, y=y)).content
# chunks
i, chunks = 8, []
while i < len(raw):
    ln, typ = struct.unpack(">I4s", raw[i:i + 8]); chunks.append((typ.decode(), ln)); i += 12 + ln
p("chunks", chunks)
im = Image.open(io.BytesIO(raw)); p("mode", im.mode, im.info)
a = np.asarray(im.convert("RGB"), dtype=np.float64)
elev = a[..., 0] * 256 + a[..., 1] + a[..., 2] / 256 - 32768
p("elev range", elev.min(), elev.max())

def slope(e, z, y0):
    n = 256 * 2 ** z
    rows = (y0 * 256 + np.arange(e.shape[0] - 2) + 0.5) / n
    lat = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * rows))))
    res = 40075016.686 * np.cos(np.radians(lat)) / n
    A = e
    dzdx = ((A[:-2, 2:] + 2 * A[1:-1, 2:] + A[2:, 2:]) - (A[:-2, :-2] + 2 * A[1:-1, :-2] + A[2:, :-2])) / (8 * res[:, None])
    dzdy = ((A[2:, :-2] + 2 * A[2:, 1:-1] + A[2:, 2:]) - (A[:-2, :-2] + 2 * A[:-2, 1:-1] + A[:-2, 2:])) / (8 * res[:, None])
    return np.degrees(np.arctan(np.hypot(dzdx, dzdy)))

for zz in (12, 13, 14, 15):
    xx, yy = tile_xy(47.2130, 11.0270, zz)
    t = np.asarray(Image.open(io.BytesIO(requests.get(URL.format(z=zz, x=xx, y=yy)).content)).convert("RGB"), dtype=np.float64)
    e = t[..., 0] * 256 + t[..., 1] + t[..., 2] / 256 - 32768
    s = slope(e, zz, yy)
    p(f"z{zz}: slope pct>30 {np.mean(s > 30):.2f} >35 {np.mean(s > 35):.2f} >40 {np.mean(s > 40):.2f} >45 {np.mean(s > 45):.2f}  median {np.median(s):.1f}")
    # reference image
    col = np.zeros(s.shape + (3,), np.uint8) + 255
    for lo, c in [(30, (245, 228, 0)), (35, (243, 146, 0)), (40, (226, 0, 26)), (45, (139, 26, 143))]:
        col[s >= lo] = c
    Image.fromarray(col).resize((512, 512), Image.NEAREST).save(os.path.join(OUT, f"ref_slope_z{zz}.png"))
    # elevation step structure: histogram of first differences
    d = np.abs(np.diff(e, axis=1))
    p(f"   z{zz} |dE| px-to-px: median {np.median(d):.2f} p95 {np.percentile(d, 95):.2f} zero-share {np.mean(d < 0.01):.2f}")

# ---- browser decode vs PIL
srv_dir = os.path.join(OUT, "www"); os.makedirs(srv_dir, exist_ok=True)
open(os.path.join(srv_dir, "t.png"), "wb").write(raw)
open(os.path.join(srv_dir, "index.html"), "w").write("<!doctype html><body>ok</body>")
class Q(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a): pass
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 8766), functools.partial(Q, directory=srv_dir))
threading.Thread(target=srv.serve_forever, daemon=True).start()
from playwright.sync_api import sync_playwright
JS = """async (url) => {
  const out = {};
  const blob = await (await fetch(url)).blob();
  async function viaBitmap(opts) {
    const bmp = await createImageBitmap(blob, opts);
    const c = document.createElement('canvas'); c.width = 256; c.height = 256;
    const ctx = c.getContext('2d', {willReadFrequently: true}); ctx.drawImage(bmp, 0, 0);
    return Array.from(ctx.getImageData(0, 0, 256, 256).data);
  }
  out.none = await viaBitmap({colorSpaceConversion: 'none', premultiplyAlpha: 'none'});
  out.def = await viaBitmap({});
  const img = await new Promise(r => { const i = new Image(); i.onload = () => r(i); i.src = URL.createObjectURL(blob); });
  const c = document.createElement('canvas'); c.width = 256; c.height = 256;
  const ctx = c.getContext('2d', {willReadFrequently: true}); ctx.drawImage(img, 0, 0);
  out.img = Array.from(ctx.getImageData(0, 0, 256, 256).data);
  return out;
}"""
with sync_playwright() as pw:
    br = pw.chromium.launch(); pg = br.new_page(); pg.goto("http://127.0.0.1:8766/index.html")
    res = pg.evaluate(JS, "http://127.0.0.1:8766/t.png"); br.close()
ref = np.asarray(im.convert("RGBA"), dtype=np.int64).reshape(-1)
for k, v in res.items():
    v = np.asarray(v, dtype=np.int64)
    diff = np.abs(v - ref)
    rgb = v.reshape(256, 256, 4).astype(np.float64)
    e2 = rgb[..., 0] * 256 + rgb[..., 1] + rgb[..., 2] / 256 - 32768
    p(f"browser[{k}]: channel max diff {diff.max()}, share differing {np.mean(diff > 0):.3f}, elev max err {np.abs(e2 - elev).max():.2f} m")
srv.shutdown()
