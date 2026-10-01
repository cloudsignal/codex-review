# codex-review

An external code-review, prompt-eval, and web-research CLI (and agent skill) for your working tree,
powered by [OpenAI Codex](https://github.com/openai/codex).

Run one review round against your code, get findings in a Markdown file, fix them, and re-review on
the **same thread** until it's clean. Critique a prompt, or run it and have its output judged blind
against one you already have. Ask a question that needs outside facts and get a sourced answer. It
drives `codex` in a **read-only** sandbox: the model runs no commands that write to your tree.

- **Read-only.** The model's commands can't modify your source. The tool itself writes only its
  output file and its thread state (under `--out-dir` and the state directory), and it sends the
  material under review to Codex (the same as any code-review tool).
- **Threaded.** Each review, research question, and prompt critique keeps a Codex thread per topic,
  so a follow-up round re-verifies earlier findings against your current code.
- **Cost-aware model selection.** Reviews stay pinned to a strong default. Research and evals pick
  the smallest model that fits the task, and step down a tier when your subscription is near its
  limit.
- **No lock-in.** Two Python files, standard library only. Nothing to `pip install`.
- **Spend-aware.** A one-line token-usage summary on every run, a `limits` check for your plan usage
  (no tokens, no network), and `runs`/`kill` so a long background run is never a runaway.

## Features

- **Topic-based threads.** One job is one kebab-case topic slug. The thread and the output file key
  on it, so every round stays grouped.
- **Resumable review sessions.** A `fix-round` resumes the same Codex thread and re-verifies each
  earlier finding against the current tree, so the reviewer remembers what it already flagged. State
  is keyed on origin, branch, and topic, and lives across checkouts (pruned after 30 days).
- **Four review kinds.** `plan`, `design`, `implementation`, and `fix-round`, each with its own
  prompt template.
- **Web research.** `research` answers a question with live web search and cites its sources; a
  review's `--research` flag lets the reviewer look up an outside fact (a release, an advisory,
  documented API behavior) when a finding depends on it.
- **Prompt evals.** `eval-advise` critiques a prompt and returns a revised version with a way to test
  it. `eval-compare` runs a prompt through Codex and, given an existing result, judges the two
  **blind**: shuffled into A and B, judged outside your repository, then unblinded in the file.
- **Automatic model selection.** Research and eval runs pick a model and effort from a per-action
  ladder by task size (`--tier light|standard|deep`, or a cheap router call when you don't pass
  one), check the choice against your live model catalog, and step down one tier near a usage
  limit. `advise` previews the choice for free.
- **Selectable reviewer.** `--model` and `--effort` (with `CODEX_REVIEW_MODEL` /
  `CODEX_REVIEW_EFFORT` env fallback); the strong default is `gpt-6.1-sol` at `xhigh`, with
  `gpt-6-sol` on a codex CLI that does not list it yet.
- **Token-usage reporting.** A one-line usage summary on every run, with `--usage [text|json]` for a
  fuller breakdown.
- **Subscription-limits check.** `limits` reads your plan usage off disk (no tokens, no network) and
  reports `OK` / `NEAR` / `REACHED`, so an agent can gate a long run on remaining headroom.
- **Trackable, killable runs.** `runs` lists in-flight runs and `kill` stops one, so a long
  background run is never an invisible runaway.
- **Credential-safe state.** Any credentials embedded in the git remote URL are stripped before the
  origin is stored, and state files are created owner-only.
- **Zero dependencies.** Standard library only. Works as a plain CLI and as a
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

**Keep reasoning high.** The default `gpt-6.1-sol` at `xhigh` is slow (minutes to tens of minutes a
round) on purpose: the extra reasoning is what drives the reviewer to range across more areas and
press harder on edge cases, which is the whole value. Reviews never step down on their own. Lower `--effort`, or
pass `--model auto --tier light`, only when you deliberately want a quick, lighter pass.

## Requirements

- **Platform: macOS or Linux** (both CI-tested). Windows is not supported natively; run it under
  WSL, which is Linux.
