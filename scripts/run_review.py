#!/usr/bin/env python3
"""Run one round of an external Codex code review.

Drives `codex exec` / `codex exec resume` in a read-only sandbox, keeps one Codex thread
per review topic (so fix-rounds re-verify earlier findings against the current tree), and
appends each round's findings to a Markdown file. Standard library only.

Subcommands: `limits` (subscription usage, no spend), `runs` (in-flight reviews),
`kill <pid|topic>` (stop one).
"""
import argparse
import atexit
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

KINDS = ("plan", "design", "implementation", "fix-round")
PRUNE_DAYS = 30

# The DEFAULT reviewer + effort. Heavier reasoning gives a better review, so this is the
# documented default; both are selectable per run (--model/--effort, or the
# CODEX_REVIEW_MODEL / CODEX_REVIEW_EFFORT env fallback). Precedence: CLI > env > default.
DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_EFFORT = "xhigh"
# The reasoning-effort values codex accepts; validated before the (paid) call so a typo
# fails fast rather than burning a round.
EFFORTS = ("minimal", "low", "medium", "high", "xhigh")

# Where a review's findings file is written, relative to --cwd (override with --out-dir or
# the CODEX_REVIEW_OUT_DIR env var).
DEFAULT_OUT_DIR = ".codex-review/reviews"


def resolve_model_effort(cli_model, cli_effort):
    """CLI > env > default, with effort validated against EFFORTS. Exits on a bad value."""
    model = (cli_model or env("CODEX_REVIEW_MODEL", "") or DEFAULT_MODEL).strip()
    effort = (cli_effort or env("CODEX_REVIEW_EFFORT", "") or DEFAULT_EFFORT).strip()
    if not model:
        sys.exit("--model must not be blank")
    if effort not in EFFORTS:
        sys.exit("invalid effort %r; choose one of: %s" % (effort, ", ".join(EFFORTS)))
    return model, effort


# --------------------------------------------------------------------------
# Token-usage capture + formatting.
#
# A review is a real credit spend, so both the operator and any driving agent want a spend
# signal. Codex reports usage on its --json event stream; the exact shape has shifted
# across versions (a top-level `usage` object; a `total_token_usage`/`last_token_usage`
# pair on a token-count event), so extract_usage is deliberately shape-tolerant and returns
# None (callers degrade gracefully) when the stream carried none.
_USAGE_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens")
_USAGE_CONTAINER_KEYS = ("total_token_usage", "usage", "token_usage", "last_token_usage")
_USAGE_NESTED_KEYS = ("info", "turn")


def _coerce_usage(obj):
    if not isinstance(obj, dict):
        return None
    picked = {}
    for key in _USAGE_KEYS:
        value = obj.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value >= 0:
            picked[key] = value
    return picked or None


def _usage_from_event(event):
    if not isinstance(event, dict):
        return None
    for key in _USAGE_CONTAINER_KEYS:
        found = _coerce_usage(event.get(key))
        if found:
            return found
    for wrapper in _USAGE_NESTED_KEYS:
        inner = event.get(wrapper)
        if isinstance(inner, dict):
            for key in _USAGE_CONTAINER_KEYS:
                found = _coerce_usage(inner.get(key))
                if found:
                    return found
    return None


def extract_usage(events):
    """Best-effort cumulative token usage from a parsed codex --json event stream.

    Returns a dict of int token fields from the LAST usage-bearing event (codex reports
    running totals), or None when the stream carried none. Callers degrade gracefully.
    """
    latest = None
    for event in events:
        found = _usage_from_event(event)
        if found:
            latest = found
    return latest


def _human(n):
    if not isinstance(n, int):
        return str(n)
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return ("%.1fk" % (n / 1000)).replace(".0k", "k")
    return ("%.1fM" % (n / 1_000_000)).replace(".0M", "M")


def format_usage(usage):
    """One-line human summary, always safe to print. None -> a 'not reported' line."""
    if not usage:
        return "usage: not reported by codex for this round"
    inp = usage.get("input_tokens", 0)
    out = usage.get("output_tokens", 0)
    cached = usage.get("cached_input_tokens", 0)
    total = inp + out
    line = "usage: %s in / %s out (%s total)" % (_human(inp), _human(out), _human(total))
    if cached:
        line += " [%s cached]" % _human(cached)
    return line


def format_usage_detail(usage, as_json=False):
    """Fuller per-field breakdown for the --usage flag. as_json returns a JSON string."""
    if as_json:
        return json.dumps(usage or {}, sort_keys=True)
    if not usage:
        return "usage detail: none reported"
    rows = []
    for key in _USAGE_KEYS:
        if key in usage:
            rows.append("  %-20s %d" % (key, usage[key]))
    inp = usage.get("input_tokens", 0)
    out = usage.get("output_tokens", 0)
    rows.append("  %-20s %d" % ("total_tokens", inp + out))
    return "usage detail:\n" + "\n".join(rows)


