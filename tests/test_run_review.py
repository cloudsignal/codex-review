"""Unit tests for run_review.py. All tests use a stub codex binary; none spend credits."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path

RUNNER = Path(__file__).resolve().parent.parent / "scripts" / "run_review.py"


def _runner_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("run_review_under_test", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

STUB = """#!/bin/bash
# Stub codex. Records argv, emits the JSONL events codex exec --json emits,
# and writes the file named by -o.
#   STUB_MODE=fail     exits 2 with no output file
#   STUB_MODE=auth     exits 1 with an authentication error on stderr
#   STUB_MODE=nothread succeeds but emits no thread.started event
#   STUB_MODE=hang     blocks before its first event (e.g. waiting on stdin): no stdout at all
#   STUB_MODE=slow     a genuinely long review: events flow, then it stalls mid-turn
#   STUB_MODE=garbage  writes a non-JSON line, then stalls
#   STUB_MODE=orphan   emits thread.started, leaves a descendant holding stdout, then stalls
#   STUB_MODE=empty    completes but writes an empty review file
#   STUB_MODE=stdin    reads inherited stdin to EOF before its first event, as codex-cli
#                      0.153.3 does; blocks for ever on a pipe nobody closes
# `--version` answers like the real CLI and is recorded separately (never in STUB_LOG).
if [ "${1:-}" = "--version" ]; then
  printf '%s\\n' "$@" >> "$STUB_LOG.version"
  if [ -n "${STUB_VERSION_FAIL:-}" ]; then exit 1; fi
  if [ -n "${STUB_VERSION_BYTES:-}" ]; then printf 'codex-cli \\xff0.0.0\\n'; exit 0; fi
  echo "${STUB_VERSION:-codex-cli 0.0.0-stub}"
  exit 0
fi
printf '%s\\n' "$@" >> "$STUB_LOG"
if [ "${STUB_MODE:-ok}" = "hang" ]; then
  echo 'Reading additional input from stdin...' >&2
  echo $$ > "$STUB_LOG.pid"
  exec sleep 30
fi
if [ "${STUB_MODE:-ok}" = "slow" ]; then
  echo '{"type":"thread.started","thread_id":"stub-thread-1"}'
  echo '{"type":"turn.started"}'
  echo '{"type":"item.started","item":{"type":"command_execution"}}'
  echo $$ > "$STUB_LOG.pid"
  exec sleep 30
fi
if [ "${STUB_MODE:-ok}" = "garbage" ]; then
  echo 'not a json event'
  echo $$ > "$STUB_LOG.pid"
  exec sleep 30
fi
if [ "${STUB_MODE:-ok}" = "orphan" ]; then
  echo '{"type":"thread.started","thread_id":"stub-thread-1"}'
  sleep 30 &
  echo $! > "$STUB_LOG.orphan"
  echo $$ > "$STUB_LOG.pid"
  exec sleep 30
fi
if [ "${STUB_MODE:-ok}" = "stdin" ]; then
  echo 'Reading additional input from stdin...' >&2
  cat > "$STUB_LOG.stdin"
fi
if [ "${STUB_MODE:-ok}" = "fail" ]; then
  echo "stub: simulated failure" >&2
  exit 2
fi
if [ "${STUB_MODE:-ok}" = "auth" ]; then
  echo "${STUB_AUTH_MESSAGE:-stream error: unexpected status 401 Unauthorized}" >&2
  exit 1
fi
if [ "${STUB_MODE:-ok}" = "streamerr" ]; then
  # Real codex (--json) reports the cause as JSON events on STDOUT; stderr carries only noise.
  echo 'Reading additional input from stdin...' >&2
  echo '{"type":"error","message":"'"${STUB_STREAM_MESSAGE:-You have hit your usage limit.}"'"}'
  echo '{"type":"turn.failed","error":{"message":"turn failed"}}'
  exit 1
