"""Tests for aicli.py against the fake CLI in tests/fake_cli/. No test runs the
real `claude` or `agy`."""
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAKE_CLI = ROOT / "tests" / "fake_cli"
AICLI = ROOT / "aicli.py"

sys.path.insert(0, str(ROOT))
import aicli  # noqa: E402

_STATE = None


def setUpModule():
    # Keep every test - in-process and `aicli.py` subprocesses alike - off the
    # real usage log and the live routes.json the cockpit edits.
    global _STATE
    _STATE = tempfile.TemporaryDirectory()
    os.environ["AICLI_USAGE_LOG"] = str(Path(_STATE.name) / "usage.jsonl")
    os.environ["AICLI_ROUTES"] = str(Path(_STATE.name) / "routes.json")
    os.environ["AICLI_QUOTA_CACHE"] = str(Path(_STATE.name) / "quota.json")


def tearDownModule():
    os.environ.pop("AICLI_USAGE_LOG", None)
    os.environ.pop("AICLI_ROUTES", None)
    os.environ.pop("AICLI_QUOTA_CACHE", None)
    _STATE.cleanup()


def make_config(tmp_dir, **overrides):
    """A config pointing at the fake CLIs, written as a real aicli.toml so
    both the in-process functions and `aicli.py` subprocess calls see it."""
    cfg = {
        "usage_timeout": 5,
        "claude": {
            "bin": str(FAKE_CLI / "fake-claude"),
            "args": [],
            "model_flag": "--model",
            "effort_flag": "--effort",
            "tiers": {"low": "claude-haiku-5-5", "default": "claude-sonnet-5", "deep": "claude-opus-5-5"},
        },
        "agy": {
            "bin": str(FAKE_CLI / "fake-agy"),
            "args": [],
            "model_flag": "--model",
            "effort_flag": "--effort",
            "tiers": {"low": "gemini-3.8-flash-low", "default": "gemini-3.8-flash-high", "deep": "gemini-3.8-flash-high"},
        },
    }
    for tool, patch in overrides.items():
        cfg[tool].update(patch)
    # The real workload classes, so routing tests use what ships.
    with open(ROOT / "aicli.toml", "rb") as f:
        shipped = tomllib.load(f)
    cfg["classes"] = shipped["classes"]
    cfg["fallback"] = shipped["fallback"]
    path = tmp_dir / "aicli.toml"
    path.write_text(_to_toml(cfg))
    return path, cfg


def _toml_val(value):
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{k} = {_toml_val(v)}" for k, v in value.items()) + " }"
    if isinstance(value, list):
        return "[ " + ", ".join(_toml_val(v) for v in value) + " ]"
    return json.dumps(value)


def _to_toml(cfg):
    # Minimal hand-rolled TOML writer - good enough for this flat shape.
    lines = [f"usage_timeout = {cfg['usage_timeout']}"]
    for tool in ("claude", "agy"):
        t = cfg[tool]
        lines.append(f"\n[{tool}]")
        lines.append(f'bin = "{t["bin"]}"')
        lines.append(f"args = {json.dumps(t['args'])}")
        lines.append(f'model_flag = "{t["model_flag"]}"')
        lines.append(f'effort_flag = "{t["effort_flag"]}"')
        if t.get("model_sets_effort"):
            lines.append("model_sets_effort = true")
        lines.append(f"\n[{tool}.tiers]")
        for tier, model in t["tiers"].items():
            lines.append(f'{tier} = "{model}"')
    if "fallback" in cfg:
        lines.append("\n[fallback]")
        lines.append(f"at = {cfg['fallback']['at']}")
        for tool, steps in cfg["fallback"].items():
            if tool == "at":
                continue
            for step in steps:
                lines.append(f"\n[[fallback.{tool}]]")
                for key, value in step.items():
                    lines.append(f"{key} = {_toml_val(value)}")
    for name, c in cfg.get("classes", {}).items():
        lines.append(f"\n[classes.{name}]")
        for key, value in c.items():
            lines.append(f"{key} = {_toml_val(value)}")
    return "\n".join(lines) + "\n"


def run_aicli(*args, config_path):
    env = {**os.environ, "AICLI_CONFIG": str(config_path)}
    return subprocess.run(
        [sys.executable, str(AICLI), *args], capture_output=True, text=True, env=env,
    )


