"""The routing page's data (routes_api.py) and its `aicli routes-api` command."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_aicli as base  # noqa: E402  (its fixtures: make_config, run_aicli)
from test_aicli import ROOT, AICLI, make_config  # noqa: E402

sys.path.insert(0, str(ROOT))
import aicli  # noqa: E402
import routes_api  # noqa: E402


def setUpModule():
    base.setUpModule()


def tearDownModule():
    base.tearDownModule()


class RoutesApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        d = Path(self._tmp.name)
        self.config_path, self.config = make_config(d)
        self.routes = d / "routes.json"
        self.env = {**os.environ, "AICLI_CONFIG": str(self.config_path), "AICLI_ROUTES": str(self.routes)}
        self._old = os.environ.get("AICLI_ROUTES")
        os.environ["AICLI_ROUTES"] = str(self.routes)
        self.addCleanup(lambda: os.environ.__setitem__("AICLI_ROUTES", self._old))
        for args in (("pjm:auto-spec", "--class", "deep-spec"), ("pjm:auto-execute", "--class", "standard-dev"),
                     ("other:thing", "--class", "standard-dev"), ("other:solo", "--class", "maintenance")):
            self.cli("route", "set-client", *args)

    def cli(self, *args, stdin=None):
        return subprocess.run([sys.executable, str(AICLI), *args], capture_output=True, text=True,
                              env=self.env, input=stdin)

    def test_scope_limits_clients_classes_and_counts_sharing(self):
        data = routes_api.payload(self.config, {}, scope="pjm:")
        self.assertEqual(sorted(data["clients"]), ["pjm:auto-execute", "pjm:auto-spec"])
        self.assertEqual(sorted(data["classes"]), ["deep-spec", "standard-dev"])
        self.assertEqual(data["class_shared"], {"standard-dev": 1})
        self.assertEqual(sorted(data["fallback_status"]["classes"]), ["deep-spec", "standard-dev"])

    def test_unscoped_payload_has_everything(self):
        data = routes_api.payload(self.config, {})
        self.assertIn("other:solo", data["clients"])
        self.assertIn("maintenance", data["classes"])
        self.assertEqual(data["class_shared"], {})

    def test_scoped_save_sets_fallback_and_cannot_touch_other_clients(self):
        fb = {"tool": "agy", "model": "gemini-4-argon-medium"}
        body = {"classes": {"deep-spec": {"fallback": fb}, "maintenance": {"fallback": fb}},
                "clients": {"other:thing": {"class": "deep-spec"}}}
        routes_api.save(self.config, body, scope="pjm:")
        merged = aicli.load_routes(self.config)
        self.assertEqual(merged["classes"]["deep-spec"]["fallback"]["model"], "gemini-4-argon-medium")
        self.assertNotEqual((merged["classes"]["maintenance"].get("fallback") or {}).get("model"),
                            "gemini-4-argon-medium")
        self.assertEqual(merged["clients"]["other:thing"]["class"], "standard-dev")

    def test_scoped_save_leaves_clients_outside_the_scope_when_the_page_omits_them(self):
        routes_api.save(self.config, {"clients": {"pjm:auto-spec": {"class": "deep-spec"}}}, scope="pjm:")
        clients = aicli.load_routes(self.config)["clients"]
        self.assertNotIn("pjm:auto-execute", clients)   # in scope and omitted: removed
        self.assertIn("other:thing", clients)           # out of scope: untouched

    def test_clearing_a_fallback_saves_none(self):
        fb = {"tool": "agy", "model": "gemini-4-argon-medium"}
        routes_api.save(self.config, {"classes": {"deep-spec": {"fallback": fb}}}, scope="pjm:")
        routes_api.save(self.config, {"classes": {"deep-spec": {"fallback": None}}}, scope="pjm:")
        self.assertFalse(aicli.fallback_steps(self.config, workload_class="deep-spec"))

    def test_cli_get_and_save_round_trip(self):
        got = self.cli("routes-api", "get", "--scope", "pjm:")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("pjm:auto-spec", json.loads(got.stdout)["clients"])
        body = {"classes": {"deep-spec": {"fallback": {"tool": "claude", "model": "claude-sonnet-5"}}}}
        saved = self.cli("routes-api", "save", "--scope", "pjm:", stdin=json.dumps(body))
        self.assertEqual(saved.returncode, 0, saved.stderr)
        again = json.loads(self.cli("routes-api", "get", "--scope", "pjm:").stdout)
        self.assertEqual(again["classes"]["deep-spec"]["fallback"]["model"], "claude-sonnet-5")
        self.assertEqual(self.cli("routes-api", "save", stdin="nope").returncode, 2)

    def test_static_dir_holds_the_page(self):
        out = self.cli("routes-api", "static").stdout.strip()
        for name in ("page.html", "routes.js", "board.js", "routes.css", "standalone.css"):
            self.assertTrue((Path(out) / name).is_file(), name)

    def test_render_page_fills_every_placeholder(self):
        page = routes_api.render_page({"scope": "pjm:", "api": "/api/ai-routes"}, heading="AI Routing",
                                      back_href="/", back_label="Back", static="/routes_static")
        self.assertNotRegex(page, r"__[A-Z_]+__")
        self.assertIn('class="scoped "', page)
        self.assertIn('"scope": "pjm:"', page)


if __name__ == "__main__":
    unittest.main()
