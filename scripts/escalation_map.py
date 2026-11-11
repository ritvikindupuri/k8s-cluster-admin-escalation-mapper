#!/usr/bin/env python3
"""Kubernetes RBAC escalation-path mapper. Python 3.9+, standard library only.

Reads Role / ClusterRole / RoleBinding / ClusterRoleBinding / ServiceAccount /
Namespace / workload manifests (YAML or JSON, including the List output of
`kubectl get ... -o yaml`) and builds a directed graph:

    identity --(capability)--> identity / workload / node root --> cluster-admin

then prints the shortest chain of hops from every non-admin identity, with an
explanation of each hop and the smallest set of grants to remove.

Read-only. It never talks to a cluster; it only parses files you give it.
Includes a small YAML loader for the subset used in manifests (documents,
mappings, lists, scalars, quotes, block scalars, simple flow forms).
"""

from __future__ import annotations

import argparse
import heapq

import json
import re
from typing import Any

class YAMLLiteError(Exception):
    pass


def looks_templated(text: str) -> bool:
    return "{{" in text or "{%" in text


def load_documents(text: str) -> list[Any]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if text.lstrip().startswith("{") or text.lstrip().startswith("["):
        try:
            val = json.loads(text)
            return val if isinstance(val, list) else [val]
        except json.JSONDecodeError:
            pass
    raw_docs = re.split(r"(?m)^---\s*(?:#.*)?$", text)
    docs: list[Any] = []
    for raw in raw_docs:
        raw = raw.strip("\n")
        if not raw.strip() or raw.strip() in {"...", "---"}:
            continue
        # drop a leading %YAML / %TAG
        lines = []
        for line in raw.split("\n"):
            if line.startswith("%"):
                continue
            if line.strip() == "...":
                break
            lines.append(line)
        if not any(l.strip() and not l.strip().startswith("#") for l in lines):
            continue
        docs.append(_Parser(lines).parse())
    return docs


class _Parser:
    def __init__(self, lines: list[str]):
        self.lines = lines
        self.i = 0

    def parse(self) -> Any:
        self._skip_blank()
        if self.i >= len(self.lines):
            return None
        indent = self._indent(self.lines[self.i])
        return self._value(indent)

    def _indent(self, line: str) -> int:
        return len(line) - len(line.lstrip(" "))

    def _raw(self) -> str | None:
        if self.i >= len(self.lines):
            return None
        return self.lines[self.i]

    def _skip_blank(self) -> None:
        while self.i < len(self.lines):
            s = self.lines[self.i]
            if s.strip() == "" or s.strip().startswith("#"):
                self.i += 1
                continue
            # keep document --- already split
            break

    def _value(self, indent: int) -> Any:
        self._skip_blank()
        if self.i >= len(self.lines):
            return None
        line = self.lines[self.i]
        if self._indent(line) < indent and line.strip():
            return None
        stripped = line.strip()
        if stripped.startswith("- " ) or stripped == "-":
            return self._list(indent)
        if stripped.startswith("[") or stripped.startswith("{"):
            self.i += 1
            rest = stripped
            # may be only the start; gather continuation if brackets unclosed
            while not _balanced(rest) and self.i < len(self.lines):
                rest += " " + self.lines[self.i].strip()
                self.i += 1
            if stripped.startswith("[") and _balanced(rest) and not rest.lstrip().startswith("{") or rest.lstrip().startswith("["):
                # if this is `key: [..]` we shouldn't be here; this is a root flow
                pass
            return _parse_flow(rest)
        # mapping or a bare scalar (unusual at indent in k8s)
        if ":" in stripped and not stripped.startswith(("'", '"')):
            # could be a plain scalar that happens to have a colon+space? YAML allows "foo: bar" as mapping
            return self._map(indent)
        self.i += 1
        return _parse_scalar(stripped)

    def _list(self, indent: int) -> list[Any]:
        out: list[Any] = []
        while self.i < len(self.lines):
            self._skip_blank()
            if self.i >= len(self.lines):
                break
            line = self.lines[self.i]
            ind = self._indent(line)
            if ind < indent:
                break
            if ind > indent:
                # dangling nested content without a key — stop
                break
            stripped = line.strip()
            if not (stripped.startswith("- ") or stripped == "-"):
                break
            item_body = stripped[1:].strip()  # after '-'
            self.i += 1
            if item_body == "" or item_body.startswith("#"):
                self._skip_blank()
                if self.i < len(self.lines) and self._indent(self.lines[self.i]) > indent:
                    out.append(self._value(self._indent(self.lines[self.i])))
                else:
                    out.append(None)
                continue
            # - key: value   OR  - value
            if item_body.startswith("[") or item_body.startswith("{"):
                chunk = item_body
                while not _balanced(chunk) and self.i < len(self.lines):
                    chunk += " " + self.lines[self.i].strip()
                    self.i += 1
                out.append(_parse_flow(chunk))
                continue
            if _is_map_start(item_body):
                key, rest = _split_key(item_body)
                node: dict[str, Any] = {}
                self._assign(node, key, rest, indent + 2)
                # following keys at indent+2 belong to this list map
                while self.i < len(self.lines):
                    self._skip_blank()
                    if self.i >= len(self.lines):
                        break
                    nxt = self.lines[self.i]
                    ni = self._indent(nxt)
                    ns = nxt.strip()
                    if ni <= indent:
                        break
                    if ns.startswith("- ") or ns == "-":
                        break
                    if ni >= indent + 2 and _is_map_start(ns) and not ns.startswith("-"):
                        k, r = _split_key(ns)
                        self.i += 1
                        self._assign(node, k, r, ni + 2 if r in (None, "") else ni + 2)
                        continue
                    break
                out.append(node)
            else:
                out.append(_parse_scalar(item_body))
        return out

    def _map(self, indent: int) -> dict[str, Any]:
        node: dict[str, Any] = {}
        while self.i < len(self.lines):
            self._skip_blank()
            if self.i >= len(self.lines):
                break
            line = self.lines[self.i]
            ind = self._indent(line)
            if ind < indent:
                break
            if ind > indent:
                break
            stripped = line.strip()
            if stripped.startswith("- ") or stripped == "-":
                break
            if not _is_map_start(stripped):
                break
            key, rest = _split_key(stripped)
            self.i += 1
            self._assign(node, key, rest, indent + 2)
        return node

    def _assign(self, node: dict[str, Any], key: str, rest: str | None, child_indent: int) -> None:
        if rest is None or rest == "" or rest.startswith("#"):
            # nested or empty
            self._skip_blank()
            if self.i >= len(self.lines):
                node[key] = None
                return
            nxt = self.lines[self.i]
            if not nxt.strip() or nxt.strip().startswith("#"):
                node[key] = None
                return
            ni = self._indent(nxt)
            if ni >= child_indent - 1 and ni > self._indent_of_current_key(child_indent):
                # child is more indented
                if ni >= child_indent or nxt.strip().startswith("-"):
                    node[key] = self._value(ni)
                    return
            node[key] = None
            return
        if rest in {"|", "|-", "|+", ">", ">-", ">+"}:
            node[key] = self._block(child_indent, folded=rest.startswith(">"))
            return
        if rest.startswith("&") or rest.startswith("!"):
            # tag or anchor — drop tag, ignore anchor name
            rest2 = re.sub(r"^(!\S*\s*|&\S+\s*)+", "", rest).strip()
            if not rest2:
                self._skip_blank()
                if self.i < len(self.lines) and self._indent(self.lines[self.i]) >= child_indent:
                    node[key] = self._value(self._indent(self.lines[self.i]))
                    return
                node[key] = None
                return
            rest = rest2
        if rest.startswith("[") or rest.startswith("{"):
            chunk = rest
            while not _balanced(chunk) and self.i < len(self.lines):
                chunk += " " + self.lines[self.i].strip()
                self.i += 1
            node[key] = _parse_flow(chunk)
            return
        node[key] = _parse_scalar(rest)

    def _indent_of_current_key(self, child_indent: int) -> int:
        return max(0, child_indent - 2)

    def _block(self, indent: int, folded: bool) -> str:
        chunks: list[str] = []
        while self.i < len(self.lines):
            line = self.lines[self.i]
            if line.strip() == "":
                chunks.append("")
                self.i += 1
                continue
            if self._indent(line) < indent:
                break
            chunks.append(line[indent:] if len(line) >= indent else line.lstrip())
            self.i += 1
        if folded:
            return " ".join(c if c else "\n" for c in chunks).strip()
        return "\n".join(chunks).rstrip("\n")


