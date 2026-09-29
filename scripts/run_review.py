#!/usr/bin/env python3
"""External Codex code reviews, prompt evals, and web research.

Drives `codex exec` / `codex exec resume` in a read-only sandbox, keeps one Codex thread
per topic (so fix-rounds re-verify earlier findings against the current tree), and appends
each round to a Markdown file. Standard library only.

Subcommands: `research` (answer a question with web search), `eval-advise` and
`eval-compare` (critique a prompt, or run it and judge it blind against an existing result),
`advise` (preview model selection, no spend), `limits` (subscription usage, no spend),
`runs` (in-flight runs), `kill <pid|topic>` (stop one).
"""
import argparse
import atexit
import hashlib
import json
import os
import random
import re
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    # The installed skill ships selection.py next to this file. Tests load this file by
    # path, which puts nothing on sys.path.
    sys.path.insert(0, str(_HERE))
import selection  # noqa: E402

KINDS = ("plan", "design", "implementation", "fix-round")
PRUNE_DAYS = 30

# A review's reviewer + effort. Reviews stay pinned to these unless a flag, the
# CODEX_REVIEW_MODEL/CODEX_REVIEW_EFFORT env vars, or --model auto says otherwise.
# Precedence: CLI > env > default. Every other action selects automatically (selection.py).
DEFAULT_MODEL = selection.REVIEW_MODEL
DEFAULT_EFFORT = selection.REVIEW_EFFORT
# The effort list used when the live model catalog cannot be read.
EFFORTS = selection.STATIC_EFFORTS
# `codex debug models` spends no tokens; bound it so a hung CLI cannot stall a run.
CATALOG_TIMEOUT = 30

# Where a review's findings file is written, relative to --cwd (override with --out-dir or
# the CODEX_REVIEW_OUT_DIR env var).
DEFAULT_OUT_DIR = ".codex-review/reviews"


def resolve_model_effort(cli_model, cli_effort):
    """A review's model and effort: CLI > env > default. Validation is separate
    (validate_choice, against the live model catalog)."""
    model = (cli_model or env("CODEX_REVIEW_MODEL", "") or DEFAULT_MODEL).strip()
    effort = (cli_effort or env("CODEX_REVIEW_EFFORT", "") or DEFAULT_EFFORT).strip()
    if not model:
        _bad_args("--model must not be blank")
    return model, effort


def _bad_args(message):
    """Exit 2, the bad-arguments code, with the reason on stderr. Callers use it only before
    any paid codex call, so nothing was spent."""
    print(message, file=sys.stderr)
    raise SystemExit(2)


def load_catalog(codex_bin):
    """The live model catalog from `codex debug models` (no tokens spent), or an unavailable
    selection.Catalog when it cannot be read: an older CLI, a failure, or a timeout."""
    try:
        proc = subprocess.run([codex_bin, "debug", "models"], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=CATALOG_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        proc = None
    models = (selection.parse_catalog(proc.stdout)
              if proc is not None and proc.returncode == 0 else None)
    if models is None:
        print("codex model catalog unavailable (`codex debug models` failed); efforts are "
              "checked against the static list only", file=sys.stderr)
    return selection.Catalog(models)


def validate_choice(model, effort, catalog):
    """Exit 2 unless codex accepts this model at this effort."""
    try:
        selection.validate(model, effort, catalog)
    except selection.SelectionError as exc:
        _bad_args(str(exc))


def current_headroom():
    """selection.effective_headroom over codex's last persisted rate-limit snapshot, read off
    disk: no spend, no network."""
    rate_limits, _ts = latest_rate_limits(_codex_home())
    return selection.effective_headroom(rate_limits, time.time())


def warn_if_near_limit(model, effort):
    """A pinned review never steps down near a limit; it says so instead."""
    headroom = current_headroom()
    if headroom is not None:
        print(f"warning: codex usage is near its limit "
              f"({selection.describe_headroom(headroom, time.time())}); this review still "
              f"runs at {model} / {effort}. Pass --effort high for a lighter round.",
              file=sys.stderr)


def repo_identity(cwd):
    """(origin, branch) keying thread state. Exit 2 when cwd is not a git worktree; a .git
    directory answers `rev-parse --abbrev-ref HEAD` but is not inside a work tree."""
    try:
        inside = git(cwd, "rev-parse", "--is-inside-work-tree")
    except (subprocess.CalledProcessError, OSError):
        inside = "false"
    if inside != "true":
        _bad_args(f"{cwd} is not a git worktree; pass --cwd <worktree>")
    try:
        branch = git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    except subprocess.CalledProcessError:
        # A fresh `git init` has no HEAD commit yet, so it has no branch to key threads by.
        _bad_args(f"{cwd} has no commits yet; commit once first (threads are keyed by branch)")
    try:
        origin = sanitize_origin(git(cwd, "remote", "get-url", "origin"))
    except subprocess.CalledProcessError:
        origin = str(cwd)
    return origin, branch


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
    """Print the optional --usage breakdown, then the always-on one-line summary. The output
    path prints last, so the summary always sits directly above it. Never raises: usage
    reporting must not change a run's outcome."""
    if detail_mode:
        print(format_usage_detail(usage, as_json=(detail_mode == "json")))
    print(format_usage(usage))

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


# Per action: the state-file prefix (None keeps the review name unchanged), the default
# output directory relative to --cwd (a run's --out-dir overrides it), the file suffix, and
# the file title.
ACTIONS = {
    "review": {"state": None, "dir": DEFAULT_OUT_DIR, "suffix": "codex-review",
               "title": "Codex review"},
    "research": {"state": "research", "dir": ".codex-review/research",
                 "suffix": "codex-research", "title": "Codex research"},
    "eval": {"state": "eval", "dir": ".codex-review/evals", "suffix": "codex-eval",
             "title": "Codex eval"},
}


def state_path(origin, branch, topic, action="review"):
    key = hashlib.sha256(f"{origin}\n{branch}".encode()).hexdigest()[:12]
    prefix = ACTIONS[action]["state"]
    # "_" appears in neither a key (hex) nor a topic (kebab-case), so an action's state can
    # never collide with a review's: research "foo" is not review "research-foo".
    name = f"{key}-{topic}.json" if prefix is None else f"{key}_{prefix}-{topic}.json"
    return state_dir() / name


def save_state(path, state):
    # State records the (credential-stripped) origin and thread id. Create the dir owner-
    # only if we make it; never re-permission a dir the user pointed us at. The temp file
    # is created 0600 at open time.
    _ensure_private_dir(path.parent)
    tmp = path.with_suffix(".json.tmp")
    _write_private_text(tmp, json.dumps(state, indent=2))
    os.replace(tmp, path)


REVIEW_RESTART = "start a new review with --kind plan|design|implementation"


def load_state(path, restart=REVIEW_RESTART):
    """Read and validate one state file, or None when it does not exist.

    Every field the run later depends on is checked here, before any Codex call, so a
    malformed file is a clear message instead of an AttributeError or a KeyError raised
    after a paid round has already been spent. `restart` tells the user how to begin a new
    thread once they delete the file.
    """
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        sys.exit(f"state file {path} could not be read ({exc}); delete it and {restart}")
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
                 f"delete it and {restart}")
    return state


