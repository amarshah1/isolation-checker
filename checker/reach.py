"""Relay-aware reachability over a small description of the deployment.

A config lists nodes and the network connections the firewall allows. Node
types:

  agent     where an agent runs
  router    forwards packets (a NAT gateway, a VPC router)
  service   terminates connections; may set
              "relay": true            it makes requests on a caller's behalf
              "stateful": true         it keeps state that callers can see
              "isolates_tenants": true it gives each caller a private view of
                                       what is behind it (check that claim with
                                       `python3 -m checker ni`)
  external  outside your infrastructure (the internet, Hugging Face, ...)

Packet-level analysis (what Tiros / VPC Reachability Analyzer check) only
continues through routers. Relay-aware analysis also continues through
services marked as relays, which is how agents in the incident reached the
internet through Artifactory's remote repositories.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field


@dataclass
class Finding:
    kind: str  # "egress" | "agent-to-agent" | "shared-state"
    message: str
    paths: list = field(default_factory=list)


def load(path):
    with open(path) as f:
        cfg = json.load(f)
    nodes = cfg["nodes"]
    for src, dst in cfg["network"]:
        for n in (src, dst):
            if n not in nodes:
                raise ValueError(f"{path}: network edge mentions unknown node {n!r}")
    return cfg


def _explore(cfg, src, relay_aware):
    """BFS from `src`. Returns {(node, through_isolator): parent state}."""
    nodes = cfg["nodes"]
    succ = {}
    for a, b in cfg["network"]:
        succ.setdefault(a, []).append(b)

    start = (src, False)
    parent = {start: None}
    queue = deque([start])
    while queue:
        node, isolated = queue.popleft()
        if node != src:
            spec = nodes[node]
            forwards = spec["type"] == "router" or (relay_aware and spec.get("relay", False))
            if not forwards:
                continue
        nxt_isolated = isolated or bool(nodes[node].get("isolates_tenants", False))
        for nxt in succ.get(node, []):
            state = (nxt, nxt_isolated)
            if state not in parent:
                parent[state] = (node, isolated)
                queue.append(state)
    return parent


def _path(cfg, parent, state):
    hops = []
    while state is not None:
        hops.append(state[0])
        state = parent[state]
    hops.reverse()
    return " -> ".join(_label(cfg, n) for n in hops)


def _label(cfg, node):
    spec = cfg["nodes"][node]
    tags = []
    if spec.get("isolates_tenants"):
        tags.append("isolating")
    if spec.get("relay"):
        tags.append("relay")
    return f"{node} ({', '.join(tags)})" if tags else node


def check(cfg, relay_aware):
    nodes = cfg["nodes"]
    agents = [n for n, s in nodes.items() if s["type"] == "agent"]
    findings = []
    seen = set()
    exposed = {}  # stateful service -> {agent: path}, reached with no isolating layer
    covered = {}  # stateful service -> agents that reach it only through an isolating layer

    for agent in agents:
        parent = _explore(cfg, agent, relay_aware)
        for state in parent:
            node, isolated = state
            if node == agent:
                continue
            spec = nodes[node]
            path = _path(cfg, parent, state)
            if spec["type"] == "external" and (agent, node) not in seen:
                seen.add((agent, node))
                findings.append(Finding("egress", f"{agent} can reach {node}", [path]))
            if spec["type"] == "agent" and (agent, node) not in seen:
                seen.add((agent, node))
                findings.append(Finding("agent-to-agent", f"{agent} can reach {node}", [path]))
            if spec.get("stateful"):
                if isolated:
                    covered.setdefault(node, set()).add(agent)
                else:
                    exposed.setdefault(node, {}).setdefault(agent, path)

    for service, by_agent in exposed.items():
        if len(by_agent) > 1:
            findings.append(
                Finding(
                    "shared-state",
                    f"{service} keeps state reachable from {', '.join(sorted(by_agent))}"
                    " with no isolating layer in between",
                    list(by_agent.values()),
                )
            )

    notes = [
        f"{service} is shared by {', '.join(sorted(agents_))} only through an isolating layer;"
        " that layer's claim is checked by `python3 -m checker ni`"
        for service, agents_ in covered.items()
        if len(agents_) > 1 and len(exposed.get(service, {})) == 0
    ]
    return findings, notes


CHECKS = (
    ("egress", "no agent can reach an external network"),
    ("agent-to-agent", "no agent can reach another agent"),
    ("shared-state", "no stateful service is shared by agents without an isolating layer"),
)


def format_report(path, cfg):
    lines = [f"Reachability check: {path}"]
    if cfg.get("description"):
        lines.append(f"  {cfg['description']}")
    failed = []  # relay-aware checks that failed
    for relay_aware, title in (
        (False, "Packet-level (what Tiros / VPC Reachability Analyzer check)"),
        (True, "Relay-aware (also follows services that make requests for their callers)"),
    ):
        findings, notes = check(cfg, relay_aware)
        lines += ["", title + ":"]
        for kind, desc in CHECKS:
            hits = [f for f in findings if f.kind == kind]
            if not hits:
                lines.append(f"  PASS  {desc}")
                continue
            if relay_aware:
                failed.append(kind)
            lines.append(f"  FAIL  {desc}")
            for f in hits:
                lines.append(f"          {f.message}")
                lines += [f"            {p}" for p in f.paths]
        lines += [f"  NOTE  {n}" for n in notes]
    return "\n".join(lines), failed