def _print_usage(usage, detail_mode):
    """Print the always-on one-line usage summary, plus a breakdown if --usage was given.

    Printed to stdout BEFORE the findings-path line so the final stdout line stays the
    findings path (the skill/agent reads that last line). Never raises: usage reporting
    must not change a review's outcome.
    """
    print(format_usage(usage))
    if detail_mode:
        print(format_usage_detail(usage, as_json=(detail_mode == "json")))

# Matched with fullmatch: `$` alone would accept a trailing newline and put it in a filename.
TOPIC_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
TOPIC_MAX = 64

# Substrings that mark a Codex failure as an authentication failure rather than a generic
# one, so the exit message can name the recovery step (codex login). Taken from the CLI's
# own strings, which say "refresh token has expired" and "re-authorization required"
# rather than anything a generic "token expired" marker would catch.
AUTH_MARKERS = ("refresh token", "could not be refreshed", "re-authorization required",
                "reauthentication required", "reauthorization required", "sign in again",
                "not signed in", "not logged in", "codex login", "unauthorized", "401",
                "authentication", "auth error", "invalid api key", "please log in")


def env(name, default):
    return os.environ.get(name, default)


def usable(value):
    """True for a non-blank string, the only shape a thread id or path may take."""
    return isinstance(value, str) and bool(value.strip())


def state_dir():
    default = Path.home() / ".config" / "codex-review" / "state"
    return Path(env("CODEX_REVIEW_STATE_DIR", str(default)))


def _ensure_private_dir(directory):
    """Create `directory` owner-only if absent. NEVER alters an EXISTING directory's
    permissions (it may be a shared location the user deliberately chose)."""
    existed = directory.exists()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    if not existed:
        try:
            os.chmod(directory, 0o700)  # best-effort; POSIX only
        except OSError:
            pass


def _write_private_text(path, text):
    """Write `text` to `path` created owner-only at open time (0600), overwriting. Refuses
    to follow a symlink at the path (O_NOFOLLOW where supported), so a pre-planted symlink
    in a shared state directory can't redirect the write to another file."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path), flags, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)


# --------------------------------------------------------------------------
# In-flight run registry + process guard.
#
# A review round is a long, PAID run (tens of minutes to hours at xhigh). To make sure a
# backgrounded round is never an invisible runaway, each run drops a marker file while it
# is live (removed on completion), so `runs` can list what is in flight and `kill` can stop
# one. A signal handler + atexit tears down the codex child if this process is asked to
# stop (e.g. a background task is cancelled), so stopping the wrapper stops the spend.
_TRACKED_CHILD_PIDS = set()


def _runs_dir():
    return state_dir() / "runs"


def _run_marker_path():
    return _runs_dir() / ("%d.json" % os.getpid())


def _write_run_marker(info):
    try:
        _ensure_private_dir(_runs_dir())
        path = _run_marker_path()
        _write_private_text(path, json.dumps(info))
        return path
    except OSError:
        return None


def _remove_run_marker(path):
    if path:
        try:
            path.unlink()
        except OSError:
            pass


def _pid_alive(pid):
    """Non-destructive liveness check. On POSIX, signal 0 probes without delivering; on
    Windows os.kill would call TerminateProcess even for signal 0, so use OpenProcess."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive, just not ours to signal
    return True


def _pid_alive_windows(pid):
    import ctypes
    from ctypes import wintypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return code.value == STILL_ACTIVE
        return False
    finally:
        kernel32.CloseHandle(handle)


def _terminate_pid(pid, grace=8.0):
    """SIGTERM, wait up to `grace` seconds, then SIGKILL. Safe on an already-dead pid."""
    if not isinstance(pid, int):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.time() + grace
    while time.time() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.1)
    # SIGKILL is POSIX-only; on Windows there is no such signal, so fall back to SIGTERM
    # (which os.kill maps to TerminateProcess there).
    hard = getattr(signal, "SIGKILL", signal.SIGTERM)
    try:
        os.kill(pid, hard)
    except OSError:
        pass


def _cleanup_children(*_args):
    for pid in list(_TRACKED_CHILD_PIDS):
        _terminate_pid(pid, grace=3.0)
    _remove_run_marker(_run_marker_path())


