"""Tests for the standalone `limits` subcommand (subscription usage from the last
persisted codex snapshot). No token spend, no network: it reads rollout logs off disk.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import run_review  # noqa: E402

RUNNER = Path(__file__).resolve().parent.parent / "scripts" / "run_review.py"


def _snapshot(primary_pct=7.0, secondary=None, reached=None, plan="prolite",
              window_minutes=10080, resets_at=1788295118):
    rl = {
        "limit_id": "codex",
        "primary": {"used_percent": primary_pct, "window_minutes": window_minutes,
                    "resets_at": resets_at},
        "secondary": secondary,
        "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
        "plan_type": plan,
        "rate_limit_reached_type": reached,
        "spend_control_reached": None,
    }
    return rl


def _write_rollout(codex_home, rate_limits, ts="2026-08-26T09:23:20.880Z", day="26"):
    d = Path(codex_home) / "sessions" / "2026" / "08" / day
    d.mkdir(parents=True, exist_ok=True)
    f = d / ("rollout-2026-08-%sT11-44-53-abc.jsonl" % day)
    lines = [
        {"timestamp": "2026-08-26T08:44:53.000Z", "type": "session_meta",
         "payload": {"type": "session_meta"}},
        {"timestamp": ts, "type": "event_msg",
         "payload": {"type": "token_count",
                     "info": {"total_token_usage": {"total_tokens": 1}},
                     "rate_limits": rate_limits}},
    ]
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return f


class LimitsLogicTest(unittest.TestCase):
    def test_status_ok_near_reached(self):
        self.assertEqual(run_review.usage_status(_snapshot(7.0)), "OK")
        self.assertEqual(run_review.usage_status(_snapshot(80.0)), "NEAR")
        self.assertEqual(run_review.usage_status(_snapshot(99.9)), "NEAR")
        self.assertEqual(run_review.usage_status(_snapshot(100.0)), "REACHED")
        self.assertEqual(
            run_review.usage_status(_snapshot(5.0, reached="primary")), "REACHED")

    def test_secondary_window_counts(self):
        rl = _snapshot(10.0, secondary={"used_percent": 92.0, "window_minutes": 300,
                                        "resets_at": 1788295118})
        self.assertEqual(run_review.usage_status(rl), "NEAR")
        text = run_review.format_account_usage(rl, "ts", now=1788000000)
        self.assertIn("primary:", text)
        self.assertIn("secondary:", text)
        self.assertIn("5h window", text)  # 300 minutes

    def test_format_includes_plan_and_status(self):
        text = run_review.format_account_usage(_snapshot(7.0), "2026-08-26T09:23:20Z",
                                               now=1788000000)
        self.assertIn("[OK]", text)
        self.assertIn("plan: prolite", text)
        self.assertIn("7.0% used of the 7d window", text)
        self.assertIn("as of: 2026-08-26T09:23:20Z", text)

    def test_latest_rate_limits_reads_newest(self):
        with tempfile.TemporaryDirectory() as home:
            _write_rollout(home, _snapshot(7.0))
            rl, ts = run_review.latest_rate_limits(home)
            self.assertIsNotNone(rl)
            self.assertEqual(rl["plan_type"], "prolite")
            self.assertEqual(ts, "2026-08-26T09:23:20.880Z")

    def test_no_sessions_returns_none(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(run_review.latest_rate_limits(home), (None, None))


class LimitsCliTest(unittest.TestCase):
    def _run(self, home, *extra):
        env = dict(os.environ)
        env["CODEX_HOME"] = str(home)
        return subprocess.run([sys.executable, str(RUNNER), "limits", *extra],
                              capture_output=True, text=True, env=env)

    def test_ok_prints_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as home:
            _write_rollout(home, _snapshot(7.0))
            r = self._run(home)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("[OK]", r.stdout)
            self.assertIn("7.0%", r.stdout)

    def test_reached_exits_one(self):
        with tempfile.TemporaryDirectory() as home:
            _write_rollout(home, _snapshot(100.0))
            r = self._run(home)
            self.assertEqual(r.returncode, 1)
            self.assertIn("[REACHED]", r.stdout)

    def test_near_exits_zero_unless_strict(self):
        with tempfile.TemporaryDirectory() as home:
            _write_rollout(home, _snapshot(85.0))
            self.assertEqual(self._run(home).returncode, 0)
            self.assertEqual(self._run(home, "--strict").returncode, 1)

    def test_json_output(self):
        with tempfile.TemporaryDirectory() as home:
            _write_rollout(home, _snapshot(7.0))
            r = self._run(home, "--json")
            self.assertEqual(r.returncode, 0, r.stderr)
            obj = json.loads(r.stdout)
            self.assertEqual(obj["status"], "OK")
            self.assertEqual(obj["rate_limits"]["plan_type"], "prolite")

    def test_no_snapshot_exits_three(self):
        with tempfile.TemporaryDirectory() as home:
            r = self._run(home)
            self.assertEqual(r.returncode, 3)
            self.assertIn("no codex usage snapshot", r.stderr)


if __name__ == "__main__":
    unittest.main()
