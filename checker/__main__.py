"""Command line: python3 -m checker {ni,reach,demo} ..."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import ni, reach
from .model import DEPLOYMENTS, TENANTS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIGS = ("incident.json", "proxy-only.json", "fixed.json")


def cmd_ni(args):
    dep = DEPLOYMENTS[args.deployment]
    start = time.time()
    result = ni.check(dep, depth=args.depth, observer=args.observer)
    print(ni.format_result(result, dep.summary))
    print(f"\n({time.time() - start:.1f}s)")
    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w") as f:
            json.dump(result.to_json(), f, indent=2)
        print(f"Wrote {len(result.leaks)} leak(s) to {args.json}")
    return 1 if result.leaks else 0


def cmd_reach(args):
    cfg = reach.load(args.config)
    text, failed = reach.format_report(args.config, cfg)
    print(text)
    return 1 if failed else 0


def cmd_demo(args):
    rows = []
    for name, dep in DEPLOYMENTS.items():
        result = ni.check(dep, depth=args.depth)
        print(ni.format_result(result, dep.summary))
        print("\n" + "=" * 78 + "\n")
        channels = ", ".join(c for c, _ in result.by_channel()) or "none"
        rows.append((f"ni --deployment {name}", "FAIL" if result.leaks else "PASS", channels))
    for cfg_name in CONFIGS:
        path = os.path.join(ROOT, "configs", cfg_name)
        text, failed = reach.format_report(os.path.relpath(path), reach.load(path))
        print(text)
        print("\n" + "=" * 78 + "\n")
        rows.append((f"reach {cfg_name}", "FAIL" if failed else "PASS", ", ".join(failed)))

    print("Summary")
    width = max(len(r[0]) for r in rows)
    for check, verdict, detail in rows:
        print(f"  {check:<{width}}  {verdict}  {detail}")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="python3 -m checker", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("ni", help="search for channels between agents through the shared-service model")
    a.add_argument("--deployment", choices=sorted(DEPLOYMENTS), default="direct")
    a.add_argument("--depth", type=int, default=3, help="longest trace to try (default 3)")
    a.add_argument("--observer", choices=TENANTS, default="B")
    a.add_argument("--json", metavar="PATH", help="also write the leaks as JSON, for replay/replay.py")
    a.set_defaults(func=cmd_ni)

    b = sub.add_parser("reach", help="check what agents can reach in a network config")
    b.add_argument("config")
    b.set_defaults(func=cmd_reach)

    c = sub.add_parser("demo", help="run every check on every deployment and config")
    c.add_argument("--depth", type=int, default=3)
    c.set_defaults(func=cmd_demo)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
