#!/usr/bin/env python3
"""Convert `escalation_map.py --json` output into graph data for index.html.

  python3 scripts/escalation_map.py examples/primitives --json > out.json
  python3 assets/graph/to_d3.py out.json                 # writes graph-data.js + graph-data.json
  python3 assets/graph/to_d3.py --run examples/primitives  # runs the tool for you

Standard library only. The page loads graph-data.js, so it also works from file://.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL = HERE.parent.parent / "scripts" / "escalation_map.py"

EDGE_LABELS = {
    "POD_CREATE": "create pod\n(no Pod Security)",
    "NODE_TOKENS": "steal SA token\non the node",
    "ADMIN": "already\ncluster-admin",
    "IMPERSONATE": "impersonate",
    "BIND": "bind cluster-admin",
    "ESCALATE": "escalate on role",
    "WEBHOOK": "mutating webhook",
}


def node_type(nid):
    if nid == "CLUSTER_ADMIN":
        return "admin"
    if nid == "NODE_ROOT":
        return "noderoot"
    if nid.startswith("Group:"):
        return "group"
    if nid.startswith("User:"):
        return "user"
    if nid.startswith("SA:"):
        return "sa"
    return "other"


def display(nid):
    if nid == "CLUSTER_ADMIN":
        return "CLUSTER-ADMIN"
    if nid == "NODE_ROOT":
        return "NODE ROOT"
    return nid.split(":", 1)[1] if ":" in nid else nid


def build(report, title):
    nodes, edges, seen = {}, [], set()
    depth = {}
    for p in report["paths"]:
        depth[p["start"]] = max(depth.get(p["start"], 0), len(p["hops"]))
        for h in p["hops"]:
            for n in (h["from"], h["to"]):
                nodes.setdefault(n, {"id": n, "label": display(n), "type": node_type(n)})
            key = (h["from"], h["to"], h["kind"])
            if key in seen:
                continue
            seen.add(key)
            edges.append({
                "source": h["from"], "target": h["to"], "kind": h["kind"],
                "label": EDGE_LABELS.get(h["kind"], h["kind"].lower().replace("_", " ")),
                "confidence": h.get("confidence", ""),
            })
    # layer = longest distance from any source; the sink goes in the last layer
    layer = {n: 0 for n in nodes}
    for _ in range(len(nodes)):
        for e in edges:
            layer[e["target"]] = max(layer[e["target"]], layer[e["source"]] + 1)
    last = max(layer.values())
    for n in nodes:
        if nodes[n]["type"] == "admin":
            layer[n] = last
        nodes[n]["layer"] = layer[n]
        nodes[n]["depth"] = depth.get(n, 0)
    return {"title": title, "nodes": list(nodes.values()), "edges": edges,
            "summary": report.get("summary", {})}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help="--json output file, or a fixture dir with --run")
    ap.add_argument("--run", action="store_true", help="treat input as a fixture dir and run the tool")
    ap.add_argument("--title", default=None)
    ap.add_argument("--out", default=str(HERE))
    a = ap.parse_args()
    if a.run:
        raw = subprocess.run([sys.executable, str(TOOL), a.input, "--json"],
                             capture_output=True, text=True).stdout
        name = Path(a.input.rstrip("/")).name
    else:
        raw = Path(a.input).read_text()
        name = None
    title = a.title or ("Figure 2: Escalation paths to cluster-admin" + (f" (examples/{name})" if name else ""))
    try:
        report = json.loads(raw)
    except ValueError:
        sys.exit("input is not the tool's --json output")
    if not report.get("paths"):
        sys.exit("no escalation paths in this report, nothing to draw")
    data = build(report, title)
    out = Path(a.out)
    (out / "graph-data.json").write_text(json.dumps(data, indent=2) + "\n")
    (out / "graph-data.js").write_text("window.GRAPH_DATA = " + json.dumps(data, indent=2) + ";\n")
    print(f"{len(data['nodes'])} nodes, {len(data['edges'])} edges -> {out}")


if __name__ == "__main__":
    main()