def _install_run_cleanup():
    """Ensure a stop signal to this wrapper tears down the codex child + the marker."""
    atexit.register(_cleanup_children)

    def _handler(_signum, _frame):
        _cleanup_children()
        raise SystemExit(130)

    # Feature-detect: SIGHUP is POSIX-only (absent on Windows); install what exists.
    names = ("SIGTERM", "SIGINT", "SIGHUP")
    for name in names:
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError, AttributeError):
            pass


def git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def sanitize_origin(url):
    """Strip any userinfo (``user:token@``) from a remote URL so a credential embedded in
    the origin (e.g. ``https://user:TOKEN@host/repo``) never reaches the state file. The
    scp-style ``git@host:owner/repo`` form (no scheme; ``git`` is not a secret) is left
    intact, as are plain URLs."""
    m = re.match(r'^([a-zA-Z][a-zA-Z0-9+.-]*://)([^/@]*@)(.*)$', url)
    return (m.group(1) + m.group(3)) if m else url


def state_path(origin, branch, topic):
    key = hashlib.sha256(f"{origin}\n{branch}".encode()).hexdigest()[:12]
    return state_dir() / f"{key}-{topic}.json"


def save_state(path, state):
    # State records the (credential-stripped) origin and thread id. Create the dir owner-
    # only if we make it; never re-permission a dir the user pointed us at. The temp file
    # is created 0600 at open time.
    _ensure_private_dir(path.parent)
    tmp = path.with_suffix(".json.tmp")
    _write_private_text(tmp, json.dumps(state, indent=2))
    os.replace(tmp, path)


def load_state(path):
    """Read and validate one state file, or None when it does not exist.

    Every field the run later depends on is checked here, before any Codex call, so a
    malformed file is a clear message instead of an AttributeError or a KeyError raised
    after a paid round has already been spent.
    """
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        sys.exit(f"state file {path} could not be read ({exc}); "
                 "delete it and start a new review with --kind plan|design|implementation")
    if not isinstance(state, dict):
        problems = [f"expected a JSON object, found {type(state).__name__}"]
    else:
        problems = []
        if not usable(state.get("thread_id")):
            problems.append("no usable thread id")
        if not usable(state.get("findings_file")):
            problems.append("no usable findings path")
        if not isinstance(state.get("rounds"), list):
            problems.append("no rounds list")
    if problems:
        sys.exit(f"state file {path} is unusable ({'; '.join(problems)}); "
                 "delete it and start a new review with --kind plan|design|implementation")
    return state


def prune_stale():
    d = state_dir()
    if not d.exists():
        return
    cutoff = time.time() - PRUNE_DAYS * 86400
    for f in d.glob("*.json"):
        # Every failure here skips the file rather than raising: pruning runs on every
        # review, so one malformed entry must never block an unrelated topic's round.
        try:
            data = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        last_used = data.get("last_used", 0)
        if not isinstance(last_used, (int, float)) or isinstance(last_used, bool):
            continue
        if last_used < cutoff:
            try:
                f.unlink()
            except OSError:
                continue


def render(kind, topic, docs, ask):
    template = Path(__file__).resolve().parent.parent / "references" / f"{kind}.md"
    text = template.read_text()
    return (
        text.replace("{{TOPIC}}", topic)
        .replace("{{DOCS}}", "\n".join(f"- {d}" for d in docs))
        .replace("{{ASK}}", ask or "None.")
    )


def topic_slug(value):
    """argparse type for --topic. Rejects before any Codex call, so a bad slug
    cannot burn a paid round and then fail on an unopenable path."""
    if len(value) > TOPIC_MAX or not TOPIC_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(
            f"topic must be a kebab-case slug matching {TOPIC_RE.pattern} "
            f"and at most {TOPIC_MAX} characters; got {value!r}"
        )
    return value


def findings_path(cwd, topic, out_dir=DEFAULT_OUT_DIR):
    reviews = cwd / out_dir
    reviews.mkdir(parents=True, exist_ok=True)
    return reviews / f"{date.today().isoformat()}-{topic}-codex-review.md"


# The codex-cli version this tool was last exercised against end to end. The model is
# pinned above; the CLI is whatever is installed, and a CLI change is the usual cause of a
# break no flag explains (0.153.3 began reading a non-terminal stdin before its first turn).
# Every failure message names the running version and whether it is this one.
VALIDATED_CODEX_CLI = "0.153.3"