- Python 3.9+ (standard library only)
- The [OpenAI Codex CLI](https://github.com/openai/codex) on your `PATH`, authenticated once with
  `codex login`
- Last validated end to end with codex-cli 0.159.2 (`VALIDATED_CODEX_CLI` in the script). Newer
  releases usually work; every failure message reports the version in use so a CLI change is
  visible at once.

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
review, an eval, or a research question. To update an installed copy, run the same command again.

## Usage

### Reviews

```bash
python3 scripts/run_review.py \
  --kind <plan|design|implementation|fix-round> \
  --topic <kebab-slug> \
  --doc <path> [--doc <path> ...] \
  --ask "<what to focus on; for fix-round: what you changed>" \
  --cwd <worktree to review> \
  [--research]
```

The last line printed is the **output file path**; read it for the review.

> **Run long reviews in the background.** At the default `xhigh` effort a round often takes tens of
> minutes (sometimes hours) on a real codebase. Run it as a background job and poll, rather than a
> foreground call that may time out while codex is still working. Lower `--effort` for a faster,
> lighter pass.

| Kind | When |
|------|------|
| `plan` | Review a plan before you build it |
| `design` | Review a design doc |
| `implementation` | Review the code you wrote |
| `fix-round` | Re-review after applying findings (same thread) |

Use the same **topic** slug across every round of one review, because the thread and the findings
file key on it.

### Research

```bash
python3 scripts/run_review.py research --topic <slug> --ask "<question>" \
  [--tier light|standard|deep] [--doc <context path> ...] --cwd <worktree>
```

Answers with live web search and cites its sources. A later run on the same topic continues the
thread, so a follow-up question keeps the earlier context.

### Prompt evals

```bash
# Critique a prompt; add --result to diagnose from an output it produced
python3 scripts/run_review.py eval-advise --topic <slug> --prompt <file> \
  [--result <file>] --ask "<what the prompt is for>" [--tier L] --cwd <worktree>

# Run a prompt through codex; add --result to judge the two outputs blind
python3 scripts/run_review.py eval-compare --topic <slug> --prompt <file> \
  [--result <file>] [--ask "<judging criteria>"] [--tier L] --cwd <worktree>
```

`--prompt` is the prompt exactly as it would run; write inline text to a file first. Each
`--prompt`/`--result` file is capped at 150 KiB and the rendered prompt at 800 KiB. On Linux the
caps are about 96 KiB and 128 KiB: codex takes the prompt as one command-line argument, and Linux
limits a single argument to 128 KiB. An input over a cap is refused before any spend. The judge
shares a model family with codex's answer, so treat a low-confidence win as a tie, or pass
`--judge-model` for a different judge. `eval advise` and `eval compare` (with a space) also work;
the hyphenated forms exist because some agent shell guards refuse a bare `eval`.

### Model selection

Reviews use `gpt-6.1-sol` at `xhigh` unless you say otherwise. On a codex CLI that does not list
`gpt-6.1-sol` (0.159.2 does, 0.156.1 does not), a review falls back to `gpt-6-sol` and says so on
stderr and in the review file; a model you name with `--model` or `CODEX_REVIEW_MODEL` is never
swapped. Research and evals select automatically: pass `--tier` (free, and you know the task best), or omit it and a cheap router call
(`gpt-6-luna` at `low`) picks the tier.

| Action | light | standard | deep |
|---|---|---|---|
| review (`--model auto` only) | sol / medium | sol / high | sol / xhigh |
| research | luna / medium | sol / medium | astra / high |
| eval-advise | luna / high | sol / high | astra / high |
| eval-compare (generate) | luna / medium | sol / medium | sol / high |
| eval-compare (judge) | sol / high | sol / high | sol / xhigh |

Model names are short for `gpt-6-luna`, `gpt-6-sol`, and `gpt-6-astra`. Each pick is checked
against the live catalog (`codex debug models`, no spend); when a rung is missing it falls back
down the ladder, and never above the tier you asked for. Near a usage limit (a window at 80% or more that has not reset) automatic
selection steps down one tier and says so. `--model` or `--effort` turns automatic selection off
for that run.

```bash
python3 scripts/run_review.py advise [review|research|eval-advise|eval-compare] [--tier L] [--json]
```

`advise` prints what each action would pick right now and why. It calls no model and spends
nothing.

### Common options

```
--model <codex-model>       # a review's default: gpt-6.1-sol; 'auto' selects from the review ladder
--effort <low|medium|high|xhigh|...>   # a review's default: xhigh; checked against the live catalog
--tier <light|standard|deep>           # with automatic selection: skip the router
--out-dir <dir>             # default: .codex-review/<reviews|research|evals>/ (relative to --cwd)
--usage [text|json]         # fuller token-usage breakdown
```

Precedence for a review's model/effort: CLI flag > `CODEX_REVIEW_MODEL` / `CODEX_REVIEW_EFFORT`
env > default. A bad model or effort fails before Codex runs.

## Subscription usage & in-flight runs

```bash
python3 scripts/run_review.py limits            # plan usage: OK / NEAR / REACHED (no spend)
python3 scripts/run_review.py runs              # list in-flight runs
python3 scripts/run_review.py kill <pid|topic>  # stop one
```

`limits` reads codex's last persisted rate-limit snapshot off disk (no tokens, no network). Exit `1`
on REACHED, `3` if there is no snapshot yet; `--strict` also exits `1` on NEAR (handy to gate a run
on remaining headroom). `runs`/`kill` make a backgrounded run trackable and stoppable, and a stop
signal ends the run at any step, the router call included, before it reaches a paid call.

## Where things go

- **Reviews:** `<out-dir>/<date>-<topic>-codex-review.md` (default `.codex-review/reviews/`), one
  section appended per round.
- **Research:** `.codex-review/research/<date>-<topic>-codex-research.md`.
- **Evals:** `.codex-review/evals/<date>-<topic>-codex-eval.md`.
- **Thread state:** `~/.config/codex-review/state/` (override with `CODEX_REVIEW_STATE_DIR`), pruned
  after 30 days.

You may want `.codex-review/` in your `.gitignore`, or commit the reviews as a record.

## Environment variables

| Variable | Purpose |
|----------|---------|
| `CODEX_BIN` | Path to the `codex` binary (default `codex`) |
| `CODEX_REVIEW_MODEL` / `CODEX_REVIEW_EFFORT` | A review's model / effort (below a CLI flag) |
| `CODEX_REVIEW_OUT_DIR` | Review findings directory (below `--out-dir`) |
| `CODEX_REVIEW_STATE_DIR` | Where thread state lives |
| `CODEX_REVIEW_LABEL_SEED` | An integer that fixes `eval-compare`'s A/B shuffle, for reproducible runs |
| `CODEX_REVIEW_TIMEOUT` | Per-call timeout in seconds (default 3600). The timeout message says whether codex was still working (raise this, or lower `--effort`), produced no output at all (a startup or input problem; a longer timeout will not help), or wrote something other than its `--json` events (check the CLI version named at the end of the message) |

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | Run completed; output written |
| `1` | Codex call failed (an auth error names `codex login`; every failure names the codex-cli version in use and whether it is the validated one). When part of a run was already paid, the result is kept: printed to stdout if the output file was unwritable, or saved when only `eval-compare`'s judge failed |
| `2` | Bad arguments, refused before any spend (e.g. an invalid topic slug, an unknown model or effort, an oversized input, a `--cwd` that is not a git worktree) |
| `3` | No thread exists for this topic; start with a non-`fix-round` kind |
| `130` | Stopped by a signal (Ctrl-C, `kill`, or a cancelled background task) |

## Security note

This tool runs an executable script that shells out to your local `codex` CLI in a read-only
sandbox. Under the state directory it stores a thread id, the output path, per-round metadata, and
the repository's remote URL **with any embedded credentials stripped** (`https://user:token@host/repo`
→ `https://host/repo`). State files are created owner-only (`0600`), and the state directory is
created `0700` when the tool makes it; an existing directory you point it at is left as you set it
(POSIX modes; on Windows this is best-effort). It makes no network calls of its own; all model access
goes through `codex`, and only `research` and a review's `--research` let the model search the web.
Prompts and results passed to the eval actions are fenced as data, and the blind judge runs in an
empty directory outside your repository. Review the script before installing, as you would any skill
that ships code.

Platform note: macOS and Linux are supported and CI-tested. Windows is not a supported target; run
it under WSL. If run on native Windows the wrapper degrades safely (non-destructive `OpenProcess`
liveness, and `kill`'s identity check declines rather than risk the wrong process), but that path is
untested. On POSIX, `kill` ignores a marker past its run's deadline and, before signaling, checks via
`ps` that the PID's command still names `run_review`/`codex`, failing closed if it can't verify. This
makes signaling the wrong process after a crash-leftover marker plus PID reuse very unlikely, but it
is a heuristic, not a guarantee: it does not compare process creation identity.

## Development

```bash
python3 -m unittest discover -s tests
```

Standard-library `unittest`; no third-party dependencies. The tests drive a stub `codex` and spend
nothing.

## License

MIT. See [LICENSE](LICENSE).
