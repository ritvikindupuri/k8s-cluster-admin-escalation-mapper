#!/usr/bin/env python3
"""Render the --html graph for every fixture plus two synthetic graphs in headless Chrome
and fail if a card, label or edge overlaps something it should not.

  pip install playwright            # then: playwright install chromium   (or set CHROME=/path/to/chrome)
  python3 tests/render_check.py [--shots DIR]

--shots DIR also saves a PNG of each page. Not part of the shipped skill zip.
"""
import argparse
import os
import random
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import escalation_map as em  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

FIXTURES = [("keda-style", ["--psa-exempt-ns", "kube-system"]), ("nodes-proxy", []), ("primitives", []),
            ("anonymous-tokens", []), ("clean", []), ("synthetic-large", [])]


def stress_graph(out):
    """40+ nodes, 5 columns, mixed confidence, long names, hand-built so it is larger than any real fixture."""
    random.seed(7)
    kinds = ["POD_CREATE", "EXEC", "SECRETS_READ", "TOKEN_REQUEST", "IMPERSONATE", "BIND", "NODES_PROXY", "WL_SA", "SECRET_MINT"]
    starts = [f"Group:tenant-{i}-devs" if i % 3 == 0 else f"SA:app-{i}/runner" if i % 3 == 1 else f"User:user{i}@example.com" for i in range(24)]
    layers = [[f"SA:svc-{i}/worker-with-long-name-{i}" for i in range(9)],
              [f"WL:ns-{i}/Deployment/controller-{i}" for i in range(6)],
              [em.NODE_ROOT] + [f"SA:kube-system/deployer-{i}" for i in range(3)]]
    final = []
    for s in starts:
        path, cur = [], s
        for layer in layers:
            if random.random() < 0.75:
                nxt = random.choice(layer)
                path.append((cur, NS(dst=nxt, kind=random.choice(kinds), text=f"{cur} to {nxt}", how="synthetic", via="ClusterRole/x",
                                     conf=random.choice(["certain", "likely", "likely", "heuristic"]))))
                cur = nxt
        path.append((cur, NS(dst=em.GOAL, kind="ADMIN" if cur != s else "BIND", text="goal", how="synthetic", via="ClusterRole/cluster-admin", conf="certain")))
        final.append({"start": s, "sev": random.choice(["critical", "high", "high", "medium"]), "also": [], "path": path})
    em.write_html(out, final, ["SA:ops/admin"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", help="directory for PNG screenshots")
    a = ap.parse_args()
    tmp = Path(tempfile.mkdtemp())
    pages = []
    for name, flags in FIXTURES:
        out = tmp / f"{name}.html"
        subprocess.run([sys.executable, str(ROOT / "scripts" / "escalation_map.py"), str(ROOT / "examples" / name), "--html", str(out)] + flags,
                       capture_output=True, text=True)
        pages.append((name, out))
    out = tmp / "synthetic-stress.html"
    stress_graph(str(out))
    pages.append(("synthetic-stress", out))
    bad = 0
    with sync_playwright() as p:
        kw = {"args": ["--no-sandbox", "--allow-file-access-from-files"]}
        if os.environ.get("CHROME"):
            kw["executable_path"] = os.environ["CHROME"]
        b = p.chromium.launch(**kw)
        for name, path in pages:
            pg = b.new_page(viewport={"width": 1800, "height": 1000}, device_scale_factor=2 if a.shots else 1)
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto(path.as_uri())
            pg.wait_for_function("window.__ready===true", timeout=20000)
            issues = pg.evaluate("window.__issues")
            n = pg.evaluate("document.querySelectorAll('g.node').length")
            hidden = pg.evaluate("document.querySelector('.hint').textContent.includes('hidden')")
            ok = not issues and not errs
            bad += 0 if ok else 1
            print(f"{'ok  ' if ok else 'FAIL'} {name:18s} cards={n:3d} overlaps={len(issues)} js_errors={len(errs)}{'  (some labels hidden, dense)' if hidden else ''}")
            for i in issues + errs:
                print("     ", i)
            if a.shots:
                Path(a.shots).mkdir(parents=True, exist_ok=True)
                pg.screenshot(path=str(Path(a.shots) / f"{name}.png"))
            pg.close()
        b.close()
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