def _is_map_start(stripped: str) -> bool:
    if stripped.startswith("-"):
        return False
    return _split_key(stripped)[0] is not None and ":" in stripped


def _split_key(stripped: str) -> tuple[str | None, str | None]:
    # key: value  /  "key": value  /  'key': value
    stripped = stripped.split(" #", 1)[0].rstrip() if " #" in stripped and not _in_quotes_hash(stripped) else stripped
    m = re.match(r"^([A-Za-z0-9_./*?-]+)\s*:(?:\s+(.*))?$", stripped)
    if m and not stripped.startswith(("'", '"')):
        rest = m.group(2)
        return m.group(1), (rest if rest is not None else "")
    if stripped.startswith('"') or stripped.startswith("'"):
        q = stripped[0]
        end = 1
        while end < len(stripped):
            if stripped[end] == "\\" and q == '"':
                end += 2
                continue
            if stripped[end] == q:
                break
            end += 1
        else:
            return None, None
        key = _parse_scalar(stripped[: end + 1])
        rest = stripped[end + 1 :].lstrip()
        if not rest.startswith(":"):
            return None, None
        rest = rest[1:].lstrip()
        return str(key), rest
    # unusual keys with colons in quotes already handled
    return None, None


def _in_quotes_hash(s: str) -> bool:
    # crude: if odd number of quotes before #
    return False


def _balanced(s: str) -> bool:
    brace = 0
    brack = 0
    quote = ""
    i = 0
    while i < len(s):
        ch = s[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in ('"', "'"):
            quote = ch
        elif ch == "{":
            brace += 1
        elif ch == "}":
            brace -= 1
        elif ch == "[":
            brack += 1
        elif ch == "]":
            brack -= 1
        i += 1
    return brace == 0 and brack == 0 and quote == ""


def _parse_flow(s: str) -> Any:
    s = s.strip()
    if s.startswith("{") and s.endswith("}"):
        inner = s[1:-1].strip()
        if not inner:
            return {}
        out: dict[str, Any] = {}
        for part in _split_flow(inner):
            if ":" not in part:
                continue
            k, v = part.split(":", 1)
            out[k.strip().strip("\"'") ] = _parse_flow(v.strip()) if v.strip()[:1] in "[{" else _parse_scalar(v.strip())
        return out
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        return [
            _parse_flow(p.strip()) if p.strip()[:1] in "[{" else _parse_scalar(p.strip())
            for p in _split_flow(inner)
            if p.strip()
        ]
    return _parse_scalar(s)


def _split_flow(inner: str) -> list[str]:
    parts: list[str] = []
    buf = ""
    depth = 0
    quote = ""
    for i, ch in enumerate(inner):
        if quote:
            buf += ch
            if ch == "\\" and quote == '"':
                continue
            if ch == quote:
                quote = ""
            continue
        if ch in ('"', "'"):
            quote = ch
            buf += ch
            continue
        if ch in "[{":
            depth += 1
            buf += ch
            continue
        if ch in "]}":
            depth -= 1
            buf += ch
            continue
        if ch == "," and depth == 0:
            parts.append(buf)
            buf = ""
            continue
        buf += ch
    if buf.strip():
        parts.append(buf)
    return parts


def _parse_scalar(s: str) -> Any:
    s = s.strip()
    if s.endswith(",") and not (s.startswith("'") or s.startswith('"')):
        s = s[:-1].rstrip()
    if not s or s == "~" or s.lower() in {"null", "none"}:
        return None
    if s.lower() in {"true", "yes", "on"}:
        return True
    if s.lower() in {"false", "no", "off"}:
        return False
    if s.startswith("'") and s.endswith("'") and len(s) >= 2:
        return s[1:-1].replace("''", "'")
    if s.startswith('"') and s.endswith('"') and len(s) >= 2:
        return bytes(s[1:-1], "utf-8").decode("unicode_escape") if "\\" in s else s[1:-1]
    # Helm / unquoted
    if re.fullmatch(r"-?\d+", s):
        try:
            return int(s)
        except ValueError:
            return s
    if re.fullmatch(r"-?\d+\.\d+", s):
        try:
            return float(s)
        except ValueError:
            return s
    return s


# helpers used by auditors
def dig(obj: Any, *path: str, default: Any = None) -> Any:
    cur = obj
    for p in path:
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        else:
            return default
    return cur


def as_list(val: Any) -> list[Any]:
    if val is None:
        return []
    if isinstance(val, list):
        return val
    return [val]

import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

GOAL = "CLUSTER_ADMIN"
NODE_ROOT = "NODE_ROOT"
SEV_ORDER = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}

CORE_GROUPS = {
    "", "apps", "batch", "extensions", "policy", "networking.k8s.io", "storage.k8s.io",
    "rbac.authorization.k8s.io", "admissionregistration.k8s.io", "apiextensions.k8s.io",
    "apiregistration.k8s.io", "autoscaling", "certificates.k8s.io", "coordination.k8s.io",
    "discovery.k8s.io", "events.k8s.io", "flowcontrol.apiserver.k8s.io", "node.k8s.io",
    "scheduling.k8s.io", "authentication.k8s.io", "authorization.k8s.io", "metrics.k8s.io",
    "resource.k8s.io", "internal.apiserver.k8s.io", "snapshot.storage.k8s.io",
}
WORKLOAD_KINDS = {
    "Pod": ("", "pods"), "Deployment": ("apps", "deployments"), "StatefulSet": ("apps", "statefulsets"),
    "DaemonSet": ("apps", "daemonsets"), "ReplicaSet": ("apps", "replicasets"),
    "Job": ("batch", "jobs"), "CronJob": ("batch", "cronjobs"),
    "ReplicationController": ("", "replicationcontrollers"),
}
# (group, resource) pairs whose create (or update/patch on controllers) puts a pod spec in your hands
POD_MAKERS = [("", "pods"), ("apps", "deployments"), ("apps", "daemonsets"), ("apps", "statefulsets"),
              ("apps", "replicasets"), ("batch", "jobs"), ("batch", "cronjobs"),
              ("", "replicationcontrollers")]
SENSITIVE_HOSTPATHS = ("/", "/etc", "/etc/kubernetes", "/var/lib/kubelet", "/var/lib", "/var/run",
                       "/run", "/proc", "/root", "/home", "/var/log", "/sys", "/dev", "/boot", "/lib",
                       "/usr", "/var", "/opt")