fi
out=""
prev=""
for a in "$@"; do
  if [ "$prev" = "-o" ]; then out="$a"; fi
  prev="$a"
done
if [ "${STUB_MODE:-ok}" = "empty" ]; then
  echo '{"type":"thread.started","thread_id":"stub-thread-1"}'
  echo '{"type":"turn.completed"}'
  : > "$out"
  exit 0
fi
if [ "${STUB_MODE:-ok}" != "nothread" ]; then
  echo '{"type":"thread.started","thread_id":"stub-thread-1"}'
fi
if [ -n "${STUB_USAGE:-}" ]; then
  echo '{"type":"turn.completed","usage":'"$STUB_USAGE"'}'
else
  echo '{"type":"turn.completed"}'
fi
printf 'STUB REVIEW FINDINGS\\n' > "$out"
"""


class RunReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.state_dir = self.tmp / "state"
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "git@example.com:acme/repo.git"],
            cwd=self.repo, check=True,
        )
        subprocess.run(["git", "checkout", "-q", "-b", "feature-x"], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "--allow-empty", "-m", "init"],
            cwd=self.repo, check=True,
        )
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "thing.md").write_text("the doc\n")
        self.stub = self.tmp / "codex-stub"
        self.stub.write_text(STUB)
        self.stub.chmod(0o755)
        self.stub_log = self.tmp / "stub.log"
        self.stub_log.write_text("")

    def run_review(self, *extra, env_extra=None, topic="thing", stdin=None):
        env = dict(os.environ)
        env.update({
            "CODEX_BIN": str(self.stub),
            "CODEX_REVIEW_STATE_DIR": str(self.state_dir),
            "STUB_LOG": str(self.stub_log),
        })
        env.update(env_extra or {})
        return subprocess.run(
            [sys.executable, str(RUNNER), "--cwd", str(self.repo),
             "--topic", topic, "--doc", "docs/thing.md", *extra],
            capture_output=True, text=True, env=env, stdin=stdin,
        )

    def version_calls(self):
        log = Path(str(self.stub_log) + ".version")
        return log.read_text() if log.exists() else ""

    def stub_pid_alive(self):
        pid = int(Path(str(self.stub_log) + ".pid").read_text())
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    def run_markers(self):
        # The marker directory itself: `runs` prunes dead markers, so asking it would pass
        # even if the timeout path had forgotten the marker.
        return sorted((self.state_dir / "runs").glob("*.json"))

    def assert_ends_with_version_note(self, text, prefix="codex-cli 0.0.0-stub"):
        # The contract: the version note is the LAST line of every failure message.
        self.assertTrue(text.rstrip().splitlines()[-1].startswith(prefix), text)

    def state_files(self):
        return sorted(self.state_dir.glob("*.json"))

    def expected_findings(self, topic="thing"):
        # resolve(): the runner resolves --cwd, and on macOS the temp dir is reached
        # through the /var -> /private/var symlink.
        return (self.repo.resolve() / ".codex-review" / "reviews"
                / f"{date.today().isoformat()}-{topic}-codex-review.md")

    def test_new_review_creates_state_and_findings(self):
        r = self.run_review("--kind", "plan", "--ask", "focus on task 2")
        self.assertEqual(r.returncode, 0, r.stderr)
        files = self.state_files()
        self.assertEqual(len(files), 1)
        state = json.loads(files[0].read_text())
        self.assertEqual(state["thread_id"], "stub-thread-1")
        self.assertEqual(state["topic"], "thing")
        self.assertEqual(state["rounds"][0]["kind"], "plan")
        findings = Path(state["findings_file"])
        # Assert the exact contracted location, not merely that some file exists:
        # a location-blind check hid a fallback that wrote to the repository root.
        self.assertEqual(findings, self.expected_findings())
        self.assertTrue(findings.exists())
        text = findings.read_text()
        self.assertIn("STUB REVIEW FINDINGS", text)
        self.assertIn("## Round 1", text)
        self.assertEqual(r.stdout.strip().splitlines()[-1], str(findings))

    def test_model_effort_and_sandbox_flags_passed(self):
        r = self.run_review("--kind", "plan")
        self.assertEqual(r.returncode, 0, r.stderr)
        argv = self.stub_log.read_text().splitlines()
        self.assertIn("-m", argv)
        self.assertIn("gpt-5.6-sol", argv)
        self.assertIn('model_reasoning_effort="xhigh"', argv)
        self.assertIn('sandbox_mode="read-only"', argv)
        self.assertIn("--json", argv)

    def test_prompt_renders_template_placeholders(self):
        r = self.run_review("--kind", "plan", "--ask", "focus on task 2")
        self.assertEqual(r.returncode, 0, r.stderr)
        # The prompt is one argv entry but spans many lines in the log, so
        # assert against the whole log text, not its last line.
        log = self.stub_log.read_text()
        self.assertIn("- docs/thing.md", log)
        self.assertIn("focus on task 2", log)
        self.assertNotIn("{{", log)

    def test_second_round_resumes_same_thread(self):
        self.assertEqual(self.run_review("--kind", "plan").returncode, 0)
        r = self.run_review("--kind", "fix-round", "--ask", "fixed items 1 and 2")
        self.assertEqual(r.returncode, 0, r.stderr)
        argv = self.stub_log.read_text().splitlines()
        self.assertIn("resume", argv)
        self.assertIn("stub-thread-1", argv)
        state = json.loads(self.state_files()[0].read_text())
        self.assertEqual(len(state["rounds"]), 2)
        text = Path(state["findings_file"]).read_text()
        self.assertIn("## Round 1", text)
        self.assertIn("## Round 2", text)

    def test_resumed_round_recreates_a_deleted_findings_directory(self):
        # State lives 30 days, so a resumed topic can point at a findings path whose
        # directory no longer exists (the tree was moved or cleaned). The round must land
        # in the file rather than lose an already-paid review.
        self.assertEqual(self.run_review("--kind", "plan").returncode, 0)
        findings = self.expected_findings()
        shutil.rmtree(findings.parent)
        self.assertFalse(findings.parent.exists())
        r = self.run_review("--kind", "fix-round", "--ask", "fixed items 1 and 2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(findings.exists())
        text = findings.read_text()
        self.assertIn("## Round 2", text)
        self.assertIn("STUB REVIEW FINDINGS", text)
        state = json.loads(self.state_files()[0].read_text())
        self.assertEqual(len(state["rounds"]), 2)

    def test_fix_round_without_state_exits_3(self):
        r = self.run_review("--kind", "fix-round", "--ask", "fixed")
        self.assertEqual(r.returncode, 3)
        self.assertIn("no review thread", r.stderr)

    def test_out_dir_override(self):
        r = self.run_review("--kind", "plan", "--out-dir", "reviews")
        self.assertEqual(r.returncode, 0, r.stderr)
        state = json.loads(self.state_files()[0].read_text())
        self.assertTrue(state["findings_file"].endswith(
            "/reviews/%s-thing-codex-review.md" % date.today().isoformat()))

    def test_credential_in_origin_never_reaches_state(self):
        subprocess.run(
            ["git", "remote", "set-url", "origin",
             "https://user:SECRETTOKEN@example.com/acme/repo.git"],
            cwd=self.repo, check=True)
        r = self.run_review("--kind", "plan")
        self.assertEqual(r.returncode, 0, r.stderr)
        blob = self.state_files()[0].read_text()
        self.assertNotIn("SECRETTOKEN", blob)
        self.assertIn("https://example.com/acme/repo.git", blob)

    def test_state_file_is_owner_only(self):
        r = self.run_review("--kind", "plan")
        self.assertEqual(r.returncode, 0, r.stderr)
        mode = self.state_files()[0].stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_codex_failure_leaves_no_state(self):
        r = self.run_review("--kind", "plan", env_extra={"STUB_MODE": "fail"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("simulated failure", r.stderr)
        self.assertEqual(self.state_files(), [])

    def test_stdout_stream_error_is_surfaced_on_failure(self):
        # The real cause is a JSON event on STDOUT (codex --json); the old code showed only
        # stderr ("Reading additional input...") and hid it. It must now be surfaced.
        r = self.run_review("--kind", "plan", env_extra={"STUB_MODE": "streamerr"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("codex reported", r.stderr)
        self.assertIn("usage limit", r.stderr)
        self.assertEqual(self.state_files(), [])

    def test_stdout_auth_error_names_recovery(self):
        # An auth error arriving on STDOUT (not stderr) must still trip the AUTH_MARKERS hint.
        r = self.run_review("--kind", "plan", env_extra={
            "STUB_MODE": "streamerr",
            "STUB_STREAM_MESSAGE": "unexpected status 401 Unauthorized",
        })
        self.assertEqual(r.returncode, 1)
        self.assertIn("codex login", r.stderr)

    def test_prune_removes_stale_state(self):
        self.state_dir.mkdir(parents=True)
        stale = self.state_dir / "deadbeef0000-old.json"
        stale.write_text(json.dumps({"last_used": time.time() - 40 * 86400}))
        fresh = self.state_dir / "deadbeef0000-new.json"
        fresh.write_text(json.dumps({"last_used": time.time()}))
        self.assertEqual(self.run_review("--kind", "plan").returncode, 0)
        names = [p.name for p in self.state_files()]
        self.assertNotIn("deadbeef0000-old.json", names)
        self.assertIn("deadbeef0000-new.json", names)

    def test_missing_thread_event_leaves_no_state(self):
        r = self.run_review("--kind", "plan", env_extra={"STUB_MODE": "nothread"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("no thread.started", r.stderr)
        self.assertIn("codex-cli 0.0.0-stub", r.stderr)  # an event-schema change lands here
        # The paid review text is surfaced rather than silently discarded.
        self.assertIn("STUB REVIEW FINDINGS", r.stderr)
        self.assertEqual(self.state_files(), [])
        self.assertFalse(self.expected_findings().exists())

    def test_corrupt_state_thread_id_is_refused(self):
        self.assertEqual(self.run_review("--kind", "plan").returncode, 0)
        spath = self.state_files()[0]
        state = json.loads(spath.read_text())
        state["thread_id"] = None
        spath.write_text(json.dumps(state))
        self.stub_log.write_text("")
        r = self.run_review("--kind", "fix-round", "--ask", "fixed")
        self.assertEqual(r.returncode, 1)
        self.assertIn("usable thread id", r.stderr)
        # Refused before spending a round.
        self.assertEqual(self.stub_log.read_text(), "")

    def test_invalid_topic_rejected_before_calling_codex(self):
        # "thing\n" is the reason the pattern is matched with fullmatch: re.match
        # accepts a trailing newline at `$` and would put it into a filename.
        for bad in ("feature/foo", "../escape", "Thing", "thing\n", "a" * 65):
            with self.subTest(topic=bad):
                self.stub_log.write_text("")
                r = self.run_review("--kind", "plan", topic=bad)
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertIn("kebab-case slug", r.stderr)
                self.assertEqual(self.stub_log.read_text(), "")
                self.assertEqual(self.state_files(), [])

    def test_auth_failure_names_the_recovery_step(self):
        # The exact messages the pinned CLI emits, taken from its own binary. A generic
        # "token expired" marker misses all of them ("refresh token HAS expired").
        messages = [
            "stream error: unexpected status 401 Unauthorized",
            "Your access token could not be refreshed because your refresh token has "
            "expired. Please log out and sign in again.",
            "Your access token could not be refreshed because your refresh token was "
            "already used. Please log out and sign in again.",
            "Your access token could not be refreshed because your refresh token was "
            "revoked. Please log out and sign in again.",
            "Token refresh not possible, re-authorization required.",
            "ChatGPT account ID not available, please re-run `codex login`",
        ]
        for message in messages:
            with self.subTest(message=message[:40]):
                r = self.run_review("--kind", "plan", env_extra={
                    "STUB_MODE": "auth", "STUB_AUTH_MESSAGE": message,
                })
                self.assertEqual(r.returncode, 1)
                self.assertIn("codex login", r.stderr)
                self.assertEqual(self.state_files(), [])

    def test_missing_codex_binary_reports_cleanly(self):
        r = self.run_review("--kind", "plan",
                            env_extra={"CODEX_BIN": str(self.tmp / "not-installed")})
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("could not run the codex binary", r.stderr)
        self.assert_ends_with_version_note(r.stderr, "codex CLI version: unknown")
        self.assertEqual(self.state_files(), [])

    def test_codex_stdin_is_closed_so_an_inherited_open_pipe_cannot_block_it(self):
        # codex-cli 0.153.3 reads a non-terminal stdin to EOF before its first turn and
        # appends it to the prompt. An agent's background task hands this wrapper a pipe
        # that is never closed, so a codex that inherits it blocks until the timeout with
        # no output, and the round is reported as "timed out" instead of "never started".
        # The wrapper must therefore give codex a closed stdin, whatever it inherited.
        read_end, write_end = os.pipe()
        self.addCleanup(os.close, write_end)  # held open for the whole run, like the harness
        try:
            r = self.run_review("--kind", "plan", stdin=read_end, env_extra={
                "STUB_MODE": "stdin", "CODEX_REVIEW_TIMEOUT": "5"})
        finally:
            os.close(read_end)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("STUB REVIEW FINDINGS", self.expected_findings().read_text())
        self.assertEqual(self.version_calls(), "")  # a clean round never asks for the version
        # Closed, not fed: a stdin=PIPE that was written to and closed would also not hang,
        # but real codex would append those bytes to the prompt.
        self.assertEqual(Path(str(self.stub_log) + ".stdin").read_bytes(), b"")

    def test_timeout_with_no_output_names_a_startup_problem(self):
        # A working review emits JSON events within seconds; a child that produced nothing
        # by the deadline was blocked at startup (stdin, auth prompt, sandbox), not slow.
        # The message must say so, or the next move is to raise the timeout and wait again.
        r = self.run_review("--kind", "plan", env_extra={
            "STUB_MODE": "hang", "CODEX_REVIEW_TIMEOUT": "2"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("timed out after 2s", r.stderr)
        self.assertIn("no output at all", r.stderr)
        self.assertIn("not a slow review", r.stderr)
        self.assertIn("Reading additional input from stdin", r.stderr)  # child stderr surfaced
        self.assert_ends_with_version_note(r.stderr)
        self.assertNotIn("still working", r.stderr)
        self.assertEqual(self.state_files(), [])
        # The child was reaped and its run marker removed, not abandoned behind the message.
        self.assertFalse(self.stub_pid_alive())
        self.assertEqual(self.run_markers(), [])

    def test_timeout_mid_review_reports_the_progress_made(self):
        r = self.run_review("--kind", "plan", env_extra={
            "STUB_MODE": "slow", "CODEX_REVIEW_TIMEOUT": "2"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("timed out after 2s", r.stderr)
        self.assertIn("still working", r.stderr)
        self.assertIn("3 events", r.stderr)
        self.assertIn("item.started", r.stderr)
        self.assertIn("CODEX_REVIEW_TIMEOUT", r.stderr)
        self.assertNotIn("no output at all", r.stderr)
        self.assert_ends_with_version_note(r.stderr)
        self.assertFalse(self.stub_pid_alive())
        self.assertEqual(self.state_files(), [])

    def test_timeout_keeps_partial_output_when_a_descendant_holds_the_pipes(self):
        # After the direct child is killed a grandchild can keep stdout/stderr open, so the
        # retried communicate() times out too. The evidence read so far must still be used.
        r = self.run_review("--kind", "plan", env_extra={
            "STUB_MODE": "orphan", "CODEX_REVIEW_TIMEOUT": "2"})
        orphan = int(Path(str(self.stub_log) + ".orphan").read_text())

        def kill_orphan():
            try:
                os.kill(orphan, 9)
            except ProcessLookupError:
                pass
        self.addCleanup(kill_orphan)  # the fixture's descendant must not outlive the test
        self.assertEqual(r.returncode, 1)
        self.assertIn("timed out after 2s", r.stderr)
        self.assertIn("still working", r.stderr)
        self.assertIn("1 event received", r.stderr)
        self.assertIn("thread.started", r.stderr)
        self.assert_ends_with_version_note(r.stderr)
        self.assertFalse(self.stub_pid_alive())
        self.assertEqual(self.run_markers(), [])
        self.assertEqual(self.state_files(), [])

    def test_timeout_with_non_json_output_is_reported_as_such(self):
        r = self.run_review("--kind", "plan", env_extra={
            "STUB_MODE": "garbage", "CODEX_REVIEW_TIMEOUT": "2"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("timed out after 2s", r.stderr)
        self.assertIn("not --json events", r.stderr)
        self.assertIn("not a json event", r.stderr)
        self.assertNotIn("still working", r.stderr)
        self.assertNotIn("no output at all", r.stderr)
        self.assert_ends_with_version_note(r.stderr)
        self.assertFalse(self.stub_pid_alive())
        self.assertEqual(self.state_files(), [])

    def test_failure_messages_name_the_codex_cli_version(self):
        # A break that comes from a CLI change (0.153.3's stdin read was one) is invisible
        # unless the failure names the CLI version and whether it is the one validated.
        validated = _runner_module().VALIDATED_CODEX_CLI
        r = self.run_review("--kind", "plan", env_extra={"STUB_MODE": "fail"})
        self.assertEqual(r.returncode, 1)
        self.assert_ends_with_version_note(r.stderr)
        self.assertIn(f"last validated with codex-cli {validated}", r.stderr)
        self.assertIn("likely cause", r.stderr)
        self.assertEqual(self.version_calls().strip(), "--version")
        # An empty review is a failure too, and ends the same way.
        r = self.run_review("--kind", "plan", env_extra={"STUB_MODE": "empty"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("empty review", r.stderr)
        self.assert_ends_with_version_note(r.stderr)
        # A --version reply that is not UTF-8 must not replace the failure with a traceback.
        r = self.run_review("--kind", "plan", env_extra={
            "STUB_MODE": "fail", "STUB_VERSION_BYTES": "1"})
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("codex exited 2", r.stderr)
        self.assert_ends_with_version_note(r.stderr, "codex-cli \ufffd0.0.0")
        r = self.run_review("--kind", "plan", env_extra={
            "STUB_MODE": "fail", "STUB_VERSION": f"codex-cli {validated}"})
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"codex-cli {validated}", r.stderr)
        self.assertNotIn("likely cause", r.stderr)
        # --version failing must not mask the real failure.
        r = self.run_review("--kind", "plan", env_extra={
            "STUB_MODE": "fail", "STUB_VERSION_FAIL": "1"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("stub: simulated failure", r.stderr)
        self.assertIn("codex exited 2", r.stderr)
        self.assert_ends_with_version_note(r.stderr, "codex CLI version: unknown")

    def test_malformed_state_is_refused_and_never_blocks_other_topics(self):
        self.state_dir.mkdir(parents=True)
        # A list where an object belongs used to raise AttributeError inside
        # prune_stale(), which runs on every review and so wedged every topic.
        (self.state_dir / "deadbeef0000-bogus.json").write_text("[1, 2, 3]")
        (self.state_dir / "deadbeef0000-nolast.json").write_text('{"last_used": "soon"}')
        r = self.run_review("--kind", "plan")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("## Round 1", self.expected_findings().read_text())

    def test_state_missing_rounds_is_refused_before_spending_a_round(self):
        self.assertEqual(self.run_review("--kind", "plan").returncode, 0)
        spath = [p for p in self.state_files() if p.name.endswith("-thing.json")][0]
        state = json.loads(spath.read_text())
        del state["rounds"]
        spath.write_text(json.dumps(state))
        self.stub_log.write_text("")
        r = self.run_review("--kind", "fix-round", "--ask", "fixed")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no rounds list", r.stderr)
        self.assertEqual(self.stub_log.read_text(), "")

    def test_state_that_is_a_list_is_refused_before_spending_a_round(self):
        self.state_dir.mkdir(parents=True)
        r = self.run_review("--kind", "plan")
        self.assertEqual(r.returncode, 0, r.stderr)
        spath = [p for p in self.state_files() if p.name.endswith("-thing.json")][0]
        spath.write_text("[1, 2, 3]")
        self.stub_log.write_text("")
        r = self.run_review("--kind", "plan")
        self.assertEqual(r.returncode, 1)
        self.assertIn("expected a JSON object", r.stderr)
        self.assertEqual(self.stub_log.read_text(), "")

    def test_model_and_effort_env_override(self):
        # env vars select the reviewer when no CLI flag is given.
        r = self.run_review("--kind", "plan", env_extra={
            "CODEX_REVIEW_MODEL": "cheap-model",
            "CODEX_REVIEW_EFFORT": "low",
        })
        self.assertEqual(r.returncode, 0, r.stderr)
        argv = self.stub_log.read_text().splitlines()
        self.assertIn("cheap-model", argv)
        self.assertIn('model_reasoning_effort="low"', argv)
        self.assertNotIn("gpt-5.6-sol", argv)
        self.assertNotIn('model_reasoning_effort="xhigh"', argv)

    def test_cli_model_effort_beats_env(self):
        # Precedence: CLI > env > default.
        r = self.run_review("--kind", "plan", "--model", "cli-model", "--effort", "high",
                            env_extra={
                                "CODEX_REVIEW_MODEL": "env-model",
                                "CODEX_REVIEW_EFFORT": "low",
                            })
        self.assertEqual(r.returncode, 0, r.stderr)
        argv = self.stub_log.read_text().splitlines()
        self.assertIn("cli-model", argv)
        self.assertIn('model_reasoning_effort="high"', argv)
        self.assertNotIn("env-model", argv)

    def test_invalid_effort_fails_before_codex(self):
        r = self.run_review("--kind", "plan", "--effort", "bogus")
        self.assertEqual(r.returncode, 1)
        self.assertIn("invalid effort", r.stderr)
        self.assertEqual(self.stub_log.read_text(), "")  # never reached codex

    def test_usage_line_prints_and_findings_path_stays_last(self):
        # The stub emits usage on turn.completed; the summary prints but the LAST stdout
        # line remains the findings path (the skill/agent reads that line).
        r = self.run_review("--kind", "plan", env_extra={
            "STUB_USAGE": '{"input_tokens":1200,"output_tokens":340}',
        })
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.strip().splitlines()
        self.assertTrue(any(l.startswith("usage:") for l in lines), r.stdout)
        self.assertIn("1.2k in", r.stdout)
        state = json.loads(self.state_files()[0].read_text())
        self.assertEqual(lines[-1], state["findings_file"])

    def test_usage_flag_prints_detail(self):
        r = self.run_review("--kind", "plan", "--usage", "json", env_extra={
            "STUB_USAGE": '{"input_tokens":1200,"output_tokens":340}',
        })
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('"input_tokens": 1200', r.stdout)


if __name__ == "__main__":
    unittest.main()
