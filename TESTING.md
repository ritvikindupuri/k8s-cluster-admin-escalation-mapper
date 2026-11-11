# Testing

This is the write-up that used to live in the README: a live kind-cluster run, the bugs that run turned up, and the five bundled fixtures.

## Live kind cluster

Validated on **kind v0.27.0 / Kubernetes v1.32.2** on 10 November 2026. The cluster was local (no cloud) and was deleted afterwards.

A baseline dump of a stock kind control plane reported **0** escalation paths from non-system identities. Built-in `kube-system` controller ServiceAccounts are skipped as starting points; they can still show up as intermediate hops. Pass `--include-system` if you want them listed. The `aws-auth` hop stays off unless the dump looks like EKS, or you pass `--eks`.

We then seeded ten known escalation primitives, plus a few accounts that should stay clean, and re-ran the mapper on a real `kubectl get … -o yaml` dump plus a trimmed Secret list (metadata only, no values). **10 of 10** expected paths were found, with **0** false positives on the negative controls. A full dump parse and analysis took about 80 ms.

### Seeded paths

| Identity | What was seeded | Mapper | Live check |
| --- | --- | --- | --- |
| `Group:dev-team` | Create a pod as `SA:dev/deploy-bot` (already cluster-admin) | HIT | Confirmed: pod-create as that ServiceAccount succeeded |
| `Group:node-tenants` | Privileged / host-level pod in an unlabeled namespace, then steal a token on the node | HIT | Confirmed: pod-create in the unlabeled namespace was allowed |
| `SA:ops/rbac-manager` | `bind` cluster-admin onto yourself | HIT | Confirmed with a token-only client / `kubectl auth can-i` |
| `SA:ops/impersonator` | Impersonate a cluster-admin identity | HIT | Path is impersonating `SA:dev/deploy-bot`. A live `--as-group=system:masters` check also succeeded |
| `SA:iam/role-editor` | `escalate` on ClusterRoles | HIT | Confirmed with a token-only client / `kubectl auth can-i` |
| `SA:audit/secret-reader` | List secrets over a token Secret for `SA:kube-system/ci-admin` | HIT | Confirmed: token theft succeeded |
| `Group:tenant-a` | Operator confused-deputy (heuristic) into `widgets-operator`, which can read kube-system secrets | HIT | Detected as a `[heuristic]` path; not live-exploited |
| `Group:support` | `pods/exec` into a workload running as cluster-admin | HIT | Detected from the dump; not live-exec'd |
| `SA:monitoring/scraper` | `nodes/proxy` into that same workload through the kubelet | HIT | Detected from the dump; kubelet reachability was not proven |
| `Group:webhook-admins` | Write access to mutating webhooks | HIT | Detected from the dump; a webhook was not actually registered |

`SA:pki/csr-bot` can create and approve client CSRs, but it is reported as a standing exposure, not a path. On Kubernetes 1.22+ the API server rejects `O=system:masters` on the `kubernetes.io/kube-apiserver-client` signer. We saw that rejection on this 1.32.2 cluster.

### Negative controls

These were supposed to have no path, and they did not:

| Identity | Why it should be clean | Result |
| --- | --- | --- |
| `restricted-devs` | Pod-create only in a Pod Security `restricted` namespace | none |
| `auditors` | Can list secrets, but no token Secrets were in the trimmed list | none |
| `viewer` | Read-only `view` | none |
| `cm-editor` | Role pinned with `resourceNames` to a ConfigMap | none |

The PSA-restricted and read-only controls also stayed denied in live `kubectl auth can-i` checks.

### Bugs this run found, now fixed

All of these landed in [3c38653](https://github.com/ritvikindupuri/k8s-cluster-admin-escalation-mapper/commit/3c386534a27d425df6393167de5f595d571171e9):

- **kubectl `kind: List` YAML.** `kubectl` writes compact lists, with sequence items at the same column as the parent key. The parser used to reject that. The bundled fixtures were indented the other way, so the bug stayed hidden until we fed it a real dump.
- **Stock kube-system controllers.** Built-in control-plane ServiceAccounts are no longer used as starting points, unless you pass `--include-system`. They can still appear as intermediate hops.
- **`aws-auth`.** That hop is off unless the dump looks like EKS, or you pass `--eks`.
- **CSR to cluster-admin.** Create+approve of `kube-apiserver-client` CSRs is a standing exposure, not a path, because 1.22+ rejects `O=system:masters`.
- **Trimmed Secret lists.** Given metadata-only Secrets, the tool no longer invents token targets that were not in the input.

## Bundled fixtures

The five examples under `examples/` are invented, inert YAML. Do not apply them to a cluster. Expected output lives in `examples/expected/`. Re-run on 10 November 2026, all five still matched:

| Fixture | Command | Result | Exit |
| --- | --- | --- | --- |
| `keda-style` | `python3 scripts/escalation_map.py examples/keda-style --psa-exempt-ns kube-system` | 2 high paths: `Group:tenant-a-devs` reaches cluster-admin through the operator (heuristic), and the operator can create a pod as `SA:kube-system/platform-bot` | 1 |
| `nodes-proxy` | `python3 scripts/escalation_map.py examples/nodes-proxy` | 1 high path: `SA:monitoring/metrics-agent` reaches cluster-admin through `nodes/proxy` into a privileged DaemonSet | 1 |
| `primitives` | `python3 scripts/escalation_map.py examples/primitives` | 5 high paths (pod-create, webhook, escalate, bind, impersonate). `SA:ci/cert-bot` is a CSR exposure, not a path. The restricted-namespace identity has no path | 1 |
| `anonymous-tokens` | `python3 scripts/escalation_map.py examples/anonymous-tokens` | 2 critical paths: `system:unauthenticated` via secret read, `system:authenticated` via TokenRequest | 1 |
| `clean` | `python3 scripts/escalation_map.py examples/clean` | No paths | 0 |

To re-check:

```bash
python3 scripts/escalation_map.py examples/keda-style --psa-exempt-ns kube-system | diff -u examples/expected/keda-style.txt -
python3 scripts/escalation_map.py examples/nodes-proxy | diff -u examples/expected/nodes-proxy.txt -
python3 scripts/escalation_map.py examples/primitives | diff -u examples/expected/primitives.txt -
python3 scripts/escalation_map.py examples/anonymous-tokens | diff -u examples/expected/anonymous-tokens.txt -
python3 scripts/escalation_map.py examples/clean | diff -u examples/expected/clean.txt -
```

`diff` will be silent on a match. The first four commands exit `1` because they found paths; that is expected, and does not mean the diff failed.
