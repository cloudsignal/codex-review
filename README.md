# codex-review

An external code-review CLI (and agent skill) for your working tree, powered by
[OpenAI Codex](https://github.com/openai/codex).

Run one review round against your code, get findings in a Markdown file, fix them, and re-review on
the **same thread** until it's clean. It drives `codex` in a **read-only** sandbox: the model runs no
commands that write to your tree.

- **Read-only review.** The model's commands can't modify your source. The tool itself writes only
  the findings file and its thread state (under `--out-dir` and the state directory), and it sends
  the code under review to Codex (the same as any code-review tool).
- **Threaded.** Each review keeps a Codex thread per topic, so fix-rounds re-verify earlier findings
  against your current code.
- **No lock-in.** One Python file, standard library only. Nothing to `pip install`.
- **Spend-aware.** A one-line token-usage summary on every run, a `limits` check for your plan usage
  (no tokens, no network), and `runs`/`kill` so a long background review is never a runaway.

## Features

- **Topic-based threads.** One review is one kebab-case topic slug. The thread and the findings file
  key on it, so every round of a review stays grouped.
- **Resumable review sessions.** A `fix-round` resumes the same Codex thread and re-verifies each
  earlier finding against the current tree, so the reviewer remembers what it already flagged. State
  is keyed on origin, branch, and topic, and lives across checkouts (pruned after 30 days).
- **Four review kinds.** `plan`, `design`, `implementation`, and `fix-round`, each with its own
  prompt template.
- **Read-only review.** The model runs no commands that write to your tree.
- **Selectable reviewer.** `--model` and `--effort` (with `CODEX_REVIEW_MODEL` /
  `CODEX_REVIEW_EFFORT` env fallback); the strong default is `gpt-5.6-sol` at `xhigh`.
- **Token-usage reporting.** A one-line usage summary on every run, with `--usage [text|json]` for a
  fuller breakdown.
- **Subscription-limits check.** `limits` reads your plan usage off disk (no tokens, no network) and
  reports `OK` / `NEAR` / `REACHED`, so an agent can gate a long run on remaining headroom.
- **Trackable, killable runs.** `runs` lists in-flight reviews and `kill` stops one, so a long
  background review is never an invisible runaway.
- **Credential-safe state.** Any credentials embedded in the git remote URL are stripped before the
  origin is stored, and state files are created owner-only.
- **Zero dependencies.** One Python file, standard library only. Works as a plain CLI and as a
  [skills.sh](https://www.skills.sh/) agent skill.

## Why an external Codex review?

The point is a **second, independent reviewer**: one that didn't write the code. Reviewing your own
work, or having the same model that wrote the code review it, inherits the author's blind spots. A
separate Codex pass is an objective check, and Codex is particularly strong at catching **oversights**
and at **reasoning** about correctness and edge cases.

## Recommended workflow

Best used inside an agent (e.g. Claude Code) as part of spec-driven development: review each stage on
its own thread, and **loop each to zero findings** rather than taking a single round as the verdict.

1. Review the **plan** (`--kind plan`) → fix → re-review until clean.
2. Review the **design** (`--kind design`) → same loop.
3. Review the **implementation** (`--kind implementation`) → same loop.

Each round is one turn of a converging loop. In practice it takes **~6 rounds** to reach zero:

> Codex reports findings → the agent applies fixes (and pushes back, with reasoning, where the
> reviewer is wrong) → `--kind fix-round` re-verifies every prior finding against the new tree →
> repeat until a round returns nothing.

Keep the same `--topic` across the loop so the thread remembers earlier findings and checks whether
each is resolved.

**Keep reasoning high.** The default `gpt-5.6-sol` at `xhigh` is slow (tens of minutes a round) on
purpose: the extra reasoning is what drives the reviewer to range across more areas and press harder
on edge cases, which is the whole value. Lower `--effort` only when you deliberately want a quick,
lighter pass.

## Requirements

- Python 3.9+ (standard library only)
- The [OpenAI Codex CLI](https://github.com/openai/codex) on your `PATH`, authenticated once with
  `codex login`

## Install

### As a plain CLI

```bash
git clone https://github.com/cloudsignal/codex-review
python3 codex-review/scripts/run_review.py --help
```

### As an agent skill ([skills.sh](https://www.skills.sh/))

```bash
npx skills add cloudsignal/codex-review
```

The `SKILL.md` tells an agent (Claude Code, or any skills.sh-compatible tool) when and how to run a
review.

## Usage

```bash
python3 scripts/run_review.py \
  --kind <plan|design|implementation|fix-round> \
  --topic <kebab-slug> \
  --doc <path> [--doc <path> ...] \
  --ask "<what to focus on; for fix-round: what you changed>" \
  --cwd <worktree to review>
```

The last line printed is the **findings file path**; read it for the review.

> **Run long reviews in the background.** At the default `xhigh` effort a round often takes tens of
> minutes (sometimes hours) on a real codebase. Run it as a background job and poll, rather than a
> foreground call that may time out while codex is still working. Lower `--effort` for a faster,
> lighter pass.

### Review kinds

| Kind | When |
|------|------|
| `plan` | Review a plan before you build it |
| `design` | Review a design doc |
| `implementation` | Review the code you wrote |
| `fix-round` | Re-review after applying findings (same thread) |

Use the same **topic** slug across every round of one review, because the thread and the findings file key
on it.

### Model, effort, and output

```
--model <codex-model>       # default: gpt-5.6-sol
--effort <minimal|low|medium|high|xhigh>   # default: xhigh
--out-dir <dir>             # default: .codex-review/reviews/ (relative to --cwd)
--usage [text|json]         # fuller token-usage breakdown
```

Precedence for model/effort: CLI flag > `CODEX_REVIEW_MODEL` / `CODEX_REVIEW_EFFORT` env > default.

## Subscription usage & in-flight runs

```bash
python3 scripts/run_review.py limits            # plan usage: OK / NEAR / REACHED (no spend)
python3 scripts/run_review.py runs              # list in-flight reviews
python3 scripts/run_review.py kill <pid|topic>  # stop one
```

`limits` reads codex's last persisted rate-limit snapshot off disk (no tokens, no network). Exit `1`
on REACHED, `3` if there is no snapshot yet; `--strict` also exits `1` on NEAR (handy to gate a run
on remaining headroom). `runs`/`kill` make a backgrounded review trackable and stoppable.

## Where things go

- **Findings:** `<out-dir>/<date>-<topic>-codex-review.md`, one section appended per round.
- **Thread state:** `~/.config/codex-review/state/` (override with `CODEX_REVIEW_STATE_DIR`), pruned
  after 30 days.

## Environment variables

| Variable | Purpose |
|----------|---------|
| `CODEX_BIN` | Path to the `codex` binary (default `codex`) |
| `CODEX_REVIEW_MODEL` / `CODEX_REVIEW_EFFORT` | Model / effort fallback (below a CLI flag) |
| `CODEX_REVIEW_OUT_DIR` | Findings directory (below `--out-dir`) |
| `CODEX_REVIEW_STATE_DIR` | Where thread state lives |
| `CODEX_REVIEW_TIMEOUT` | Per-round timeout in seconds (default 3600) |

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | Review completed; findings written |
| `1` | Codex call failed (an auth error names `codex login`) |
| `2` | Bad arguments (e.g. the topic isn't a valid kebab slug) |
| `3` | No thread exists for this topic; start with a non-`fix-round` kind |

## Security note

This tool runs an executable script that shells out to your local `codex` CLI in a read-only
sandbox. Under the state directory it stores a thread id, the findings path, per-round metadata, and
the repository's remote URL **with any embedded credentials stripped** (`https://user:token@host/repo`
→ `https://host/repo`). State files are created owner-only (`0600`), and the state directory is
created `0700` when the tool makes it; an existing directory you point it at is left as you set it
(POSIX modes; on Windows this is best-effort). It makes no network calls of its own; all model access
goes through `codex`. Review the script before installing, as you would any skill that ships code.

Platform note: the process registry (`runs`/`kill`) targets POSIX (macOS/Linux); on Windows,
liveness uses the non-destructive `OpenProcess`, but `kill`'s identity check is POSIX-only and
declines there (stop a background run by cancelling the background job). On POSIX, `kill` ignores a
marker past its run's deadline and, before signaling, checks via `ps` that the PID's command still
names `run_review`/`codex`, failing closed if it can't verify. This makes signaling the wrong
process after a crash-leftover marker + PID reuse very unlikely, but it is a heuristic, not a
guarantee: it does not compare process creation identity.

## Development

```bash
python3 -m unittest discover -s tests
```

Standard-library `unittest`; no third-party dependencies.

## License

MIT. See [LICENSE](LICENSE).
