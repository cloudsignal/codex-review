---
name: codex-review
description: Use when a plan, design, or implementation is ready for external review; when the user asks for a code review, an external review, or another review round; when a prompt needs an outside critique or a second model's answer to compare against; or when a question needs a second model to research outside facts on the web.
license: MIT
---

# Codex review, eval, and research

One runner drives [OpenAI Codex](https://github.com/openai/codex) in a **read-only** sandbox, for
code reviews, prompt evals, and web research. The model runs no commands that write to your tree.
The tool itself writes only its output file and its thread state, and sends the material under
review to Codex. Reviews run on `gpt-6-sol` at `xhigh`. Every other action picks its own model and
effort, and costs less when the task is small or the subscription is near its limit.

## Requirements

- macOS or Linux (Windows via WSL)
- Python 3.9+ (standard library only)
- The `codex` CLI on `PATH`, authenticated once with `codex login`

## Actions

`R` below stands for `python3 scripts/run_review.py`.

| Job | Command |
|---|---|
| Review a plan, design, or implementation | `R --kind plan\|design\|implementation --topic T --doc P [--doc P] --ask "<focus>" --cwd W` |
| Re-review after applying fixes | `R --kind fix-round --topic T --doc P --ask "<what you fixed>" --cwd W` |
| Research a question on the web | `R research --topic T --ask "<question>" --tier L --cwd W` |
| Critique a prompt | `R eval-advise --topic T --prompt F [--result F] --ask "<what it is for>" --tier L --cwd W` |
| See codex's answer to a prompt | `R eval-compare --topic T --prompt F --tier L --cwd W` |
| Compare codex's answer with an existing one | the line above plus `--result F`, with criteria in `--ask` |
| Preview model selection (free) | `R advise [review\|research\|eval-advise\|eval-compare] [--tier L]` |
| Check subscription usage (free) | `R limits [--strict] [--json]` |
| List or stop running jobs | `R runs`, `R kill <pid\|topic>` |

- `--topic` is a kebab-case slug, the same across every round of one job.
- `--cwd` is the git worktree. `--doc` and relative `--prompt`/`--result` paths resolve against it.
- `--prompt` is the prompt exactly as it would run. For inline text, write it to a scratch file
  first.
- `eval-advise` and `eval-compare` are also spelled `eval advise` and `eval compare`. Prefer the
  hyphenated forms: some agent shell guards refuse any command containing a bare `eval`.
- The last stdout line is the output file and the line above it is the token usage. Read the
  file and report it. `--usage` (or `--usage json`) adds a per-field breakdown above that line.
- `R limits` reads codex's last rate-limit snapshot off disk, with no tokens and no network, and
  shows OK, NEAR, or REACHED per window. It exits 1 when a limit is reached and 3 when there is
  no snapshot yet; with `--strict` it also exits 1 when a window is near, so you can gate a long
  paid run on it.

Outputs land under `--cwd` in `.codex-review/reviews/`, `.codex-review/research/`, or
`.codex-review/evals/`, named `<date>-<topic>-codex-review.md`, `-codex-research.md`, or
`-codex-eval.md`. `--out-dir` (relative to `--cwd`) overrides the directory for one run;
`CODEX_REVIEW_OUT_DIR` overrides it for reviews. Reviews, research, and `eval-advise` keep one
codex thread per topic, so a later run with the same topic continues the conversation.

## Choosing --tier

Pass `--tier` on every research and eval run: it is free, and you know the task better than the
router. Without it, a cheap router call (`gpt-6-luna` at `low`) picks the tier.

| Tier | Use for |
|---|---|
| `light` | a lookup-sized question, or a short prompt with one obvious answer |
| `standard` | a normal multi-step question, or a prompt critique |
| `deep` | open-ended synthesis, a security-relevant question, conflicting sources, or a prompt with many interacting constraints |

`--model` or `--effort` turns automatic selection off for the run, so `--tier` beside them is
refused (in `eval-compare` it still steers an automatic judge). Use them when the user names
a model, or in `eval-compare` to see the result on the configuration the prompt runs on in
practice. Near a usage limit, automatic selection steps down one tier and says so; `R advise`
shows what it would pick right now.

## Reviews

- Kinds: `plan`, `design`, or `implementation` for a first round; `fix-round` after applying
  findings, with `--ask` summarizing the fixes. The reviewer re-verifies each prior finding.
- `--research` lets the reviewer search the web when a finding depends on an outside fact, such
  as a release, an advisory, or documented API behavior.
- Reviews stay on `gpt-6-sol` / `xhigh` and never step down; near a limit they warn. Override
  with `--model`/`--effort` or `CODEX_REVIEW_MODEL`/`CODEX_REVIEW_EFFORT` (a flag beats the env,
  the env beats the default). For a deliberately cheap round, such as a trivial fix-round, pass
  `--model auto --tier light`.
- Loop to zero: apply the findings you agree with, push back with technical reasoning where the
  reviewer is wrong, and run a `fix-round` on the same topic until a round returns nothing. A
  topic typically converges in about six rounds. Under spec-driven development, review the plan,
  the design, and the implementation each on its own topic.

## Eval

- `eval-advise` returns issues ranked by impact, the full revised prompt, and how to test it.
  Add `--result` to diagnose from an output the prompt produced.
- `eval-compare` runs the prompt through codex once, as written. With `--result`, a separate run
  judges the two blind (shuffled into A and B, outside the repository) and the file states which
  was which. It takes no `--doc`. The judge shares a model family with codex's answer, so treat a
  low-confidence win as a tie, or pass `--judge-model` for a different judge.

## Running and failures

- **Run every paid command as a background task, not a foreground call.** An `xhigh` review
  takes tens of minutes and sometimes one to three hours, far past most agent harnesses'
  synchronous command timeout, which reports a timeout while codex is still working. For a
  bounded foreground pass, lower the effort.
- `R runs` lists live jobs; `R kill` stops the codex child first, which ends the spend.
  Cancelling a background run (or Ctrl-C) does the same at any step, the router included: the
  run exits 130 and never goes on to a paid call. An interrupted `eval-compare` judge still
  saves codex's already-paid answer.
- Exit 2 means bad arguments, refused before any spend: a bad model or effort, an oversized or
  non-UTF-8 input, or a `--cwd` that is not a git worktree.
- Exit 3 means a `fix-round` found no thread for the topic; start with a first-round kind.
- Exit 1 means the codex call failed. stderr says why, nothing was saved, and re-running is
  safe. An auth error names `codex login`. A timeout says whether codex never started (fix the
  cause), was still working (raise `CODEX_REVIEW_TIMEOUT` or lower the effort), or wrote
  non-event output (check the codex-cli version that ends the message).
- Exit 1 can also mean part of the run was paid and kept. When the output file was
  unwritable, the paid result is printed to stdout. When `eval-compare`'s judge failed, codex's
  answer is saved and the last stdout line is its file. Read what was printed before re-running.
- A kill in the final moment can append a round twice. Check the file's tail and delete the
  duplicate section.
- Never edit the state directory (`~/.config/codex-review/state/`, or `CODEX_REVIEW_STATE_DIR`)
  by hand. If a state file is reported unusable, delete it and start a new thread.

## Environment variables

- `CODEX_BIN`: path to the `codex` binary (default `codex`)
- `CODEX_REVIEW_MODEL` / `CODEX_REVIEW_EFFORT`: a review's model / effort (below a CLI flag)
- `CODEX_REVIEW_OUT_DIR`: the review findings directory (below `--out-dir`)
- `CODEX_REVIEW_STATE_DIR`: where thread state lives (default `~/.config/codex-review/state`)
- `CODEX_REVIEW_TIMEOUT`: per-call timeout in seconds (default 3600)
- `CODEX_REVIEW_LABEL_SEED`: an integer that fixes `eval-compare`'s A/B shuffle, for reproducible runs