def codex_cli_version(codex_bin):
    """`codex --version` as the CLI prints it (``codex-cli 0.153.3``), or None."""
    try:
        cp = subprocess.run([codex_bin, "--version"], stdin=subprocess.DEVNULL,
                            capture_output=True, encoding="utf-8", errors="replace",
                            timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = (cp.stdout or "").strip().splitlines()
    if cp.returncode != 0 or not lines:
        return None
    return lines[0].strip()


def _version_note(codex_bin):
    """One line for failure messages: the CLI in use and whether it is the validated one."""
    version = codex_cli_version(codex_bin)
    if version is None:
        return (f"codex CLI version: unknown (`{codex_bin} --version` failed); this tool was "
                f"last validated with codex-cli {VALIDATED_CODEX_CLI}")
    if version.split()[-1] == VALIDATED_CODEX_CLI:
        return f"{version} (the version this tool was last validated with)"
    return (f"{version}; this tool was last validated with codex-cli {VALIDATED_CODEX_CLI}, "
            "so a CLI behaviour change is a likely cause")


def _parse_events(out):
    """The JSON events codex --json wrote to stdout: (events, thread_id, stream_errors)."""
    events, thread_id, stream_errors = [], None, []
    for line in out.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        events.append(event)
        etype = event.get("type")
        if etype == "thread.started":
            thread_id = event.get("thread_id")
        elif etype == "error":
            msg = event.get("message")
            if msg:
                stream_errors.append(str(msg))
        elif etype == "turn.failed":
            err = event.get("error")
            msg = err.get("message") if isinstance(err, dict) else None
            stream_errors.append(str(msg) if msg else "turn failed")
    return events, thread_id, stream_errors


def _partial_text(data):
    """Partial stream output off a TimeoutExpired: bytes even in text mode, or None."""
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data


def _timeout_message(timeout, out, stderr_text, codex_bin):
    """Say what the timeout means. "Timed out" alone reads as "slow review", and the fix
    for a slow review (a longer timeout, a lower effort) is exactly wrong for a child that
    never started: a working review emits JSON events within seconds, so no output at all
    by the deadline is a startup or input problem, not a long one. Like every failure
    message, it ends with the codex-cli version note."""
    out, stderr_text = out or "", stderr_text or ""
    lines = [f"codex timed out after {timeout}s; no state was changed"]
    if not out.strip():
        lines.append(
            "codex produced no output at all in that time. A working review emits JSON "
            "events within seconds, so this is a startup or input problem (codex waiting "
            "on stdin or a prompt, or failing to set up its sandbox), not a slow review: "
            "raising CODEX_REVIEW_TIMEOUT would only wait longer for the same hang.")
    else:
        events, _thread_id, _errors = _parse_events(out)
        if events:
            count = f"{len(events)} event{'s' if len(events) != 1 else ''} received"
            lines.append(
                f"codex was still working: {count}, the last of type "
                f"{events[-1].get('type')!r}. The review is genuinely longer than this "
                f"timeout; raise CODEX_REVIEW_TIMEOUT (now {timeout}) or lower --effort.")
        else:
            lines.append(
                f"codex wrote {len(out)} characters that are not --json events before the "
                "deadline, so it never reached a turn; the output starts: "
                f"{out.strip()[:200]!r}")
    if stderr_text.strip():
        lines.append("codex stderr:\n" + stderr_text.rstrip())
    lines.append(_version_note(codex_bin))  # the last line of every failure, by contract
    return "\n".join(lines)


def run_codex(cmd_prefix, prompt, cwd, timeout, model, effort, topic=None):
    with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as f:
        out_file = f.name
    cmd = cmd_prefix + [
        "--json",
        "-m", model,
        "-c", f'model_reasoning_effort="{effort}"',
        "-c", 'sandbox_mode="read-only"',
        "-o", out_file,
        prompt,
    ]
    # Popen (not subprocess.run) so the codex child is tracked in the run registry and can
    # be stopped by `kill` or by a stop signal to this wrapper (see _install_run_cleanup).
    # codex stays in this process's group, so a foreground timeout that group-kills the
    # wrapper still takes codex with it (no orphan).
    # stdin=DEVNULL is load-bearing, not hygiene: codex-cli 0.153.3 reads a non-terminal
    # stdin to EOF before its first turn and appends it to the prompt ("Reading additional
    # input from stdin..."). Inherited from an agent's background task, that stdin is a pipe
    # nobody closes, so codex blocked with no output until the timeout below and the round
    # read as "timed out" rather than "never started". A closed stdin ends the whole class:
    # no present or future codex flag can wait on input the child does not have.
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=str(cwd)
        )
    except (FileNotFoundError, PermissionError) as exc:
        os.unlink(out_file)
        sys.exit(f"could not run the codex binary {cmd[0]!r} ({exc.strerror}); "
                 "install the Codex CLI or set CODEX_BIN; no state was changed\n"
                 + _version_note(cmd[0]))
    now = time.time()
    marker = _write_run_marker({
        "mode": "inspect", "pid": os.getpid(), "codex_pid": proc.pid,
        "topic": topic, "cwd": str(cwd), "model": model, "effort": effort,
        "started": now, "deadline": now + timeout,
    })
    _TRACKED_CHILD_PIDS.add(proc.pid)
    try:
        out, stderr_text = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_pid(proc.pid)
        try:
            # Retrying communicate() after a timeout keeps everything read so far, which is
            # the evidence the message below is built from.
            out, stderr_text = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired as still_open:
            # A descendant of codex still holds the pipes after the child itself was
            # stopped. The exception carries everything read so far; use that.
            out = _partial_text(still_open.output)
            stderr_text = _partial_text(still_open.stderr)
            try:
                # Reap the stopped child itself; the pipes it left behind can stay open.
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        os.unlink(out_file)
        sys.exit(_timeout_message(timeout, out, stderr_text, cmd[0]))
    finally:
        _TRACKED_CHILD_PIDS.discard(proc.pid)
        _remove_run_marker(marker)
    returncode = proc.returncode
    events, thread_id, stream_errors = _parse_events(out)
    if returncode != 0:
        sys.stderr.write(stderr_text)
        os.unlink(out_file)
        message = f"codex exited {returncode}; no state was changed"
        # codex emits the REAL cause as JSON events on STDOUT (we pass --json); stderr in a
        # failure carries only "Reading additional input from stdin..." plus an unrelated
        # models_cache warning, so the actual error is invisible unless we surface these.
        if stream_errors:
            message += "\ncodex reported: " + "; ".join(dict.fromkeys(stream_errors))
        haystack = (stderr_text + "\n" + "\n".join(stream_errors)).lower()
        if any(marker in haystack for marker in AUTH_MARKERS):
            message += ("\ncodex authentication looks expired or revoked: "
                        "run `codex login` and retry this round")
        sys.exit(message + "\n" + _version_note(cmd[0]))
    review = Path(out_file).read_text()
    os.unlink(out_file)
    if not review.strip():
        sys.exit("codex returned an empty review; no state was changed\n"
                 + _version_note(cmd[0]))
    return thread_id, review, extract_usage(events)


