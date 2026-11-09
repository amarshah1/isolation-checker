"""A small model of a shared, Artifactory-like package store, and three ways
of deploying it in front of several agents.

Every operation is a pure function of the state, so the checker can replay any
sequence of operations and get the same answer every time. The universe is
deliberately tiny (two agents, two names, a capacity of two entries) because
the checker tries every interleaving up to a bound.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

TENANTS = ("A", "B")
NAMES = ("x", "y")
VALUES = ("0", "1")
CAPACITY = 2  # total entries the backend can hold, shared by every tenant

QUOTA = "quota-exceeded"


def agent_ops():
    """Every operation an agent can send. An operation is a tuple (kind, *args).

    mkdir(n)      create a folder                      WebDAV MKCOL
    put(n)        upload a file                        PUT
    get(n)        download a file                      GET
    list()        list my top-level folder             PROPFIND, Depth: 1
    setprop(n=v)  set a property on an item            PROPPATCH / ?properties=
    getprop(n)    read that property                   PROPFIND
    fetch(n)      pull a package via the remote repo   GET on a remote repository
    """
    ops = [("list",)]
    for n in NAMES:
        ops += [("mkdir", n), ("put", n), ("get", n), ("getprop", n), ("fetch", n)]
        ops += [("setprop", n, v) for v in VALUES]
    return ops


def show_op(op):
    kind, *args = op
    if kind == "setprop":
        return f"setprop({args[0]}={args[1]})"
    return f"{kind}({', '.join(args)})"


def show_obs(obs):
    if isinstance(obs, tuple):
        return "[" + ", ".join(obs) + "]"
    return obs


# ---------------------------------------------------------------------------
# The shared backend
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Backend:
    """State of the shared service. Immutable, so exploring is just recursion."""

    dirs: frozenset = frozenset()
    files: frozenset = frozenset()
    props: tuple = ()  # sorted (path, value) pairs
    remote: frozenset = frozenset()  # keys held in the remote-repository cache

    def size(self):
        return len(self.dirs) + len(self.files) + len(self.remote)

    def exists(self, path):
        return path in self.dirs or path in self.files


def _mkdir(s, path):
    if s.exists(path):
        return s, "exists"
    if s.size() >= CAPACITY:
        return s, QUOTA
    return replace(s, dirs=s.dirs | {path}), "created"


def _put(s, path):
    if path in s.dirs:
        return s, "conflict"
    if path in s.files:
        return s, "overwritten"
    if s.size() >= CAPACITY:
        return s, QUOTA
    return replace(s, files=s.files | {path}), "created"


def _get(s, path):
    return s, "200" if path in s.files else "404"


def _list(s, folder):
    prefix = folder + "/"
    children = {p[len(prefix):].split("/")[0] for p in s.dirs | s.files if p.startswith(prefix)}
    return s, tuple(sorted(children))


def _setprop(s, path, value):
    if not s.exists(path):
        return s, "404"
    props = dict(s.props)
    props[path] = value
    return replace(s, props=tuple(sorted(props.items()))), "ok"


def _getprop(s, path):
    if not s.exists(path):
        return s, "404"
    return s, dict(s.props).get(path, "unset")


def _fetch(s, key):
    # A remote repository caches whatever it is asked for, including names that
    # do not exist upstream (the incident's "fabricated names being cached").
    if key in s.remote:
        return s, "served-from-cache"
    if s.size() >= CAPACITY:
        return s, QUOTA
    return replace(s, remote=s.remote | {key}), "fetched-upstream"


# ---------------------------------------------------------------------------
# Deployments: how each agent's request is turned into a backend request
# ---------------------------------------------------------------------------


class Deployment:
    name = ""
    summary = ""

    def path(self, tenant, name):
        raise NotImplementedError

    def folder(self, tenant):
        raise NotImplementedError

    def remote_key(self, tenant, name):
        raise NotImplementedError

    def refuse(self, s, tenant, op):
        """Return a response to send instead of forwarding, or None to forward."""
        return None

    def handle(self, s, tenant, op):
        refusal = self.refuse(s, tenant, op)
        if refusal is not None:
            return s, refusal
        kind, *args = op
        if kind == "list":
            return _list(s, self.folder(tenant))
        if kind == "fetch":
            return _fetch(s, self.remote_key(tenant, args[0]))
        path = self.path(tenant, args[0])
        if kind == "mkdir":
            return _mkdir(s, path)
        if kind == "put":
            return _put(s, path)
        if kind == "get":
            return _get(s, path)
        if kind == "setprop":
            return _setprop(s, path, args[1])
        if kind == "getprop":
            return _getprop(s, path)
        raise ValueError(f"unknown operation {op!r}")


class Direct(Deployment):
    """Agents talk to the service directly and share one namespace.

    This is the incident's setup: anyone can create, list and tag anything,
    remote fetches land in one shared cache, and capacity is shared.
    """

    name = "direct"
    summary = "agents use the shared service directly (the incident's setup)"

    def path(self, tenant, name):
        return f"cache/{name}"

    def folder(self, tenant):
        return "cache"

    def remote_key(self, tenant, name):
        return name


class NaiveProxy(Deployment):
    """A proxy that gives each agent its own folder, with two plausible mistakes.

    1. Remote fetches are forwarded unchanged, on the theory that the remote
       cache is harmless because agents cannot write packages into it.
    2. The backend's capacity is still shared by everyone.
    """

    name = "naive-proxy"
    summary = "per-agent folders, but a shared remote cache and shared capacity"

    def path(self, tenant, name):
        return f"cache/{tenant}/{name}"

    def folder(self, tenant):
        return f"cache/{tenant}"

    def remote_key(self, tenant, name):
        return name


class IsolatingProxy(NaiveProxy):
    """Per-agent folders, per-agent remote-cache keys, and per-agent capacity.

    The proxy enforces each agent's share of capacity itself, so the shared
    backend never fills up and never returns an error that depends on what
    another agent did. Giving each agent its own remote-cache keys gives up
    cross-agent cache hits; the alternative is to forbid agent-triggered
    fetches entirely and serve a read-only mirror.
    """

    name = "isolating-proxy"
    summary = "per-agent folders, remote-cache keys and capacity"

    def remote_key(self, tenant, name):
        return f"{tenant}/{name}"

    def share(self):
        return CAPACITY // len(TENANTS)

    def usage(self, s, tenant):
        mine = f"cache/{tenant}/"
        stored = sum(1 for p in s.dirs | s.files if p.startswith(mine))
        cached = sum(1 for k in s.remote if k.startswith(f"{tenant}/"))
        return stored + cached

    def refuse(self, s, tenant, op):
        kind, *args = op
        if kind in ("mkdir", "put"):
            adds_entry = not s.exists(self.path(tenant, args[0]))
        elif kind == "fetch":
            adds_entry = self.remote_key(tenant, args[0]) not in s.remote
        else:
            adds_entry = False
        if adds_entry and self.usage(s, tenant) >= self.share():
            return QUOTA
        return None


DEPLOYMENTS = {d.name: d for d in (Direct(), NaiveProxy(), IsolatingProxy())}
