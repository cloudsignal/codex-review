"""Tests for the in-flight run registry: the `runs` and `kill` subcommands, marker
lifecycle, and process-guard helpers. A review is a long paid run; these make a
backgrounded round trackable and stoppable so it is never an invisible runaway.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import run_review  # noqa: E402

RUNNER = Path(__file__).resolve().parent.parent / "scripts" / "run_review.py"


class SanitizeOriginTest(unittest.TestCase):
    def test_strips_userinfo_from_url(self):
        self.assertEqual(
            run_review.sanitize_origin("https://user:tok@example.com/a/b.git"),
            "https://example.com/a/b.git")
        self.assertEqual(
            run_review.sanitize_origin("https://tok@example.com/a/b.git"),
            "https://example.com/a/b.git")

    def test_leaves_scp_and_plain_urls(self):
        self.assertEqual(
            run_review.sanitize_origin("git@github.com:owner/repo.git"),
            "git@github.com:owner/repo.git")
        self.assertEqual(
            run_review.sanitize_origin("https://example.com/a/b.git"),
            "https://example.com/a/b.git")
        self.assertEqual(run_review.sanitize_origin("/local/path"), "/local/path")


class StalenessTest(unittest.TestCase):
    def test_finite(self):
        self.assertTrue(run_review._finite(1.0))
        self.assertTrue(run_review._finite(0))
        for bad in (True, None, "x", float("nan"), float("inf"), float("-inf")):
            self.assertFalse(run_review._finite(bad), bad)

    def test_marker_stale_by_deadline(self):
        now = 1_000_000.0
        self.assertFalse(run_review._marker_stale({"deadline": now + 10}, now))
        self.assertTrue(run_review._marker_stale({"deadline": now - 10_000}, now))

    def test_deadline_governs_over_age(self):
        # A legitimately long run: started 7h ago but deadline is still hours away.
        now = 1_000_000.0
        info = {"started": now - 7 * 3600, "deadline": now + 3 * 3600}
        self.assertFalse(run_review._marker_stale(info, now))

    def test_no_timestamps_is_stale(self):
        self.assertTrue(run_review._marker_stale({}, 1_000_000.0))
        self.assertTrue(run_review._marker_stale({"deadline": "nan"}, 1_000_000.0))


class IdentityGuardTest(unittest.TestCase):
    def test_review_named_process_matches(self):
        p = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)", "run_review-stub"])
        try:
            self.assertTrue(run_review._process_looks_like_review(p.pid))
        finally:
            p.kill()
            p.wait()

    def test_unrelated_process_does_not_match(self):
        # A plain sleep with no codex/run_review token must NOT be treated as a review.
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            self.assertFalse(run_review._process_looks_like_review(p.pid))
        finally:
            p.kill()
            p.wait()

    def test_dead_pid_is_not_a_review_process(self):
        self.assertFalse(run_review._process_looks_like_review(2 ** 30))
        self.assertFalse(run_review._process_looks_like_review(None))


class RegistryHelpersTest(unittest.TestCase):
    def test_pid_alive_and_terminate(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            self.assertTrue(run_review._pid_alive(proc.pid))
            run_review._terminate_pid(proc.pid, grace=5.0)
            # This test IS the parent, so reap the child before asserting it ended (an
            # un-reaped child lingers as a zombie that os.kill(pid, 0) still sees). In the
            # real `kill` path the target is not the killer's child, so there is no zombie.
            proc.wait(timeout=5)
            self.assertIsNotNone(proc.poll())
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()

    def test_pid_alive_false_for_dead(self):
        self.assertFalse(run_review._pid_alive(2 ** 30))  # implausible pid
        self.assertFalse(run_review._pid_alive(None))


class RunsKillCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.env = dict(os.environ)
        self.env["CODEX_REVIEW_STATE_DIR"] = str(self.tmp)
        self.runs = self.tmp / "runs"
        self.runs.mkdir(parents=True)
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait()

    def _spawn(self):
        # The "run_review" argv token makes this stand-in match the kill identity guard,
        # which only signals PIDs whose command names run_review or codex.
        p = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)", "run_review-stub"])
        self.procs.append(p)
        return p

    def _marker(self, pid, topic="alpha", mode="inspect", deadline=None):
        now = time.time()
        info = {"mode": mode, "pid": pid, "codex_pid": pid, "topic": topic,
                "cwd": "/x", "model": "m", "started": now,
                "deadline": now + 3600 if deadline is None else deadline}
        (self.runs / ("%d.json" % pid)).write_text(json.dumps(info))

    def _cli(self, *args):
        return subprocess.run([sys.executable, str(RUNNER), *args],
                              capture_output=True, text=True, env=self.env)

    def test_runs_empty(self):
        r = self._cli("runs")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no review runs in flight", r.stdout)

    def test_runs_lists_live_and_prunes_dead(self):
        live = self._spawn()
        self._marker(live.pid, topic="live-one")
        self._marker(2 ** 30, topic="dead-one")  # dead pid -> pruned
        r = self._cli("runs")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("live-one", r.stdout)
        self.assertNotIn("dead-one", r.stdout)
        # dead marker pruned from disk
        self.assertFalse((self.runs / ("%d.json" % (2 ** 30))).exists())

    def test_kill_by_pid_stops_process_and_removes_marker(self):
        live = self._spawn()
        self._marker(live.pid, topic="to-kill")
        r = self._cli("kill", str(live.pid))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("stopped review run", r.stdout)
        live.wait(timeout=10)
        self.assertIsNotNone(live.poll())  # process ended
        self.assertFalse((self.runs / ("%d.json" % live.pid)).exists())

    def test_kill_by_topic(self):
        live = self._spawn()
        self._marker(live.pid, topic="by-topic")
        r = self._cli("kill", "by-topic")
        self.assertEqual(r.returncode, 0, r.stderr)
        live.wait(timeout=10)
        self.assertIsNotNone(live.poll())

    def test_kill_no_match(self):
        r = self._cli("kill", "nope")
        self.assertEqual(r.returncode, 3)
        self.assertIn("no in-flight review run matching", r.stderr)

    def test_malformed_marker_is_pruned_not_wedging(self):
        # A non-object marker must not crash `runs` for other runs; it is pruned.
        (self.runs / "junk.json").write_text("[]")
        (self.runs / "bad.json").write_text("not json at all")
        live = self._spawn()
        self._marker(live.pid, topic="good")
        r = self._cli("runs")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("good", r.stdout)
        self.assertFalse((self.runs / "junk.json").exists())
        self.assertFalse((self.runs / "bad.json").exists())

    def test_stale_deadline_marker_pruned_even_if_pid_alive(self):
        live = self._spawn()
        self._marker(live.pid, topic="expired", deadline=time.time() - 10_000)
        r = self._cli("runs")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no review runs in flight", r.stdout)  # stale -> not listed
        self.assertFalse((self.runs / ("%d.json" % live.pid)).exists())  # and pruned


class MarkerLifecycleTest(unittest.TestCase):
    """A completed inspect run must leave no marker behind (uses the stub codex)."""
    STUB = (
        "#!/bin/bash\n"
        'printf "%s\\n" "$@" >> "$STUB_LOG"\n'
        'out=""; prev=""\n'
        'for a in "$@"; do if [ "$prev" = "-o" ]; then out="$a"; fi; prev="$a"; done\n'
        'echo \'{"type":"thread.started","thread_id":"t1"}\'\n'
        'echo \'{"type":"turn.completed"}\'\n'
        'printf "FINDINGS\\n" > "$out"\n'
    )

    def test_completed_run_leaves_no_marker(self):
        tmp = Path(tempfile.mkdtemp())
        state = tmp / "state"
        repo = tmp / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "remote", "add", "origin", "git@x:a/b.git"], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-q", "--allow-empty", "-m", "i"], cwd=repo, check=True)
        (repo / "d.md").write_text("doc\n")
        stub = tmp / "codex"
        stub.write_text(self.STUB)
        stub.chmod(0o755)
        env = dict(os.environ)
        env.update({"CODEX_BIN": str(stub), "CODEX_REVIEW_STATE_DIR": str(state),
                    "STUB_LOG": str(tmp / "log")})
        r = subprocess.run([sys.executable, str(RUNNER), "--cwd", str(repo),
                            "--topic", "thing", "--doc", "d.md", "--kind", "plan"],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        runs = state / "runs"
        leftover = list(runs.glob("*.json")) if runs.is_dir() else []
        self.assertEqual(leftover, [], "a completed run must leave no marker")


if __name__ == "__main__":
    unittest.main()
