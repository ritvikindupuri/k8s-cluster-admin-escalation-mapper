"""Renders assets/architecture.png. Needs matplotlib and Pillow (docs only, not needed to run the tool)."""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Rectangle, Circle, Polygon
import matplotlib.image as mpimg
from pathlib import Path

HERE = Path(__file__).parent
BLUE = "#326CE5"; INK = "#1f2937"; GREY = "#6b7280"; LIGHT = "#eef2fb"; RED = "#d64545"; GREEN = "#2e9e6a"

fig, ax = plt.subplots(figsize=(16.8, 6.1), dpi=200)
ax.set_xlim(0, 168); ax.set_ylim(5, 66); ax.axis("off")

def tile(cx, cy, w=20, h=20):
    ax.add_patch(FancyBboxPatch((cx-w/2, cy-h/2), w, h, boxstyle="round,pad=0,rounding_size=2.2",
                                fc=LIGHT, ec="#c9d4ee", lw=1.4, zorder=2))

def label(cx, title, sub, y=28):
    ax.text(cx, y, title, ha="center", va="top", fontsize=11.5, fontweight="bold", color=INK, zorder=5)
    ax.text(cx, y-4.2, sub, ha="center", va="top", fontsize=9.5, color=GREY, zorder=5, linespacing=1.4)

def arrow(x0, x1, y=40):
    ax.add_patch(FancyArrowPatch((x0, y), (x1, y), arrowstyle="-|>", mutation_scale=18, lw=2, color=BLUE, zorder=3))

XS = [14, 42, 70, 98, 126, 153]
CY = 40

# 1 cluster: official Kubernetes logo
tile(XS[0], CY)
img = mpimg.imread(HERE / "icons" / "k8s.png")
ax.imshow(img, extent=(XS[0]-7.5, XS[0]+7.5, CY-7.3, CY+7.7), zorder=4, aspect="auto")
label(XS[0], "Kubernetes cluster", "RBAC objects\nand workloads")

# 2 kubectl: terminal window
tile(XS[1], CY)
x, y = XS[1], CY
ax.add_patch(FancyBboxPatch((x-7.5, y-6), 15, 12, boxstyle="round,pad=0,rounding_size=1", fc="#111827", ec="none", zorder=4))
ax.add_patch(Rectangle((x-7.5, y+3.4), 15, 2.6, fc="#374151", ec="none", zorder=5))
for i, c in enumerate(["#ef4444", "#f59e0b", "#22c55e"]):
    ax.add_patch(Circle((x-5.6+i*1.9, y+4.7), 0.6, fc=c, ec="none", zorder=6))
ax.text(x-6, y+0.2, ">", color="#22c55e", fontsize=15, fontweight="bold", family="DejaVu Sans Mono", va="center", zorder=6)
ax.plot([x-2.8, x+4.8], [y-0.4, y-0.4], color="#e5e7eb", lw=2.2, solid_capstyle="round", zorder=6)
ax.plot([x-2.8, x+1.5], [y-3.2, y-3.2], color="#9ca3af", lw=2.2, solid_capstyle="round", zorder=6)
label(XS[1], "kubectl get", "export as YAML,\nno Secret values")

# 3 YAML files: stacked documents
tile(XS[2], CY)
x, y = XS[2], CY
for dx, dy in [(2.2, -1.8), (0, 0)]:
    pts = [(x-5.5+dx, y-7+dy), (x+3+dx, y-7+dy), (x+3+dx, y+4+dy), (x+0.5+dx, y+7+dy), (x-5.5+dx, y+7+dy)]
    ax.add_patch(Polygon(pts, closed=True, fc="white", ec=BLUE, lw=1.6, zorder=4+(dx==0)))
ax.add_patch(Polygon([(x+0.5, y+7), (x+0.5, y+4), (x+3, y+4)], closed=True, fc="#dbe5fb", ec=BLUE, lw=1.2, zorder=6))
for k, w in enumerate([7, 5, 7, 4]):
    ax.plot([x-4, x-4+w], [y+1.6-k*2.6, y+1.6-k*2.6], color=BLUE if k % 2 == 0 else GREY, lw=2, solid_capstyle="round", zorder=7)
label(XS[2], "Manifest files", "YAML or JSON\ndumps")