ESCAPE_CAPS = {"SYS_ADMIN", "SYS_MODULE", "SYS_RAWIO", "DAC_READ_SEARCH", "SYS_BOOT", "BPF"}
EDGE_FIX = {
    "ADMIN": "Replace the wildcard with the specific verbs and resources this identity needs.",
    "BIND": "Remove the `bind` verb on roles/clusterroles, or pin it with resourceNames to the exact low-privilege roles that may be granted.",
    "ESCALATE": "Remove the `escalate` verb. Nothing but the RBAC controller or a break-glass role should hold it.",
    "IMPERSONATE": "Remove `impersonate`, or pin it with resourceNames to the single user or group a proxy really needs.",
    "POD_CREATE": "Drop create/patch on pods and workload controllers from this role, or confine the namespace with Pod Security `restricted` and deploy through a pipeline identity instead.",
    "EXEC": "Remove pods/exec, pods/attach and pods/ephemeralcontainers from day-to-day roles. Keep them in a time-boxed break-glass role.",
    "SECRETS_READ": "Remove get/list/watch on secrets. List returns every secret value too, so `list` is as bad as `get`. Mount what the workload needs instead.",
    "SECRET_MINT": "Remove `create` on secrets. A secret of type kubernetes.io/service-account-token mints a long-lived token for any ServiceAccount in the namespace.",
    "TOKEN_REQUEST": "Remove create on serviceaccounts/token, or pin to the single ServiceAccount via resourceNames.",
    "NODES_PROXY": "Remove nodes/proxy. Monitoring agents should use the nodes/metrics, nodes/stats and nodes/log subresources (fine-grained kubelet authorization, GA in Kubernetes 1.36), and port 10250 should not be reachable from pods.",
    "CSR": "Remove update on certificatesigningrequests/approval, or restrict `approve` on signers to a non-client-auth signer name.",
    "WEBHOOK": "Remove write access to mutatingwebhookconfigurations. Only the controller that owns the webhook should patch it, and only its own configuration by name.",
    "AWS_AUTH": "Remove write access to configmap aws-auth in kube-system, or migrate to EKS access entries.",
    "DEPUTY": "Check that the operator rejects references to tenant-controlled endpoints and uses a namespaced identity per tenant. Limit who can create the operator's custom resources to a dedicated role.",
    "WL_SA": "Give the workload its own ServiceAccount with only the permissions it needs and set automountServiceAccountToken: false where it does not call the API.",
    "WL_ESCAPE": "Remove privileged, hostPID, hostPath and dangerous capabilities from the workload, and enforce Pod Security `baseline` or `restricted` on the namespace.",
    "NODE_TOKENS": "Keep powerful ServiceAccounts away from nodes that run tenant or internet-facing workloads (taints, dedicated node pools).",
}


# ----------------------------------------------------------------- model

@dataclass(frozen=True)
class Grant:
    ns: str | None          # None = cluster-wide
    groups: tuple
    resources: tuple
    verbs: tuple
    names: tuple | None
    via: str


@dataclass
class Edge:
    dst: str
    kind: str
    text: str
    how: str
    via: str
    cost: int = 1
    conf: str = "certain"   # certain | likely | heuristic


@dataclass
class Workload:
    ns: str
    kind: str
    name: str
    sa: str
    escapes: list
    labels: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"WL:{self.ns}/{self.kind}/{self.name}"


def _t(v) -> tuple:
    return tuple(str(x) for x in as_list(v))


def res_match(patterns, res: str) -> bool:
    for p in patterns:
        if p == "*" or p == res:
            return True
        if p.endswith("/*") and res.startswith(p[:-1]):
            return True
        if p.startswith("*/") and res.endswith(p[1:]):
            return True
    return False


def grant_allows(g: Grant, verb: str, group: str, res: str) -> bool:
    if "*" not in g.verbs and verb not in g.verbs:
        return False
    if "*" not in g.groups and group not in g.groups:
        return False
    return res_match(g.resources, res)


