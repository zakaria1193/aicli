"""Tests for aicli.py against the fake CLI in tests/fake_cli/. No test runs the
real `claude` or `agy`."""
import json
import os
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
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
    # ...and off the real skills cache and agy's own skills folder.
    os.environ["AICLI_SKILLS_CACHE"] = str(Path(_STATE.name) / "skills")
    os.environ["AICLI_AGY_SKILLS_DIR"] = str(Path(_STATE.name) / "agy-skills")


def tearDownModule():
    os.environ.pop("AICLI_USAGE_LOG", None)
    os.environ.pop("AICLI_ROUTES", None)
    os.environ.pop("AICLI_QUOTA_CACHE", None)
    os.environ.pop("AICLI_SKILLS_CACHE", None)
    os.environ.pop("AICLI_AGY_SKILLS_DIR", None)
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
    cfg["classes"] = {
        **shipped["classes"],
        # one plain class per tool, for tests of the command itself
        "t-claude": {"tool": "claude", "tier": "default"},
        "t-agy": {"tool": "agy", "tier": "default"},
    }
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
    if "optiplex_projects" in cfg:
        lines.append(f"optiplex_projects = {_toml_val(cfg['optiplex_projects'])}")
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
        if "reserve" in cfg["fallback"]:
            lines.append(f"reserve = {cfg['fallback']['reserve']}")
        for tool, steps in cfg["fallback"].items():
            if tool in ("at", "reserve"):
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
            result = run_aicli("argv", "--class", "t-claude", "--json", config_path=config_path)
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
                "argv", "--class", "t-agy", "--json",
                "--", "--effort", "high", "--model=custom",
                config_path=config_path,
            )
        argv = json.loads(result.stdout)
        self.assertEqual(argv, [str(FAKE_CLI / "fake-agy"), "--effort", "high", "--model=custom"])

    def test_argv_pass_through_args_after_dashdash(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, _ = make_config(Path(d))
            result = run_aicli(
                "argv", "--class", "t-claude", "--json", "--", "-p", "/usage",
                config_path=config_path,
            )
        argv = json.loads(result.stdout)
        self.assertEqual(argv[-2:], ["-p", "/usage"])

    def test_argv_caller_supplied_model_flag_wins(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, _ = make_config(Path(d))
            result = run_aicli(
                "argv", "--class", "t-claude", "--json", "--", "--model", "custom-model",
                config_path=config_path,
            )
        argv = json.loads(result.stdout)
        self.assertEqual(argv, [str(FAKE_CLI / "fake-claude"), "--model", "custom-model"])
        self.assertEqual(argv.count("--model"), 1)

    def test_argv_strips_redundant_leading_tool_name(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, _ = make_config(Path(d))
            result = run_aicli(
                "argv", "--class", "t-claude", "--json", "--", "claude", "--session-id", "s1",
                config_path=config_path,
            )
        argv = json.loads(result.stdout)
        self.assertEqual(argv, [str(FAKE_CLI / "fake-claude"), "--model", "claude-sonnet-5", "--session-id", "s1"])

    def test_argv_non_default_shell_quoted_line(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, _ = make_config(Path(d))
            result = run_aicli("argv", "--class", "t-claude", config_path=config_path)
        self.assertEqual(
            result.stdout.strip(),
            f"{FAKE_CLI / 'fake-claude'} --model claude-sonnet-5",
        )

    def test_callers_cannot_pass_tier_effort_or_model(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, _ = make_config(Path(d))
            for flag, value in (("--tier", "low"), ("--effort", "high"), ("--model", "x")):
                for cmd in ("argv", "run", "resolve"):
                    result = run_aicli(cmd, "--class", "t-claude", flag, value, config_path=config_path)
                    self.assertEqual(result.returncode, 2, (cmd, flag))

    def test_argv_binary_not_found_exits_1(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, _ = make_config(Path(d), claude={"bin": str(Path(d) / "missing")})
            result = subprocess.run(
                [sys.executable, str(AICLI), "argv", "--class", "t-claude"],
                capture_output=True, text=True,
                env={**os.environ, "AICLI_CONFIG": str(config_path), "PATH": "/nonexistent"},
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn("claude CLI not found", result.stderr)


class JobTests(unittest.TestCase):
    """The tool-neutral job: a caller names a client and a class, aicli builds the flags."""

    def argv(self, cls, *args, files=None):
        with tempfile.TemporaryDirectory() as d:
            config_path, _ = make_config(Path(d))
            args = [a.replace("{d}", d) if isinstance(a, str) else a for a in args]
            for name, text in (files or {}).items():
                (Path(d) / name).write_text(text)
            result = run_aicli("argv", "--class", cls, "--json", *args, config_path=config_path)
        return result

    def test_there_is_no_tool_flag(self):
        for cmd in ("argv", "run", "resolve"):
            with tempfile.TemporaryDirectory() as d:
                config_path, _ = make_config(Path(d))
                result = run_aicli(cmd, "--tool", "claude", "--class", "t-agy", config_path=config_path)
            self.assertEqual(result.returncode, 2, cmd)

    def test_claude_interactive_job(self):
        result = self.argv("t-claude", "--session-id", "s1", "--name", "n", "--mcp-config", "m.json",
                           "--permission-mode", "dontAsk", "--system-prompt-file", "p.md",
                           "--settings", "s.json", "--prompt", "do it")
        self.assertEqual(json.loads(result.stdout), [
            str(FAKE_CLI / "fake-claude"), "--model", "claude-sonnet-5",
            "--session-id", "s1", "--name", "n", "--strict-mcp-config", "--mcp-config", "m.json",
            "--permission-mode", "dontAsk", "--append-system-prompt-file", "p.md",
            "--settings", "s.json", "do it"])

    def test_claude_streaming_job_resumes(self):
        result = self.argv("t-claude", "--resume", "s1", "--output-format", "stream-json", "--prompt=-hi")
        self.assertEqual(json.loads(result.stdout), [
            str(FAKE_CLI / "fake-claude"), "--model", "claude-sonnet-5", "-p", "--output-format", "stream-json",
            "--verbose", "--include-partial-messages", "--resume", "s1", " -hi"])

    def test_agy_job_is_one_shot_with_the_system_prompt_in_front(self):
        result = self.argv("t-agy", "--session-id", "s1", "--settings", "s.json", "--mcp-config", "m.json",
                           "--permission-mode", "auto", "--system-prompt-file", "{d}/p.md",
                           "--output-format", "json", "--prompt", "do it", files={"p.md": "rules\n"})
        self.assertEqual(json.loads(result.stdout), [
            str(FAKE_CLI / "fake-agy"), "--model", "gemini-3.8-flash-high",
            "-p", "rules\n\ndo it", "--output-format", "json"])

    def test_interactive_agy_stays_open_and_a_conversation_is_continued(self):
        result = self.argv("t-agy", "--interactive", "--prompt", "hi")
        self.assertEqual(json.loads(result.stdout)[-2:], ["-i", "hi"])
        result = self.argv("t-agy", "--output-format", "stream-json", "--conversation", "c1",
                           "--system-prompt-file", "{d}/p.md", "--prompt", "again", files={"p.md": "rules"})
        argv = json.loads(result.stdout)
        self.assertEqual(argv[argv.index("-p") + 1], "again")            # no persona on a continued conversation
        self.assertEqual(argv[argv.index("--conversation") + 1], "c1")

    def test_claude_ignores_a_conversation_and_interactive_means_no_dash_p(self):
        argv = json.loads(self.argv("t-claude", "--interactive", "--conversation", "c1", "--prompt", "hi").stdout)
        self.assertNotIn("-p", argv)
        self.assertNotIn("--conversation", argv)

    def test_oneshot_and_interactive_conflict(self):
        self.assertEqual(self.argv("t-claude", "--oneshot", "--interactive").returncode, 2)

    def test_bypass_permissions_is_each_tools_own_flag(self):
        claude = json.loads(self.argv("t-claude", "--permission-mode", "bypass", "--prompt", "x").stdout)
        self.assertEqual(claude[claude.index("--permission-mode") + 1], "bypassPermissions")
        agy = json.loads(self.argv("t-agy", "--permission-mode", "bypass", "--prompt", "x").stdout)
        self.assertIn("--dangerously-skip-permissions", agy)
        self.assertNotIn("--dangerously-skip-permissions", json.loads(self.argv("t-agy", "--prompt", "x").stdout))

    def test_prompt_file(self):
        result = self.argv("t-agy", "--prompt-file", "{d}/q.md", files={"q.md": "from file"})
        self.assertEqual(json.loads(result.stdout)[-2:], ["-p", "from file"])

    def test_prompt_and_prompt_file_conflict(self):
        result = self.argv("t-agy", "--prompt", "a", "--prompt-file", "{d}/q.md", files={"q.md": "b"})
        self.assertEqual(result.returncode, 2)

    def test_session_id_and_resume_conflict(self):
        result = self.argv("t-claude", "--session-id", "a", "--resume", "b", "--prompt", "x")
        self.assertEqual(result.returncode, 2)

    def test_remote_control_is_each_tools_own_flag(self):
        claude = json.loads(self.argv("t-claude", "--remote-control", "--name", "n", "--prompt", "x").stdout)
        self.assertIn("--remote-control", claude)
        self.assertEqual(claude[claude.index("--name") + 1], "n")
        agy = json.loads(self.argv("t-agy", "--remote-control", "--prompt", "x").stdout)
        self.assertIn("--remote-control", agy)

    def test_without_remote_control_nothing_changes(self):
        claude = json.loads(self.argv("t-claude", "--prompt", "x").stdout)
        self.assertNotIn("--remote-control", claude)
        agy = json.loads(self.argv("t-agy", "--prompt", "x").stdout)
        self.assertNotIn("--remote-control", agy)

    def test_chrome_is_claude_only_and_ignored_on_agy(self):
        claude = json.loads(self.argv("t-claude", "--chrome", "--prompt", "x").stdout)
        self.assertIn("--chrome", claude)
        agy = json.loads(self.argv("t-agy", "--chrome", "--prompt", "x").stdout)
        self.assertNotIn("--chrome", agy)

    def test_without_chrome_nothing_changes(self):
        claude = json.loads(self.argv("t-claude", "--prompt", "x").stdout)
        self.assertNotIn("--chrome", claude)
        agy = json.loads(self.argv("t-agy", "--prompt", "x").stdout)
        self.assertNotIn("--chrome", agy)

    def test_the_router_not_the_caller_picks_the_cli_for_the_same_job(self):
        job = ("--prompt", "hi", "--output-format", "json")
        claude = json.loads(self.argv("t-claude", *job).stdout)
        agy = json.loads(self.argv("t-agy", *job).stdout)
        self.assertEqual(claude[0], str(FAKE_CLI / "fake-claude"))
        self.assertEqual(agy[0], str(FAKE_CLI / "fake-agy"))
        self.assertIn("-p", agy)


class RunTests(unittest.TestCase):
    def test_run_execs_the_fake_cli_with_built_argv(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, _ = make_config(Path(d))
            result = run_aicli(
                "run", "--class", "t-agy", "--", "-p", "hello",
                config_path=config_path,
            )
        self.assertEqual(result.returncode, 0)
        argv = json.loads(result.stdout)
        self.assertEqual(
            argv,
            [str(FAKE_CLI / "fake-agy"), "--model", "gemini-3.8-flash-high", "-p", "hello"],
        )


class TrustTests(unittest.TestCase):
    """No backend may ever sit on a "trust this folder" prompt."""

    def _run(self, d, tool_class, claude_json, agy_json, cwd):
        config_path, _ = make_config(Path(d))
        env = {**os.environ, "AICLI_CONFIG": str(config_path),
               "AICLI_CLAUDE_JSON": str(claude_json), "AICLI_AGY_SETTINGS": str(agy_json)}
        return subprocess.run([sys.executable, str(AICLI), "run", "--class", tool_class, "--", "-p", "x"],
                              capture_output=True, text=True, env=env, cwd=cwd)

    def test_agy_run_trusts_cwd_and_git_root(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d).resolve()
            repo = d / "repo"
            (repo / "sub").mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            agy = d / "settings.json"
            agy.write_text('{"keep": 1}')
            r = self._run(d, "t-agy", d / "claude.json", agy, repo / "sub")
            self.assertEqual(r.returncode, 0, r.stderr)
            data = json.loads(agy.read_text())
            self.assertEqual(data["keep"], 1)
            self.assertEqual(sorted(data["trustedWorkspaces"]), sorted([str(repo), str(repo / "sub")]))
            self._run(d, "t-agy", d / "claude.json", agy, repo / "sub")   # idempotent
            self.assertEqual(len(json.loads(agy.read_text())["trustedWorkspaces"]), 2)

    def test_claude_run_trusts_cwd(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d).resolve()
            cj = d / "claude.json"
            cj.write_text('{"projects": {"/x": {"a": 1}}}')
            work = d / "work"
            work.mkdir()
            cfg, _ = make_config(d)
            routes = aicli.load_config(cfg)
            tool = "claude"
            env = {**os.environ, "AICLI_CONFIG": str(cfg), "AICLI_CLAUDE_JSON": str(cj)}
            code = ("import sys; sys.path.insert(0, %r); import aicli; aicli.trust_cwd('claude')" % str(AICLI.parent))
            subprocess.run([sys.executable, "-c", code], env=env, cwd=work, check=True)
            data = json.loads(cj.read_text())
            self.assertTrue(data["projects"][str(work)]["hasTrustDialogAccepted"])
            self.assertEqual(data["projects"]["/x"], {"a": 1})

    def test_missing_config_never_fails(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["AICLI_CLAUDE_JSON"] = str(Path(d) / "nope.json")
            try:
                self.assertFalse(aicli.trust_cwd("claude", d))
                self.assertFalse(aicli.trust_cwd("unknown", d))
            finally:
                del os.environ["AICLI_CLAUDE_JSON"]


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
            self.assertEqual((tool, tier), ("agy", "default"))
            self.assertEqual(aicli.route_model(aicli.load_routes(config), None, "standard-dev"), "gemini-4-hydrogen-medium")

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

    def test_routes_command_json_carries_optiplex_projects(self):
        """pjm's cockpit badges OptiPlex projects off this (homelab-0019): no caller reads
        routes.json directly any more, so the merged list must be reachable from the CLI."""
        with tempfile.TemporaryDirectory() as d:
            config_path, config = make_config(Path(d))
            config["optiplex_projects"] = ["pjm", "homelab"]
            config_path.write_text(_to_toml(config))
            result = run_aicli("routes", "--json", config_path=config_path)
            self.assertEqual(result.returncode, 0)
            data = json.loads(result.stdout)
            self.assertEqual(data["optiplex_projects"], ["pjm", "homelab"])

    def test_route_set_and_remove(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, _ = make_config(Path(d))
            routes_path = Path(d) / "routes.json"
            env = {**os.environ, "AICLI_CONFIG": str(config_path), "AICLI_ROUTES": str(routes_path),
                   "AICLI_SKILLS": str(Path(d) / "no-skills.toml")}  # routing only: no class skills

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

    def test_route_reset_class_and_client(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, _ = make_config(Path(d))
            routes_path = Path(d) / "routes.json"
            env = {**os.environ, "AICLI_CONFIG": str(config_path), "AICLI_ROUTES": str(routes_path)}

            subprocess.run([sys.executable, str(AICLI), "route", "set-class", "maintenance",
                            "--tool", "claude", "--tier", "deep"], capture_output=True, text=True, env=env, check=True)
            self.assertIn("maintenance", json.loads(routes_path.read_text())["classes"])

            res = subprocess.run([sys.executable, str(AICLI), "route", "reset-class", "maintenance"],
                                 capture_output=True, text=True, env=env)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertNotIn("maintenance", json.loads(routes_path.read_text()).get("classes", {}))

            # Reset of a row with no override reports failure, not a crash.
            res = subprocess.run([sys.executable, str(AICLI), "route", "reset-client", "pjm:nothing"],
                                 capture_output=True, text=True, env=env)
            self.assertEqual(res.returncode, 1)

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
            self.assertEqual(data["tool"], "agy")
            self.assertEqual(data["model"], "gemini-4-hydrogen-medium")
            self.assertIn("quota", data)

    def test_run_meta_out(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, _ = make_config(Path(d))
            meta_file = Path(d) / "run.meta.json"
            # FAKE_CLI fake-agy will be execv'd
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
            self.assertEqual(data["tool"], "agy")
            self.assertEqual(data["model"], "gemini-4-hydrogen-medium")
            self.assertIn("quota", data)

    def test_run_meta_out_reports_the_callers_own_model(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, _ = make_config(Path(d))
            meta_file = Path(d) / "run.meta.json"
            res = subprocess.run(
                [sys.executable, str(AICLI), "run", "--class", "t-claude",
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
            [sys.executable, str(AICLI), "run", "--client", "t:x", "--class", f"t-{tool}", "--", *args],
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
                [sys.executable, str(AICLI), "run", "--class", "t-claude", "--", "-p", "x"],
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
                [sys.executable, str(AICLI), "run", "--class", "t-claude", "--meta-out", str(meta_file)],
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
            self.assertEqual((tool, tier, cls), ("agy", "default", "standard-dev"))
            self.assertEqual(aicli.route_model(routes, "pjm:p-0001:auto-execute", "standard-dev"), "gemini-4-hydrogen-medium")

            # Test hierarchical match for maintainer run
            tool, tier, effort, cls = aicli.resolve_route(config, client="pjm:maintainer:homelab:routine", routes=routes)
            self.assertEqual((tool, tier, cls), ("agy", "low", "maintenance"))

    def test_configured_routine_maintainer_runs_on_agy_flash_medium(self):
        config = aicli.load_config(ROOT / "aicli.toml")
        classes, _, _ = aicli._default_sections(config)
        clients = {
            "pjm:maintainer:routine": {
                "class": "maintenance",
                "tool": "agy",
                "model": "gemini-3.8-flash-medium",
            }
        }
        routes = {"classes": classes, "clients": clients}
        client = "pjm:maintainer:salsa-social:routine"
        self.assertEqual(aicli.resolve_route(config, client=client, workload_class="maintenance",
                                             routes=routes)[0], "agy")
        self.assertEqual(aicli.route_model(routes, client, "maintenance"), "gemini-3.8-flash-medium")
        # a deep run is not pinned by it
        self.assertIsNone(aicli.route_model(routes, "pjm:maintainer:salsa-social:deep", "maintenance"))

    def test_configured_scrum_master_runs_on_agy_low(self):
        config = aicli.load_config(ROOT / "aicli.toml")
        classes, _, _ = aicli._default_sections(config)
        clients = {
            "pjm:scrum-master": {
                "class": "managerial-audit",
                "tool": "agy",
                "tier": "low",
            }
        }
        got = aicli.resolve_route(config, client="pjm:scrum-master", workload_class="managerial-audit",
                                  routes={"classes": classes, "clients": clients})
        self.assertEqual(got[:2], ("agy", "low"))

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
            self.assertEqual(aicli.route_model(routes, "pjm:p-0001:auto-execute", "standard-dev"), "gemini-4-hydrogen-medium")
            # A client override that changes tool or tier without pinning a model does not apply the class's pinned model.
            routes["clients"]["pjm:custom-tool"] = {"class": "standard-dev", "tool": "claude"}
            routes["clients"]["pjm:custom-tier"] = {"class": "standard-dev", "tier": "low"}
            self.assertIsNone(aicli.route_model(routes, "pjm:custom-tool", "standard-dev"))
            self.assertIsNone(aicli.route_model(routes, "pjm:custom-tier", "standard-dev"))

    def test_temporary_model_override_and_expiration(self):
        now = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
        exp_5h = aicli.compute_model_override_expires_at(now=now)
        self.assertEqual(exp_5h, "2026-11-22T17:00:00+00:00")

        # Active override
        entry_active = {"model": "claude-opus-5-5", "model_expires_at": exp_5h}
        self.assertEqual(aicli.active_model_override(entry_active, now=now), "claude-opus-5-5")

        # 4 hours later (still active)
        now_4h = now + timedelta(hours=4)
        self.assertEqual(aicli.active_model_override(entry_active, now=now_4h), "claude-opus-5-5")

        # 5 hours later (expired)
        now_5h = now + timedelta(hours=5)
        self.assertIsNone(aicli.active_model_override(entry_active, now=now_5h))

        # route_model honors active override and ignores expired
        routes = {
            "classes": {"deep-spec": {"tool": "claude", "tier": "deep"}},
            "clients": {"pjm:test": entry_active},
        }
        self.assertEqual(aicli.route_model(routes, "pjm:test", "deep-spec", now=now), "claude-opus-5-5")
        self.assertIsNone(aicli.route_model(routes, "pjm:test", "deep-spec", now=now_5h))

    def test_set_client_model_sets_5h_temporary_override(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, config = make_config(Path(d))
            routes_file = aicli.default_routes_path(config)
            parser = aicli.build_parser()
            args = parser.parse_args(["route", "set-client", "my:caller", "--class", "standard-dev", "--temporary-override", "claude-opus-5-5"])
            ret = aicli.cmd_route(args, config)
            self.assertEqual(ret, 0)
            loaded = aicli.load_routes(config, routes_path=routes_file)
            client = loaded["clients"]["my:caller"]
            self.assertEqual(client["model"], "claude-opus-5-5")
            self.assertEqual(client["temporary_override"], "claude-opus-5-5")
            self.assertIn("model_expires_at", client)
            self.assertIn("temporary_override_expires_at", client)

            # 5h later, load_routes drops the temporary override
            future = datetime.now(timezone.utc) + timedelta(hours=5, minutes=1)
            future_loaded = aicli.load_routes(config, routes_path=routes_file, now=future)
            self.assertNotIn("model", future_loaded["clients"].get("my:caller", {}))
            self.assertNotIn("temporary_override", future_loaded["clients"].get("my:caller", {}))

    def test_cli_temporary_override_subcommand(self):
        with tempfile.TemporaryDirectory() as d:
            config_path, config = make_config(Path(d))
            routes_file = aicli.default_routes_path(config)
            parser = aicli.build_parser()
            args = parser.parse_args(["route", "temporary-override", "my:caller", "gemini-4-hydrogen-high"])
            ret = aicli.cmd_route(args, config)
            self.assertEqual(ret, 0)
            loaded = aicli.load_routes(config, routes_path=routes_file)
            client = loaded["clients"]["my:caller"]
            self.assertEqual(client["temporary_override"], "gemini-4-hydrogen-high")
            self.assertEqual(client["model"], "gemini-4-hydrogen-high")

    def test_shipped_config_gemini_4_tiers_and_persona_classes(self):
        with open(ROOT / "aicli.toml", "rb") as f:
            shipped = tomllib.load(f)
        agy_tiers = shipped.get("agy", {}).get("tiers", {})
        self.assertEqual(agy_tiers.get("low"), "gemini-3.8-flash-low")
        self.assertEqual(agy_tiers.get("default"), "gemini-4-hydrogen-medium")
        self.assertEqual(agy_tiers.get("deep"), "gemini-4-hydrogen-high")

        # Classes must represent roles/personas, not models
        classes = shipped.get("classes", {})
        self.assertNotIn("hydrogen-low", classes)
        self.assertNotIn("hydrogen-high", classes)
        self.assertNotIn("agy-high", classes)
        self.assertIn("career-assistant", classes)
        self.assertIn("career-coach", classes)
        self.assertEqual(classes["career-assistant"]["model"], "gemini-4-hydrogen-low")
        self.assertEqual(classes["career-coach"]["tier"], "deep")

    def test_available_models_includes_hydrogen_variants(self):
        for m in ("gemini-4-hydrogen-low", "gemini-4-hydrogen-medium", "gemini-4-hydrogen-high"):
            self.assertIn(m, aicli.AVAILABLE_MODELS["agy"])
            self.assertEqual(aicli.infer_agy_group(m), "Gemini Models")

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

    def test_load_routes_merges_client_and_optiplex_defaults_from_toml(self):
        config = {
            "classes": {"maintenance": {"tool": "agy", "tier": "low"}},
            "clients": {"pjm:maint": {"class": "maintenance"}},
            "optiplex_projects": ["pjm", "homelab"],
        }
        with tempfile.TemporaryDirectory() as d:
            routes_file = Path(d) / "routes.json"  # never written: no override yet
            loaded = aicli.load_routes(config, routes_path=routes_file)
        self.assertEqual(loaded["clients"]["pjm:maint"], {"class": "maintenance"})
        self.assertEqual(loaded["optiplex_projects"], ["pjm", "homelab"])

    def test_apply_route_override_writes_only_the_diff(self):
        config = {"classes": {"maintenance": {"tool": "agy", "tier": "low", "description": "d"}}}
        with tempfile.TemporaryDirectory() as d:
            routes_file = Path(d) / "routes.json"
            aicli.apply_route_override("classes", "maintenance", {"tool": "claude", "tier": "low"},
                                        config=config, routes_path=routes_file)
            saved = json.loads(routes_file.read_text())
            # Only the field that actually changed is written - not `tier`,
            # not `description`, and never the merged row.
            self.assertEqual(saved["classes"]["maintenance"], {"tool": "claude"})

    def test_apply_route_override_drops_override_back_at_default(self):
        config = {"classes": {"maintenance": {"tool": "agy", "tier": "low"}}}
        with tempfile.TemporaryDirectory() as d:
            routes_file = Path(d) / "routes.json"
            aicli.apply_route_override("classes", "maintenance", {"tool": "claude"},
                                        config=config, routes_path=routes_file)
            self.assertIn("maintenance", json.loads(routes_file.read_text())["classes"])
            # Setting it back to the toml default removes the override entirely.
            aicli.apply_route_override("classes", "maintenance", {"tool": "agy"},
                                        config=config, routes_path=routes_file)
            self.assertNotIn("maintenance", json.loads(routes_file.read_text()).get("classes", {}))

    def test_apply_route_override_writes_a_new_row_in_full(self):
        config = {"classes": {"maintenance": {"tool": "agy", "tier": "low"}}}
        with tempfile.TemporaryDirectory() as d:
            routes_file = Path(d) / "routes.json"
            merged = aicli.apply_route_override("clients", "pjm:new", {"class": "maintenance", "tool": "claude"},
                                                 config=config, routes_path=routes_file)
            self.assertEqual(merged, {"class": "maintenance", "tool": "claude"})
            self.assertEqual(json.loads(routes_file.read_text())["clients"]["pjm:new"],
                              {"class": "maintenance", "tool": "claude"})

    def test_reset_route_override_drops_whole_row(self):
        config = {"classes": {"maintenance": {"tool": "agy", "tier": "low"}}}
        with tempfile.TemporaryDirectory() as d:
            routes_file = Path(d) / "routes.json"
            aicli.apply_route_override("classes", "maintenance", {"tool": "claude", "tier": "deep"},
                                        config=config, routes_path=routes_file)
            self.assertTrue(aicli.reset_route_override("classes", "maintenance", config=config, routes_path=routes_file))
            self.assertNotIn("maintenance", json.loads(routes_file.read_text()).get("classes", {}))
            loaded = aicli.load_routes(config, routes_path=routes_file)
            self.assertEqual(loaded["classes"]["maintenance"], {"tool": "agy", "tier": "low"})

    def test_reset_route_override_drops_one_field(self):
        config = {"classes": {"maintenance": {"tool": "agy", "tier": "low"}}}
        with tempfile.TemporaryDirectory() as d:
            routes_file = Path(d) / "routes.json"
            aicli.apply_route_override("classes", "maintenance", {"tool": "claude", "tier": "deep"},
                                        config=config, routes_path=routes_file)
            self.assertTrue(aicli.reset_route_override("classes", "maintenance", config=config,
                                                         routes_path=routes_file, field="tier"))
            self.assertEqual(json.loads(routes_file.read_text())["classes"]["maintenance"], {"tool": "claude"})

    def test_reset_route_override_no_override_reports_false(self):
        with tempfile.TemporaryDirectory() as d:
            routes_file = Path(d) / "routes.json"
            self.assertFalse(aicli.reset_route_override("classes", "maintenance", routes_path=routes_file))

    def test_stale_override_reported_for_a_vanished_default(self):
        config = {"classes": {"maintenance": {"tool": "agy", "tier": "low"}}}
        with tempfile.TemporaryDirectory() as d:
            routes_file = Path(d) / "routes.json"
            # An override for a class that no longer exists in aicli.toml: a
            # field-only override, not a whole added row.
            aicli.save_routes({"classes": {"retired-class": {"tier": "deep"}}}, routes_path=routes_file)
            overridden, stale = aicli._overridden_names(config, routes_path=routes_file)
            self.assertIn("retired-class", overridden["classes"])
            self.assertIn("retired-class", stale["classes"])
            # Still applied if it is a whole added row (has its own `tool`).
            aicli.save_routes({"clients": {"pjm:gone": {"tool": "agy"}}}, routes_path=routes_file)
            _, stale2 = aicli._overridden_names(config, routes_path=routes_file)
            self.assertNotIn("pjm:gone", stale2["clients"])


if __name__ == "__main__":
    unittest.main()



CLAUDE_OUT = """Current session: 20% used · resets Oct 22, 3:49am (Europe/Paris)
Current week (all models): 98% used · resets Oct 26, 5:59pm (Europe/Paris)
"""
CLAUDE_ROOM = """Current session: 20% used · resets Oct 22, 3:49am (Europe/Paris)
Current week (all models): 60% used · resets Oct 26, 5:59pm (Europe/Paris)
"""
AGY_UNDER_50 = """Gemini Models\tWeekly Limit Remaining\t70%\t2026-10-23T22:43:51Z
Gemini Models\tFive Hour Limit Remaining\t60%\t2026-10-22T02:33:30Z
Claude and GPT models\tWeekly Limit Remaining\t100%\t2026-10-28T21:47:44Z
Gemini 4 Argon\tGemini 4 Argon Weekly Limit\t93%\t2026-10-27T22:01:20Z
"""
AGY_ROOM = """Gemini Models\tWeekly Limit Remaining\t15%\t2026-10-23T22:43:51Z
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
    out_groups = {}
    for g, v in groups.items():
        name = g.replace("_", " ")
        if isinstance(v, dict):
            out_groups[name] = {
                period: (val if isinstance(val, dict) else {"pct": val, "reset": "2026-10-27T22:01:20Z"})
                for period, val in v.items()
            }
        else:
            out_groups[name] = {"weekly": {"pct": v, "reset": "2026-10-27T22:01:20Z"}}
    return {"state": "ok", "detail": "", "bars": {}, "groups": out_groups}


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

    def pick(self, claude, agy, workload_class="conversation", tier="default", routes=None, tool="claude", now=None):
        return aicli.pick_fallback(
            self.config, tool, tier, {"claude": claude, "agy": agy}.get,
            workload_class=workload_class, routes=routes, now=now,
        )

    def test_claude_with_room_stays_on_claude(self):
        self.assertIsNone(self.pick(reading(weekly=60, five_hour=10), agy_reading(**{"Gemini 4 Argon": 7})))

    def test_claude_under_the_pause_stays_on_claude(self):
        # pjm pauses Claude at 97%: at 95-96% Claude still counts as available, so the
        # fallback's small pool must not be spent first (salsa-social, 2026-11-08)
        self.assertEqual(aicli.fallback_at(self.config), 97)
        self.assertIsNone(self.pick(reading(weekly=96, five_hour=10), agy_reading(**{"Gemini 4 Argon": 7})))

    def test_conversation_falls_back_to_agy_gemini_4_argon_medium(self):
        fb = self.pick(reading(weekly=98, five_hour=10), agy_reading(**{"Gemini 4 Argon": 7}))
        self.assertEqual((fb["tool"], fb["model"], fb["group"]), ("agy", "gemini-4-argon-medium", "Gemini 4 Argon"))
        self.assertEqual(fb["from_model"], "claude-sonnet-5")
        self.assertIn("98%", fb["reason"])
        self.assertIn("fallback at 97%", fb["reason"])
        self.assertTrue(fb.get("next_ok_at"))

    def test_standard_dev_stays_on_agy_hydrogen_medium_under_reserve_threshold(self):
        fb = self.pick(
            reading(weekly=20, five_hour=10),
            agy_reading(Gemini_Models={"five_hour": 79, "weekly": 30}),
            workload_class="standard-dev",
            tool="agy",
        )
        self.assertIsNone(fb)

    def test_standard_dev_falls_back_when_reserve_threshold_reached(self):
        # 1. five_hour reaches 80% (100 - reserve: 20%), weekly < 80%, claude at 60% (< 97% global threshold)
        fb_5h = self.pick(
            reading(weekly=60, five_hour=20),
            agy_reading(Gemini_Models={
                "five_hour": {"pct": 80, "reset": "2026-10-22T02:33:30Z"},
                "weekly": {"pct": 30, "reset": "2026-10-27T22:01:20Z"},
            }),
            workload_class="standard-dev",
            tool="agy",
        )
        self.assertIsNotNone(fb_5h)
        self.assertEqual((fb_5h["tool"], fb_5h["model"], fb_5h["effort"]), ("claude", "claude-sonnet-5", "medium"))
        self.assertEqual((fb_5h["from_tool"], fb_5h["from_model"]), ("agy", "gemini-4-hydrogen-medium"))
        self.assertIn("reserve yield at 80% (reserve: 20%)", fb_5h["reason"])
        self.assertEqual(fb_5h["next_ok_at"], "2026-10-22T02:33:30+00:00")

        # 2. weekly reaches 85% while five_hour < 80%
        fb_wk = self.pick(
            reading(weekly=60, five_hour=20),
            agy_reading(Gemini_Models={"five_hour": 20, "weekly": 85}),
            workload_class="standard-dev",
            tool="agy",
        )
        self.assertIsNotNone(fb_wk)
        self.assertEqual((fb_wk["tool"], fb_wk["model"], fb_wk["effort"]), ("claude", "claude-sonnet-5", "medium"))
        self.assertIn("85% used (reserve yield at 80% (reserve: 20%))", fb_wk["reason"])

    def test_critical_priority_classes_use_full_quota_ceiling(self):
        # maintenance has priority = "critical", so at 85% used (above standard 80% cap), it stays on agy
        fb_crit = self.pick(
            reading(weekly=60, five_hour=20),
            agy_reading(Gemini_Models={"five_hour": 85, "weekly": 85}),
            workload_class="maintenance",
            tool="agy",
        )
        self.assertIsNone(fb_crit)

        # But when quota reaches fallback_at (97%), even critical classes fall back if a fallback exists
        # or error out if no fallback
        routes_with_fb = aicli.load_routes(self.config)
        routes_with_fb["classes"]["maintenance"]["fallback"] = {"tool": "claude", "model": "claude-haiku-5-5"}
        fb_97 = self.pick(
            reading(weekly=60, five_hour=20),
            agy_reading(Gemini_Models={"five_hour": 97, "weekly": 97}),
            workload_class="maintenance",
            tool="agy",
            routes=routes_with_fb,
        )
        self.assertIsNotNone(fb_97)
        self.assertEqual((fb_97["tool"], fb_97["model"]), ("claude", "claude-haiku-5-5"))
        self.assertIn("fallback at 97%", fb_97["reason"])

    def test_standard_dev_temporary_allowance_override_and_expiry(self):
        from datetime import datetime, timezone
        now = datetime(2026, 11, 22, 18, 0, tzinfo=timezone.utc)
        routes_active = aicli.load_routes(self.config)
        routes_active["classes"]["standard-dev"]["allowance_override"] = {
            "pct": 90,
            "expires_at": "2026-11-22T23:00:00+00:00",
        }
        # At 85% used (< 90% override), stays on agy
        self.assertIsNone(self.pick(
            reading(weekly=20),
            agy_reading(Gemini_Models={"five_hour": 85, "weekly": 40}),
            workload_class="standard-dev",
            tool="agy",
            routes=routes_active,
            now=now,
        ))
        # At 90% used (>= 90% override), falls back to claude-sonnet-5
        fb_90 = self.pick(
            reading(weekly=20),
            agy_reading(Gemini_Models={"five_hour": 90, "weekly": 40}),
            workload_class="standard-dev",
            tool="agy",
            routes=routes_active,
            now=now,
        )
        self.assertIsNotNone(fb_90)
        self.assertIn("allowance cap 90%", fb_90["reason"])

        # After expires_at (23:01 UTC), override is expired and primary_at reverts to reserve yield (80%)
        after = datetime(2026, 11, 22, 23, 1, tzinfo=timezone.utc)
        fb_expired = self.pick(
            reading(weekly=20),
            agy_reading(Gemini_Models={"five_hour": 85, "weekly": 40}),
            workload_class="standard-dev",
            tool="agy",
            routes=routes_active,
            now=after,
        )
        self.assertIsNotNone(fb_expired)
        self.assertIn("reserve yield at 80% (reserve: 20%)", fb_expired["reason"])

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

    def test_every_step_out_stays_on_primary(self):
        self.assertIsNone(self.pick(reading(weekly=100), agy_reading(**{"Gemini 4 Argon": 99})))
        self.assertIsNone(self.pick(
            reading(weekly=97),
            agy_reading(Gemini_Models=60),
            workload_class="standard-dev",
            tool="agy",
        ))

    def test_an_unknown_claude_reading_never_falls_back(self):
        self.assertIsNone(self.pick({"state": "unknown", "bars": {}}, agy_reading(**{"Gemini 4 Argon": 0})))

    def test_no_chain_no_fallback(self):
        self.assertIsNone(aicli.pick_fallback({"claude": {}}, "claude", "deep", lambda t: reading(weekly=100)))

    def test_status_marks_per_class_state(self):
        status = aicli.fallback_status(self.config, {
            "claude": reading(weekly=40),
            "agy": agy_reading(Gemini_Models=85, **{"Gemini 4 Argon": 7}),
        })
        classes = status["classes"]
        self.assertEqual(classes["standard-dev"]["state"], "fallback")
        self.assertEqual(classes["standard-dev"]["model"], "gemini-4-hydrogen-medium")
        self.assertIsNone(classes["standard-dev"]["allowance"])
        self.assertEqual(classes["standard-dev"]["effective_at"], 80)
        self.assertEqual(classes["standard-dev"]["priority"], "standard")
        self.assertEqual(classes["standard-dev"]["effective_tool"], "claude")
        self.assertEqual(classes["standard-dev"]["effective_model"], "claude-sonnet-5")
        self.assertEqual(classes["standard-dev"]["effective_effort"], "medium")
        self.assertEqual(classes["maintenance"]["state"], "ok")
        self.assertEqual(classes["maintenance"]["priority"], "critical")
        self.assertEqual(classes["maintenance"]["effective_at"], 97)

        # When both agy (>= 80%) and claude (>= 97%) are exhausted, standard-dev is blocked
        status_blocked = aicli.fallback_status(self.config, {
            "claude": reading(weekly=98),
            "agy": agy_reading(Gemini_Models=85, **{"Gemini 4 Argon": 7}),
        })
        self.assertEqual(status_blocked["classes"]["standard-dev"]["state"], "blocked")
        self.assertEqual(status_blocked["classes"]["conversation"]["state"], "fallback")
        self.assertEqual(status_blocked["classes"]["conversation"]["effective_model"], "gemini-4-argon-medium")
        self.assertEqual(status_blocked["classes"]["deep-spec"]["state"], "blocked")
        self.assertIsNone(status_blocked["classes"]["deep-spec"]["fallback"])

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

        # 3. A cached reading at 55% (< 97% global threshold, but >= 50% allowance cap)
        # also expires once its bar's reset timestamp passes!
        agy_55 = {
            "state": "ok",
            "detail": "",
            "bars": {"five_hour": {"pct": 55, "reset": "2026-10-22T01:50:00Z", "group": "Gemini Models"}},
            "groups": {"Gemini Models": {"five_hour": {"pct": 55, "reset": "2026-10-22T01:50:00Z"}}},
        }
        aicli._cached_quota("agy", agy_55, at=97, now=t_exhausted)
        hit = aicli._read_quota_cache().get("agy")
        self.assertTrue(aicli._cache_entry_valid_for_timer(hit, at=97, now=t_before))
        self.assertFalse(aicli._cache_entry_valid_for_timer(hit, at=97, now=t_after))

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
    """`aicli run/resolve` against the fake CLIs."""

    def run_cli(self, *args, claude=CLAUDE_ROOM, agy=AGY_ROOM):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            config_path, _ = make_config(d)
            (d / "claude.txt").write_text(claude)
            (d / "agy.txt").write_text(agy)
            log = d / "usage.jsonl"
            res = subprocess.run(
                [sys.executable, str(AICLI), *args], capture_output=True, text=True,
                env={**os.environ, "AICLI_CONFIG": str(config_path), "AICLI_USAGE_LOG": str(log),
                     "AICLI_QUOTA_CACHE": str(d / "quota.json"), "FAKE_CLAUDE_USAGE": str(d / "claude.txt"), "FAKE_AGY_USAGE": str(d / "agy.txt"),
                     "AICLI_SKILLS": str(d / "no-skills.toml")},  # routing only: no class skills
            )
            rows = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout, rows

    def test_run_standard_dev_stays_on_agy_hydrogen_medium_under_reserve_threshold(self):
        out, rows = self.run_cli("run", "--class", "standard-dev", "--client", "pjm:auto-execute", "--", "-p", "hi", agy=AGY_UNDER_50)
        self.assertEqual(json.loads(out), [str(FAKE_CLI / "fake-agy"), "--model", "gemini-4-hydrogen-medium", "-p", "hi"])
        self.assertEqual(rows[-1]["tool"], "agy")
        self.assertEqual(rows[-1]["model"], "gemini-4-hydrogen-medium")
        self.assertNotIn("fallback", rows[-1])

    def test_run_standard_dev_moves_to_claude_sonnet_5_when_agy_reaches_reserve_threshold(self):
        # AGY_ROOM has Gemini Models 15% remaining (85% used >= 80% reserve cap); CLAUDE_ROOM has 60% used (< 97%)
        out, rows = self.run_cli("run", "--class", "standard-dev", "--client", "x:y", "--", "-p", "hi")
        self.assertEqual(
            json.loads(out),
            [str(FAKE_CLI / "fake-claude"), "--model", "claude-sonnet-5", "--effort", "medium", "-p", "hi"],
        )
        self.assertEqual(rows[-1]["tool"], "claude")
        self.assertEqual(rows[-1]["model"], "claude-sonnet-5")
        self.assertEqual(rows[-1]["fallback"]["from_tool"], "agy")
        self.assertEqual(rows[-1]["fallback"]["from_model"], "gemini-4-hydrogen-medium")
        self.assertIn("reserve yield at 80% (reserve: 20%)", rows[-1]["fallback"]["reason"])

    def test_run_deep_spec_has_no_fallback_and_stays_on_claude(self):
        out, rows = self.run_cli("run", "--class", "deep-spec", "--client", "x:y", "--", "-p", "hi", claude=CLAUDE_OUT)
        self.assertEqual(json.loads(out), [str(FAKE_CLI / "fake-claude"), "--model", "claude-opus-5-5", "-p", "hi"])
        self.assertNotIn("fallback", rows[-1])

    def test_run_drops_the_callers_agy_model_on_fallback(self):
        out, _ = self.run_cli("run", "--class", "standard-dev", "--", "--model", "gemini-4-hydrogen-medium", "-p", "hi")
        self.assertEqual(
            json.loads(out),
            [str(FAKE_CLI / "fake-claude"), "--model", "claude-sonnet-5", "--effort", "medium", "-p", "hi"],
        )

    def test_no_fallback_flag(self):
        out, _ = self.run_cli("run", "--no-fallback", "--class", "standard-dev", "--", "-p", "hi")
        self.assertEqual(json.loads(out)[0], str(FAKE_CLI / "fake-agy"))

    def test_resolve_stays_on_agy_when_claude_fallback_is_exhausted(self):
        out, _ = self.run_cli("resolve", "--class", "standard-dev", "--json", claude=CLAUDE_OUT)
        data = json.loads(out)
        self.assertEqual(data["tool"], "agy")
        self.assertEqual(data["model"], "gemini-4-hydrogen-medium")
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
        out, _ = self.run_cli("fallback", "--json", claude=CLAUDE_ROOM, agy=AGY_ROOM)
        data = json.loads(out)
        self.assertEqual(data["classes"]["standard-dev"]["state"], "fallback")
        self.assertEqual(data["classes"]["standard-dev"]["effective_model"], "claude-sonnet-5")
        out_blocked, _ = self.run_cli("fallback", "--json", claude=CLAUDE_OUT, agy=AGY_ROOM)
        data_blocked = json.loads(out_blocked)
        self.assertEqual(data_blocked["classes"]["deep-spec"]["state"], "blocked")
        self.assertEqual(data_blocked["classes"]["standard-dev"]["state"], "blocked")


class NoProbeWithoutFallbackTests(unittest.TestCase):
    """A class with no `fallback` never starts a quota probe (the fake CLIs
    record every `/usage` call they are asked for through FAKE_CLI_USAGE_CALLS);
    only `resolve --quota`, `usage` and `fallback` force one."""

    def _run(self, *args, env_extra=None):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            config_path, _ = make_config(d)
            calls = d / "usage_calls.log"
            res = subprocess.run(
                [sys.executable, str(AICLI), *args], capture_output=True, text=True,
                env={**os.environ, "AICLI_CONFIG": str(config_path),
                     "AICLI_USAGE_LOG": str(d / "usage.jsonl"), "AICLI_QUOTA_CACHE": str(d / "quota.json"),
                     "FAKE_CLI_USAGE_CALLS": str(calls), **(env_extra or {})},
            )
            probed = calls.read_text().splitlines() if calls.exists() else []
        return res, probed

    def test_run_deep_spec_has_no_fallback_and_never_probes(self):
        res, probed = self._run("run", "--class", "deep-spec", "--", "-p", "hi")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(probed, [])

    def test_run_standard_dev_has_a_fallback_and_probes(self):
        res, probed = self._run("run", "--class", "standard-dev", "--", "-p", "hi")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("agy", probed)

    def test_meta_out_absent_when_no_fallback_and_no_cache(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            config_path, _ = make_config(d)
            meta_file = d / "run.meta.json"
            res = subprocess.run(
                [sys.executable, str(AICLI), "run", "--class", "deep-spec", "--meta-out", str(meta_file),
                 "--", "-p", "hi"],
                capture_output=True, text=True,
                env={**os.environ, "AICLI_CONFIG": str(config_path), "AICLI_USAGE_LOG": str(d / "usage.jsonl"),
                     "AICLI_QUOTA_CACHE": str(d / "quota.json")},
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertNotIn("quota", json.loads(meta_file.read_text()))

    def test_resolve_json_without_quota_flag_never_probes(self):
        res, probed = self._run("resolve", "--class", "deep-spec", "--json")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(probed, [])
        self.assertNotIn("quota", json.loads(res.stdout))

    def test_resolve_quota_flag_forces_a_probe(self):
        res, probed = self._run("resolve", "--class", "deep-spec", "--quota", "--json")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("claude", probed)
        self.assertIn("quota", json.loads(res.stdout))

    def test_usage_command_always_probes(self):
        res, probed = self._run("usage", "--json")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("claude", probed)


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

    def test_agy_auth_status_does_not_trust_stale_ok_quota_cache(self):
        with tempfile.TemporaryDirectory() as d:
            dp = Path(d)
            quota_path = dp / "quota.json"
            quota_path.write_text(json.dumps({
                "agy": {
                    "at": time.time(),
                    "result": {"state": "ok", "bars": {"five_hour": {"pct": 10, "reset": "2099-01-01T00:00:00Z"}}},
                }
            }))
            unauth_bin = dp / "agy-unauth"
            unauth_bin.write_text(
                "#!/bin/sh\n"
                "echo 'Authentication required. Please visit the URL to log in:'\n"
                "echo '  https://accounts.google.com/o/oauth2/auth?access_type=offline'\n"
                "exit 1\n"
            )
            unauth_bin.chmod(0o755)
            cfg = {"agy": {"bin": str(unauth_bin)}, "claude": {"bin": str(FAKE_CLI / "fake-claude")}}
            with unittest.mock.patch.dict(os.environ, {"AICLI_QUOTA_CACHE": str(quota_path)}):
                st = aicli.auth_status(cfg, "agy")
                self.assertFalse(st["authenticated"])
                self.assertIn("not logged in", st["detail"].lower())
                cached = json.loads(quota_path.read_text())
                self.assertEqual(cached["agy"]["result"]["state"], "down")

    def test_agy_usage_auth_failure_invalidates_cached_ok(self):
        with tempfile.TemporaryDirectory() as d:
            dp = Path(d)
            quota_path = dp / "quota.json"
            quota_path.write_text(json.dumps({
                "agy": {
                    "at": time.time(),
                    "result": {"state": "ok", "bars": {"five_hour": {"pct": 10, "reset": "2099-01-01T00:00:00Z"}}},
                }
            }))
            unauth_bin = dp / "agy-unauth"
            unauth_bin.write_text(
                "#!/bin/sh\n"
                "echo 'Authentication required. Please visit the URL to log in:'\n"
                "exit 1\n"
            )
            unauth_bin.chmod(0o755)
            cfg = {"agy": {"bin": str(unauth_bin)}}
            with unittest.mock.patch.dict(os.environ, {"AICLI_QUOTA_CACHE": str(quota_path)}):
                res = aicli._agy_usage(cfg, timeout=5)
                self.assertEqual(res["state"], "down")
                out = aicli._cached_quota("agy", res)
                self.assertEqual(out["state"], "down")
                cached = json.loads(quota_path.read_text())
                self.assertEqual(cached["agy"]["result"]["state"], "down")

    def test_auto_reauth_refuses_same_tool_and_falls_back_with_url(self):
        cfg = {
            "claude": {"bin": str(FAKE_CLI / "fake-claude")},
            "agy": {"bin": str(FAKE_CLI / "fake-agy")},
            "classes": {"google-chrome-handler": {"tool": "agy", "tier": "default"}},
        }
        with unittest.mock.patch.object(
            aicli, "start_login",
            return_value={"ok": True, "auth_url": "https://accounts.google.com/o/oauth2/auth?x=1"},
        ):
            res = aicli.auto_reauth(cfg, "agy")
            self.assertFalse(res["ok"])
            self.assertFalse(res["auto_reauthed"])
            self.assertEqual(res["auth_url"], "https://accounts.google.com/o/oauth2/auth?x=1")
            self.assertIn("refusing auto-reauth", res["error"].lower())


class AllowanceCliTests(unittest.TestCase):
    def test_set_and_clear_allowance_cli(self):
        with tempfile.TemporaryDirectory() as d:
            dp = Path(d)
            config_path, _ = make_config(dp)
            routes_path = dp / "routes.json"
            quota_path = dp / "quota.json"
            future_reset = "2099-01-01T05:00:00Z"
            quota_path.write_text(json.dumps({
                "agy": {
                    "at": 1700000000.0,
                    "result": {
                        "state": "ok",
                        "groups": {"Gemini Models": {"five_hour": {"pct": 55, "reset": future_reset}}},
                    },
                }
            }))
            env = {
                **os.environ,
                "AICLI_CONFIG": str(config_path),
                "AICLI_ROUTES": str(routes_path),
                "AICLI_QUOTA_CACHE": str(quota_path),
            }

            r_bad = subprocess.run(
                [sys.executable, str(AICLI), "route", "set-allowance", "101"],
                capture_output=True, text=True, env=env,
            )
            self.assertEqual(r_bad.returncode, 2)

            r_bad_str = subprocess.run(
                [sys.executable, str(AICLI), "route", "set-allowance", "abc"],
                capture_output=True, text=True, env=env,
            )
            self.assertEqual(r_bad_str.returncode, 2)

            r_ok = subprocess.run(
                [sys.executable, str(AICLI), "route", "set-allowance", "80"],
                capture_output=True, text=True, env=env,
            )
            self.assertEqual(r_ok.returncode, 0, r_ok.stderr)
            saved = json.loads(routes_path.read_text())
            self.assertEqual(saved["classes"]["standard-dev"]["allowance_override"]["pct"], 80)
            self.assertEqual(saved["classes"]["standard-dev"]["allowance_override"]["expires_at"], "2099-01-01T05:00:00+00:00")

            r_clr = subprocess.run(
                [sys.executable, str(AICLI), "route", "clear-allowance"],
                capture_output=True, text=True, env=env,
            )
            self.assertEqual(r_clr.returncode, 0, r_clr.stderr)
            saved = json.loads(routes_path.read_text())
            self.assertNotIn("allowance_override", saved.get("classes", {}).get("standard-dev", {}))


def _skill(folder, name, body="Do the thing."):
    folder = Path(folder) / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name} skill\n---\n\n{body}\n")
    return folder


CATALOG = """
[sources.up]
repo = "{repo}"
license = "MIT"

[skills.shared]
source = "up"
path = "skills/shared"

[skills.claude-review]
source = "up"
path = "skills/claude-review"

[skills.gemini-review]
source = "up"
path = "skills/gemini-review"

[classes.t-claude]
all = ["shared"]

[classes.t-agy]
all = ["shared"]

[classes.code-review]
claude = ["claude-review"]
agy = ["gemini-review"]
"""


class SkillsTests(unittest.TestCase):
    """Skills a class loads, staged natively for the CLI the router picked."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.config_path, self.cfg = make_config(self.d)
        (self.d / "cat").mkdir()
        self.catalog = self.d / "cat" / "skills.toml"
        self.catalog.write_text(CATALOG.format(repo="unused"))
        for n in ("shared", "claude-review", "gemini-review"):
            _skill(self.d / "cat" / "vendor", n)
        self.routes = self.d / "routes.json"
        self.env = {**os.environ, "AICLI_CONFIG": str(self.config_path), "AICLI_ROUTES": str(self.routes),
                    "AICLI_SKILLS": str(self.catalog), "AICLI_SKILLS_CACHE": str(self.d / "cache" / "skills"),
                    "AICLI_AGY_SKILLS_DIR": str(self.d / "agy-skills")}

    def aicli(self, *args):
        return subprocess.run([sys.executable, str(AICLI), *args], capture_output=True, text=True, env=self.env)

    def argv(self, *args):
        res = self.aicli("argv", "--json", *args)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout), res.stderr

    def test_claude_gets_a_plugin_dir_with_the_class_skills(self):
        argv, _ = self.argv("--class", "t-claude", "--prompt", "go")
        self.assertEqual(argv[3], "--plugin-dir")
        plugin = Path(argv[4])
        self.assertTrue((plugin / ".claude-plugin" / "plugin.json").is_file())
        self.assertEqual(sorted(p.name for p in (plugin / "skills").iterdir()), ["shared"])
        self.assertTrue(argv[-1].startswith("go\n\nSkills loaded for this job"))
        self.assertIn("shared", argv[-1])
        # the same set reuses the same staged folder
        again, _ = self.argv("--class", "t-claude", "--prompt", "go")
        self.assertEqual(again[4], argv[4])

    def test_agy_finds_the_skills_in_its_own_folder(self):
        argv, _ = self.argv("--class", "t-agy", "--prompt", "go")
        self.assertNotIn("--plugin-dir", argv)
        installed = self.d / "agy-skills" / "aicli-shared"
        self.assertTrue((installed / "SKILL.md").is_file())
        self.assertTrue((installed / ".aicli-hash").is_file())
        self.assertIn("Skills loaded for this job (use the ones that fit): shared.", argv[argv.index("-p") + 1])
        # a changed skill is refreshed on the next run
        _skill(self.d / "cat" / "vendor", "shared", body="Changed.")
        self.argv("--class", "t-agy", "--prompt", "go")
        self.assertIn("Changed.", (installed / "SKILL.md").read_text())

    def test_each_cli_gets_its_own_review_skill(self):
        self.routes.write_text(json.dumps({"classes": {"code-review": {"tool": "claude"}}}))
        argv, _ = self.argv("--class", "code-review", "--prompt", "go")
        self.assertEqual([p.name for p in (Path(argv[argv.index("--plugin-dir") + 1]) / "skills").iterdir()],
                         ["claude-review"])
        self.routes.write_text(json.dumps({"classes": {"code-review": {"tool": "agy"}}}))
        argv, _ = self.argv("--class", "code-review", "--prompt", "go")
        self.assertIn("gemini-review", argv[argv.index("-p") + 1])
        self.assertTrue((self.d / "agy-skills" / "aicli-gemini-review" / "SKILL.md").is_file())
        self.assertFalse((self.d / "agy-skills" / "aicli-claude-review").exists())

    def test_class_skills_key_replaces_the_catalog_default(self):
        self.routes.write_text(json.dumps({"classes": {"t-claude": {"skills": []}}}))
        argv, _ = self.argv("--class", "t-claude", "--prompt", "go")
        self.assertNotIn("--plugin-dir", argv)
        self.assertEqual(argv[-1], "go")
        self.routes.write_text(json.dumps({"classes": {"t-claude": {"skills": {"claude": ["claude-review"]}}}}))
        argv, _ = self.argv("--class", "t-claude", "--prompt", "go")
        self.assertIn("claude-review", argv[-1])
        self.assertNotIn("shared", argv[-1])

    def test_agy_skills_dir_defaults_to_the_global_config_skills(self):
        cfg = {"agy": {"trust_file": "/h/g/antigravity-cli/settings.json"}}
        old = os.environ.pop("AICLI_AGY_SKILLS_DIR", None)
        try:
            self.assertEqual(aicli.agy_skills_dir(cfg), Path("/h/g/config/skills"))
            self.assertEqual(aicli.agy_skills_dir({"agy": {"skills_dir": "/x", **cfg["agy"]}}), Path("/x"))
            self.assertIsNone(aicli.agy_skills_dir({"agy": {}}))
        finally:
            if old is not None:
                os.environ["AICLI_AGY_SKILLS_DIR"] = old

    def test_caller_adds_a_skill_by_name_or_folder(self):
        own = _skill(self.d / "mine", "my-skill")
        argv, _ = self.argv("--class", "t-claude", "--skill", "claude-review", "--skill", str(own), "--prompt", "go")
        names = sorted(p.name for p in (Path(argv[argv.index("--plugin-dir") + 1]) / "skills").iterdir())
        self.assertEqual(names, ["claude-review", "my-skill", "shared"])

    def test_no_skills(self):
        argv, _ = self.argv("--class", "t-claude", "--no-skills", "--prompt", "go")
        self.assertNotIn("--plugin-dir", argv)
        self.assertEqual(argv[-1], "go")

    def test_unknown_skill_warns_and_the_run_goes_on(self):
        argv, err = self.argv("--class", "t-claude", "--skill", "nope", "--prompt", "go")
        self.assertIn("skill 'nope' not found", err)
        self.assertIn("--plugin-dir", argv)

    def test_no_prompt_no_note(self):
        argv, _ = self.argv("--class", "t-claude", "--", "-p", "raw")
        self.assertEqual(argv[-2:], ["-p", "raw"])
        self.assertIn("--plugin-dir", argv)

    def test_cache_wins_over_vendor(self):
        _skill(self.d / "cache" / "skills", "shared", body="Fresh.")
        argv, _ = self.argv("--class", "t-claude", "--prompt", "go")
        staged = Path(argv[argv.index("--plugin-dir") + 1]) / "skills" / "shared" / "SKILL.md"
        self.assertIn("Fresh.", staged.read_text())

    def test_resolve_and_list_show_the_skills(self):
        res = self.aicli("resolve", "--class", "t-agy", "--json")
        self.assertEqual(json.loads(res.stdout)["skills"], ["shared"])
        res = self.aicli("skills", "list", "--class", "code-review", "--json")
        rows = {r["tool"]: [s["name"] for s in r["skills"]] for r in json.loads(res.stdout)}
        self.assertEqual(rows, {"claude": ["claude-review"], "agy": ["gemini-review"]})

    def _upstream(self, body="v1"):
        repo = self.d / "upstream"
        if not repo.exists():
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
            (repo / "LICENSE").write_text("MIT License\n")
        for n in ("shared", "claude-review", "gemini-review"):
            _skill(repo / "skills", n, body=body)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", body],
                       cwd=repo, check=True)
        self.catalog.write_text(CATALOG.format(repo=repo.as_uri()))
        return repo

    def test_update_fetches_into_the_cache_and_never_fails(self):
        self._upstream("v1")
        res = self.aicli("skills", "update", "--json")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual({r["status"] for r in json.loads(res.stdout)}, {"new"})
        cache = self.d / "cache" / "skills"
        self.assertIn("v1", (cache / "shared" / "SKILL.md").read_text())
        self.assertTrue((cache / "licenses" / "up.txt").is_file())
        lock = json.loads((cache / "skills.lock.json").read_text())
        self.assertEqual(lock["shared"]["license"], "MIT")

        res = self.aicli("skills", "update", "--json")
        self.assertEqual({r["status"] for r in json.loads(res.stdout)}, {"unchanged"})

        self._upstream("v2")
        res = self.aicli("skills", "update", "--json", "--only", "shared")
        self.assertEqual([(r["name"], r["status"]) for r in json.loads(res.stdout)], [("shared", "updated")])
        self.assertIn("v2", (cache / "shared" / "SKILL.md").read_text())

        # an unreachable source keeps the copies it has, and still exits 0
        self.catalog.write_text(CATALOG.format(repo=(self.d / "gone").as_uri()))
        res = self.aicli("skills", "update", "--json")
        self.assertEqual(res.returncode, 0)
        self.assertTrue(all(r["status"].startswith("failed") for r in json.loads(res.stdout)))
        self.assertIn("v2", (cache / "shared" / "SKILL.md").read_text())

    def test_update_vendor_writes_next_to_the_catalog(self):
        self._upstream("v1")
        res = self.aicli("skills", "update", "--vendor")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("v1", (self.d / "cat" / "vendor" / "shared" / "SKILL.md").read_text())


class ShippedSkillsTests(unittest.TestCase):
    """The catalog that ships: every skill a class names is in it, and vendored."""

    def test_catalog_is_complete_and_vendored(self):
        with open(ROOT / "skills" / "skills.toml", "rb") as f:
            catalog = tomllib.load(f)
        known = set(catalog["skills"])
        for cls, spec in catalog["classes"].items():
            for key, names in spec.items():
                self.assertIn(key, ("all", *aicli.TOOLS), cls)
                self.assertTrue(set(names) <= known, f"{cls}: {set(names) - known}")
        lock = json.loads((ROOT / "skills" / "vendor" / "skills.lock.json").read_text())
        for name, s in catalog["skills"].items():
            self.assertIn(s["source"], catalog["sources"], name)
            self.assertTrue((ROOT / "skills" / "vendor" / name / "SKILL.md").is_file(), name)
            self.assertEqual(lock[name]["sha256"], aicli.tree_hash(ROOT / "skills" / "vendor" / name), name)

    def test_review_skill_differs_per_cli(self):
        with open(ROOT / "skills" / "skills.toml", "rb") as f:
            spec = tomllib.load(f)["classes"]["code-review"]
        self.assertNotEqual(spec["claude"], spec["agy"])