def preload_state(origin, branch, topic, action="review"):
    """Prune expired threads, then load this topic's state. Callers run it before any paid
    call, the router included, so a corrupt file is refused and an expired thread is dropped
    before anything is spent, not after."""
    prune_stale()
    restart = (REVIEW_RESTART if action == "review"
               else "run the same command again to start a new thread")
    return load_state(state_path(origin, branch, topic, action), restart)


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


# Prompt templates live in references/ and JSON output schemas in assets/, beside scripts/.
TEMPLATES = _HERE.parent / "references"
SCHEMAS = _HERE.parent / "assets"
_PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")


def render_text(template, values):
    """Fill {{NAME}} placeholders in ONE pass. Inserted values are never scanned again, so
    caller text containing "{{ASK}}" reaches codex verbatim; sequential str.replace calls
    would splice later values into earlier ones."""
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)], template)


def load_template(name):
    return (TEMPLATES / f"{name}.md").read_text()


def doc_list(docs):
    return "\n".join(f"- {d}" for d in docs) or "None."


def render(kind, topic, docs, ask):
    return render_text(load_template(kind),
                       {"TOPIC": topic, "DOCS": doc_list(docs), "ASK": ask or "None."})


ROUTER_TIMEOUT = 120


def fence(label, text, nonce):
    """Delimit untrusted content with a per-run nonce, so content that carries a fake end
    marker cannot close its own block."""
    return f"<<<BEGIN {label} {nonce}>>>\n{text}\n<<<END {label} {nonce}>>>"


def _first_line(code):
    text = str(code).strip() if code is not None else ""
    return text.splitlines()[0] if text else "no detail"


def route_tier(codex, *, action, ask, docs, cwd, research, catalog, topic, timeout,
               prompt_under_test=None):
    """(tier, source, usage) from one cheap ephemeral codex call. Any failure falls back to
    standard with the reason in `source`: the router never fails a run. It waits at most
    ROUTER_TIMEOUT, or the run's own timeout when that is shorter."""
    if not catalog.supports(selection.ROUTER_MODEL, selection.ROUTER_EFFORT):
        return ("standard", f"router fallback ({selection.ROUTER_MODEL} is not in the codex "
                "catalog)", None)
    sizes = []
    for doc in docs:
        try:
            sizes.append(f"- {doc} ({(cwd / doc).stat().st_size} bytes)")
        except OSError:
            sizes.append(f"- {doc} (missing)")
    task = ask.strip() or "None."
    if prompt_under_test is not None:
        task += (f"\n\nPrompt under test ({len(prompt_under_test.encode('utf-8'))} bytes), "
                 f"first 2000 characters:\n{prompt_under_test[:2000]}")
    prompt = render_text(load_template("router"), {
        "ACTION": action,
        "RESEARCH": "yes" if research else "no",
        "DOCS": "\n".join(sizes) or "None.",
        "TASK": fence("TASK", task, secrets.token_hex(4)),
    })
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        return "standard", "router fallback (the task is too large to route)", None
    # An empty directory: the router has nothing to explore, so it answers fast.
    with tempfile.TemporaryDirectory() as empty:
        try:
            _thread, text, usage = run_codex(
                [codex, "exec"], prompt, Path(empty), min(ROUTER_TIMEOUT, timeout),
                selection.ROUTER_MODEL, selection.ROUTER_EFFORT, topic,
                ephemeral=True, schema=SCHEMAS / "router.schema.json")
        except CodexCallFailed as exc:
            # Only a failed call falls back. A stop signal (plain SystemExit) propagates, so a
            # cancelled run never goes on to pay for the main call.
            return "standard", f"router fallback ({_first_line(exc.code)})", exc.usage
    try:
        tier, reason = selection.parse_router_output(text)
    except selection.SelectionError as exc:
        return "standard", f"router fallback ({exc})", usage
    return tier, f'router: "{reason}"', usage


def make_router(codex, **context):
    """A route() closure that calls route_tier at most once per run, so eval compare's two
    selections share one router call and its usage is counted once."""
    memo = {}

    def route():
        if "result" in memo:
            tier, source, _usage = memo["result"]
            return tier, source, None
        memo["result"] = route_tier(codex, **context)
        return memo["result"]

    return route


