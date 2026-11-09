#!/usr/bin/env python3
"""Replay leaks found by the checker against a real WebDAV server.

    python3 -m checker ni --deployment direct --json out/direct.json
    python3 replay/replay.py out/direct.json --url http://127.0.0.1:8080

For each leak, world 1 and world 2 each run in a fresh, empty folder on the
server, and the script records what agent B actually observes:

  CONFIRMED       B's real observations differ between the worlds: a real channel
  NOT REPRODUCED  the model predicted a difference the server does not show,
                  so the model is wrong about this service
  SKIPPED         the leak needs an operation this server does not have

It also reports every step where the server's answer differs from the model's
answer. Those mismatches are the model being wrong about the service, even when
the leak itself is confirmed.

Only leaks from the `direct` deployment can be replayed: the proxies exist only
in the model so far.
"""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import sys
import urllib.parse
import uuid
import xml.etree.ElementTree as ET

DAV = "{DAV:}"
PROP_NS = "urn:verified-sandboxing"
PROP_NAME = "tag"
QUOTA = "quota-exceeded"

STATUS_WORDS = {
    "mkdir": {201: "created", 405: "exists", 409: "conflict", 507: QUOTA},
    "put": {200: "overwritten", 201: "created", 204: "overwritten", 405: "conflict", 409: "conflict", 507: QUOTA},
    "get": {200: "200", 404: "404"},
}


class Unsupported(Exception):
    pass


def parse_multistatus(body):
    """Return [(href path, [(status code, {prop tag: text})])] from a 207 body."""
    out = []
    root = ET.fromstring(body)
    for resp in root.iter(f"{DAV}response"):
        href = urllib.parse.unquote(urllib.parse.urlparse(resp.findtext(f"{DAV}href", "")).path)
        stats = []
        for ps in resp.findall(f"{DAV}propstat"):
            status_line = ps.findtext(f"{DAV}status", "")
            code = int(status_line.split()[1]) if len(status_line.split()) > 1 else 0
            props = {}
            prop = ps.find(f"{DAV}prop")
            if prop is not None:
                for el in prop:
                    props[el.tag] = (el.text or "").strip()
            stats.append((code, props))
        out.append((href, stats))
    return out


class WebDAV:
    def __init__(self, url, auth=None, admin=None):
        u = urllib.parse.urlparse(url)
        self.host, self.port = u.hostname, u.port or 80
        self.prefix = u.path.rstrip("/")
        self.auth = auth or {}
        self.admin = admin
        self.conn = None  # one keep-alive connection; a new one per request runs out of ports

    def request(self, who, method, path, body=None, headers=None):
        h = dict(headers or {})
        cred = self.auth.get(who) if who else self.admin
        if cred:
            h["Authorization"] = "Basic " + base64.b64encode(cred.encode()).decode()
        if body is not None:
            body = body.encode() if isinstance(body, str) else body
            h.setdefault("Content-Type", "application/xml; charset=utf-8")
        reused = self.conn is not None
        if self.conn is None:
            self.conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        try:
            self.conn.request(method, self.prefix + path, body=body, headers=h)
            r = self.conn.getresponse()
            data = r.read()
        except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError):
            self.conn.close()
            self.conn = None
            if not reused:
                raise
            # The server had already closed the kept-alive connection, so the
            # request never reached it; sending it again cannot apply it twice.
            return self.request(who, method, path, body, headers)
        if r.will_close:
            self.conn.close()
            self.conn = None
        return r.status, data

    def new_world(self):
        """A fresh, empty folder standing in for a freshly reset backend."""
        root = f"/vs-replay-{uuid.uuid4().hex[:8]}"
        for p in (root + "/", root + "/cache/"):
            status, _ = self.request(None, "MKCOL", p)
            if status != 201:
                raise RuntimeError(f"could not create {p} on the server (HTTP {status})")
        return root

    def drop_world(self, root):
        self.request(None, "DELETE", root + "/")

    def do(self, root, who, op):
        kind, *args = op
        folder = f"{root}/cache"
        if kind == "fetch":
            raise Unsupported("fetch needs a remote repository; a plain WebDAV server has none")
        if kind == "list":
            status, body = self.request(who, "PROPFIND", folder + "/", headers={"Depth": "1"})
            if status != 207:
                return f"HTTP {status}"
            names = set()
            for href, _ in parse_multistatus(body):
                p = href.rstrip("/")
                if p != folder and p.startswith(folder + "/"):
                    names.add(p[len(folder) + 1:].split("/")[0])
            return tuple(sorted(names))
        path = f"{folder}/{args[0]}"
        if kind == "mkdir":
            status, _ = self.request(who, "MKCOL", path + "/")
            return STATUS_WORDS["mkdir"].get(status, f"HTTP {status}")
        if kind == "put":
            status, _ = self.request(who, "PUT", path, body=b"payload",
                                     headers={"Content-Type": "application/octet-stream"})
            return STATUS_WORDS["put"].get(status, f"HTTP {status}")
        if kind == "get":
            status, _ = self.request(who, "GET", path)
            return STATUS_WORDS["get"].get(status, f"HTTP {status}")
        if kind == "setprop":
            body = (
                '<?xml version="1.0" encoding="utf-8"?>'
                f'<D:propertyupdate xmlns:D="DAV:" xmlns:V="{PROP_NS}">'
                f"<D:set><D:prop><V:{PROP_NAME}>{args[1]}</V:{PROP_NAME}></D:prop></D:set>"
                "</D:propertyupdate>"
            )
            status, resp = self.request(who, "PROPPATCH", path, body=body)
            if status == 404:
                return "404"
            if status != 207:
                return f"HTTP {status}"
            codes = [code for _, stats in parse_multistatus(resp) for code, _ in stats]
            return "ok" if codes and all(c == 200 for c in codes) else f"HTTP 207/{codes}"
        if kind == "getprop":
            body = (
                '<?xml version="1.0" encoding="utf-8"?>'
                f'<D:propfind xmlns:D="DAV:" xmlns:V="{PROP_NS}"><D:prop><V:{PROP_NAME}/></D:prop></D:propfind>'
            )
            status, resp = self.request(who, "PROPFIND", path, body=body, headers={"Depth": "0"})
            if status == 404:
                return "404"
            if status != 207:
                return f"HTTP {status}"
            tag = f"{{{PROP_NS}}}{PROP_NAME}"
            for _, stats in parse_multistatus(resp):
                for code, props in stats:
                    if tag in props:
                        return props[tag] if code == 200 else "unset"
            return "unset"
        raise ValueError(f"unknown operation {op!r}")


