#!/usr/bin/env python3
"""Render assets/graph/index.html to PNG (2x) and SVG with headless Chromium.

  pip install playwright && playwright install chromium
  python3 assets/graph/to_d3.py --run examples/primitives
  python3 assets/graph/render.py
Set CHROME=/path/to/chrome to use an existing browser instead of Playwright's.
"""
import os
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
ASSETS = HERE.parent
W, H = 1700, 800

with sync_playwright() as p:
    kw = {"args": ["--no-sandbox", "--allow-file-access-from-files"]}
    if os.environ.get("CHROME"):
        kw["executable_path"] = os.environ["CHROME"]
    b = p.chromium.launch(**kw)
    pg = b.new_page(viewport={"width": W, "height": H}, device_scale_factor=2)
    pg.on("console", lambda m: print("page:", m.text))
    pg.goto((HERE / "index.html").as_uri())
    pg.wait_for_function("window.__ready===true")
    issues = pg.evaluate("window.__issues")
    if issues:
        sys.exit("overlaps found: " + "; ".join(issues))
    pg.screenshot(path=str(ASSETS / "escalation-graph-primitives.png"))
    svg = pg.evaluate("""() => {
      const s = document.getElementById('graph').cloneNode(true);
      s.removeAttribute('style'); s.removeAttribute('id');
      s.setAttribute('width', 1700); s.setAttribute('height', 800);
      s.querySelector('.viewport').removeAttribute('transform');
      return s.outerHTML;
    }""")
    (ASSETS / "escalation-graph-primitives.svg").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n' + svg.replace("<svg ", '<svg xmlns:xlink="http://www.w3.org/1999/xlink" ', 1) + "\n")
    b.close()
print("wrote escalation-graph-primitives.png and .svg")