class Cluster:
    def __init__(self, psa_exempt=(), assume_control_plane=False, heuristics=True):
        self.roles: dict = {}            # (kind, ns, name) -> doc
        self.bindings: list = []
        self.sas: set = set()
        self.namespaces: dict = {}
        self.workloads: list = []
        self.token_secrets: dict = defaultdict(set)   # ns -> {sa names}
        self.admission_kinds: set = set()
        self.psa_exempt = set(psa_exempt)
        self.assume_control_plane = assume_control_plane
        self.heuristics = heuristics
        self.users: set = set()
        self.groups: set = {"system:masters", "system:authenticated", "system:unauthenticated"}
        self._agg_cache: dict = {}
        self._grants: dict = {}
        self._edges: dict = {}
        self.notes: list = []
        self.counts: dict = defaultdict(int)

    # -- loading
    def add_doc(self, doc, origin: str):
        if not isinstance(doc, dict):
            return
        kind = doc.get("kind")
        if kind and str(kind).endswith("List") and isinstance(doc.get("items"), list):
            for it in doc["items"]:
                self.add_doc(it, origin)
            return
        meta = doc.get("metadata") or {}
        name = meta.get("name")
        ns = meta.get("namespace")
        self.counts[kind] += 1
        if kind in ("ClusterRole", "Role") and name:
            self.roles[(kind, ns if kind == "Role" else None, name)] = doc
        elif kind in ("ClusterRoleBinding", "RoleBinding") and name:
            self.bindings.append(doc)
        elif kind == "ServiceAccount" and name:
            self.sas.add((ns or "default", name))
        elif kind == "Namespace" and name:
            self.namespaces[name] = meta.get("labels") or {}
        elif kind in WORKLOAD_KINDS and name:
            self._add_workload(doc, kind, name, ns or "default", meta)
        elif kind == "Secret" and (doc.get("type") == "kubernetes.io/service-account-token"):
            sa = ((meta.get("annotations") or {}).get("kubernetes.io/service-account.name"))
            if sa:
                self.token_secrets[ns or "default"].add(sa)
        elif kind in ("ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding", "ClusterPolicy",
                      "Policy", "ConstraintTemplate"):
            self.admission_kinds.add(kind)

    def _add_workload(self, doc, kind, name, ns, meta):
        if kind == "Pod" and meta.get("ownerReferences"):
            return
        spec = doc.get("spec") or {}
        if kind == "CronJob":
            spec = dig(spec, "jobTemplate", "spec", "template", "spec", default={}) or {}
        elif kind != "Pod":
            spec = dig(spec, "template", "spec", default={}) or {}
        if not isinstance(spec, dict):
            return
        sa = spec.get("serviceAccountName") or spec.get("serviceAccount") or "default"
        self.workloads.append(Workload(ns, kind, name, sa, escape_reasons(spec), meta.get("labels") or {}))
        self.sas.add((ns, sa))

    # -- indexes (built after loading)
    def finalize(self):
        self.all_ns = set(self.namespaces)
        for b in self.bindings:
            self.all_ns.add((b.get("metadata") or {}).get("namespace") or "")
        for ns, _ in self.sas:
            self.all_ns.add(ns)
        for w in self.workloads:
            self.all_ns.add(w.ns)
        self.all_ns.discard("")
        # subject index: subject key -> list of Grant
        self.subject_grants = defaultdict(list)
        self.sa_by_ns = defaultdict(set)
        for b in self.bindings:
            self._index_binding(b)
        for ns in self.all_ns:
            self.sas.add((ns, "default"))
        for ns, n in self.sas:
            self.sa_by_ns[ns].add(n)
        self.wl_by_ns = defaultdict(list)
        for w in self.workloads:
            self.wl_by_ns[w.ns].append(w)
        self.operator_groups = self._operator_group_index() if self.heuristics else {}

    def _subject_key(self, s, default_ns):
        kind = s.get("kind")
        name = s.get("name")
        if not name:
            return None
        if kind == "ServiceAccount":
            ns = s.get("namespace") or default_ns
            if not ns:
                return None
            return f"SA:{ns}/{name}"
        if kind == "User":
            return f"User:{name}"
        if kind == "Group":
            return f"Group:{name}"
        return None

    def _index_binding(self, b):
        meta = b.get("metadata") or {}
        bkind = b.get("kind")
        bns = meta.get("namespace") if bkind == "RoleBinding" else None
        ref = b.get("roleRef") or {}
        rkind, rname = ref.get("kind"), ref.get("name")
        if not rname:
            return
        scope = bns  # None = cluster
        rules = self.resolve_rules(rkind, bns if rkind == "Role" else None, rname)
        if rules is None:
            self.notes.append(f"{bkind}/{meta.get('name')} references missing {rkind}/{rname} (not in the input)")
            return
        bdesc = f"{bkind}/{(bns + '/') if bns else ''}{meta.get('name')}"
        rdesc = f"{rkind}/{rname}"
        for s in as_list(b.get("subjects")):
            key = self._subject_key(s, bns)
            if not key:
                continue
            self._register(key)
            via = f"{rdesc} via {bdesc}"
            for r in rules:
                self.subject_grants[key].append(self._mk_grant(scope, r, via))

    def _register(self, key):
        if key.startswith("User:"):
            self.users.add(key[5:])
        elif key.startswith("Group:"):
            self.groups.add(key[6:])
        elif key.startswith("SA:"):
            ns, n = key[3:].split("/", 1)
            self.sas.add((ns, n))
            self.all_ns.add(ns)

    @staticmethod
    def _mk_grant(scope, rule, via):
        if not isinstance(rule, dict):
            return Grant(scope, (), (), (), None, via)
        names = rule.get("resourceNames")
        return Grant(scope, _t(rule.get("apiGroups")), _t(rule.get("resources")), _t(rule.get("verbs")),
                     _t(names) if names else None, via)

    def resolve_rules(self, kind, ns, name):
        doc = self.roles.get((kind, ns, name))
        if doc is None:
            return None
        rules = [r for r in as_list(doc.get("rules")) if isinstance(r, dict)]
        agg = doc.get("aggregationRule")
        if kind == "ClusterRole" and agg:
            ck = ("agg", name)
            if ck in self._agg_cache:
                return self._agg_cache[ck]
            self._agg_cache[ck] = rules  # break cycles
            sels = as_list(dig(agg, "clusterRoleSelectors", default=[]))
            for (k, n2, nm), d2 in list(self.roles.items()):
                if k != "ClusterRole" or nm == name:
                    continue
                labels = dig(d2, "metadata", "labels", default={}) or {}
                if any(self._selector_matches(sel, labels) for sel in sels):
                    sub = self.resolve_rules("ClusterRole", None, nm) or []
                    rules = rules + [x for x in sub if x not in rules]
            self._agg_cache[ck] = rules
        return rules

    @staticmethod
    def _selector_matches(sel, labels):
        ml = sel.get("matchLabels") or {}
        if any(str(labels.get(k)) != str(v) for k, v in ml.items()):
            return False
        for ex in as_list(sel.get("matchExpressions")):
            key, op, vals = ex.get("key"), ex.get("operator"), as_list(ex.get("values"))
            have = key in labels
            if op == "Exists" and not have:
                return False
            if op == "DoesNotExist" and have:
                return False
            if op == "In" and str(labels.get(key)) not in [str(v) for v in vals]:
                return False
            if op == "NotIn" and have and str(labels.get(key)) in [str(v) for v in vals]:
                return False
        return bool(ml or sel.get("matchExpressions"))

    # -- effective grants
    def groups_of(self, key):
        if key.startswith("SA:"):
            ns = key[3:].split("/", 1)[0]
            return ["system:serviceaccounts", f"system:serviceaccounts:{ns}", "system:authenticated"]
        if key.startswith("User:"):
            return ["system:unauthenticated"] if key[5:] == "system:anonymous" else ["system:authenticated"]
        return []

    def role_node_grants(self, key):
        # ROLE:ClusterRole/name@*   ROLE:Role/ns/name@ns
        body, scope = key[5:].rsplit("@", 1)
        parts = body.split("/")
        scope = None if scope == "*" else scope
        if parts[0] == "ClusterRole":
            rules = self.resolve_rules("ClusterRole", None, parts[1]) or []
            via = f"ClusterRole/{parts[1]}"
        else:
            rules = self.resolve_rules("Role", parts[1], parts[2]) or []
            via = f"Role/{parts[1]}/{parts[2]}"
        return [self._mk_grant(scope, r, via) for r in rules]

    def grants(self, key):
        if key in self._grants:
            return self._grants[key]
        if key.startswith("ROLE:"):
            out = self.role_node_grants(key)
        elif key.startswith("NSADMIN:"):
            ns = key[8:]
            out = [Grant(ns, ("*",), ("*",), ("*",), None, f"admin of namespace {ns}")]
        else:
            out = list(self.subject_grants.get(key, []))
            for g in self.groups_of(key):
                for gr in self.subject_grants.get(f"Group:{g}", []):
                    out.append(Grant(gr.ns, gr.groups, gr.resources, gr.verbs, gr.names,
                                     f"{gr.via} (inherited from group {g})"))
            if key == "Group:system:masters":
                out.append(Grant(None, ("*",), ("*",), ("*",), None, "built-in group system:masters (bypasses RBAC)"))
        self._grants[key] = out
        return out

    # -- queries
    def hits(self, grants, verbs, group, res, cluster_scoped=False):
        """Return {ns|None: [names|None, [via...]]}. None key = cluster-wide."""
        out: dict = {}
        for g in grants:
            if cluster_scoped and g.ns is not None:
                continue
            for v in verbs:
                if not grant_allows(g, v, group, res):
                    continue
                if v == "create" and g.names:
                    continue
                cur = out.setdefault(g.ns, [set(), []])
                if g.names is None or cur[0] is None:
                    cur[0] = None
                else:
                    cur[0] |= set(g.names)
                if g.via not in cur[1]:
                    cur[1].append(g.via)
        return out

    def expand(self, hits):
        """Collapse cluster-wide + per-namespace hits to {ns: (names, via)} over known namespaces."""
        res: dict = {}
        cluster = hits.get(None)
        for ns in sorted(self.all_ns):
            parts = []
            if cluster:
                parts.append(cluster)
            if ns in hits:
                parts.append(hits[ns])
            if not parts:
                continue
            names: set | None = set()
            vias: list = []
            for p in parts:
                if p[0] is None or names is None:
                    names = None
                else:
                    names |= p[0]
                for v in p[1]:
                    if v not in vias:
                        vias.append(v)
            res[ns] = (names, vias)
        return res

    def is_admin(self, grants):
        for g in grants:
            if g.ns is None and "*" in g.verbs and "*" in g.groups and "*" in g.resources and g.names is None:
                return g
        return None

    def admits_privileged(self, ns):
        """-> (admits, conf, note)"""
        if ns in self.psa_exempt:
            return True, "certain", f"namespace {ns} is exempt from Pod Security Admission"
        labels = self.namespaces.get(ns)
        enforce = (labels or {}).get("pod-security.kubernetes.io/enforce")
        if enforce in ("baseline", "restricted"):
            return False, "certain", f"namespace {ns} enforces Pod Security {enforce}"
        if labels is None:
            return True, "likely", f"no Namespace object for {ns} in the input, so no Pod Security label is visible"
        if enforce == "privileged":
            return True, "certain", f"namespace {ns} enforces Pod Security `privileged`"
        return True, "likely", f"namespace {ns} carries no Pod Security enforce label"

    def _operator_group_index(self):
        """SA key -> sorted custom API groups it reconciles. Only SAs that are strong on their own."""
        out = {}
        for key in {f"SA:{ns}/{n}" for ns, n in self.sas}:
            grants = self.grants(key)
            if not grants or self.is_admin(grants):
                continue
            strong = bool(self.hits(grants, ["get", "list"], "", "secrets").get(None)) or \
                any(self.hits(grants, ["create"], g, r).get(None) for g, r in POD_MAKERS) or \
                bool(self.hits(grants, ["get"], "*", "*").get(None))
            if not strong:
                continue
            groups = set()
            for g in grants:
                if g.ns is not None:
                    continue
                if not ({"get", "list", "watch", "update", "patch", "*"} & set(g.verbs)):
                    continue
                for grp in g.groups:
                    if grp not in CORE_GROUPS and grp != "*":
                        groups.add(grp)
            if groups:
                out[key] = sorted(groups)
        return out


