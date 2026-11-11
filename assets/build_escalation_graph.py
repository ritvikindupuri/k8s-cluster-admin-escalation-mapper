"""Renders assets/escalation-graph-primitives.png.

Needs matplotlib (docs only, not needed to run the tool). The layout follows
the paths in examples/primitives; regenerate after those paths change.

  python3 assets/build_escalation_graph.py
"""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

HERE = Path(__file__).parent

POS = {
    "Group:team-a-devs": (0, 4),
    "User:dana@example.com": (0, 2.9),
    "NODE ROOT": (3.8, 4),
    "SA:ops/platform-admin": (6.4, 3.45),
    "SA:ci/release-bot": (0, 1.8),
    "SA:ci/rbac-sync": (0, 0.9),
    "SA:ci/policy-sync": (0, 0),
    "CLUSTER-ADMIN": (9.6, 1.9),
}
EDGES = [
    ("Group:team-a-devs", "NODE ROOT", "create pod (no Pod Security)"),
    ("NODE ROOT", "SA:ops/platform-admin", "steal SA token on node"),
    ("SA:ops/platform-admin", "CLUSTER-ADMIN", "already admin"),
    ("User:dana@example.com", "SA:ops/platform-admin", "impersonate"),
    ("SA:ci/release-bot", "CLUSTER-ADMIN", "bind clusterrole"),
    ("SA:ci/rbac-sync", "CLUSTER-ADMIN", "escalate role"),
    ("SA:ci/policy-sync", "CLUSTER-ADMIN", "mutating webhook"),
]

fig, ax = plt.subplots(figsize=(14, 6.5))
ax.set_xlim(-1.6, 11.2)
ax.set_ylim(-0.7, 4.8)
ax.axis("off")
W, H = 2.5, 0.5


def col(name):
    if name == "CLUSTER-ADMIN":
        return "#f4b6b6"
    if name == "NODE ROOT":
        return "#ffe1a8"
    return "#dbe8fb"


for src, dst, label in EDGES:
    x1, y1 = POS[src]
    x2, y2 = POS[dst]
    ax.add_patch(
        FancyArrowPatch(
            (x1 + W / 2, y1),
            (x2 - W / 2, y2),
            arrowstyle="-|>",
            mutation_scale=16,
            lw=1.6,
            color="#444",
            connectionstyle="arc3,rad=0.0",
        )
    )
    mx, my = (x1 + W / 2 + x2 - W / 2) / 2, (y1 + y2) / 2
    ax.text(
        mx,
        my + 0.22,
        label,
        ha="center",
        fontsize=9,
        color="#a33",
        bbox=dict(fc="white", ec="none", pad=1),
    )

for name, (x, y) in POS.items():
    ax.add_patch(
        FancyBboxPatch(
            (x - W / 2, y - H / 2),
            W,
            H,
            boxstyle="round,pad=0.05",
            fc=col(name),
            ec="#333",
            lw=1.2,
        )
    )
    ax.text(
        x,
        y,
        name,
        ha="center",
        va="center",
        fontsize=10,
        weight="bold" if name in ("CLUSTER-ADMIN", "NODE ROOT") else None,
    )

ax.set_title("Escalation paths to cluster-admin (examples/primitives)", fontsize=14, weight="bold")
plt.savefig(HERE / "escalation-graph-primitives.png", dpi=150, bbox_inches="tight")
