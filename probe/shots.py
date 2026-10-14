"""Render protofox.at/skitour with the freshly computed data and the real tile servers."""
import functools, http.server, os, sys, threading
from playwright.sync_api import sync_playwright

SITE, OUT = sys.argv[1], sys.argv[2]
os.makedirs(OUT, exist_ok=True)


class Q(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 8765), functools.partial(Q, directory=SITE))
threading.Thread(target=srv.serve_forever, daemon=True).start()
logs = []
VIEWS = [
    # name, hash, setup clicks (wx, terr, window)
    ("1_alps_snow_p72", "#7/47.15/12.6", "snow", "off", "p72"),
    ("2_alps_snow_f48", "#7/47.15/12.6", "snow", "off", "f48"),
    ("3_alps_sl_f24", "#7/47.15/12.6", "sl", "off", "f24"),
    ("4_tirol_wind_f24", "#9/47.15/11.4", "wind", "off", "f24"),
    ("5_kuehtai_slope", "#14/47.2130/11.0270", "off", "slope", "p72"),
    ("6_kuehtai_aspect", "#14/47.2130/11.0270", "off", "aspect", "p72"),
    ("7_stubai_sl_z12", "#12/47.03/11.15", "sl", "off", "f48"),
    ("8_stubai_lee_z13", "#13/47.10/11.20", "wind", "lee", "f24"),
    ("9_depth", "#8/47.2/12.8", "depth", "off", "p72"),
]
with sync_playwright() as pw:
    br = pw.chromium.launch()
    for vp, tag, dpr in [({"width": 390, "height": 844}, "m", 2), ({"width": 1280, "height": 860}, "d", 1)]:
        ctx = br.new_context(viewport=vp, device_scale_factor=dpr)
        page = ctx.new_page()
        page.on("console", lambda m, tag=tag: logs.append(f"[{tag}] {m.type}: {m.text}"))
        page.on("pageerror", lambda e, tag=tag: logs.append(f"[{tag}] PAGEERROR: {e}"))
        page.on("requestfailed", lambda r, tag=tag: logs.append(f"[{tag}] FAILED {r.url[:120]} {r.failure}"))
        page.goto("http://127.0.0.1:8765/skitour/")
        page.wait_for_timeout(3000)
        for name, h, wx, terr, win in VIEWS:
            page.evaluate(f"""() => {{
                location.hash = "{h}";
                setWx("{wx}"); setTerr("{terr}");
                document.querySelectorAll('#wx button').forEach(b => b.setAttribute('aria-pressed', b.dataset.v === "{wx}"));
                document.querySelectorAll('#terr button').forEach(b => b.setAttribute('aria-pressed', b.dataset.v === "{terr}"));
                if (META && META.windows.some(w => w.id === "{win}")) setWin("{win}");
            }}""")
            page.wait_for_timeout(6000)
            page.screenshot(path=os.path.join(OUT, f"{tag}{name}.png"))
        # tap info in the Stubai
        page.evaluate("""() => { location.hash = "#12/47.03/11.15"; setWx("snow"); setTerr("slope"); }""")
        page.wait_for_timeout(4000)
        page.mouse.click(vp["width"] // 2 + (180 if tag == "d" else 0), vp["height"] // 2)
        page.wait_for_timeout(5000)
        page.screenshot(path=os.path.join(OUT, f"{tag}10_popup.png"))
        ctx.close()
    br.close()
open(os.path.join(OUT, "console.txt"), "w").write("\n".join(logs))
print("\n".join(logs[-60:]))
srv.shutdown()