class WhichTests(unittest.TestCase):
    def test_which_prints_configured_bin(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, _ = make_config(Path(d))
            result = run_aicli("which", "claude", config_path=config_path)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), str(FAKE_CLI / "fake-claude"))

    def test_which_falls_back_to_path_when_configured_bin_missing(self):
        with tempfile.TemporaryDirectory() as d:
            path_dir = Path(d) / "path-bin"
            path_dir.mkdir()
            (path_dir / "claude").symlink_to(FAKE_CLI / "fake-claude")
            config_path, _ = make_config(Path(d), claude={"bin": str(Path(d) / "missing")})
            env = {**os.environ, "AICLI_CONFIG": str(config_path), "PATH": f"{path_dir}:{os.environ['PATH']}"}
            result = subprocess.run(
                [sys.executable, str(AICLI), "which", "claude"], capture_output=True, text=True, env=env,
            )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), str(path_dir / "claude"))

    def test_which_skips_usr_local_bin_to_avoid_wrapper_loop(self):
        # /usr/local/bin is where the tmux-wrapper symlinks live (see
        # tools/optiplex-ai-tmux.sh); resolve_bin must never return a match
        # found there, since that would exec the wrapper and loop. Swap in a
        # stand-in SKIP_DIRS entry so the test doesn't touch the real path.
        with tempfile.TemporaryDirectory() as d:
            wrapper_dir = Path(d) / "wrapper-bin"
            wrapper_dir.mkdir()
            wrapper = wrapper_dir / "claude"
            wrapper.write_text("#!/bin/sh\necho wrapper\n")
            wrapper.chmod(0o755)
            path_dir = Path(d) / "path-bin"
            path_dir.mkdir()
            (path_dir / "claude").symlink_to(FAKE_CLI / "fake-claude")
            config_path, _ = make_config(Path(d), claude={"bin": str(Path(d) / "missing")})
            config = aicli.load_config(config_path)

            old_skip, old_path = aicli.SKIP_DIRS, os.environ.get("PATH", "")
            aicli.SKIP_DIRS = {str(wrapper_dir)}
            os.environ["PATH"] = f"{wrapper_dir}:{path_dir}"
            try:
                bin_path = aicli.resolve_bin(config, "claude")
            finally:
                aicli.SKIP_DIRS, os.environ["PATH"] = old_skip, old_path
        self.assertEqual(bin_path, str(path_dir / "claude"))

    def test_which_unknown_tool_not_found(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, _ = make_config(Path(d), claude={"bin": str(Path(d) / "missing")})
            result = subprocess.run(
                [sys.executable, str(AICLI), "which", "claude"],
                capture_output=True, text=True,
                env={**os.environ, "AICLI_CONFIG": str(config_path), "PATH": "/nonexistent"},
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn("claude CLI not found", result.stderr)

    def test_which_rejects_bad_tool_name(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, _ = make_config(Path(d))
            result = run_aicli("which", "bogus", config_path=config_path)
        self.assertEqual(result.returncode, 2)


class ArgvTests(unittest.TestCase):
    def test_argv_default_tier_adds_model_flag(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, _ = make_config(Path(d))
            result = run_aicli("argv", "--tool", "claude", "--json", config_path=config_path)
        argv = json.loads(result.stdout)
        self.assertEqual(argv, [str(FAKE_CLI / "fake-claude"), "--model", "claude-sonnet-5"])

    def test_argv_tier_and_effort_come_from_the_router(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, _ = make_config(Path(d))
            routes_path = Path(d) / "routes.json"
            routes_path.write_text(json.dumps({"classes": {"t": {"tool": "agy", "tier": "deep", "effort": "high"}}}))
            result = subprocess.run(
                [sys.executable, str(AICLI), "argv", "--class", "t", "--json"],
                capture_output=True, text=True,
                env={**os.environ, "AICLI_CONFIG": str(config_path), "AICLI_ROUTES": str(routes_path)},
            )
        argv = json.loads(result.stdout)
        self.assertEqual(
            argv,
            [str(FAKE_CLI / "fake-agy"), "--model", "gemini-3.8-flash-high", "--effort", "high"],
        )

    def test_argv_model_sets_effort_drops_effort(self):
        # agy refuses `--model gemini-3.8-flash-low --effort medium`, so when
        # the tier's model names the effort, aicli must not add one.
        with tempfile.TemporaryDirectory() as d:
            config_path, _ = make_config(Path(d), agy={"model_sets_effort": True})
            routes_path = Path(d) / "routes.json"
            routes_path.write_text(json.dumps({"classes": {"t": {"tool": "agy", "tier": "low", "effort": "medium"}}}))
            result = subprocess.run(
                [sys.executable, str(AICLI), "argv", "--class", "t", "--json", "--", "--effort=high", "-p", "hi"],
                capture_output=True, text=True,
                env={**os.environ, "AICLI_CONFIG": str(config_path), "AICLI_ROUTES": str(routes_path)},
            )
        argv = json.loads(result.stdout)
        self.assertEqual(
            argv,
            [str(FAKE_CLI / "fake-agy"), "--model", "gemini-3.8-flash-low", "-p", "hi"],
        )

    def test_argv_model_sets_effort_keeps_effort_with_caller_model(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, _ = make_config(Path(d), agy={"model_sets_effort": True})
            result = run_aicli(
                "argv", "--tool", "agy", "--json",
                "--", "--effort", "high", "--model=custom",
                config_path=config_path,
            )
        argv = json.loads(result.stdout)
        self.assertEqual(argv, [str(FAKE_CLI / "fake-agy"), "--effort", "high", "--model=custom"])

    def test_argv_pass_through_args_after_dashdash(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, _ = make_config(Path(d))
            result = run_aicli(
                "argv", "--tool", "claude", "--json", "--", "-p", "/usage",
                config_path=config_path,
            )
        argv = json.loads(result.stdout)
        self.assertEqual(argv[-2:], ["-p", "/usage"])

    def test_argv_caller_supplied_model_flag_wins(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, _ = make_config(Path(d))
            result = run_aicli(
                "argv", "--tool", "claude", "--json", "--", "--model", "custom-model",
                config_path=config_path,
            )
        argv = json.loads(result.stdout)
        self.assertEqual(argv, [str(FAKE_CLI / "fake-claude"), "--model", "custom-model"])
        self.assertEqual(argv.count("--model"), 1)

    def test_argv_strips_redundant_leading_tool_name(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, _ = make_config(Path(d))
            result = run_aicli(
                "argv", "--tool", "claude", "--json", "--", "claude", "--session-id", "s1",
                config_path=config_path,
            )
        argv = json.loads(result.stdout)
        self.assertEqual(argv, [str(FAKE_CLI / "fake-claude"), "--model", "claude-sonnet-5", "--session-id", "s1"])

    def test_argv_non_default_shell_quoted_line(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, _ = make_config(Path(d))
            result = run_aicli("argv", "--tool", "claude", config_path=config_path)
        self.assertEqual(
            result.stdout.strip(),
            f"{FAKE_CLI / 'fake-claude'} --model claude-sonnet-5",
        )

    def test_callers_cannot_pass_tier_effort_or_model(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, _ = make_config(Path(d))
            for flag, value in (("--tier", "low"), ("--effort", "high"), ("--model", "x")):
                for cmd in ("argv", "run", "resolve"):
                    result = run_aicli(cmd, "--tool", "claude", flag, value, config_path=config_path)
                    self.assertEqual(result.returncode, 2, (cmd, flag))

    def test_argv_binary_not_found_exits_1(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, _ = make_config(Path(d), claude={"bin": str(Path(d) / "missing")})
            result = subprocess.run(
                [sys.executable, str(AICLI), "argv", "--tool", "claude"],
                capture_output=True, text=True,
                env={**os.environ, "AICLI_CONFIG": str(config_path), "PATH": "/nonexistent"},
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn("claude CLI not found", result.stderr)


class RunTests(unittest.TestCase):
    def test_run_execs_the_fake_cli_with_built_argv(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, _ = make_config(Path(d))
            result = run_aicli(
                "run", "--tool", "agy", "--", "-p", "hello",
                config_path=config_path,
            )
        self.assertEqual(result.returncode, 0)
        argv = json.loads(result.stdout)
        self.assertEqual(
            argv,
            [str(FAKE_CLI / "fake-agy"), "--model", "gemini-3.8-flash-high", "-p", "hello"],
        )


class UsageTests(unittest.TestCase):
    def test_usage_json_matches_old_compute_shape(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, _ = make_config(Path(d))
            result = run_aicli("usage", "--json", config_path=config_path)
        self.assertEqual(result.returncode, 0)
        data = json.loads(result.stdout)
        self.assertEqual(
            data["claude"],
            {
                "state": "ok", "detail": "",
                "bars": {
                    "five_hour": {"pct": 76, "reset": "Aug 25, 1:09am (Europe/Paris)"},
                    "weekly": {"pct": 6, "reset": "Aug 31, 6:59pm (Europe/Paris)"},
                },
            },
        )
        self.assertEqual(data["agy"]["state"], "ok")
        # Gemini is the primary group and reported for both periods here, so
        # it wins even though "Claude and GPT models" also has a weekly line.
        self.assertEqual(data["agy"]["bars"]["weekly"]["pct"], 6)
        self.assertEqual(data["agy"]["bars"]["weekly"]["group"], "Gemini Models")
        self.assertNotIn("five_hour", data["agy"]["bars"])  # "disabled" line

    def test_usage_tool_not_found_reports_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, _ = make_config(Path(d), claude={"bin": str(Path(d) / "missing")})
            result = subprocess.run(
                [sys.executable, str(AICLI), "usage", "--json"],
                capture_output=True, text=True,
                env={**os.environ, "AICLI_CONFIG": str(config_path), "PATH": "/nonexistent"},
            )
        self.assertEqual(result.returncode, 0)  # usage always exits 0
        data = json.loads(result.stdout)
        self.assertEqual(data["claude"], {"state": "unknown", "detail": "claude CLI not found", "bars": {}})

    def test_usage_readable_line_without_json(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, _ = make_config(Path(d))
            result = run_aicli("usage", config_path=config_path)
        self.assertIn("claude: ok", result.stdout)
        self.assertIn("agy: ok", result.stdout)


class ConfigTests(unittest.TestCase):
    def test_missing_config_file_exits_2(self):
        result = subprocess.run(
            [sys.executable, str(AICLI), "which", "claude"],
            capture_output=True, text=True,
            env={**os.environ, "AICLI_CONFIG": "/nonexistent/aicli.toml"},
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("/nonexistent/aicli.toml", result.stderr)

    def test_invalid_config_file_exits_2(self):
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "aicli.toml"
            bad.write_text("not = valid = toml")
            result = subprocess.run(
                [sys.executable, str(AICLI), "which", "claude"],
                capture_output=True, text=True,
                env={**os.environ, "AICLI_CONFIG": str(bad)},
            )
        self.assertEqual(result.returncode, 2)


class RoutingTests(unittest.TestCase):
    def test_resolve_route_by_class(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, config = make_config(Path(d))
            tool, tier, effort, cls = aicli.resolve_route(config, workload_class="maintenance")
            self.assertEqual((tool, tier), ("agy", "low"))
            self.assertEqual(cls, "maintenance")

            tool, tier, effort, cls = aicli.resolve_route(config, workload_class="deep-spec")
            self.assertEqual((tool, tier), ("claude", "deep"))

            tool, tier, effort, cls = aicli.resolve_route(config, workload_class="standard-dev")
            self.assertEqual((tool, tier), ("claude", "default"))

    def test_resolve_route_unknown_class_raises(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, config = make_config(Path(d))
            with self.assertRaises(ValueError):
                aicli.resolve_route(config, workload_class="nonexistent-class")

    def test_resolve_route_client_override(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, config = make_config(Path(d))
            routes = {
                "classes": {"maintenance": {"tool": "agy", "tier": "low"}},
                "clients": {"custom-client": {"class": "maintenance", "tool": "claude", "tier": "deep"}},
            }
            tool, tier, _, cls = aicli.resolve_route(config, client="custom-client", routes=routes)
            self.assertEqual((tool, tier), ("claude", "deep"))
            self.assertEqual(cls, "maintenance")

    def test_argv_with_class(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, _ = make_config(Path(d))
            result = run_aicli("argv", "--class", "maintenance", "--json", config_path=config_path)
            self.assertEqual(result.returncode, 0)
            argv = json.loads(result.stdout)
            self.assertEqual(argv, [str(FAKE_CLI / "fake-agy"), "--model", "gemini-3.8-flash-low"])

    def test_routes_command_json(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, _ = make_config(Path(d))
            result = run_aicli("routes", "--json", config_path=config_path)
            self.assertEqual(result.returncode, 0)
            data = json.loads(result.stdout)
            self.assertIn("maintenance", data["classes"])
            self.assertEqual(data["classes"]["maintenance"]["tool"], "agy")

    def test_route_set_and_remove(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, _ = make_config(Path(d))
            routes_path = Path(d) / "routes.json"
            env = {**os.environ, "AICLI_CONFIG": str(config_path), "AICLI_ROUTES": str(routes_path)}

            # Set class route
            res = subprocess.run([sys.executable, str(AICLI), "route", "set-class", "maintenance",
                                  "--tool", "claude", "--tier", "deep"], capture_output=True, text=True, env=env)
            self.assertEqual(res.returncode, 0)

            # Check argv reflects the update
            res = subprocess.run([sys.executable, str(AICLI), "argv", "--class", "maintenance", "--json"],
                                 capture_output=True, text=True, env=env)
            self.assertEqual(res.returncode, 0)
            argv = json.loads(res.stdout)
            self.assertEqual(argv, [str(FAKE_CLI / "fake-claude"), "--model", "claude-opus-5-5"])

            # Set client route
            res = subprocess.run([sys.executable, str(AICLI), "route", "set-client", "pjm:maint",
                                  "--class", "maintenance", "--tool", "agy", "--tier", "low"],
                                 capture_output=True, text=True, env=env)
            self.assertEqual(res.returncode, 0)
            res = subprocess.run([sys.executable, str(AICLI), "argv", "--client", "pjm:maint", "--json"],
                                 capture_output=True, text=True, env=env)
            argv = json.loads(res.stdout)
            self.assertEqual(argv, [str(FAKE_CLI / "fake-agy"), "--model", "gemini-3.8-flash-low"])

            # Remove client route
            res = subprocess.run([sys.executable, str(AICLI), "route", "remove-client", "pjm:maint"],
                                 capture_output=True, text=True, env=env)
            self.assertEqual(res.returncode, 0)

    def test_route_set_client_rejects_unknown_class(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, _ = make_config(Path(d))
            routes_path = Path(d) / "routes.json"
            env = {**os.environ, "AICLI_CONFIG": str(config_path), "AICLI_ROUTES": str(routes_path)}
            res = subprocess.run([sys.executable, str(AICLI), "route", "set-client", "x:y", "--class", "nope"],
                                 capture_output=True, text=True, env=env)
            self.assertEqual(res.returncode, 2)
            self.assertIn("unknown workload class", res.stderr)
            self.assertFalse(routes_path.exists())

    def test_resolve_json(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, _ = make_config(Path(d))
            res = subprocess.run(
                [sys.executable, str(AICLI), "resolve", "--class", "standard-dev", "--json"],
                capture_output=True, text=True,
                env={**os.environ, "AICLI_CONFIG": str(config_path)},
            )
            self.assertEqual(res.returncode, 0)
            data = json.loads(res.stdout)
            self.assertEqual(data["class"], "standard-dev")
            self.assertEqual(data["tool"], "claude")
            self.assertEqual(data["model"], "claude-sonnet-5")
            self.assertIn("quota", data)

    def test_run_meta_out(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, _ = make_config(Path(d))
            meta_file = Path(d) / "run.meta.json"
            # FAKE_CLI fake-claude will be execv'd
            res = subprocess.run(
                [sys.executable, str(AICLI), "run", "--class", "standard-dev", "--client", "pjm:test:run-1",
                 "--meta-out", str(meta_file), "--", "echo", "hello"],
                capture_output=True, text=True,
                env={**os.environ, "AICLI_CONFIG": str(config_path)},
            )
            self.assertEqual(res.returncode, 0)
            self.assertTrue(meta_file.exists())
            data = json.loads(meta_file.read_text())
            self.assertEqual(data["client"], "pjm:test:run-1")
            self.assertEqual(data["class"], "standard-dev")
            self.assertEqual(data["tool"], "claude")
            self.assertEqual(data["model"], "claude-sonnet-5")
            self.assertIn("quota", data)

    def test_run_meta_out_reports_the_callers_own_model(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, _ = make_config(Path(d))
            meta_file = Path(d) / "run.meta.json"
            res = subprocess.run(
                [sys.executable, str(AICLI), "run", "--tool", "claude",
                 "--meta-out", str(meta_file), "--", "--model", "claude-opus-5-5", "-p", "hi"],
                capture_output=True, text=True,
                env={**os.environ, "AICLI_CONFIG": str(config_path)},
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            data = json.loads(meta_file.read_text())
            self.assertEqual(data["model"], "claude-opus-5-5")

    def _run_logged(self, d, *args, tool="claude"):
        config_path, _ = make_config(Path(d))
        usage_log = Path(d) / "usage.jsonl"
        res = subprocess.run(
            [sys.executable, str(AICLI), "run", "--client", "t:x", "--tool", tool, "--", *args],
            capture_output=True, text=True,
            env={**os.environ, "AICLI_CONFIG": str(config_path), "AICLI_USAGE_LOG": str(usage_log)},
        )
        rows = [json.loads(line) for line in usage_log.read_text().splitlines()]
        return res, rows

    def test_run_logs_the_token_counts_claude_reports(self):
        with tempfile.TemporaryDirectory() as d:
            res, rows = self._run_logged(d, "-p", "hi", "--output-format", "json")
        self.assertEqual(res.returncode, 0)
        self.assertIn('"modelUsage"', res.stdout)  # output passed through untouched
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["model"], row["reported"], row["client"]), ("claude-sonnet-5", True, "t:x"))
        self.assertNotIn("estimated", row)
        self.assertEqual((row["input"], row["output"], row["cache_read"], row["total"]), (3, 7, 100, 130))

    def test_run_logs_the_token_counts_agy_reports(self):
        with tempfile.TemporaryDirectory() as d:
            _, rows = self._run_logged(d, "-p", "hi", "--output-format", "json", tool="agy")
        self.assertEqual((rows[0]["reported"], rows[0]["input"], rows[0]["output"]), (True, 11, 5))
        self.assertNotIn("estimated", rows[0])

    def test_run_logs_counts_from_the_final_stream_event(self):
        with tempfile.TemporaryDirectory() as d:
            _, rows = self._run_logged(d, "-p", "hi", "--output-format", "stream-json")
        self.assertEqual({r["model"] for r in rows}, {"claude-sonnet-5", "claude-haiku-5-5"})
        self.assertTrue(all(r["reported"] for r in rows))

    def test_run_estimates_counts_when_the_cli_reports_none(self):
        with tempfile.TemporaryDirectory() as d:
            res, rows = self._run_logged(d, "-p", "hello there")
        self.assertEqual(res.returncode, 0)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["reported"])
        self.assertTrue(rows[0]["estimated"])
        self.assertGreater(rows[0]["input"], 0)
        self.assertGreater(rows[0]["output"], 0)
        self.assertEqual(rows[0]["total"], rows[0]["input"] + rows[0]["output"])

    def test_run_passes_the_exit_code_through_and_still_logs(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, _ = make_config(Path(d))
            usage_log = Path(d) / "usage.jsonl"
            res = subprocess.run(
                [sys.executable, str(AICLI), "run", "--tool", "claude", "--", "-p", "x"],
                capture_output=True, text=True,
                env={**os.environ, "AICLI_CONFIG": str(config_path), "AICLI_USAGE_LOG": str(usage_log),
                     "FAKE_CLI_EXIT_CODE": "3"},
            )
            self.assertEqual(res.returncode, 3)
            self.assertEqual(len(usage_log.read_text().splitlines()), 1)

    def test_estimate_is_marked_and_skipped_for_no_output(self):
        self.assertEqual(aicli.estimate_tokens(400), 100)
        self.assertEqual(aicli.run_usage_fields("claude", "m", "", 0, 0), (None, False))
        usage, est = aicli.run_usage_fields("claude", "m", "abcdefgh", 8, 8)
        self.assertTrue(est)
        self.assertEqual(usage["m"]["total"], 4)

    def test_run_cli_not_found_writes_no_meta_or_usage(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, _ = make_config(Path(d), claude={"bin": str(Path(d) / "missing")})
            meta_file = Path(d) / "run.meta.json"
            usage_log = Path(d) / "usage.jsonl"
            res = subprocess.run(
                [sys.executable, str(AICLI), "run", "--tool", "claude", "--meta-out", str(meta_file)],
                capture_output=True, text=True,
                env={**os.environ, "AICLI_CONFIG": str(config_path), "PATH": "/nonexistent",
                     "AICLI_USAGE_LOG": str(usage_log)},
            )
            self.assertEqual(res.returncode, 1)
            self.assertFalse(meta_file.exists())
            self.assertFalse(usage_log.exists())

    def test_save_routes_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory() as d:
            routes_path = Path(d) / "routes.json"
            aicli.save_routes({"classes": {}, "clients": {}}, routes_path=routes_path)
            aicli.save_routes({"classes": {}, "clients": {"a": {}}}, routes_path=routes_path)
            self.assertEqual(json.loads(routes_path.read_text())["clients"], {"a": {}})
            self.assertEqual([p.name for p in Path(d).iterdir()], ["routes.json"])

    def test_save_routes_through_a_link_writes_the_linked_file(self):
        with tempfile.TemporaryDirectory() as d:
            real = Path(d) / "deploy" / "routes.json"
            real.parent.mkdir()
            real.write_text("{}\n")
            link = Path(d) / "routes.json"
            link.symlink_to(real)
            aicli.save_routes({"classes": {}, "clients": {"a": {}}}, routes_path=link)
            self.assertTrue(link.is_symlink())
            self.assertEqual(json.loads(real.read_text())["clients"], {"a": {}})

    def test_default_config_prefers_the_user_config(self):
        with tempfile.TemporaryDirectory() as d:
            user_config = Path(d) / "aicli.toml"
            saved = aicli.USER_CONFIG
            self.addCleanup(setattr, aicli, "USER_CONFIG", saved)
            aicli.USER_CONFIG = user_config
            self.assertEqual(aicli.default_config_path(), ROOT / "aicli.toml")
            user_config.write_text("")
            self.assertEqual(aicli.default_config_path(), user_config)

    def test_resolve_route_hierarchical_client_matching(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, config = make_config(Path(d))
            routes = {
                "classes": config["classes"],
                "clients": {
                    "pjm:auto-execute": {"class": "standard-dev"},
                    "pjm:maintainer:routine": {"class": "maintenance"},
                    "pjm:scrum-master": {"class": "managerial-audit"},
                },
                "optiplex_projects": ["pjm", "homelab"],
            }
            # Test exact match
            tool, tier, effort, cls = aicli.resolve_route(config, client="pjm:scrum-master", routes=routes)
            self.assertEqual((tool, tier, cls), ("agy", "low", "managerial-audit"))

            # Test hierarchical match for ticket run
            tool, tier, effort, cls = aicli.resolve_route(config, client="pjm:p-0001:auto-execute", routes=routes)
            self.assertEqual((tool, tier, cls), ("claude", "default", "standard-dev"))

            # Test hierarchical match for maintainer run
            tool, tier, effort, cls = aicli.resolve_route(config, client="pjm:maintainer:homelab:routine", routes=routes)
            self.assertEqual((tool, tier, cls), ("agy", "low", "maintenance"))

    def test_client_and_class_pin_a_model(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, config = make_config(Path(d))
            routes = {
                "classes": config["classes"],
                "clients": {
                    "pjm:auto-spec@deep-spec": {"model": "claude-opus-5-5"},
                    "pjm:auto-spec": {"model": "claude-sonnet-5"},
                },
                "optiplex_projects": [],
            }
            self.assertEqual(aicli.route_model(routes, "pjm:p-0001:auto-spec", "deep-spec"), "claude-opus-5-5")
            self.assertEqual(aicli.route_model(routes, "pjm:p-0001:auto-spec", "standard-dev"), "claude-sonnet-5")
            self.assertIsNone(aicli.route_model(routes, "pjm:p-0001:auto-plan", "deep-spec"))

    def test_optiplex_projects_load_and_save(self):
        with tempfile.TemporaryDirectory() as d:
            routes_file = Path(d) / "routes.json"
            routes = {
                "classes": {"test": {"tool": "agy", "tier": "low"}},
                "clients": {"c1": {"class": "test"}},
                "optiplex_projects": ["pjm", "homelab", "farah"],
            }
            aicli.save_routes(routes, routes_path=routes_file)
            loaded = aicli.load_routes(routes_path=routes_file)
            self.assertEqual(loaded["optiplex_projects"], ["pjm", "homelab", "farah"])
            self.assertIn("c1", loaded["clients"])


if __name__ == "__main__":
    unittest.main()



CLAUDE_OUT = """Current session: 20% used · resets Oct 22, 3:49am (Europe/Paris)
Current week (all models): 96% used · resets Oct 26, 5:59pm (Europe/Paris)
"""
AGY_ROOM = """Gemini Models\tWeekly Limit Remaining\t40%\t2026-10-23T22:43:51Z
Claude and GPT models\tWeekly Limit Remaining\t100%\t2026-10-28T21:47:44Z
Gemini 4 Argon\tGemini 4 Argon Weekly Limit\t93%\t2026-10-27T22:01:20Z
"""
AGY_ARGON_OUT = """Gemini Models\tWeekly Limit Remaining\t40%\t2026-10-23T22:43:51Z
Claude and GPT models\tWeekly Limit Remaining\t100%\t2026-10-28T21:47:44Z
Gemini 4 Argon\tGemini 4 Argon Weekly Limit\t2%\t2026-10-27T22:01:20Z
"""


def reading(reset="Oct 26, 5:59pm (Europe/Paris)", **bars):
    return {"state": "ok", "detail": "", "bars": {k: {"pct": v, "reset": reset} for k, v in bars.items()}}


def agy_reading(**groups):
    return {"state": "ok", "detail": "", "bars": {},
            "groups": {g.replace("_", " "): {"weekly": {"pct": v, "reset": "2026-10-27T22:01:20Z"}} for g, v in groups.items()}}


class FallbackTests(unittest.TestCase):
    def setUp(self):
        with open(ROOT / "aicli.toml", "rb") as f:
            self.config = tomllib.load(f)
        # Each test its own quota cache.
        self._dir = tempfile.TemporaryDirectory()
        self._old_cache = os.environ["AICLI_QUOTA_CACHE"]
        os.environ["AICLI_QUOTA_CACHE"] = str(Path(self._dir.name) / "quota.json")

    def tearDown(self):
        os.environ["AICLI_QUOTA_CACHE"] = self._old_cache
        self._dir.cleanup()

    def pick(self, claude, agy, workload_class="standard-dev", tier="default", routes=None):
        return aicli.pick_fallback(
            self.config, "claude", tier, {"claude": claude, "agy": agy}.get,
            workload_class=workload_class, routes=routes,
        )

    def test_claude_with_room_stays_on_claude(self):
        self.assertIsNone(self.pick(reading(weekly=60, five_hour=10), agy_reading(**{"Gemini 4 Argon": 7})))

    def test_standard_dev_falls_back_to_agy_gemini_4_argon_medium(self):
        fb = self.pick(reading(weekly=96, five_hour=10), agy_reading(**{"Gemini 4 Argon": 7}))
        self.assertEqual((fb["tool"], fb["model"], fb["group"]), ("agy", "gemini-4-argon-medium", "Gemini 4 Argon"))
        self.assertEqual(fb["from_model"], "claude-sonnet-5")
        self.assertIn("96%", fb["reason"])
        self.assertTrue(fb.get("next_ok_at"))

    def test_deep_spec_opus_has_no_fallback(self):
        fb = self.pick(
            reading(weekly=100, five_hour=100),
            agy_reading(Gemini_Models=10, **{"Claude and GPT models": 0, "Gemini 4 Argon": 7}),
            workload_class="deep-spec",
            tier="deep",
        )
        self.assertIsNone(fb)

    def test_the_five_hour_bar_counts_too(self):
        fb = self.pick(reading(weekly=10, five_hour=100), agy_reading(**{"Gemini 4 Argon": 10}))
        self.assertEqual(fb["model"], "gemini-4-argon-medium")

    def test_every_step_out_stays_on_claude(self):
        self.assertIsNone(self.pick(reading(weekly=100), agy_reading(**{"Gemini 4 Argon": 99})))

    def test_an_unknown_claude_reading_never_falls_back(self):
        self.assertIsNone(self.pick({"state": "unknown", "bars": {}}, agy_reading(**{"Gemini 4 Argon": 0})))

    def test_no_chain_no_fallback(self):
        self.assertIsNone(aicli.pick_fallback({"claude": {}}, "claude", "deep", lambda t: reading(weekly=100)))

    def test_status_marks_per_class_state(self):
        status = aicli.fallback_status(self.config, {
            "claude": reading(weekly=96),
            "agy": agy_reading(Gemini_Models=60, **{"Gemini 4 Argon": 7}),
        })
        classes = status["classes"]
        self.assertEqual(classes["standard-dev"]["state"], "fallback")
        self.assertEqual(classes["standard-dev"]["effective_tool"], "agy")
        self.assertEqual(classes["standard-dev"]["effective_model"], "gemini-4-argon-medium")
        self.assertEqual(classes["deep-spec"]["state"], "blocked")
        self.assertIsNone(classes["deep-spec"]["fallback"])
        self.assertEqual(classes["maintenance"]["state"], "ok")

    def test_agy_usage_keeps_every_group_including_argon(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, _ = make_config(Path(d))
            result = run_aicli("usage", "--json", config_path=config_path)
        groups = json.loads(result.stdout)["agy"]["groups"]
        self.assertEqual(groups["Claude and GPT models"]["weekly"]["pct"], 50)
        self.assertEqual(groups["Gemini Models"]["weekly"]["pct"], 6)
        self.assertEqual(groups["Gemini 4 Argon"]["weekly"]["pct"], 7)

    def test_timer_relies_on_next_ok_at_and_retests_once_reset_arrives(self):
        from datetime import datetime, timezone
        t_exhausted = datetime(2026, 10, 22, 0, 10, tzinfo=timezone.utc)
        # Exhausted until Oct 22, 3:50am Europe/Paris (01:50 UTC)
        ok = reading(reset="Oct 22, 3:50am (Europe/Paris)", five_hour=98)
        saved = aicli._cached_quota("claude", ok, at=95, now=t_exhausted)
        self.assertEqual(saved["next_ok_at"], "2026-10-22T01:50:00+00:00")

        # 1. Before next_ok_at (00:20 UTC): cache is valid and relied upon
        t_before = datetime(2026, 10, 22, 0, 20, tzinfo=timezone.utc)
        got_before = aicli._cached_quota(
            "claude", {"state": "unknown", "detail": "timed out", "bars": {}}, at=95, now=t_before
        )
        self.assertEqual(got_before["bars"]["five_hour"]["pct"], 98)
        self.assertIn("cached_at", got_before)

        # 2. At/after next_ok_at (01:51 UTC): timer elapsed! Pre-reset exhausted reading is expired!
        t_after = datetime(2026, 10, 22, 1, 51, tzinfo=timezone.utc)
        got_after = aicli._cached_quota(
            "claude", {"state": "unknown", "detail": "timed out", "bars": {}}, at=95, now=t_after
        )
        self.assertEqual(got_after["state"], "unknown")

    def test_check_quota_verifies_fixtures_and_warns_on_wording_change(self):
        report = aicli.check_quota_health(self.config, live=False)
        self.assertTrue(report["ok"], report["errors"])
        self.assertGreaterEqual(len(report["checks"]), 3)

        # A changed wording in agy /usage with % is caught as a warning
        bad_agy = aicli.parse_agy_usage(
            "Gemini Models\tWeekly Limit Remaining\t40%\t2026-10-23T22:43:51Z\n"
            "New Model Group\tStrange Unrecognized Window\t80%\t2026-10-23T22:43:51Z\n"
        )
        self.assertTrue(bad_agy.get("warnings"))

        # An unparseable reset string in claude /usage is caught as a warning
        bad_claude = aicli.parse_claude_usage(
            "Current session: 58% used · resets whenever it feels like it\n"
        )
        self.assertTrue(bad_claude.get("warnings"))


class FallbackCliTests(unittest.TestCase):
    """`aicli run/resolve` against the fake CLIs, with the Claude plan at 96%."""

    def run_cli(self, *args, agy=AGY_ROOM):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            config_path, _ = make_config(d)
            (d / "claude.txt").write_text(CLAUDE_OUT)
            (d / "agy.txt").write_text(agy)
            log = d / "usage.jsonl"
            res = subprocess.run(
                [sys.executable, str(AICLI), *args], capture_output=True, text=True,
                env={**os.environ, "AICLI_CONFIG": str(config_path), "AICLI_USAGE_LOG": str(log),
                     "AICLI_QUOTA_CACHE": str(d / "quota.json"), "FAKE_CLAUDE_USAGE": str(d / "claude.txt"), "FAKE_AGY_USAGE": str(d / "agy.txt")},
            )
            rows = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout, rows

    def test_run_standard_dev_moves_to_agy_gemini_4_argon_medium(self):
        out, rows = self.run_cli("run", "--class", "standard-dev", "--client", "x:y", "--", "-p", "hi")
        self.assertEqual(json.loads(out), [str(FAKE_CLI / "fake-agy"), "--model", "gemini-4-argon-medium", "-p", "hi"])
        self.assertEqual(rows[-1]["tool"], "agy")
        self.assertEqual(rows[-1]["model"], "gemini-4-argon-medium")
        self.assertEqual(rows[-1]["fallback"]["from_tool"], "claude")
        self.assertEqual(rows[-1]["fallback"]["group"], "Gemini 4 Argon")

    def test_run_deep_spec_has_no_fallback_and_stays_on_claude(self):
        out, rows = self.run_cli("run", "--class", "deep-spec", "--client", "x:y", "--", "-p", "hi")
        self.assertEqual(json.loads(out), [str(FAKE_CLI / "fake-claude"), "--model", "claude-opus-5-5", "-p", "hi"])
        self.assertNotIn("fallback", rows[-1])

    def test_run_drops_the_callers_claude_model_on_fallback(self):
        out, _ = self.run_cli("run", "--class", "standard-dev", "--", "--model", "claude-sonnet-5", "-p", "hi")
        self.assertEqual(json.loads(out), [str(FAKE_CLI / "fake-agy"), "--model", "gemini-4-argon-medium", "-p", "hi"])

    def test_run_with_a_pinned_tool_stays_on_it(self):
        out, rows = self.run_cli("run", "--tool", "claude", "--class", "standard-dev", "--", "-p", "hi")
        self.assertEqual(json.loads(out)[0], str(FAKE_CLI / "fake-claude"))
        self.assertNotIn("fallback", rows[-1])

    def test_no_fallback_flag(self):
        out, _ = self.run_cli("run", "--no-fallback", "--class", "standard-dev", "--", "-p", "hi")
        self.assertEqual(json.loads(out)[0], str(FAKE_CLI / "fake-claude"))

    def test_resolve_names_the_fallback_for_a_pinned_tool(self):
        out, _ = self.run_cli("resolve", "--tool", "claude", "--class", "standard-dev", "--json")
        data = json.loads(out)
        self.assertEqual((data["tool"], data["model"]), ("agy", "gemini-4-argon-medium"))
        self.assertEqual(data["fallback"]["group"], "Gemini 4 Argon")

    def test_resolve_stays_on_claude_when_argon_is_exhausted(self):
        out, _ = self.run_cli("resolve", "--class", "standard-dev", "--json", agy=AGY_ARGON_OUT)
        data = json.loads(out)
        self.assertEqual(data["tool"], "claude")
        self.assertIsNone(data["fallback"])

    def test_route_set_class_can_configure_and_clear_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            config_path, _ = make_config(d)
            routes_path = d / "routes.json"
            env = {**os.environ, "AICLI_CONFIG": str(config_path), "AICLI_ROUTES": str(routes_path)}
            # Set fallback on deep-spec
            r1 = subprocess.run(
                [sys.executable, str(AICLI), "route", "set-class", "deep-spec",
                 "--tool", "claude", "--tier", "deep",
                 "--fallback-tool", "agy", "--fallback-model", "gemini-4-argon-high"],
                capture_output=True, text=True, env=env,
            )
            self.assertEqual(r1.returncode, 0, r1.stderr)
            saved = json.loads(routes_path.read_text())
            self.assertEqual(
                saved["classes"]["deep-spec"]["fallback"],
                {"tool": "agy", "model": "gemini-4-argon-high", "group": "Gemini 4 Argon"},
            )
            # Clear fallback on standard-dev
            r2 = subprocess.run(
                [sys.executable, str(AICLI), "route", "set-class", "standard-dev",
                 "--tool", "claude", "--tier", "default", "--no-fallback"],
                capture_output=True, text=True, env=env,
            )
            self.assertEqual(r2.returncode, 0, r2.stderr)
            saved2 = json.loads(routes_path.read_text())
            self.assertIsNone(saved2["classes"]["standard-dev"]["fallback"])

    def test_fallback_command_json(self):
        out, _ = self.run_cli("fallback", "--json")
        data = json.loads(out)
        self.assertEqual(data["classes"]["standard-dev"]["state"], "fallback")
        self.assertEqual(data["classes"]["standard-dev"]["effective_model"], "gemini-4-argon-medium")
        self.assertEqual(data["classes"]["deep-spec"]["state"], "blocked")


class AuthCliTests(unittest.TestCase):
    def test_auth_status_json(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, _ = make_config(Path(d))
            env = {**os.environ, "AICLI_CONFIG": str(config_path)}
            r = subprocess.run(
                [sys.executable, str(AICLI), "auth", "status", "--json"],
                capture_output=True, text=True, env=env,
            )
            data = json.loads(r.stdout)
            self.assertIn("claude", data)
            self.assertIn("agy", data)
            self.assertTrue(data["claude"]["authenticated"])

    def test_programmatic_auth_flow(self):
        cfg = {"claude": {"bin": str(FAKE_CLI / "fake-claude")}}
        res_start = aicli.start_login(cfg, "claude")
        self.assertTrue(res_start.get("ok"))
        self.assertIn("https://claude.com/cai/oauth/authorize", res_start.get("auth_url", ""))

        res_sub = aicli.submit_code(cfg, "claude", "valid-code")
        self.assertTrue(res_sub.get("ok"))
        self.assertIn("authenticated", res_sub.get("message", ""))