def escape_reasons(spec: dict) -> list:
    """Why a pod spec hands out node root (or node secrets). Review-only, no payloads."""
    out = []
    if truthy_(spec.get("hostPID")):
        out.append("hostPID")
    for vol in as_list(spec.get("volumes")):
        hp = dig(vol, "hostPath", "path") if isinstance(vol, dict) else None
        if hp:
            p = str(hp).rstrip("/") or "/"
            if p.endswith(".sock") or p in SENSITIVE_HOSTPATHS or p.startswith("/var/lib/kubelet") \
                    or p.startswith("/etc/kubernetes"):
                out.append(f"hostPath {hp}")
    for c in as_list(spec.get("containers")) + as_list(spec.get("initContainers")):
        if not isinstance(c, dict):
            continue
        sc = c.get("securityContext") or {}
        if truthy_(sc.get("privileged")):
            out.append(f"privileged container {c.get('name', '?')}")
        caps = {str(x).upper() for x in as_list(dig(sc, "capabilities", "add", default=[]))}
        bad = sorted(caps & ESCAPE_CAPS)
        if bad:
            out.append(f"capability {','.join(bad)} on {c.get('name', '?')}")
        if "SYS_PTRACE" in caps and truthy_(spec.get("hostPID")):
            out.append("SYS_PTRACE with hostPID")
    seen, uniq = set(), []
    for r in out:
        if r not in seen:
            seen.add(r)
            uniq.append(r)
    return uniq


def truthy_(v) -> bool:
    return v is True or (isinstance(v, str) and v.lower() == "true")


RBAC = "rbac.authorization.k8s.io"


def via_text(vias, n=2):
    vias = list(dict.fromkeys(vias))
    s = "; ".join(vias[:n])
    if len(vias) > n:
        s += f" (+{len(vias) - n} more)"
    return s


def _ec(conf):
    return {"certain": 0, "likely": 1, "heuristic": 4}[conf]


def mk(dst, kind, text, how, via, cost=1, conf="certain"):
    return Edge(dst, kind, text, how, via, cost * 2 + _ec(conf), conf)


def sa_key(ns, name):
    return f"SA:{ns}/{name}"


def role_admin_equiv(cl: Cluster, name: str) -> bool:
    return bool(cl.is_admin(cl.role_node_grants(f"ROLE:ClusterRole/{name}@*")))


def edges_for(cl: Cluster, key: str, shallow: bool = False) -> list:
    ck = (key, shallow)
    if ck in cl._edges:
        return cl._edges[ck]
    out: list = []
    if key == GOAL:
        pass
    elif key == NODE_ROOT:
        seen = set()
        for w in cl.workloads:
            k = sa_key(w.ns, w.sa)
            if k in seen:
                continue
            seen.add(k)
            out.append(mk(k, "NODE_TOKENS", f"read the projected token of {k[3:]}",
                          "Root on a node can read every projected ServiceAccount token of pods on that node, and a pod author can pick the node.",
                          "node root", 1, "likely"))
        if cl.assume_control_plane:
            out.append(mk(GOAL, "NODE_TOKENS", "land on a control-plane node and read its admin credentials",
                          "On a self-managed cluster a pod pinned to a control-plane node can read the kubeconfig and PKI under /etc/kubernetes.",
                          "node root (--assume-control-plane)", 1, "likely"))
    elif key.startswith("WL:"):
        w = next((x for x in cl.workloads if x.key == key), None)
        if w:
            out.append(mk(sa_key(w.ns, w.sa), "WL_SA", f"take the ServiceAccount token mounted in {w.kind}/{w.name}",
                          f"Anyone running code in that pod can read /var/run/secrets/kubernetes.io/serviceaccount/token and act as {w.ns}/{w.sa}.",
                          f"{w.kind} {w.ns}/{w.name}", 1))
            if w.escapes:
                out.append(mk(NODE_ROOT, "WL_ESCAPE", f"break out to the node ({'; '.join(w.escapes[:3])})",
                              "The pod spec already gives host-level access, so code in this pod is root on its node.",
                              f"{w.kind} {w.ns}/{w.name}", 1))
    else:
        out = identity_edges(cl, key, shallow)
    cl._edges[ck] = out
    return out


