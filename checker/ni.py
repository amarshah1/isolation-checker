"""Bounded non-interference check ("two worlds") for a deployment model.

Definition (Goguen-Meseguer, purge form): for every trace, what the observer
sees must equal what it would see if every other tenant's steps were deleted.
A trace where that fails is a channel: the other tenant can choose between the
two worlds, and the observer can tell which one it is in.

The search is brute force over every interleaving of both agents' operations
up to `depth` steps. "No leak found" is a statement about traces up to that
length, not a proof for all traces.
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import CAPACITY, NAMES, QUOTA, TENANTS, Backend, agent_ops, show_obs, show_op

CHANNEL_ORDER = (
    "shared storage namespace",
    "shared remote-repository cache",
    "shared capacity (quota)",
)


@dataclass(frozen=True)
class Leak:
    observer: str
    world1: tuple  # ((tenant, op), ...) including the other tenants' steps
    world2: tuple  # the same trace with every other tenant's steps deleted
    obs1: tuple  # what the observer saw in world 1, one entry per observer step
    obs2: tuple  # ... and in world 2

    @property
    def index(self):
        """Which of the observer's steps first saw a difference."""
        return next(i for i, (a, b) in enumerate(zip(self.obs1, self.obs2)) if a != b)

    @property
    def observer_op(self):
        return self.world2[self.index][1]

    @property
    def sender_kinds(self):
        return tuple(sorted({op[0] for t, op in self.world1 if t != self.observer}))

    @property
    def channel(self):
        i = self.index
        if QUOTA in (self.obs1[i], self.obs2[i]):
            return "shared capacity (quota)"
        if self.observer_op[0] == "fetch":
            return "shared remote-repository cache"
        return "shared storage namespace"

    def variant(self):
        if self.channel == "shared capacity (quota)":
            return f"{self.observer_op[0]} <- anything that uses capacity"
        return f"{self.observer_op[0]} <- {', '.join(self.sender_kinds)}"

    def to_json(self):
        enc = lambda trace: [[t, *op] for t, op in trace]  # noqa: E731
        return {
            "channel": self.channel,
            "variant": self.variant(),
            "observer": self.observer,
            "world1": enc(self.world1),
            "world2": enc(self.world2),
            "model_obs1": [list(o) if isinstance(o, tuple) else o for o in self.obs1],
            "model_obs2": [list(o) if isinstance(o, tuple) else o for o in self.obs2],
        }


@dataclass
class Result:
    deployment: str
    depth: int
    observer: str
    explored: int
    leaks: list  # one shortest Leak per distinct variant

    def by_channel(self):
        groups = {}
        for leak in self.leaks:
            groups.setdefault(leak.channel, []).append(leak)
        order = {c: i for i, c in enumerate(CHANNEL_ORDER)}
        return sorted(groups.items(), key=lambda kv: order.get(kv[0], len(order)))

    def to_json(self):
        return {
            "deployment": self.deployment,
            "depth": self.depth,
            "observer": self.observer,
            "traces_explored": self.explored,
            "leaks": [leak.to_json() for leak in self.leaks],
        }


def run(deployment, trace, observer):
    """Run a trace from an empty backend; return what `observer` saw."""
    s = Backend()
    seen = []
    for tenant, op in trace:
        s, obs = deployment.handle(s, tenant, op)
        if tenant == observer:
            seen.append(obs)
    return tuple(seen)


def check(deployment, depth=3, observer="B"):
    steps = [(t, op) for t in TENANTS for op in agent_ops()]
    observer_steps = [st for st in steps if st[0] == observer]
    world2_cache = {}
    found = {}  # (channel, observer kind, sender kinds) -> shortest Leak
    explored = 0

    def world2_obs(world2):
        if world2 not in world2_cache:
            world2_cache[world2] = run(deployment, world2, observer)
        return world2_cache[world2]

    def every_sender_step_matters(trace, seen):
        # Skip leaks padded with steps that change nothing the observer sees,
        # e.g. "A fetch(x) -> A put(x) -> B get(x)" when "A put(x) -> B get(x)"
        # already shows the same thing.
        for j, (tenant, _) in enumerate(trace):
            if tenant != observer and run(deployment, trace[:j] + trace[j + 1:], observer) == seen:
                return False
        return True

    def visit(state, trace, seen, others_acted):
        nonlocal explored
        if trace:
            explored += 1
        # Only an observer step can reveal something new, and only once another
        # tenant has acted.
        if others_acted and trace[-1][0] == observer:
            world2 = tuple(st for st in trace if st[0] == observer)
            seen2 = world2_obs(world2)
            if seen != seen2:
                if every_sender_step_matters(trace, seen):
                    leak = Leak(observer, trace, world2, seen, seen2)
                    key = (leak.channel, leak.observer_op[0], leak.sender_kinds)
                    if leak.channel == "shared capacity (quota)":
                        # Which of A's operations used up the capacity doesn't matter.
                        key = key[:2]
                    if key not in found or len(trace) < len(found[key].world1):
                        found[key] = leak
                return  # longer traces with this prefix are the same leak
        if len(trace) == depth:
            return
        # On the last step, only an observer step can be checked.
        for st in observer_steps if len(trace) + 1 == depth else steps:
            nxt, obs = deployment.handle(state, *st)
            visit(
                nxt,
                trace + (st,),
                seen + (obs,) if st[0] == observer else seen,
                others_acted or st[0] != observer,
            )

    visit(Backend(), (), (), False)
    leaks = sorted(found.values(), key=lambda l: (len(l.world1), l.variant()))
    return Result(deployment.name, depth, observer, explored, leaks)


def _show_trace(trace):
    return " -> ".join(f"{t} {show_op(op)}" for t, op in trace)


def format_result(result, summary=""):
    obs = result.observer
    others = ", ".join(t for t in TENANTS if t != obs)
    lines = [
        f"Non-interference check - deployment: {result.deployment}",
    ]
    if summary:
        lines.append(f"  ({summary})")
    lines += [
        f"  agents {', '.join(TENANTS)} | names {', '.join(NAMES)} | capacity {CAPACITY}"
        f" | every trace up to {result.depth} steps ({result.explored:,} traces)",
        f"  Question: can anything {others} does change what {obs} observes?",
        "",
    ]
    if not result.leaks:
        lines.append(
            f"PASS: no leak in any trace up to {result.depth} steps."
            " (Bounded search, not a proof for longer traces.)"
        )
        return "\n".join(lines)

    groups = result.by_channel()
    lines.append(f"FAIL: {len(groups)} kind(s) of channel, {len(result.leaks)} distinct leaks")
    for n, (channel, leaks) in enumerate(groups, 1):
        ex = leaks[0]
        i = ex.index
        lines += [
            "",
            f"{n}. {channel} - {len(leaks)} variant(s)",
            f"   world 1:  {_show_trace(ex.world1)}",
            f"   world 2:  {_show_trace(ex.world2)}",
            f"   {obs}'s {show_op(ex.observer_op)} returns {show_obs(ex.obs1[i])} in world 1"
            f" but {show_obs(ex.obs2[i])} in world 2",
            f"   variants ({obs}'s operation <- {others}'s operations):",
        ]
        lines += [f"     {leak.variant()}" for leak in leaks]
    return "\n".join(lines)
