"""Run with: python3 -m unittest discover -s tests"""

import importlib.util
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from checker import ni, reach  # noqa: E402
from checker.model import DEPLOYMENTS  # noqa: E402

_spec = importlib.util.spec_from_file_location("replay_mod", os.path.join(ROOT, "replay", "replay.py"))
replay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(replay)

STORAGE = "shared storage namespace"
REMOTE = "shared remote-repository cache"
QUOTA = "shared capacity (quota)"


class NonInterference(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = {name: ni.check(dep, depth=3) for name, dep in DEPLOYMENTS.items()}

    def channels(self, name):
        return {c for c, _ in self.results[name].by_channel()}

    def variants(self, name):
        return {leak.variant() for leak in self.results[name].leaks}

    def test_direct_has_the_incidents_channels(self):
        self.assertEqual(self.channels("direct"), {STORAGE, REMOTE, QUOTA})
        self.assertIn("list <- mkdir", self.variants("direct"))  # MKCOL message board
        self.assertIn("getprop <- mkdir, setprop", self.variants("direct"))  # properties
        self.assertIn("fetch <- fetch", self.variants("direct"))  # cached remote names

    def test_message_board_leak_is_two_steps(self):
        leak = next(l for l in self.results["direct"].leaks if l.variant() == "list <- mkdir")
        self.assertEqual(leak.world1, (("A", ("mkdir", "x")), ("B", ("list",))))
        self.assertEqual((leak.obs1, leak.obs2), ((("x",),), ((),)))

    def test_naive_proxy_closes_the_namespace_but_not_the_cache_or_quota(self):
        self.assertEqual(self.channels("naive-proxy"), {REMOTE, QUOTA})

    def test_isolating_proxy_has_no_leak_up_to_depth_4(self):
        self.assertEqual(self.results["isolating-proxy"].leaks, [])
        self.assertEqual(ni.check(DEPLOYMENTS["isolating-proxy"], depth=4).leaks, [])

    def test_isolating_proxy_also_protects_A_from_B(self):
        self.assertEqual(ni.check(DEPLOYMENTS["isolating-proxy"], depth=3, observer="A").leaks, [])
        self.assertTrue(ni.check(DEPLOYMENTS["direct"], depth=3, observer="A").leaks)

    def test_every_reported_leak_reproduces_in_the_model(self):
        for name, result in self.results.items():
            for leak in result.leaks:
                self.assertTrue(all(t == "B" for t, _ in leak.world2))
                self.assertEqual(ni.run(DEPLOYMENTS[name], leak.world1, "B"), leak.obs1)
                self.assertEqual(ni.run(DEPLOYMENTS[name], leak.world2, "B"), leak.obs2)
                self.assertNotEqual(leak.obs1, leak.obs2)


class Reachability(unittest.TestCase):
    def kinds(self, config, relay_aware):
        cfg = reach.load(os.path.join(ROOT, "configs", config))
        return {f.kind for f in reach.check(cfg, relay_aware)[0]}

    def test_incident_passes_packet_level_but_fails_relay_aware(self):
        self.assertEqual(self.kinds("incident.json", relay_aware=False), {"shared-state"})
        self.assertEqual(self.kinds("incident.json", relay_aware=True), {"egress", "shared-state"})

    def test_proxy_alone_does_not_stop_egress(self):
        self.assertEqual(self.kinds("proxy-only.json", relay_aware=False), set())
        self.assertEqual(self.kinds("proxy-only.json", relay_aware=True), {"egress"})

    def test_fixed_config_passes(self):
        self.assertEqual(self.kinds("fixed.json", relay_aware=False), set())
        self.assertEqual(self.kinds("fixed.json", relay_aware=True), set())

    def test_egress_path_goes_through_the_relay(self):
        cfg = reach.load(os.path.join(ROOT, "configs", "incident.json"))
        egress = [f for f in reach.check(cfg, True)[0] if f.kind == "egress"]
        self.assertIn("agent-a -> artifactory (relay) -> internet", [p for f in egress for p in f.paths])


class ReplayParsing(unittest.TestCase):
    def test_parse_multistatus(self):
        body = b"""<?xml version="1.0" encoding="utf-8"?>
        <D:multistatus xmlns:D="DAV:" xmlns:V="urn:verified-sandboxing">
          <D:response><D:href>/w/cache/</D:href>
            <D:propstat><D:prop/><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>
          <D:response><D:href>http://127.0.0.1:8080/w/cache/x%20y/</D:href>
            <D:propstat><D:prop><V:tag>1</V:tag></D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat>
          </D:response>
        </D:multistatus>"""
        parsed = replay.parse_multistatus(body)
        self.assertEqual([href for href, _ in parsed], ["/w/cache/", "/w/cache/x y/"])
        self.assertEqual(parsed[1][1], [(200, {"{urn:verified-sandboxing}tag": "1"})])


if __name__ == "__main__":
    unittest.main()
