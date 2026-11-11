---
name: k8s-cluster-admin-escalation-mapper
description: "Map how a Kubernetes identity gets to cluster-admin through chained RBAC hops. Use when someone asks who can really become cluster-admin, wants a privilege-escalation path analysis of a cluster's RBAC (YAML dump or kubectl get -o yaml), a namespace-admin to cluster-admin review for a multi-tenant cluster, or asks whether bind, escalate, impersonate, pods/exec, nodes/proxy, secrets or pod-create grants chain together, including operator service accounts."
---

# Kubernetes Cluster-Admin Escalation Mapper

Checkers tell you that a role is risky. This skill tells you where it leads.

A namespace admin on a shared cluster is not cluster-admin. But if they can write the custom resources of an operator whose service account reads every Secret, and one of those Secrets holds a deploy bot's token, they are. That is a chain of three ordinary-looking grants. Linting each role on its own will not show it; you have to put the roles, bindings, service accounts and pods in one graph and search it.

`scripts/escalation_map.py` does that. It reads RBAC and workload YAML (or JSON), builds the graph, and prints the shortest chain from each identity to cluster-admin with each hop explained, followed by the smallest set of grants whose removal breaks the most chains.

It is read-only and offline. It parses files you hand it and never talks to a cluster.

## Getting the input

Any of these work, mixed freely:

```bash
kubectl get clusterroles,clusterrolebindings,roles,rolebindings,serviceaccounts,namespaces -A -o yaml > rbac.yaml
kubectl get deploy,sts,ds,job,cronjob,pod -A -o yaml > workloads.yaml     # needed for pod hops
helm template ... > rendered.yaml
```

Workloads matter. Without them the tool can still see who can create pods or read secrets, but it cannot tell which service accounts are in use, which pods are privileged, or what `pods/exec` or `nodes/proxy` would land on.

Secret values are not needed and should not be copied around. If you want the tool to know which service accounts have long-lived token secrets, give it a trimmed list that keeps only `kind`, `type`, `metadata.name`, `metadata.namespace` and the `kubernetes.io/service-account.name` annotation. Anything else is ignored.

Pipe with `-` for stdin (separate documents with `---`), pass directories, or mix. Templated files with `{{ }}` are skipped with a warning; render them first.

## Running it

```bash
python3 scripts/escalation_map.py rbac.yaml workloads.yaml
python3 scripts/escalation_map.py dump/ --from SA:payments/ci        # one identity (substring match)
python3 scripts/escalation_map.py dump/ --pods                       # also "if this pod is popped"
python3 scripts/escalation_map.py dump/ --psa-exempt-ns kube-system,monitoring
python3 scripts/escalation_map.py dump/ --json > paths.json
python3 scripts/escalation_map.py dump/ --dot | dot -Tsvg > paths.svg
```

Exit status: `0` no path at or above `--fail-on` (default `high`), `1` there is one, `2` bad input.

## What counts as a hop

| Hop | Meaning |
| --- | --- |
| Already admin | `*` on `*` cluster-wide, or the `system:masters` group |
| bind / escalate | bind with create on role bindings, escalate with edit or create on roles, including pinned `resourceNames` |
| impersonate | users, groups (needs a user too, as the API requires), service accounts |
| Create or patch pods and workload controllers | take over any service account in that namespace; and node root when the namespace does not enforce Pod Security `baseline` or `restricted` |
| pods/exec, attach, ephemeralcontainers | land in running pods in that namespace and take their tokens |
| secrets get, list, watch | reach service accounts through token secrets (precise when token secrets are in your input, assumed otherwise) |
| create secrets | mint a token secret for any service account in the namespace |
| serviceaccounts/token create | request a token for named service accounts |
| nodes/proxy | `get` alone reaches the kubelet's exec endpoint; lands in every pod |
| CSR approval | create + approve on the client signer: standing exposure (Kubernetes 1.22+ rejects `O=system:masters` on `kube-apiserver-client`, so it is no longer a complete path on stock clusters) |
| Mutating webhook configurations | rewrite every pod admitted anywhere |
| `aws-auth` ConfigMap write | map an IAM role to `system:masters` on EKS clusters still using it |
| Workload to node | privileged, hostPID, sensitive hostPath, `SYS_ADMIN` and similar are folded in as node-root hops |
| Operator confused deputy | heuristic: a tenant who can write an operator's custom resources borrows the operator's wider identity |

Other things it handles: aggregated ClusterRoles are expanded, RoleBindings to ClusterRoles are scoped to the namespace, groups are inherited (`system:authenticated`, `system:unauthenticated`, `system:serviceaccounts`, `system:serviceaccounts:<ns>`), and a path open to a broad group is reported once with the other identities listed under "also open to".

## Reading the output

- **CRITICAL** means the chain starts at anyone without credentials, any authenticated identity, or any service account. **HIGH** means one specific identity.
- A hop marked `[likely]` rests on an assumption the tool states (no Pod Security label visible, a kubelet reachable from pods). `[heuristic]` is the operator confused-deputy rule; verify it by reading that operator's handling of the custom resource before you call it a finding. `--no-heuristics` turns it off.
- "Standing exposures" lists grants that are dangerous but do not complete a path in the data given, such as write access to validating webhooks or APIServices, and aggregated roles that quietly give every admin or edit holder secret access.
- The cut list is greedy: it picks the grant that removes the most remaining paths, then repeats. Removing those, in that order, is usually a smaller change than fixing path by path.

## Reporting

For each path, lead with the start and the end, then the hops in order with the grant that makes each possible, then the one change that breaks it. Do not paste secret values or tokens. Say what the input did not include (workloads, namespaces, secrets list) because that limits the answer.

## Limits

- Static analysis of what you give it. A cluster that is partly dumped gets a partly mapped graph.
- Policy engines (Kyverno, Gatekeeper, ValidatingAdmissionPolicy) are not evaluated. If one blocks privileged pods in a namespace, that hop may already be closed; the tool prints a note when it sees their objects.
- Whether the kubelet is reachable, whether the control plane is schedulable, cloud IAM, and in-cluster authenticators other than RBAC are not modelled. Use `--assume-control-plane` only for self-managed clusters where a pod can be pinned to a control-plane node.
- Paths are shortest-path, not all-paths. A second path through different grants may exist behind the one shown; fix, rerun, repeat.
- It never connects to a cluster, so it cannot confirm that a hop works, only that RBAC permits it. Do not test hops on a cluster you do not own or are not authorized to test.