def preflight_auto(profile, tier, catalog):
    """Exit 2 before any paid call unless automatic selection can serve this profile: a rung
    at or below --tier, or, when the router will choose, at or below the lowest tier it can
    answer. Fallback never walks up past the chosen tier, so this is what keeps a paid router
    answer from ending in exit 2."""
    if tier:
        check = tier
    elif catalog.supports(selection.ROUTER_MODEL, selection.ROUTER_EFFORT):
        check = selection.lowest_tier(profile)
    else:
        # route_tier answers standard without a call when the router model is missing.
        check = "standard"
    try:
        selection.pick(profile, check, catalog)
    except selection.SelectionError as exc:
        hint = ("" if tier else "; the router may pick any tier, so pass --tier with one "
                "whose model is available")
        _bad_args(f"{exc}{hint}")


def describe_source(model_from, effort_from, *, default, fallback):
    """Where a run's model and effort came from, for its printed and recorded Model: line.
    Each `*_from` names a flag or env var, or None when that half came from `fallback`;
    `default` describes the case where neither half was given."""
    pair = (model_from, effort_from)
    if pair == ("--model", "--effort"):
        return "set on the command line"
    if pair == ("CODEX_REVIEW_MODEL", "CODEX_REVIEW_EFFORT"):
        return "CODEX_REVIEW_MODEL/CODEX_REVIEW_EFFORT"
    if pair == (None, None):
        return default
    return f"model from {model_from or fallback}, effort from {effort_from or fallback}"


def _explicit(cli_model, cli_effort):
    """True when the command line picks the model or effort (`--model auto` does not)."""
    return cli_model not in (None, "auto") or bool(cli_effort)


def refuse_idle_tier(tier, explicit):
    """Exit 2 when --tier would be silently ignored: --model or --effort turn automatic
    selection off, so a tier given alongside them would steer nothing."""
    if tier and explicit:
        _bad_args("--tier does nothing here: --model and --effort turn automatic selection "
                  "off. Drop --tier, or drop --model/--effort.")


def select_for(profile, *, cli_model, cli_effort, tier, catalog, route):
    """(model, effort, source, usage) for a run that is not a pinned review.

    --model or --effort turns automatic selection off for the run; the missing half comes
    from the profile's standard rung. Otherwise the tier is --tier or the router's, and
    selection.auto_pick applies the headroom step-down and the catalog fallback."""
    model = None if cli_model in (None, "auto") else cli_model
    if model or cli_effort:
        source = describe_source("--model" if model else None,
                                 "--effort" if cli_effort else None,
                                 default="set on the command line",
                                 fallback="the standard rung")
        model, effort = selection.fill_explicit(profile, model, cli_effort)
        validate_choice(model, effort, catalog)
        return model, effort, source, None
    preflight_auto(profile, tier, catalog)
    usage = None
    if tier:
        tier_source = "--tier"
    else:
        tier, tier_source, usage = route()
    try:
        model, effort, used, notes = selection.auto_pick(
            profile, tier, current_headroom(), catalog, time.time())
    except selection.SelectionError as exc:
        _bad_args(str(exc))
    return model, effort, "; ".join([f"tier {used} from {tier_source}", *notes]), usage


def topic_slug(value):
    """argparse type for --topic. Rejects before any Codex call, so a bad slug
    cannot burn a paid round and then fail on an unopenable path."""
    if len(value) > TOPIC_MAX or not TOPIC_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(
            f"topic must be a kebab-case slug matching {TOPIC_RE.pattern} "
            f"and at most {TOPIC_MAX} characters; got {value!r}"
        )
    return value


def output_path(cwd, action, topic, out_dir=None):
    """Where an action's output goes: `out_dir` (a run's --out-dir) or the action's default
    directory, relative to --cwd. Computes only: the directory is created inside
    append_section's recovery block, so a failed mkdir after a paid call prints the result
    instead of losing it."""
    spec = ACTIONS[action]
    return (cwd / (out_dir or spec["dir"])
            / f"{date.today().isoformat()}-{topic}-{spec['suffix']}.md")


def findings_path(cwd, topic, out_dir=None):
    return output_path(cwd, "review", topic, out_dir)