def show(obs):
    return [list(o) if isinstance(o, tuple) else o for o in obs]


def run_world(server, trace, observer):
    root = server.new_world()
    try:
        seen = []
        for tenant, *op in trace:
            result = server.do(root, tenant, tuple(op))
            if tenant == observer:
                seen.append(result)
        return seen
    finally:
        server.drop_world(root)


def show_trace(trace):
    def op(kind, *args):
        return f"setprop({args[0]}={args[1]})" if kind == "setprop" else f"{kind}({', '.join(args)})"
    return " -> ".join(f"{t} {op(*o)}" for t, *o in trace)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("leaks", help="JSON written by `python3 -m checker ni --deployment direct --json ...`")
    p.add_argument("--url", default="http://127.0.0.1:8080")
    p.add_argument("--auth", action="append", default=[], metavar="TENANT=USER:PASS",
                   help="credentials for one agent, e.g. --auth A=alice:pw (default: anonymous)")
    p.add_argument("--admin", metavar="USER:PASS", help="credentials used to create and delete the test folders")
    args = p.parse_args(argv)

    with open(args.leaks) as f:
        data = json.load(f)
    if data["deployment"] != "direct":
        sys.exit(f"Only leaks from the 'direct' deployment can be replayed (got {data['deployment']!r}).")
    auth = dict(a.split("=", 1) for a in args.auth)
    server = WebDAV(args.url, auth=auth, admin=args.admin)
    observer = data["observer"]

    counts = {"CONFIRMED": 0, "NOT REPRODUCED": 0, "SKIPPED": 0}
    mismatched_steps = 0
    leaks = data["leaks"]
    for n, leak in enumerate(leaks, 1):
        print(f"[{n}/{len(leaks)}] {leak['channel']}: {leak['variant']}")
        print(f"    world 1: {show_trace(leak['world1'])}")
        print(f"    world 2: {show_trace(leak['world2'])}")
        try:
            real1 = run_world(server, leak["world1"], observer)
            real2 = run_world(server, leak["world2"], observer)
        except Unsupported as e:
            counts["SKIPPED"] += 1
            print(f"    SKIPPED: {e}\n")
            continue
        model1, model2 = leak["model_obs1"], leak["model_obs2"]
        real1, real2 = show(real1), show(real2)
        print(f"    {observer} saw on the server: {real1} vs {real2}")
        verdict = "CONFIRMED" if real1 != real2 else "NOT REPRODUCED"
        counts[verdict] += 1
        notes = []
        for world, model, real in ((1, model1, real1), (2, model2, real2)):
            for i, (m, r) in enumerate(zip(model, real)):
                if m != r:
                    mismatched_steps += 1
                    notes.append(f"world {world}, {observer}'s step {i + 1}: model said {m!r}, server said {r!r}")
        print(f"    {verdict}")
        if verdict == "NOT REPRODUCED" and any(QUOTA in (m1, m2) for m1, m2 in zip(model1, model2)):
            notes.append("the model assumes a small shared capacity; this server has no capacity limit")
        for note in notes:
            print(f"      model mismatch: {note}")
        print()

    print(
        f"Summary: {counts['CONFIRMED']} confirmed, {counts['NOT REPRODUCED']} not reproduced,"
        f" {counts['SKIPPED']} skipped; {mismatched_steps} step(s) where the server disagreed with the model"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