def identity_edges(cl: Cluster, key: str, shallow: bool) -> list:
    g = cl.grants(key)
    out: list = []
    if not g:
        return out
    adm = cl.is_admin(g)
    if adm:
        return [mk(GOAL, "ADMIN", "already holds `*` on `*` cluster-wide", "Cluster-admin equivalent.", adm.via, 0)]
    H, X = cl.hits, cl.expand
    vt = via_text

    # --- bind / escalate -------------------------------------------------
    bcr = H(g, ["bind"], RBAC, "clusterroles", True).get(None)
    crb = H(g, ["create"], RBAC, "clusterrolebindings", True).get(None)
    rb = X(H(g, ["create"], RBAC, "rolebindings"))
    if bcr and crb:
        names = bcr[0]
        if names is None or any(role_admin_equiv(cl, n) for n in names):
            out.append(mk(GOAL, "BIND", "bind cluster-admin to yourself with a new ClusterRoleBinding",
                          "`bind` lets you reference a role you do not hold; create on clusterrolebindings completes it.",
                          vt(bcr[1] + crb[1])))
        elif not shallow:
            for n in names:
                if ("ClusterRole", None, n) in cl.roles:
                    nk = f"ROLE:ClusterRole/{n}@*"
                    if edges_for(cl, nk, True):
                        out.append(mk(nk, "BIND", f"bind ClusterRole {n} (the only role you may bind) cluster-wide",
                                      "Bind is pinned by resourceNames, but that role is itself powerful.", vt(bcr[1] + crb[1])))
    if bcr:
        for ns, (rn, rv) in rb.items():
            if rn is None or any(role_admin_equiv(cl, n) for n in rn):
                out.append(mk(f"NSADMIN:{ns}", "BIND", f"bind cluster-admin through a RoleBinding in {ns} (admin of {ns})",
                              "A RoleBinding to ClusterRole cluster-admin grants every verb inside that namespace.",
                              vt(bcr[1] + rv)))
            elif not shallow:
                for n in rn:
                    nk = f"ROLE:ClusterRole/{n}@{ns}"
                    if ("ClusterRole", None, n) in cl.roles and edges_for(cl, nk, True):
                        out.append(mk(nk, "BIND", f"bind ClusterRole {n} in {ns}", "Bind pinned to a powerful role.", vt(bcr[1] + rv)))
    bro = X(H(g, ["bind"], RBAC, "roles"))
    if not shallow:
        for ns, (bn, bv) in bro.items():
            if ns in rb:
                for (k, rns, rname) in cl.roles:
                    if k == "Role" and rns == ns and (bn is None or rname in bn):
                        nk = f"ROLE:Role/{ns}/{rname}@{ns}"
                        if edges_for(cl, nk, True):
                            out.append(mk(nk, "BIND", f"bind Role {rname} in {ns}", "bind on roles plus rolebinding create.", vt(bv + rb[ns][1])))
    esc = H(g, ["escalate"], RBAC, "clusterroles", True).get(None)
    if esc:
        upd = H(g, ["update", "patch"], RBAC, "clusterroles", True).get(None)
        crt = H(g, ["create"], RBAC, "clusterroles", True).get(None)
        if upd:
            out.append(mk(GOAL, "ESCALATE", "patch a ClusterRole you already hold and add `*`",
                          "`escalate` switches off the check that stops you granting permissions you lack.", vt(esc[1] + upd[1])))
        elif crt and crb:
            out.append(mk(GOAL, "ESCALATE", "create a ClusterRole with `*` and bind it to yourself",
                          "`escalate` plus create on clusterroles and clusterrolebindings.", vt(esc[1] + crt[1] + crb[1])))
    esn = X(H(g, ["escalate"], RBAC, "roles"))
    upn = X(H(g, ["update", "patch"], RBAC, "roles"))
    crn = X(H(g, ["create"], RBAC, "roles"))
    for ns, (_, ev) in esn.items():
        if ns in upn or (ns in crn and ns in rb):
            out.append(mk(f"NSADMIN:{ns}", "ESCALATE", f"rewrite or create a Role in {ns} with `*`", "`escalate` on roles, scoped to the namespace.", vt(ev)))

    # --- impersonation ---------------------------------------------------
    iu = H(g, ["impersonate"], "", "users", True).get(None)
    ig = H(g, ["impersonate"], "", "groups", True).get(None)
    isa = X(H(g, ["impersonate"], "", "serviceaccounts"))
    def _imp_user(name):
        if name.startswith("system:serviceaccount:"):
            parts = name.split(":")
            if len(parts) == 4:
                return sa_key(parts[2], parts[3])
        return f"User:{name}"
    if iu and not shallow:
        targets = set()
        if iu[0] is None:
            targets |= {f"User:{u}" for u in cl.users} | {sa_key(n, s) for n, s in cl.sas}
        else:
            targets |= {_imp_user(n) for n in iu[0]}
        for t in sorted(targets - {key}):
            out.append(mk(t, "IMPERSONATE", f"impersonate {t}", "Send Impersonate-User and the API server authorizes you as that identity.", vt(iu[1])))
    if ig and (iu or isa):
        gnames = cl.groups if ig[0] is None else set(ig[0])
        for gn in sorted(gnames):
            if shallow and gn != "system:masters":
                continue
            out.append(mk(f"Group:{gn}", "IMPERSONATE", f"impersonate group {gn}" + (" (system:masters is cluster-admin)" if gn == "system:masters" else ""),
                          "Impersonate-Group needs an Impersonate-User too, which this identity is also allowed to send.", vt(ig[1])))
    if not shallow:
        for ns, (nm, v) in isa.items():
            for n in (cl.sa_by_ns.get(ns, set()) if nm is None else nm):
                out.append(mk(sa_key(ns, n), "IMPERSONATE", f"impersonate ServiceAccount {ns}/{n}", "impersonate on serviceaccounts.", vt(v)))

    # --- pod creation: token theft and node root -------------------------
    nsmap: dict = {}
    for grp, res in POD_MAKERS:
        verbs = ["create"] if res == "pods" else ["create", "update", "patch"]
        for ns, (nm, v) in X(H(g, verbs, grp, res)).items():
            nsmap.setdefault(ns, (res, v))
    nsw = H(g, ["update", "patch"], "", "namespaces", True).get(None)
    for ns, (res, v) in nsmap.items():
        if not shallow:
            for sa in sorted(cl.sa_by_ns.get(ns, set())):
                out.append(mk(sa_key(ns, sa), "POD_CREATE", f"run a pod as {ns}/{sa} ({res} in {ns})",
                              f"Set serviceAccountName: {sa} in the pod spec; the token is mounted into your container.", vt(v)))
        adm_, conf, note = cl.admits_privileged(ns)
        if adm_:
            out.append(mk(NODE_ROOT, "POD_CREATE", f"schedule a host-level pod in {ns} ({res})",
                          f"{note[0].upper() + note[1:]}; privileged, hostPID or a hostPath mount of / is accepted, which is root on the node.", vt(v), 1, conf))
        elif nsw and (nsw[0] is None or ns in nsw[0]):
            out.append(mk(NODE_ROOT, "POD_CREATE", f"relabel {ns} to drop Pod Security enforcement, then schedule a host-level pod",
                          f"{note[0].upper() + note[1:]}, but this identity may also update namespaces.", vt(v + nsw[1]), 2, "likely"))

    # --- exec into existing pods -----------------------------------------
    exm: dict = {}
    for res, verbs in (("pods/exec", ["create"]), ("pods/attach", ["create"]), ("pods/ephemeralcontainers", ["update", "patch"])):
        for ns, (nm, v) in X(H(g, verbs, "", res)).items():
            exm.setdefault(ns, (res, v))
    if not shallow:
        for ns, (res, v) in exm.items():
            for w in cl.wl_by_ns.get(ns, []):
                out.append(mk(w.key, "EXEC", f"{res.split('/')[1]} into {w.kind}/{w.name} in {ns}", "Run a shell in a running pod and read its token or reach its node.", vt(v)))

    # --- reading and minting credentials ---------------------------------
    sr = X(H(g, ["get", "list", "watch"], "", "secrets"))
    if not shallow:
        for ns, (nm, v) in sr.items():
            if nm is not None:
                continue
            known = cl.token_secrets.get(ns)
            targets = sorted(known) if known else sorted(cl.sa_by_ns.get(ns, set()))
            for sa in targets:
                out.append(mk(sa_key(ns, sa), "SECRETS_READ", f"read secrets in {ns} for the token of {sa}",
                              "get/list on secrets returns values, including kubernetes.io/service-account-token secrets." if known
                              else "get/list on secrets returns values. Assumed: a long-lived SA token secret or equivalent credential lives in this namespace.",
                              vt(v), 1 if known else 2, "certain" if known else "likely"))
    cs = X(H(g, ["create"], "", "secrets"))
    if not shallow:
        for ns, (nm, v) in cs.items():
            for sa in sorted(cl.sa_by_ns.get(ns, set())):
                out.append(mk(sa_key(ns, sa), "SECRET_MINT", f"create a token secret for {ns}/{sa}",
                              "Create a Secret of type kubernetes.io/service-account-token annotated with the target name; the controller fills in a token.", vt(v)))
        for ns, (nm, v) in X(H(g, ["create"], "", "serviceaccounts/token")).items():
            for sa in (sorted(cl.sa_by_ns.get(ns, set())) if nm is None else sorted(nm)):
                out.append(mk(sa_key(ns, sa), "TOKEN_REQUEST", f"request a token for {ns}/{sa}", "TokenRequest returns a bearer token for that ServiceAccount.", vt(v)))
    if not shallow:
        np_ = H(g, ["get", "create", "update", "patch"], "", "nodes/proxy", True).get(None)
        if np_:
            for w in cl.workloads:
                out.append(mk(w.key, "NODES_PROXY", f"exec in {w.kind}/{w.name} through the kubelet",
                              "nodes/proxy reaches the kubelet API; its exec endpoint authorizes the WebSocket handshake as a plain `get`.",
                              vt(np_[1]), 2, "likely"))

    # --- certificates, webhooks, EKS --------------------------------------
    ccr = H(g, ["create"], "certificates.k8s.io", "certificatesigningrequests", True).get(None)
    cap = H(g, ["update", "patch"], "certificates.k8s.io", "certificatesigningrequests/approval", True).get(None)
    csg = H(g, ["approve"], "certificates.k8s.io", "signers", True).get(None)
    if ccr and cap and csg and (csg[0] is None or {"*", "kubernetes.io/*", "kubernetes.io/kube-apiserver-client"} & set(csg[0])):
        out.append(mk(GOAL, "CSR", "request and approve a client certificate with O=system:masters",
                      "Create a CSR for the kube-apiserver-client signer, approve it yourself, and the issued cert is a cluster-admin identity.",
                      vt(ccr[1] + cap[1] + csg[1])))
    mw = H(g, ["create", "update", "patch"], "admissionregistration.k8s.io", "mutatingwebhookconfigurations", True).get(None)
    if mw:
        out.append(mk(GOAL, "WEBHOOK", "register a mutating webhook that rewrites every pod and sees every object",
                      "The webhook can inject host-level containers into pods created by anyone, including controllers running as cluster-admin.",
                      vt(mw[1]), 2, "likely"))
    for ns, (nm, v) in X(H(g, ["update", "patch"], "", "configmaps")).items():
        if ns == "kube-system" and (nm is None or "aws-auth" in nm):
            out.append(mk(GOAL, "AWS_AUTH", "edit aws-auth to map an IAM role you control to system:masters",
                          "On EKS clusters still using the aws-auth ConfigMap, this is a direct cluster-admin grant.", vt(v), 1, "likely"))

    # --- confused deputy (operators) -------------------------------------
    if cl.heuristics and cl.operator_groups and not shallow:
        for s, groups in cl.operator_groups.items():
            if s == key:
                continue
            hit = [gp for gp in groups if any(
                (gp in gr.groups or "*" in gr.groups)
                and ({"create", "update", "patch", "*"} & set(gr.verbs)) and gr.names is None
                for gr in g)]
            if hit:
                out.append(mk(s, "DEPUTY", f"write custom resources ({', '.join(hit[:3])}) that operator {s[3:]} reconciles",
                              "Heuristic: the operator acts on these objects with its own wider permissions. Real cases exist (KEDA GHSA-637c-6jxx-4rwm: a namespace tenant made the operator send its token to a tenant-controlled endpoint).",
                              "custom resource write access", 3, "heuristic"))
    return out