def append_section(path, title, section):
    """Append one section, writing the title first when the file is new or empty. Recreates a
    deleted directory: state outlives a checkout, and a paid result must not be lost."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.exists() or path.stat().st_size == 0
    with path.open("a") as f:
        if fresh:
            f.write(f"# {title}\n\n")
        f.write(section.rstrip() + "\n\n")


def run_round(*, action, cwd, codex, timeout, origin, branch, topic, state, prompt, model,
              effort, web_search, round_kind, heading, usage_extra=None, usage_mode=None,
              needs_thread=False, out_dir=None):
    """One threaded round, shared by reviews, research, and eval advise: start or resume the
    topic's thread (`state` from preload_state), append the output under heading(round_no),
    save state, then print the completion line, the usage line, and (last) the output
    path."""
    spath = state_path(origin, branch, topic, action)
    if needs_thread and state is None:
        print(f"no review thread for topic '{topic}' on this branch; "
              "start a new review with --kind plan|design|implementation", file=sys.stderr)
        raise SystemExit(3)
    if state is None:
        cmd_prefix = [codex, "exec"]
    else:
        cmd_prefix = [codex, "exec", "resume", state["thread_id"]]
    thread_id, text, usage = run_codex(cmd_prefix, prompt, cwd, timeout, model, effort, topic,
                                       web_search=web_search)
    if state is None and not usable(thread_id):
        sys.stderr.write(text)
        sys.exit("codex emitted no thread.started event, so this review cannot be resumed; "
                 "the review text above was printed rather than saved, and no state was changed"
                 "\n" + _version_note(codex))
    now = time.time()
    if state is None:
        state = {"thread_id": thread_id, "origin": origin, "branch": branch, "topic": topic,
                 "findings_file": str(output_path(cwd, action, topic, out_dir)),
                 "created": now,
                 "rounds": []}
    state["last_used"] = now
    state["rounds"].append({"ts": now, "kind": round_kind})
    findings = Path(state["findings_file"])
    round_no = len(state["rounds"])
    section = heading(round_no) + text.rstrip()
    try:
        append_section(findings, f"{ACTIONS[action]['title']}: {topic}", section)
    except OSError as exc:
        print(f"could not write the findings file {findings} ({exc}); the review text was "
              "printed below instead, and no state was changed", file=sys.stderr)
        print(section)
        raise SystemExit(1)
    save_state(spath, state)
    print(f"round {round_no} ({round_kind}) complete, thread {state['thread_id']}, "
          f"model {model} effort {effort}")
    _print_usage(selection.sum_usage(usage, usage_extra), usage_mode)
    print(findings)


# The codex-cli version this tool was last exercised against end to end. The model is
# pinned above; the CLI is whatever is installed, and a CLI change is the usual cause of a
# break no flag explains (0.153.3 began reading a non-terminal stdin before its first turn).
# Every failure message names the running version and whether it is this one.
VALIDATED_CODEX_CLI = "0.156.1"


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


def max_prompt_bytes(platform, page_size=None):
    """The largest prompt codex can receive. codex takes the prompt as one command-line
    argument (stdin stays closed on purpose). macOS caps all arguments plus the environment at
    1 MB. Linux also caps any single argument at 32 pages (128 KiB on the usual 4 KiB pages),
    its terminating NUL included, so a larger prompt would fail at exec instead of being
    refused here, before any spend."""
    if platform.startswith("linux"):
        page_size = page_size or os.sysconf("SC_PAGE_SIZE")
        return min(800 * 1024, 32 * page_size - 1)
    return 800 * 1024


MAX_PROMPT_BYTES = max_prompt_bytes(sys.platform)


def check_prompt_size(prompt):
    """Exit 2 when a rendered prompt is too large to pass to codex. Actions call it before
    any paid call, the router included; run_codex calls it again as the last guard."""
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        _bad_args(f"the rendered prompt is over {MAX_PROMPT_BYTES} bytes; codex takes it as a "
                  "command-line argument, so trim the inputs")


class CodexCallFailed(SystemExit):
    """A failed codex call: a non-zero exit, a timeout, or an empty answer. Uncaught, it
    behaves exactly like sys.exit(message): the message on stderr and exit 1. It carries the
    usage codex reported before failing, so a caller that recovers (the router, the judge)
    still counts what the call spent. Callers that recover catch THIS type only: a plain
    SystemExit is a stop signal (the handler's SystemExit(130)) or a refusal, and must
    propagate, or a cancelled run would carry on and keep spending."""

    def __init__(self, message, usage=None):
        super().__init__(message)
        self.usage = usage


def run_codex(cmd_prefix, prompt, cwd, timeout, model, effort, topic=None, *,
              web_search="disabled", ephemeral=False, schema=None):
    check_prompt_size(prompt)
    with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as f:
        out_file = f.name
    cmd = cmd_prefix + [
        "--json",
        "-m", model,
        "-c", f'model_reasoning_effort="{effort}"',
        "-c", 'sandbox_mode="read-only"',
        # Always explicit: a web_search setting in ~/.codex/config.toml must never turn
        # search on for a run that did not ask for it.
        "-c", f'web_search="{web_search}"',
    ]
    if ephemeral:
        # One-shot runs (the router, eval compare) keep no session and may run outside a repo.
        cmd += ["--ephemeral", "--skip-git-repo-check"]
    if schema is not None:
        cmd += ["--output-schema", str(schema)]
    cmd += ["-o", out_file, prompt]
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
        raise CodexCallFailed(_timeout_message(timeout, out, stderr_text, cmd[0]),
                              extract_usage(_parse_events(out or "")[0]))
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
        raise CodexCallFailed(message + "\n" + _version_note(cmd[0]), extract_usage(events))
    review = Path(out_file).read_text()
    os.unlink(out_file)
    if not review.strip():
        raise CodexCallFailed("codex returned an empty review; no state was changed\n"
                              + _version_note(cmd[0]), extract_usage(events))
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



def _action_parser(name, description, *, prompt_inputs=False):
    parser = argparse.ArgumentParser(prog=f"run_review.py {name}", description=description)
    parser.add_argument("--topic", required=True, type=topic_slug,
                        help="kebab-case topic slug, stable across follow-ups")
    parser.add_argument("--ask", default="", help="the question, or what to look at")
    parser.add_argument("--doc", action="append", default=[], dest="docs",
                        help="context path relative to --cwd (repeatable)")
    parser.add_argument("--cwd", default=".", help="the git worktree to run in")
    parser.add_argument("--model", default=None,
                        help="explicit model; turns automatic selection off")
    parser.add_argument("--effort", default=None,
                        help="explicit effort; turns automatic selection off")
    parser.add_argument("--tier", choices=selection.TIERS, default=None,
                        help="light|standard|deep; skips the router")
    parser.add_argument("--usage", nargs="?", const="text", default=None,
                        choices=("text", "json"), help="print a fuller token-usage breakdown")
    parser.add_argument("--out-dir", default=None, dest="out_dir",
                        help="where the output file is written, relative to --cwd "
                             "(default: the action's directory under .codex-review/)")
    if prompt_inputs:
        parser.add_argument("--prompt", required=True, dest="prompt_file",
                            help="the prompt under test (absolute, or relative to --cwd)")
        parser.add_argument("--result", default=None, dest="result_file",
                            help="an existing output of the prompt")
    return parser


def _action_context(args):
    """The shared start of every paid action. Everything here is free: the worktree
    identity and the catalog read both happen before any spend."""
    cwd = Path(args.cwd).resolve()
    origin, branch = repo_identity(cwd)
    codex = env("CODEX_BIN", "codex")
    catalog = load_catalog(codex)
    _install_run_cleanup()
    return cwd, codex, int(env("CODEX_REVIEW_TIMEOUT", "3600")), origin, branch, catalog


def _research_subcommand(argv):
    args = _action_parser(
        "research", "Answer a question with web search, threaded per topic.").parse_args(argv)
    if not args.ask.strip():
        _bad_args("research needs --ask with the question")
    refuse_idle_tier(args.tier, _explicit(args.model, args.effort))
    cwd, codex, timeout, origin, branch, catalog = _action_context(args)
    prompt = render_text(load_template("research"),
                         {"ASK": args.ask.strip(), "DOCS": doc_list(args.docs)})
    check_prompt_size(prompt)  # before selection, which may pay for a router call
    state = preload_state(origin, branch, args.topic, "research")
    route = make_router(codex, action="research", ask=args.ask, docs=args.docs, cwd=cwd,
                        research=True, catalog=catalog, topic=args.topic, timeout=timeout)
    model, effort, source, router_usage = select_for(
        "research", cli_model=args.model, cli_effort=args.effort, tier=args.tier,
        catalog=catalog, route=route)
    print(f"selected {model} / {effort} ({source})")
    today = date.today().isoformat()
    run_round(action="research", cwd=cwd, codex=codex, timeout=timeout, origin=origin,
              branch=branch, topic=args.topic, state=state, prompt=prompt, model=model,
              effort=effort,
              web_search="live", round_kind="research",
              heading=lambda n: (f"## Round {n} ({today}, research)\n\n"
                                 f"Model: {model} / {effort} ({source})\n\n"),
              usage_extra=router_usage, usage_mode=args.usage, out_dir=args.out_dir)
    return 0


# Each --prompt/--result file; the rendered prompt is capped separately (MAX_PROMPT_BYTES).
# One file at this limit, plus its template, must still fit under that cap.
MAX_INPUT_BYTES = min(150 * 1024, MAX_PROMPT_BYTES - 32 * 1024)


def input_path(cwd, path):
    """--prompt/--result paths: absolute as given, relative ones against --cwd like --doc."""
    candidate = Path(path)
    return candidate if candidate.is_absolute() else cwd / candidate


def read_input(path, flag):
    """A --prompt/--result file as text, checked before any spend: readable, at most
    MAX_INPUT_BYTES, no NUL bytes (codex takes the prompt as an argument), UTF-8."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        _bad_args(f"{flag} {path} could not be read ({exc.strerror or exc})")
    if len(data) > MAX_INPUT_BYTES:
        _bad_args(f"{flag} {path} is {len(data)} bytes; the limit is {MAX_INPUT_BYTES}")
    if b"\x00" in data:
        _bad_args(f"{flag} {path} contains NUL bytes; pass a text file")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        _bad_args(f"{flag} {path} is not UTF-8 text")


def _eval_subcommand(argv):
    if not argv or argv[0] not in ("advise", "compare"):
        _bad_args("usage: run_review.py eval advise|compare --topic T --prompt FILE [...]")
    return _eval_advise(argv[1:]) if argv[0] == "advise" else _eval_compare(argv[1:])


def _eval_advise(argv):
    parser = _action_parser("eval advise", "Critique a prompt and return a revised version, "
                            "threaded per topic.", prompt_inputs=True)
    parser.add_argument("--research", action="store_true",
                        help="let codex use web search for outside facts")
    args = parser.parse_args(argv)
    refuse_idle_tier(args.tier, _explicit(args.model, args.effort))
    cwd, codex, timeout, origin, branch, catalog = _action_context(args)
    prompt_text = read_input(input_path(cwd, args.prompt_file), "--prompt")
    result_text = (read_input(input_path(cwd, args.result_file), "--result")
                   if args.result_file else None)
    nonce = secrets.token_hex(4)
    prompt = render_text(load_template("eval-advise"), {
        "ASK": args.ask.strip() or "None.",
        "DOCS": doc_list(args.docs),
        "PROMPT": fence("PROMPT-UNDER-TEST", prompt_text, nonce),
        "RESULT": (fence("OUTPUT", result_text, nonce) if result_text is not None
                   else "None provided."),
    })
    if args.research:
        prompt += "\n\n" + load_template("research-addendum")
    # Two inputs under their own cap can still overflow the rendered prompt; the router only
    # sees 2,000 characters, so check the real prompt before selection may pay for routing.
    check_prompt_size(prompt)
    state = preload_state(origin, branch, args.topic, "eval")
    route = make_router(codex, action="eval advise", ask=args.ask, docs=args.docs, cwd=cwd,
                        research=args.research, catalog=catalog, topic=args.topic,
                        timeout=timeout, prompt_under_test=prompt_text)
    model, effort, source, router_usage = select_for(
        "eval-advise", cli_model=args.model, cli_effort=args.effort, tier=args.tier,
        catalog=catalog, route=route)
    print(f"selected {model} / {effort} ({source})")
    today = date.today().isoformat()
    run_round(action="eval", cwd=cwd, codex=codex, timeout=timeout, origin=origin,
              branch=branch, topic=args.topic, state=state, prompt=prompt, model=model,
              effort=effort, web_search="live" if args.research else "disabled",
              round_kind="advise",
              heading=lambda n: (f"## Advise round {n} ({today})\n\n"
                                 f"Prompt: `{args.prompt_file}`\n\n"
                                 f"Model: {model} / {effort} ({source})\n\n"),
              usage_extra=router_usage, usage_mode=args.usage, out_dir=args.out_dir)
    return 0


DEFAULT_CRITERIA = "- Correctness\n- Completeness\n- Instruction-following\n- Concision"


def label_rng():
    """SystemRandom, unless CODEX_REVIEW_LABEL_SEED pins it: tests force both label orders.
    Called before any paid step, so a bad inherited value costs nothing."""
    seed = env("CODEX_REVIEW_LABEL_SEED", "")
    if not seed:
        return random.SystemRandom()
    try:
        return random.Random(int(seed))
    except ValueError:
        _bad_args(f"CODEX_REVIEW_LABEL_SEED must be an integer, got {seed!r}")


def _judge_prompt(prompt_text, response_a, response_b, criteria, nonce):
    return render_text(load_template("eval-judge"), {
        "PROMPT": fence("PROMPT", prompt_text, nonce),
        "A": fence("RESPONSE-A", response_a, nonce),
        "B": fence("RESPONSE-B", response_b, nonce),
        "CRITERIA": criteria,
    })


def _save_eval(out, topic, body):
    """Append one compare section. When the file cannot be written, print the section
    instead so a paid result is never lost, and return False."""
    try:
        append_section(out, f"Codex eval: {topic}", body)
        return True
    except OSError as exc:
        print(f"could not write the eval file {out} ({exc}); the output was printed below "
              "instead", file=sys.stderr)
        print(body)
        return False


def _eval_compare(argv):
    parser = _action_parser("eval compare", "Run a prompt through codex; with --result, judge "
                            "the two outputs blind.", prompt_inputs=True)
    parser.add_argument("--judge-model", default=None, help="explicit judge model")
    parser.add_argument("--judge-effort", default=None, help="explicit judge effort")
    args = parser.parse_args(argv)
    if (args.judge_model or args.judge_effort) and not args.result_file:
        _bad_args("--judge-model and --judge-effort need --result: without an existing "
                  "result there is nothing to judge")
    # --tier steers whichever step selects automatically; it is idle only when generate is
    # explicit and no automatic judge will run.
    auto_judge = bool(args.result_file) and not (args.judge_model or args.judge_effort)
    refuse_idle_tier(args.tier, _explicit(args.model, args.effort) and not auto_judge)
    if args.docs:
        _bad_args("eval compare takes no --doc: the prompt runs as written, and the judge runs "
                  "outside the repository so it cannot learn which result is which")
    cwd, codex, timeout, _origin, _branch, catalog = _action_context(args)
    prompt_text = read_input(input_path(cwd, args.prompt_file), "--prompt")
    result_text = (read_input(input_path(cwd, args.result_file), "--result")
                   if args.result_file else None)
    criteria = args.ask.strip() or DEFAULT_CRITERIA
    rng = label_rng() if result_text is not None else None
    if result_text is not None:
        # Everything in the judge prompt but codex's answer is known now; check it before
        # generation is paid. run_codex still guards the full prompt later.
        check_prompt_size(_judge_prompt(prompt_text, result_text, "", criteria, "0" * 8))
        if not (args.judge_model or args.judge_effort):
            # The judge is selected after generate, which may pay for routing; make sure an
            # automatic judge can run before anything is paid.
            preflight_auto("eval-judge", args.tier, catalog)
    route = make_router(codex, action="eval compare", ask=args.ask, docs=args.docs, cwd=cwd,
                        research=False, catalog=catalog, topic=args.topic, timeout=timeout,
                        prompt_under_test=prompt_text)
    judge = None
    if result_text is not None and (args.judge_model or args.judge_effort):
        # Explicit judge flags never route, so checking them first means a bad one costs
        # nothing, not even the router call the generate selection may make.
        judge = select_for("eval-judge", cli_model=args.judge_model,
                           cli_effort=args.judge_effort, tier=args.tier, catalog=catalog,
                           route=route)
    gen_model, gen_effort, gen_source, route_usage = select_for(
        "eval-generate", cli_model=args.model, cli_effort=args.effort, tier=args.tier,
        catalog=catalog, route=route)
    if result_text is not None and judge is None:
        judge = select_for("eval-judge", cli_model=None, cli_effort=None, tier=args.tier,
                           catalog=catalog, route=route)
    print(f"selected {gen_model} / {gen_effort} to generate ({gen_source})")
    if judge:
        print(f"selected {judge[0]} / {judge[1]} to judge ({judge[2]})")
    _thread, codex_result, gen_usage = run_codex(
        [codex, "exec"], prompt_text, cwd, timeout, gen_model, gen_effort, args.topic,
        ephemeral=True)
    usages = [route_usage, gen_usage]
    codex_name = f"codex ({gen_model}/{gen_effort})"
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    heading = f"## Compare ({stamp}, generate {gen_model}/{gen_effort}"
    heading += f", judge {judge[0]}/{judge[1]})" if judge else ")"
    lines = [heading, "", f"Prompt: `{args.prompt_file}`"]
    if judge:
        lines.append(f"Existing result: `{args.result_file}`")
    lines.append(f"Generate: {gen_model} / {gen_effort} ({gen_source})")
    if judge:
        lines.append(f"Judge: {judge[0]} / {judge[1]} ({judge[2]})")
    lines += ["", "### Codex result", "", codex_result.rstrip()]
    exit_code = 0
    if judge:
        judge_model, judge_effort, _source, judge_route_usage = judge
        usages.append(judge_route_usage)
        labels = selection.assign_labels(rng)
        texts = {"existing": result_text, "codex": codex_result}
        judge_prompt = _judge_prompt(prompt_text, texts[labels["A"]], texts[labels["B"]],
                                     criteria, secrets.token_hex(4))
        lines += ["", "### Verdict", ""]
        try:
            # An empty directory, not the worktree: there the judge could open the --result
            # file and learn which side is which. It needs nothing but the prompt.
            with tempfile.TemporaryDirectory() as blind:
                _thread, verdict_text, judge_usage = run_codex(
                    [codex, "exec"], judge_prompt, Path(blind), timeout, judge_model,
                    judge_effort, args.topic, ephemeral=True,
                    schema=SCHEMAS / "judge.schema.json")
        except CodexCallFailed as exc:
            # The codex result above is already paid for: keep it and say what failed. A
            # failed call can still have spent, and CodexCallFailed carries that usage.
            print(exc.code, file=sys.stderr)
            usages.append(exc.usage)
            lines.append(f"The judge run failed, so there is no verdict: "
                         f"{_first_line(exc.code)}")
            exit_code = 1
        except SystemExit:
            # A stop signal, not a failure: save the paid codex result, then stop as asked.
            lines.append("The judge run was interrupted, so there is no verdict.")
            _save_eval(output_path(cwd, "eval", args.topic, args.out_dir), args.topic, "\n".join(lines))
            raise
        else:
            usages.append(judge_usage)
            try:
                verdict = selection.unblind(selection.parse_verdict(verdict_text), labels,
                                            codex_name)
                lines.append(selection.render_verdict(verdict, labels, codex_name))
            except selection.SelectionError as exc:
                names = selection.label_names(labels, codex_name)
                lines += [f"The judge's verdict did not match the expected shape ({exc}), so "
                          f"it is recorded raw. Labels: A = {names['A']}, B = {names['B']}.",
                          "", "```", verdict_text.rstrip(), "```"]
    out = output_path(cwd, "eval", args.topic, args.out_dir)
    if not _save_eval(out, args.topic, "\n".join(lines)):
        raise SystemExit(1)
    done = f"compare complete, generate {gen_model} effort {gen_effort}"
    if judge:
        done += f", judge {judge[0]} effort {judge[1]}"
    print(done)
    _print_usage(selection.sum_usage(*usages), args.usage)
    print(out)
    return exit_code


ADVISE_ACTIONS = {
    "review": ("review",),
    "research": ("research",),
    "eval-advise": ("eval-advise",),
    "eval-compare": ("eval-generate", "eval-judge"),
}


def build_advice(action_names, tier, catalog, headroom, now):
    """What each action would run on, as data. Calls no model."""
    report = {"catalog": "live" if catalog.live else "unavailable", "headroom": headroom,
              "tier": tier, "actions": {}}
    for action in action_names:
        entries = []
        for profile in ADVISE_ACTIONS[action]:
            ladder = [{"tier": t, "model": m, "effort": e, "available": catalog.supports(m, e)}
                      for t, (m, e) in selection.LADDERS[profile].items()]
            if profile == "review":
                # What a pinned review would really run: the env vars apply, as in a run.
                model, effort = resolve_model_effort(None, None)
                from_env = [name for name in ("CODEX_REVIEW_MODEL", "CODEX_REVIEW_EFFORT")
                            if env(name, "")]
                notes = ["from " + " and ".join(from_env) if from_env
                         else "pinned review default",
                         "the ladder applies only with --model auto"]
                if catalog.live and not catalog.supports(model, effort):
                    notes.append(f"{model} / {effort} is not in the codex catalog: a review "
                                 "would exit 2")
                if headroom:
                    notes.append("near a limit: reviews warn but never step down")
                pick = {"model": model, "effort": effort, "tier": None, "notes": notes}
            else:
                try:
                    model, effort, used, notes = selection.auto_pick(
                        profile, tier, headroom, catalog, now)
                    pick = {"model": model, "effort": effort, "tier": used, "notes": notes}
                except selection.SelectionError as exc:
                    pick = {"error": str(exc)}
            entries.append({"profile": profile, "ladder": ladder, "pick": pick})
        report["actions"][action] = entries
    return report


def format_advice(report, now):
    lines = [f"model catalog: {report['catalog']}"]
    if report["headroom"]:
        lines.append("codex usage: near its limit (%s)"
                     % selection.describe_headroom(report["headroom"], now))
    else:
        lines.append("codex usage: no window near its limit")
    lines.append(f"tier: {report['tier']} ({report['tier_from']})")
    for action, entries in report["actions"].items():
        lines += ["", action]
        for entry in entries:
            lines.append(f"  {entry['profile']}")
            for rung in entry["ladder"]:
                mark = "" if rung["available"] else "  [not in catalog]"
                lines.append(f"    {rung['tier']:<9}{rung['model']} / {rung['effort']}{mark}")
            pick = entry["pick"]
            if "error" in pick:
                lines.append(f"    pick: none ({pick['error']})")
                continue
            lines.append(f"    pick: {pick['model']} / {pick['effort']}")
            lines += [f"      {note}" for note in pick["notes"]]
    return "\n".join(lines)


def _advise_subcommand(argv):
    parser = argparse.ArgumentParser(
        prog="run_review.py advise",
        description="Show what automatic model selection picks for each action and why. "
                    "Calls no model and spends nothing.")
    parser.add_argument("action", nargs="?", choices=list(ADVISE_ACTIONS))
    parser.add_argument("--tier", choices=selection.TIERS, default=None)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    catalog = load_catalog(env("CODEX_BIN", "codex"))
    now = time.time()
    report = build_advice([args.action] if args.action else list(ADVISE_ACTIONS),
                          args.tier or "standard", catalog, current_headroom(), now)
    report["tier_from"] = ("--tier" if args.tier
                           else "standard shown; a run without --tier asks the router")
    print(json.dumps(report, indent=2) if args.json else format_advice(report, now))
    return 0


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "limits":
        raise SystemExit(_limits_subcommand(sys.argv[2:]))

    if len(sys.argv) > 1 and sys.argv[1] == "runs":
        raise SystemExit(_runs_subcommand(sys.argv[2:]))

    if len(sys.argv) > 1 and sys.argv[1] == "kill":
        raise SystemExit(_kill_subcommand(sys.argv[2:]))

    if len(sys.argv) > 1 and sys.argv[1] == "research":
        raise SystemExit(_research_subcommand(sys.argv[2:]))

    if len(sys.argv) > 1 and sys.argv[1] == "eval":
        raise SystemExit(_eval_subcommand(sys.argv[2:]))

    if len(sys.argv) > 1 and sys.argv[1] in ("eval-advise", "eval-compare"):
        # The same actions without a bare `eval` word, which some agent shell guards refuse
        # as the shell builtin. SKILL.md documents these forms.
        raise SystemExit(_eval_subcommand([sys.argv[1][len("eval-"):], *sys.argv[2:]]))

    if len(sys.argv) > 1 and sys.argv[1] == "advise":
        raise SystemExit(_advise_subcommand(sys.argv[2:]))

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
                             "codex model may be selected; 'auto' selects from the review "
                             "ladder (see --tier)" % DEFAULT_MODEL)
    parser.add_argument("--effort", default=None,
                        help="reasoning effort %s (default %s, or CODEX_REVIEW_EFFORT)"
                             % ("|".join(EFFORTS), DEFAULT_EFFORT))
    parser.add_argument("--usage", nargs="?", const="text", default=None,
                        choices=("text", "json"),
                        help="print a fuller token-usage breakdown (text|json); a one-line "
                             "usage summary always prints regardless")
    parser.add_argument("--research", action="store_true",
                        help="let the reviewer use web search when a finding depends on an "
                             "outside fact")
    parser.add_argument("--tier", choices=selection.TIERS, default=None,
                        help="with --model auto: light|standard|deep, skipping the router")
    args = parser.parse_args()
    if args.tier and args.model != "auto":
        _bad_args("--tier applies only with --model auto")
    refuse_idle_tier(args.tier, bool(args.effort))

    cwd = Path(args.cwd).resolve()
    codex = env("CODEX_BIN", "codex")
    timeout = int(env("CODEX_REVIEW_TIMEOUT", "3600"))
    args.out_dir = args.out_dir or env("CODEX_REVIEW_OUT_DIR", DEFAULT_OUT_DIR)
    # A review is a long, paid run; make sure a stop signal to this wrapper tears down the
    # codex child + the run marker (so a cancelled background run never keeps spending).
    _install_run_cleanup()
    origin, branch = repo_identity(cwd)

    catalog = load_catalog(codex)
    # Rendered and size-checked before selection, since selection may pay for a router call.
    prompt = render(args.kind, args.topic, args.docs, args.ask)
    if args.research:
        prompt += "\n\n" + load_template("research-addendum")
    check_prompt_size(prompt)
    # Pruned and loaded before selection, so --model auto never pays for routing a fix-round
    # whose thread is missing, expired, or corrupt.
    state = preload_state(origin, branch, args.topic)
    if args.kind == "fix-round" and state is None:
        print(f"no review thread for topic '{args.topic}' on this branch; "
              f"{REVIEW_RESTART}", file=sys.stderr)
        raise SystemExit(3)
    router_usage = None
    if args.model == "auto":
        route = make_router(codex, action=f"{args.kind} review", ask=args.ask,
                            docs=args.docs, cwd=cwd, research=args.research,
                            catalog=catalog, topic=args.topic, timeout=timeout)
        args.model, args.effort, model_source, router_usage = select_for(
            "review", cli_model=None, cli_effort=args.effort, tier=args.tier,
            catalog=catalog, route=route)
        print(f"selected {args.model} / {args.effort} ({model_source})")
    else:
        model_source = describe_source(
            "--model" if args.model
            else "CODEX_REVIEW_MODEL" if env("CODEX_REVIEW_MODEL", "") else None,
            "--effort" if args.effort
            else "CODEX_REVIEW_EFFORT" if env("CODEX_REVIEW_EFFORT", "") else None,
            default="pinned review default", fallback="the pinned default")
        args.model, args.effort = resolve_model_effort(args.model, args.effort)
        validate_choice(args.model, args.effort, catalog)
        warn_if_near_limit(args.model, args.effort)

    today = date.today().isoformat()
    run_round(action="review", cwd=cwd, codex=codex, timeout=timeout, origin=origin,
              branch=branch, topic=args.topic, state=state, prompt=prompt, model=args.model,
              effort=args.effort, web_search="live" if args.research else "disabled",
              round_kind=args.kind,
              heading=lambda n: (f"## Round {n} ({today}, {args.kind})\n\n"
                                 f"Model: {args.model} / {args.effort} ({model_source})\n\n"),
              usage_extra=router_usage, usage_mode=args.usage,
              needs_thread=(args.kind == "fix-round"), out_dir=args.out_dir)


if __name__ == "__main__":
    main()