# --------------------------------------------------------------------------
# Subscription limits (standalone `limits` subcommand; NOT part of a review run).
#
# codex records a rate-limit snapshot on every API response and persists it in its session
# rollout logs (CODEX_HOME/sessions/**/rollout-*.jsonl, `token_count` events, under
# payload.rate_limits). This reads the most recent snapshot straight off disk -- ZERO token
# spend, NO network -- so you can see how much of the plan is used and whether a limit is
# near or already hit, WITHOUT running a review. It is only as fresh as your last codex
# activity; the output states the snapshot's timestamp. (Same data codex's own /status shows.)

USAGE_WARN_PCT = 80.0  # at/above this used_percent in any window -> NEAR


def _codex_home():
    return Path(env("CODEX_HOME", str(Path.home() / ".codex")))


def latest_rate_limits(codex_home, scan_files=8):
    """The most recent persisted rate-limit snapshot + its timestamp, or (None, None).

    Scans the newest rollout logs (newest file first by mtime) and, within a file, takes
    the LAST token_count event carrying a rate_limits block.
    """
    sessions = Path(codex_home) / "sessions"
    if not sessions.is_dir():
        return None, None
    files = sorted(sessions.rglob("rollout-*.jsonl"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files[:scan_files]:
        found = None
        try:
            with path.open() as fh:
                for line in fh:
                    if '"rate_limits"' not in line:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    payload = event.get("payload") if isinstance(event, dict) else None
                    if not isinstance(payload, dict):
                        continue
                    rl = payload.get("rate_limits")
                    if isinstance(rl, dict) and (
                            isinstance(rl.get("primary"), dict)
                            or isinstance(rl.get("secondary"), dict)
                            or rl.get("plan_type") is not None):
                        found = (rl, event.get("timestamp"))
        except OSError:
            continue
        if found:
            return found
    return None, None


def _usage_windows(rate_limits):
    """[(label, used_percent, window_minutes, resets_at)] for each present window."""
    out = []
    for key in ("primary", "secondary"):
        w = rate_limits.get(key)
        if isinstance(w, dict) and isinstance(w.get("used_percent"), (int, float)) \
                and not isinstance(w.get("used_percent"), bool):
            out.append((key, float(w["used_percent"]),
                        w.get("window_minutes"), w.get("resets_at")))
    return out


def usage_status(rate_limits):
    """'REACHED' | 'NEAR' | 'OK' from a rate_limits snapshot."""
    if rate_limits.get("rate_limit_reached_type") or rate_limits.get("spend_control_reached"):
        return "REACHED"
    windows = _usage_windows(rate_limits)
    if any(pct >= 100 for _, pct, _, _ in windows):
        return "REACHED"
    if any(pct >= USAGE_WARN_PCT for _, pct, _, _ in windows):
        return "NEAR"
    return "OK"


def _fmt_window(minutes):
    if not isinstance(minutes, (int, float)) or isinstance(minutes, bool):
        return "window"
    m = int(minutes)
    if m and m % 1440 == 0:
        return "%dd" % (m // 1440)
    if m and m % 60 == 0:
        return "%dh" % (m // 60)
    return "%dm" % m


def _fmt_reset(resets_at, now):
    if not isinstance(resets_at, (int, float)) or isinstance(resets_at, bool):
        return "unknown"
    secs = int(resets_at - now)
    if secs <= 0:
        return "now"
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    parts = []
    if days:
        parts.append("%dd" % days)
    if hours:
        parts.append("%dh" % hours)
    if not days and mins:
        parts.append("%dm" % mins)
    return "in " + " ".join(parts) if parts else "soon"


def format_account_usage(rate_limits, ts, now):
    status = usage_status(rate_limits)
    plan = rate_limits.get("plan_type") or "unknown"
    lines = ["codex subscription limits  [%s]" % status, "  plan: %s" % plan]
    windows = _usage_windows(rate_limits)
    for key, pct, wmin, resets in windows:
        lines.append("  %-9s %5.1f%% used of the %s window (resets %s)"
                     % (key + ":", pct, _fmt_window(wmin), _fmt_reset(resets, now)))
    if not windows:
        lines.append("  (the snapshot carried no window percentages)")
    credits = rate_limits.get("credits")
    if isinstance(credits, dict):
        if credits.get("unlimited"):
            lines.append("  credits: unlimited")
        elif credits.get("has_credits"):
            lines.append("  credits: balance %s" % credits.get("balance"))
    reached = rate_limits.get("rate_limit_reached_type")
    if reached:
        lines.append("  limit hit: %s" % reached)
    lines.append("  as of: %s (your last codex activity)" % (ts or "unknown"))
    if status == "NEAR":
        lines.append("  ! nearing the limit (>= %d%% used in a window)" % int(USAGE_WARN_PCT))
    elif status == "REACHED":
        lines.append("  !! limit reached: new requests may be refused until the window resets")
    return "\n".join(lines)


def _limits_subcommand(argv):
    """Standalone `limits`: print subscription usage from the last snapshot (no spend)."""
    sub = argparse.ArgumentParser(
        prog="run_review.py limits",
        description="Show codex subscription usage from the last persisted rate-limit "
                    "snapshot. No token spend, no network. Flags NEAR / REACHED.")
    sub.add_argument("--json", action="store_true", help="machine-readable output")
    sub.add_argument("--strict", action="store_true",
                     help="exit non-zero on NEAR as well as REACHED (for gating a run)")
    ns = sub.parse_args(argv)
    rate_limits, ts = latest_rate_limits(_codex_home())
    if rate_limits is None:
        if ns.json:
            print(json.dumps({"status": "UNKNOWN", "reason": "no snapshot found"}))
        else:
            print("no codex usage snapshot found under %s; run any codex command first, "
                  "then re-check" % (_codex_home() / "sessions"), file=sys.stderr)
        return 3
    status = usage_status(rate_limits)
    if ns.json:
        print(json.dumps({"status": status, "as_of": ts, "rate_limits": rate_limits},
                         sort_keys=True))
    else:
        print(format_account_usage(rate_limits, ts, time.time()))
    if status == "REACHED":
        return 1
    if status == "NEAR" and ns.strict:
        return 1
    return 0


# No legitimate review outlives its own timeout; a marker older than this ceiling is a
# crash leftover. Ignoring it bounds the window in which a reused PID could match a stale
# marker (`runs`/`kill` never act on a marker past this age).
# Grace past a run's own deadline (started + CODEX_REVIEW_TIMEOUT) before its marker is
# treated as a crash leftover. Bounds the window in which a reused PID could match a stale
# marker, WITHOUT pruning a legitimately long run (staleness derives from the run's own
# timeout, not a fixed ceiling). A marker with no/garbage deadline falls back to this.
_STALE_GRACE = 3600


def _finite(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x \
        and x not in (float("inf"), float("-inf"))


def _marker_stale(info, now):
    deadline = info.get("deadline")
    if _finite(deadline):
        return now > deadline + _STALE_GRACE
    started = info.get("started")
    if _finite(started):
        return now > started + 6 * 3600 + _STALE_GRACE  # legacy markers without a deadline
    return True  # no usable timestamp -> not trustworthy; treat as stale


def _live_run_markers(prune=True):
    """[(path, info)] for markers whose process is still alive; dead or stale ones pruned."""
    live = []
    d = _runs_dir()
    if not d.is_dir():
        return live
    now = time.time()
    for path in sorted(d.glob("*.json")):
        try:
            info = json.loads(path.read_text())
        except (OSError, ValueError):
            info = None
        if not isinstance(info, dict):
            # Corrupt/hostile marker (not a JSON object): prune it rather than let it wedge
            # `runs`/`kill` for every other run.
            if prune:
                try:
                    path.unlink()
                except OSError:
                    pass
            continue
        alive = (not _marker_stale(info, now)) and (
            _pid_alive(info.get("pid")) or _pid_alive(info.get("codex_pid")))
        if not alive:
            if prune:
                try:
                    path.unlink()
                except OSError:
                    pass
            continue
        live.append((path, info))
    return live


def _process_looks_like_review(pid):
    """Identity guard for `kill`: only signal a PID whose command still names this wrapper
    (`run_review`) or the `codex` binary, so a reused PID for something unrelated is never
    signaled. FAILS CLOSED: if the command can't be read (POSIX `ps` missing/errored, or a
    platform without this check), it returns False and `kill` declines rather than risk the
    wrong process. POSIX-verified; on other platforms it always declines."""
    if not isinstance(pid, int) or pid <= 0 or os.name == "nt":
        return False
    try:
        proc = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                              capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    cmd = proc.stdout.lower()
    return "run_review" in cmd or "codex" in cmd


def _runs_subcommand(argv):
    """`runs`: list in-flight review runs on this machine (and prune dead markers)."""
    sub = argparse.ArgumentParser(
        prog="run_review.py runs",
        description="List review runs in flight (a review is a long, paid run).")
    sub.add_argument("--json", action="store_true", help="machine-readable output")
    ns = sub.parse_args(argv)
    live = _live_run_markers()
    if ns.json:
        print(json.dumps([info for _p, info in live], sort_keys=True))
        return 0
    if not live:
        print("no review runs in flight")
        return 0
    now = time.time()
    for _path, info in live:
        elapsed = int(now - info.get("started", now))
        print("pid %s  [%s]  topic=%s  model=%s  elapsed=%dm%02ds  cwd=%s"
              % (info.get("pid"), info.get("mode"), info.get("topic"),
                 info.get("model"), elapsed // 60, elapsed % 60, info.get("cwd")))
        print("  stop with: run_review.py kill %s" % info.get("pid"))
    return 0


def _kill_subcommand(argv):
    """`kill`: stop an in-flight review run by pid or topic (SIGTERM then SIGKILL)."""
    sub = argparse.ArgumentParser(
        prog="run_review.py kill",
        description="Stop an in-flight review run by pid or topic (see `runs`).")
    sub.add_argument("target", help="the pid or topic shown by `runs`")
    ns = sub.parse_args(argv)
    matched = [(p, i) for p, i in _live_run_markers(prune=False)
               if str(i.get("pid")) == ns.target or i.get("topic") == ns.target]
    if not matched:
        print("no in-flight review run matching %r; see `runs`" % ns.target,
              file=sys.stderr)
        return 3
    for path, info in matched:
        # Identity guard: only signal PIDs that still look like a codex/review process, so
        # a marker whose PID was reused by something unrelated is never terminated.
        targets = [pid for pid in (info.get("codex_pid"), info.get("pid"))
                   if isinstance(pid, int) and _process_looks_like_review(pid)]
        if not targets:
            print("run pid %s (topic %s) is no longer a review process; not signaling. "
                  "Removing its stale marker." % (info.get("pid"), info.get("topic")),
                  file=sys.stderr)
            try:
                path.unlink()
            except OSError:
                pass
            continue
        # Stop the codex child first (stops the spend), then the wrapper (whose own signal
        # handler also reaps the child; belt and suspenders).
        for pid in targets:
            _terminate_pid(pid)
        try:
            path.unlink()
        except OSError:
            pass
        print("stopped review run pid %s (topic %s)" % (info.get("pid"), info.get("topic")))
    return 0



def main():
    if len(sys.argv) > 1 and sys.argv[1] == "limits":
        raise SystemExit(_limits_subcommand(sys.argv[2:]))

    if len(sys.argv) > 1 and sys.argv[1] == "runs":
        raise SystemExit(_runs_subcommand(sys.argv[2:]))

    if len(sys.argv) > 1 and sys.argv[1] == "kill":
        raise SystemExit(_kill_subcommand(sys.argv[2:]))

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", required=True, choices=KINDS)
    parser.add_argument("--topic", required=True, type=topic_slug,
                        help="kebab-case review topic slug")
    parser.add_argument("--doc", action="append", required=True, dest="docs",
                        help="path under review, relative to --cwd (repeatable)")
    parser.add_argument("--ask", default="", help="focus ask or fix-round summary")
    parser.add_argument("--cwd", default=".", help="worktree to review in")
    parser.add_argument("--out-dir", default=None, dest="out_dir",
                        help="where findings are written, relative to --cwd (default "
                             "%s, or CODEX_REVIEW_OUT_DIR)" % DEFAULT_OUT_DIR)
    parser.add_argument("--model", default=None,
                        help="reviewer model (default %s, or CODEX_REVIEW_MODEL); the "
                             "default is heavier reasoning for a better review, but any "
                             "codex model may be selected" % DEFAULT_MODEL)
    parser.add_argument("--effort", default=None,
                        help="reasoning effort %s (default %s, or CODEX_REVIEW_EFFORT)"
                             % ("|".join(EFFORTS), DEFAULT_EFFORT))
    parser.add_argument("--usage", nargs="?", const="text", default=None,
                        choices=("text", "json"),
                        help="print a fuller token-usage breakdown (text|json); a one-line "
                             "usage summary always prints regardless")
    args = parser.parse_args()

    cwd = Path(args.cwd).resolve()
    codex = env("CODEX_BIN", "codex")
    timeout = int(env("CODEX_REVIEW_TIMEOUT", "3600"))
    args.model, args.effort = resolve_model_effort(args.model, args.effort)
    args.out_dir = args.out_dir or env("CODEX_REVIEW_OUT_DIR", DEFAULT_OUT_DIR)
    # A review is a long, paid run; make sure a stop signal to this wrapper tears down the
    # codex child + the run marker (so a cancelled background run never keeps spending).
    _install_run_cleanup()

    try:
        origin = sanitize_origin(git(cwd, "remote", "get-url", "origin"))
    except subprocess.CalledProcessError:
        origin = str(cwd)
    branch = git(cwd, "rev-parse", "--abbrev-ref", "HEAD")

    prune_stale()
    spath = state_path(origin, branch, args.topic)
    state = load_state(spath)

    if args.kind == "fix-round" and state is None:
        print(f"no review thread for topic '{args.topic}' on this branch; "
              "start a new review with --kind plan|design|implementation", file=sys.stderr)
        raise SystemExit(3)

    prompt = render(args.kind, args.topic, args.docs, args.ask)
    if state is None:
        cmd_prefix = [codex, "exec"]
    else:
        cmd_prefix = [codex, "exec", "resume", state["thread_id"]]

    thread_id, review, usage = run_codex(
        cmd_prefix, prompt, cwd, timeout, args.model, args.effort, args.topic)

    if state is None and not usable(thread_id):
        sys.stderr.write(review)
        sys.exit("codex emitted no thread.started event, so this review cannot be resumed; "
                 "the review text above was printed rather than saved, and no state was changed"
                 "\n" + _version_note(codex))

    now = time.time()
    if state is None:
        state = {
            "thread_id": thread_id,
            "origin": origin,
            "branch": branch,
            "topic": args.topic,
            "findings_file": str(findings_path(cwd, args.topic, args.out_dir)),
            "created": now,
            "rounds": [],
        }
    state["last_used"] = now
    state["rounds"].append({"ts": now, "kind": args.kind})

    findings = Path(state["findings_file"])
    round_no = len(state["rounds"])
    header = f"## Round {round_no} ({date.today().isoformat()}, {args.kind})\n\n"
    # State outlives a checkout (30 days, keyed on origin+branch+topic), so a resumed round
    # can find its stored findings directory gone (the tree was moved or cleaned). Recreate
    # the directory rather than lose an already-paid round; if the write still fails, print
    # the review instead of dropping it, and leave the state file untouched.
    try:
        findings.parent.mkdir(parents=True, exist_ok=True)
        with findings.open("a") as f:
            if round_no == 1:
                f.write(f"# Codex review: {args.topic}\n\n")
            f.write(header + review.rstrip() + "\n\n")
    except OSError as exc:
        print(f"could not write the findings file {findings} ({exc}); the review text was "
              "printed below instead, and no state was changed", file=sys.stderr)
        print(header + review.rstrip())
        raise SystemExit(1)

    save_state(spath, state)
    print(f"round {round_no} ({args.kind}) complete, thread {state['thread_id']}, "
          f"model {args.model} effort {args.effort}")
    _print_usage(usage, args.usage)
    print(findings)


if __name__ == "__main__":
    main()
