# Fixtures

Inert YAML in the shapes this skill reads. Names are invented; nothing here should be applied to a cluster. This folder is not part of the shipped zip.

| Folder | What it models | Expected result |
| --- | --- | --- |
| `keda-style/` | A namespace admin who can write KEDA-style custom resources, an operator service account with cluster-wide `get` on everything and Job creation, `kube-system` exempt from Pod Security, and a cluster-admin bot. Modeled on the namespace-admin to cluster-admin write-up for KEDA (kedacore/keda GHSA-637c-6jxx-4rwm). Trimmed; not the release manifest. | with `--psa-exempt-ns kube-system`: 2 paths (1 high, 1 high with a heuristic hop); exit 1 |
| `nodes-proxy/` | A monitoring agent given `nodes/proxy get` to scrape kubelet metrics, running next to a cluster-admin installer. Based on the 2026 `nodes/proxy GET` WebSocket exec research. | 1 high path; exit 1 |
| `primitives/` | One identity per primitive: bind, escalate, impersonate, CSR approval, mutating webhook, pod-create into a namespace with no Pod Security label, plus a restricted namespace as a control, and an aggregated role that leaks secrets | 5 high paths, 3 exposures (CSR is an exposure on Kubernetes 1.22+); the restricted-namespace identity has no path; exit 1 |
| `anonymous-tokens/` | `system:unauthenticated` allowed to read secrets in a namespace holding a token secret for a cluster-admin SA; `system:authenticated` allowed to request tokens there | 2 critical paths; exit 1 |
| `clean/` | Restricted namespace, narrow role pinned by resourceNames, one admin group | 0 paths; exit 0 |

Extra fixture for the HTML graph: `synthetic-large/rbac.json` is a larger invented cluster (10 paths, 15 nodes, up to 4 hops, mixed identity types) used by `tests/render_check.py`. It has no expected text output.

```bash
python3 scripts/escalation_map.py examples/keda-style --psa-exempt-ns kube-system
python3 scripts/escalation_map.py examples/nodes-proxy
python3 scripts/escalation_map.py examples/primitives
python3 scripts/escalation_map.py examples/anonymous-tokens
python3 scripts/escalation_map.py examples/clean
```

`examples/expected/` holds the output of each run.