# ------------------------------------------------------------- search

def shortest(cl: Cluster, start: str):
    dist = {start: 0}
    prev: dict = {}
    heap = [(0, 0, start)]
    n = 0
    while heap:
        d, _, u = heapq.heappop(heap)
        if d > dist.get(u, 1 << 30):
            continue
        if u == GOAL:
            break
        for e in edges_for(cl, u):
            nd = d + e.cost
            if nd < dist.get(e.dst, 1 << 30):
                dist[e.dst] = nd
                prev[e.dst] = (u, e)
                n += 1
                heapq.heappush(heap, (nd, n, e.dst))
    if GOAL not in prev and start != GOAL:
        return None
    path, cur = [], GOAL
    while cur != start:
        p, e = prev[cur]
        path.append((p, e))
        cur = p
    path.reverse()
    return path


def is_system(key: str) -> bool:
    name = key.split(":", 1)[1] if ":" in key else key
    return name.startswith("system:") or name.startswith("kubeadm:")


BROAD = {"Group:system:unauthenticated": 0, "User:system:anonymous": 0, "Group:system:authenticated": 1,
         "Group:system:serviceaccounts": 2}


def start_rank(key: str) -> int:
    if key in BROAD:
        return BROAD[key]
    if key.startswith("Group:system:serviceaccounts:"):
        return 3
    if key.startswith("Group:"):
        return 4
    if key.startswith("SA:"):
        return 5
    if key.startswith("User:"):
        return 6
    return 7


def start_label(key: str) -> str:
    return {"Group:system:unauthenticated": "anyone without credentials (system:unauthenticated)",
            "Group:system:authenticated": "any authenticated identity (system:authenticated)",
            "Group:system:serviceaccounts": "any ServiceAccount in the cluster (system:serviceaccounts)"}.get(key, key)


def path_severity(key: str, path) -> str:
    rank = start_rank(key)
    sev = "critical" if rank <= 3 else "high"
    if len([1 for _, e in path if e.kind != "ADMIN"]) > 5 and sev != "critical":
        sev = "medium"
    return sev


def sig_of(path):
    return tuple((e.kind, e.dst, e.via.replace(" (inherited from group", "|g(").split("|g(")[0]) for _, e in path[1:]) + \
        ((path[0][1].kind, path[0][1].dst),)


def cut_list(sigs):
    """Greedy cover: which grants to remove so the most paths disappear."""
    units = defaultdict(set)
    for i, s in enumerate(sigs):
        structural = ("ADMIN", "NODE_TOKENS", "WL_SA", "WL_ESCAPE")
        es = [e for _, e in s["path"] if e.kind not in structural] or [e for _, e in s["path"] if e.kind != "ADMIN"]
        for e in es:
            units[(e.kind, e.via.split(" (inherited")[0])].add(i)
    left, out = set(range(len(sigs))), []
    while left and units:
        best = max(units.items(), key=lambda kv: (len(kv[1] & left), -len(kv[0][1])))
        cov = best[1] & left
        if not cov:
            break
        out.append({"kind": best[0][0], "grant": best[0][1], "paths_removed": len(cov), "fix": EDGE_FIX.get(best[0][0], "")})
        left -= cov
        if len(out) >= 8:
            break
    return out


def exposures(cl: Cluster, include_system: bool):
    out = []
    keys = sorted({k for k in cl.subject_grants} | {sa_key(n, s) for n, s in cl.sas})
    for k in keys:
        if not include_system and is_system(k):
            continue
        g = cl.grants(k)
        if cl.is_admin(g):
            continue
        v = cl.hits(g, ["create", "update", "patch", "delete"], "admissionregistration.k8s.io", "validatingwebhookconfigurations", True).get(None)
        if v:
            out.append({"identity": k, "grant": "write validatingwebhookconfigurations",
                        "why": "Can rewrite or disable the cluster's policy-engine webhooks, which removes guardrails that stop other hops."})
        a = cl.hits(g, ["update", "patch", "create"], "apiregistration.k8s.io", "apiservices", True).get(None)
        if a:
            out.append({"identity": k, "grant": "write apiservices",
                        "why": "Can redirect an aggregated API (for example metrics) to a server the attacker runs."})
    for (kind, _, name), doc in cl.roles.items():
        labels = dig(doc, "metadata", "labels", default={}) or {}
        tgt = [x for x in ("admin", "edit", "view") if str(labels.get(f"rbac.authorization.k8s.io/aggregate-to-{x}")) == "true"]
        if kind == "ClusterRole" and tgt:
            grants = cl.role_node_grants(f"ROLE:ClusterRole/{name}@*")
            bad = []
            if cl.hits(grants, ["get", "list"], "", "secrets"):
                bad.append("read secrets")
            if any(cl.hits(grants, ["create"], gp, r) for gp, r in POD_MAKERS):
                bad.append("create workloads")
            if cl.hits(grants, ["impersonate", "bind", "escalate"], "*", "*") or any(
                    "impersonate" in x.verbs or "bind" in x.verbs or "escalate" in x.verbs for x in grants):
                bad.append("bind/escalate/impersonate")
            if bad:
                out.append({"identity": f"ClusterRole/{name}", "grant": f"aggregated into {', '.join(tgt)}: {', '.join(bad)}",
                            "why": "Every holder of the aggregated built-in role silently inherits this, in every namespace where the role is bound."})
    return out


# ------------------------------------------------------------- report

def collect_inputs(paths):
    files = []
    for p in paths:
        pp = Path(p)
        if pp.is_dir():
            files += sorted(x for x in pp.rglob("*") if x.is_file() and x.suffix.lower() in (".yaml", ".yml", ".json"))
        elif pp.is_file():
            files.append(pp)
        else:
            print(f"warning: {p} not found", file=sys.stderr)
    return files


def load_cluster(files, text_inputs, **kw):
    cl = Cluster(**kw)
    errors = 0
    sources = [(str(f), f.read_text(errors="replace")) for f in files] + text_inputs
    for name, text in sources:
        if looks_templated(text):
            print(f"warning: {name} contains template markers; render it first (helm template / kustomize build)", file=sys.stderr)
            continue
        try:
            for d in load_documents(text):
                cl.add_doc(d, name)
        except (YAMLLiteError, ValueError, RecursionError) as ex:
            errors += 1
            print(f"warning: could not parse {name}: {ex}", file=sys.stderr)
    cl.finalize()
    return cl, errors


def analyse(cl: Cluster, only=None, include_system=False, pods=False):
    starts = set(cl.subject_grants) | {sa_key(n, s) for n, s in cl.sas} | {"Group:system:authenticated", "Group:system:unauthenticated"}
    if pods:
        starts |= {w.key for w in cl.workloads}
    admins, results = [], []
    for k in sorted(starts):
        if only and only.lower() not in k.lower():
            continue
        if not only and not include_system and is_system(k) and k not in BROAD and not k.startswith("Group:system:serviceaccounts"):
            continue
        g = cl.grants(k)
        if not k.startswith("WL:") and cl.is_admin(g):
            admins.append(k)
            continue
        path = shortest(cl, k)
        if path:
            results.append({"start": k, "path": path, "sev": path_severity(k, path)})
    # group identical chains, keep the broadest starting identity as representative
    groups: dict = {}
    for r in results:
        groups.setdefault(sig_of(r["path"]), []).append(r)
    final = []
    for sig, rs in groups.items():
        rs.sort(key=lambda r: (start_rank(r["start"]), r["start"]))
        rep = dict(rs[0])
        rep["also"] = [r["start"] for r in rs[1:]]
        final.append(rep)
    final.sort(key=lambda r: (-SEV_ORDER[r["sev"]], start_rank(r["start"]), len(r["path"]), r["start"]))
    return admins, final


