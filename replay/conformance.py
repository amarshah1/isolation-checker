#!/usr/bin/env python3
"""Conformance test: does the checker's model match a real server?

    python3 replay/conformance.py --url http://127.0.0.1:8080 --depth 2

Runs every trace up to --depth on both the model (the `direct` deployment)
and a real WebDAV server, each from an empty starting state, and reports every
kind of step where they answer differently.

replay.py can only confirm leaks the model already found. This catches the
opposite problem: places where the model is wrong about the service, which
can hide leaks from the checker entirely. It is a direct test of the
assumption that the backend behaves the way the model says.
"""

from __future__ import annotations

import argparse
import itertools
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from checker.model import DEPLOYMENTS, TENANTS, Backend, agent_ops, show_obs, show_op  # noqa: E402
from replay import WebDAV  # noqa: E402  (replay/replay.py)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default="http://127.0.0.1:8080")
    p.add_argument("--depth", type=int, default=2)
    args = p.parse_args(argv)

    model = DEPLOYMENTS["direct"]
    server = WebDAV(args.url)
    # A plain WebDAV server has no remote repository, so fetch can't be compared.
    steps = [(t, op) for t in TENANTS for op in agent_ops() if op[0] != "fetch"]

    mismatches = {}  # (operation kind, model answer, server answer) -> example trace
    total = 0
    for length in range(1, args.depth + 1):
        for trace in itertools.product(steps, repeat=length):
            total += 1
            s = Backend()
            root = server.new_world()
            try:
                for i, (tenant, op) in enumerate(trace):
                    s, expected = model.handle(s, tenant, op)
                    actual = server.do(root, tenant, op)
                    if expected != actual:
                        key = (op[0], show_obs(expected), show_obs(actual))
                        mismatches.setdefault(key, trace[: i + 1])
                        break
            finally:
                server.drop_world(root)

    print(f"Compared the model with {args.url} on {total:,} traces (up to {args.depth} steps, fetch excluded)\n")
    if not mismatches:
        print("PASS: the server answered exactly as the model predicts on every step.")
        return 0
    print(f"FAIL: {len(mismatches)} kind(s) of step where the server disagrees with the model\n")
    for (kind, expected, actual), trace in sorted(mismatches.items()):
        steps_text = " -> ".join(f"{t} {show_op(op)}" for t, op in trace)
        print(f"  {kind}: model says {expected}, server says {actual}")
        print(f"    e.g. {steps_text}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