# 4 agent + skill: robot head
tile(XS[3], CY)
x, y = XS[3], CY
ax.add_patch(FancyBboxPatch((x-6.5, y-5.5), 13, 10.5, boxstyle="round,pad=0,rounding_size=2", fc="white", ec=BLUE, lw=2, zorder=4))
for ex in (-2.6, 2.6):
    ax.add_patch(Circle((x+ex, y+0.4), 1.25, fc=BLUE, ec="none", zorder=5))
ax.plot([x-2.5, x+2.5], [y-2.7, y-2.7], color=BLUE, lw=2, solid_capstyle="round", zorder=5)
ax.plot([x, x], [y+5, y+7.6], color=BLUE, lw=2, zorder=4)
ax.add_patch(Circle((x, y+8.2), 1.1, fc=RED, ec="none", zorder=5))
ax.add_patch(Rectangle((x-9, y-2), 2.5, 4, fc=BLUE, ec="none", zorder=4))
ax.add_patch(Rectangle((x+6.5, y-2), 2.5, 4, fc=BLUE, ec="none", zorder=4))
label(XS[3], "AI agent + skill", "SKILL.md tells it\nwhen and how to run")

# 5 graph engine
tile(XS[4], CY)
x, y = XS[4], CY
nodes = {"a": (x-6.5, y-4.5), "b": (x-1, y+4.8), "c": (x+2, y-3), "d": (x+7, y+3.5), "e": (x-6, y+5.5)}
edges = [("a", "b"), ("a", "c"), ("c", "d"), ("b", "d"), ("e", "b")]
for p, q in edges:
    ax.plot([nodes[p][0], nodes[q][0]], [nodes[p][1], nodes[q][1]], color="#9db3e6", lw=1.8, zorder=4)
for p, q in [("a", "c"), ("c", "d")]:
    ax.plot([nodes[p][0], nodes[q][0]], [nodes[p][1], nodes[q][1]], color=RED, lw=2.8, zorder=5)
for k, (nx, ny) in nodes.items():
    col = RED if k == "d" else ("#f59e0b" if k == "a" else BLUE)
    ax.add_patch(Circle((nx, ny), 1.7 if k in "ad" else 1.35, fc=col, ec="white", lw=1.2, zorder=6))
label(XS[4], "Escalation graph", "identities, grants,\nshortest paths")

# 6 report: document with check
tile(XS[5], CY)
x, y = XS[5], CY
ax.add_patch(FancyBboxPatch((x-6, y-7.5), 12, 15, boxstyle="round,pad=0,rounding_size=1", fc="white", ec=BLUE, lw=1.8, zorder=4))
for k in range(3):
    yy = y+4.2-k*3.6
    ax.add_patch(Rectangle((x-4.2, yy-0.6), 1.2, 1.2, fc=GREEN, ec="none", zorder=5))
    ax.plot([x-1.8, x+3.8-(k == 1)*1.6], [yy, yy], color=GREY, lw=1.8, solid_capstyle="round", zorder=5)
ax.add_patch(Circle((x+5, y-6.5), 3.3, fc=GREEN, ec="white", lw=1.6, zorder=7))
ax.plot([x+3.5, x+4.7, x+6.6], [y-6.4, y-7.6, y-5.3], color="white", lw=2.2, solid_capstyle="round", solid_joinstyle="round", zorder=8)
label(XS[5], "Report", "paths, cut list,\nJSON, DOT, exit code")

for a, b in zip(XS[:-1], XS[1:]):
    arrow(a+11.2, b-11.2, CY)

# offline boundary
ax.add_patch(FancyBboxPatch((XS[2]-13.5, 8), XS[5]-XS[2]+27, 46, boxstyle="round,pad=0,rounding_size=3",
             fc="none", ec=GREY, lw=1.3, ls=(0, (5, 4)), zorder=1))
ax.text((XS[2]+XS[5])/2, 11.5, "Offline and read-only: files in, report out, no connection to a cluster",
        ha="center", va="center", fontsize=10, color=GREY, style="italic", zorder=5)

ax.text(84, 62, "Figure 1: From a cluster's RBAC dump to the shortest paths to cluster-admin",
        ha="center", va="center", fontsize=14, fontweight="bold", color=INK)
fig.savefig(HERE / "architecture.png", facecolor="white", bbox_inches="tight", pad_inches=0.25)