def n_hops(r):
    return len([1 for _, e in r['path'] if e.kind != 'ADMIN'])


def render_path(cl, r, idx):
    lines = [f"[{r['sev'].upper()}] PATH {idx}: {start_label(r['start'])} -> cluster-admin, {n_hops(r)} hop{'s' if n_hops(r) != 1 else ''}" + (" (includes a heuristic hop, verify it)" if any(e.conf == "heuristic" for _, e in r["path"]) else "")]
    if r["also"]:
        shown = ", ".join(r["also"][:6]) + (f", +{len(r['also']) - 6} more" if len(r["also"]) > 6 else "")
        lines.append(f"  Same chain also open to: {shown}")
    if r["start"].startswith("SA:"):
        ns, n = r["start"][3:].split("/", 1)
        pods = [f"{w.kind}/{w.name}" for w in cl.workloads if w.ns == ns and w.sa == n]
        if pods:
            lines.append(f"  Pods running as it: {', '.join(pods[:4])}" + (" ..." if len(pods) > 4 else ""))
    for i, (src, e) in enumerate(r["path"], 1):
        dst = {GOAL: "CLUSTER-ADMIN", NODE_ROOT: "root on a node"}.get(e.dst, e.dst)
        tag = "" if e.conf == "certain" else f"  [{e.conf}]"
        lines.append(f"  {i}. {src.replace(NODE_ROOT, 'root on a node')} -> {dst}{tag}")
        lines.append(f"     {e.text}")
        lines.append(f"     why: {e.how}")
        lines.append(f"     grant: {e.via}")
    first_cut = next(((e.kind, e.via) for _, e in r["path"] if e.kind not in ("ADMIN",)), None)
    if first_cut:
        lines.append(f"  Fix: {EDGE_FIX.get(first_cut[0], '')}")
    return "\n".join(lines)


def to_dot(final):
    rows = ["digraph escalation {", "  rankdir=LR;", '  node [shape=box,fontname="Helvetica"];']
    seen = set()
    for r in final:
        for src, e in r["path"]:
            line = f'  "{src}" -> "{e.dst}" [label="{e.kind}"];'
            if line not in seen:
                seen.add(line)
                rows.append(line)
    rows.append(f'  "{GOAL}" [style=filled,fillcolor="#f4b6b6"];')
    rows.append("}")
    return "\n".join(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Map RBAC privilege-escalation paths to cluster-admin from manifests or kubectl output. Read-only, offline.")
    ap.add_argument("paths", nargs="*", help="files or directories of YAML/JSON, or - for stdin")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--dot", action="store_true", help="print the union of reported paths as Graphviz DOT")
    ap.add_argument("--from", dest="only", metavar="TEXT", help="only paths starting at identities whose key contains TEXT (e.g. SA:payments/ci)")
    ap.add_argument("--pods", action="store_true", help="also report 'if this workload is popped' paths for every workload")
    ap.add_argument("--include-system", action="store_true", help="include system:* identities as starting points")
    ap.add_argument("--psa-exempt-ns", default="", help="comma list of namespaces exempt from Pod Security Admission")
    ap.add_argument("--assume-control-plane", action="store_true", help="treat root on any node as cluster-admin (self-managed, schedulable control plane)")
    ap.add_argument("--no-heuristics", action="store_true", help="turn off the operator confused-deputy heuristic")
    ap.add_argument("--fail-on", choices=["critical", "high", "medium", "low"], default="high")
    ap.add_argument("--list-admins", action="store_true", help="list cluster-admin-equivalent identities")
    a = ap.parse_args(argv)
    if not a.paths:
        ap.print_usage(sys.stderr)
        return 2
    texts = []
    paths = list(a.paths)
    if "-" in paths:
        paths.remove("-")
        texts.append(("<stdin>", sys.stdin.read()))
    files = collect_inputs(paths)
    if not files and not texts:
        print("error: no YAML or JSON input found", file=sys.stderr)
        return 2
    cl, errs = load_cluster(files, texts, psa_exempt=[x for x in a.psa_exempt_ns.split(",") if x],
                            assume_control_plane=a.assume_control_plane, heuristics=not a.no_heuristics)
    if not cl.roles and not cl.bindings:
        print("error: no Role/ClusterRole/Binding objects parsed; nothing to map", file=sys.stderr)
        return 2
    admins, final = analyse(cl, a.only, a.include_system, a.pods)
    admin_show = [x for x in admins if a.include_system or not is_system(x)]
    cuts = cut_list(final)
    expo = exposures(cl, a.include_system)
    worst = max([SEV_ORDER[r["sev"]] for r in final], default=-1)
    if a.json:
        print(json.dumps({
            "summary": {"paths": len(final), "admin_equivalent": admin_show,
                        "by_severity": {s: sum(1 for r in final if r["sev"] == s) for s in ("critical", "high", "medium")},
                        "objects": dict(cl.counts)},
            "paths": [{"severity": r["sev"], "start": r["start"], "also_open_to": r["also"],
                       "hops": [{"from": s, "to": e.dst, "kind": e.kind, "action": e.text, "why": e.how, "grant": e.via, "confidence": e.conf}
                                for s, e in r["path"]]} for r in final],
            "cut_list": cuts, "exposures": expo, "notes": cl.notes}, indent=2))
    elif a.dot:
        print(to_dot(final))
    else:
        c = cl.counts
        print(f"Parsed {c.get('ClusterRole', 0)} ClusterRoles, {c.get('Role', 0)} Roles, {c.get('ClusterRoleBinding', 0)} ClusterRoleBindings, "
              f"{c.get('RoleBinding', 0)} RoleBindings, {len(cl.sas)} ServiceAccounts, {len(cl.workloads)} workloads, {len(cl.all_ns)} namespaces.")
        if a.list_admins or admin_show:
            print(f"Cluster-admin equivalent identities ({len(admin_show)}): {', '.join(admin_show) or 'none'}")
            risky = []
            for k in admin_show:
                if k.startswith("SA:"):
                    ns, n = k[3:].split("/", 1)
                    pods = [w for w in cl.workloads if w.ns == ns and w.sa == n]
                    if pods:
                        risky.append(f"  - {k} runs {', '.join(w.kind + '/' + w.name for w in pods[:3])}: popping that pod is cluster-admin")
            if risky:
                print("\n".join(risky))
        print()
        if not final:
            print("No escalation path to cluster-admin found from the identities analysed.")
        for i, r in enumerate(final, 1):
            print(render_path(cl, r, i))
            print()
        if cuts:
            print("Smallest set of changes (greedy cover, most paths first):")
            for i, ctt in enumerate(cuts, 1):
                print(f"  {i}. {ctt['grant']}  [{ctt['kind']}] removes {ctt['paths_removed']} of {len(final)} path(s)")
                print(f"     {ctt['fix']}")
            print()
        if expo:
            print("Standing exposures (no complete path by themselves):")
            for x in expo:
                print(f"  - {x['identity']}: {x['grant']}. {x['why']}")
            print()
        notes = list(cl.notes)
        if cl.admission_kinds:
            notes.append(f"Admission policy objects present ({', '.join(sorted(cl.admission_kinds))}) but not evaluated; they may already block some pod-creation hops.")
        notes.append("Not modelled: webhook or policy-engine decisions, network reachability to the kubelet, cloud IAM mappings, and anything outside the files given.")
        print("Caveats:")
        for n in notes:
            print(f"  - {n}")
        print(f"\nSummary: {len(final)} distinct path(s); " + ", ".join(
            f"{sum(1 for r in final if r['sev'] == s)} {s}" for s in ("critical", "high", "medium")))
    thr = SEV_ORDER[a.fail_on]
    return 1 if worst >= thr else 0


if __name__ == "__main__":
    sys.exit(main())
