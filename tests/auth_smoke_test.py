#!/usr/bin/env python3
"""Live smoke test for real `claude` and `agy` auth detection & OAuth URL extraction.

Runs both CLIs against an isolated temporary HOME / XDG_CONFIG_HOME so real
credentials in ~/.claude and ~/.config/antigravity are NEVER touched.
Intended to be run regularly by the aicli maintainer (`make test-auth-smoke`)
to catch upstream CLI output/UI changes before a real auth outage occurs.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import aicli  # noqa: E402


class AuthSmokeTests(unittest.TestCase):
    def setUp(self):
        self.cfg = aicli.load_config()

    def test_claude_isolated_unauthenticated_and_oauth_url(self):
        bin_path = aicli.resolve_bin(self.cfg, "claude")
        if not bin_path:
            self.skipTest("claude CLI not installed")
        with tempfile.TemporaryDirectory() as home:
            env = {
                **os.environ,
                "HOME": home,
                "XDG_CONFIG_HOME": os.path.join(home, ".config"),
                "XDG_STATE_HOME": os.path.join(home, ".local", "state"),
                "AICLI_QUOTA_CACHE": os.path.join(home, "quota.json"),
                "BROWSER": "/bin/true",
            }
            env.pop("DISPLAY", None)
            env.pop("WAYLAND_DISPLAY", None)
            p = subprocess.run(
                [bin_path, "auth", "status"],
                capture_output=True, text=True, env=env, timeout=10, check=False,
            )
            self.assertNotIn('"loggedIn": true', p.stdout.lower())

    def test_agy_isolated_unauthenticated_and_oauth_url(self):
        bin_path = aicli.resolve_bin(self.cfg, "agy")
        if not bin_path:
            self.skipTest("agy CLI not installed")
        with tempfile.TemporaryDirectory() as home:
            env = {
                **os.environ,
                "HOME": home,
                "XDG_CONFIG_HOME": os.path.join(home, ".config"),
                "XDG_DATA_HOME": os.path.join(home, ".local", "share"),
                "XDG_STATE_HOME": os.path.join(home, ".local", "state"),
                "AGY_CLI_FORCE_FILE_TOKEN_STORAGE": "1",
                "BROWSER": "/bin/true",
            }
            env.pop("DISPLAY", None)
            env.pop("WAYLAND_DISPLAY", None)
            try:
                p = subprocess.run(
                    [bin_path, "-p", "/usage"],
                    capture_output=True, text=True, stdin=subprocess.DEVNULL,
                    env=env, timeout=8, check=False,
                )
                combined = (p.stdout or "") + "\n" + (p.stderr or "")
            except subprocess.TimeoutExpired as exc:
                combined = aicli._decode_stream(exc.stdout) + "\n" + aicli._decode_stream(exc.stderr)

            self.assertTrue(
                aicli._agy_output_needs_auth(combined),
                f"Expected agy unauth marker in isolated HOME output, got:\n{combined[:500]}",
            )
            url = aicli.extract_agy_login_url(combined)
            self.assertIsNotNone(url, f"Could not extract Google OAuth URL from:\n{combined[:500]}")
            self.assertTrue(url.startswith("https://accounts.google.com/"), url)


if __name__ == "__main__":
    unittest.main(verbosity=2)
