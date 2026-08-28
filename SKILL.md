---
name: codex-review
description: Use when a plan, design, or implementation is ready for external review, or when the user asks for a code review, an external review, or another review round. Runs one round of an OpenAI Codex code review against the working tree in a read-only sandbox, and tracks the review thread across fix-and-re-review rounds.
license: MIT
---

# Codex code review

Run one review round, apply the findings, then run fix rounds on the same thread until clean.
The reviewer is [OpenAI Codex](https://github.com/openai/codex) driven in a **read-only** sandbox:
the model runs no commands that write to your tree. The tool itself writes only the findings file
and its thread state, and sends the code under review to Codex.

## Requirements

- Python 3.9+ (standard library only)
- The `codex` CLI on `PATH`, authenticated once with `codex login`

## Run a round

1. Pick the review kind: `plan`, `design`, or `implementation` for a first round; `fix-round`
   after applying findings from an earlier round.
2. Pick a stable kebab-case topic slug for the whole review (one slug per feature; the thread and
   the findings file key on it).
3. Run (`--cwd` is the worktree to review):

   ```bash
   python3 scripts/run_review.py \
     --kind <kind> --topic <slug> \
     --doc <path> [--doc <path>...] \
     --ask "<focus ask, or for fix-round: what was fixed since last round>" \
     --cwd <worktree>
   ```

4. **Run it as a BACKGROUND task, not a foreground call.** At the default `xhigh` effort a round
   commonly takes **tens of minutes, sometimes 1-3 hours** on a real codebase, far longer than most
   agent harnesses' synchronous command timeout. A foreground call will hit that ceiling and report
   a timeout **even though codex is still working** (the review did not fail; the call just could
   not wait). Launch it in the background and poll for completion. A background task is also tracked
   and cancellable, so it is never an invisible runaway. If you need a bounded foreground pass, lower
   the effort (`--effort medium` or `low`) so the round finishes in time, accepting a lighter review.
5. The last stdout line is the findings file path. Read it and report the findings. The line above
   it is a one-line token-usage summary (`usage: … in / … out (… total)`).

### Options

- `--model <name>` / `--effort <minimal|low|medium|high|xhigh>` select the reviewer and its
  reasoning effort. Default is `gpt-5.6-sol` at `xhigh` (heavier reasoning = a better review);
  override for a cheaper/faster pass. Precedence: CLI flag > `CODEX_REVIEW_MODEL` /
  `CODEX_REVIEW_EFFORT` env > default. A bad `--effort` fails before Codex runs.
- `--out-dir <dir>` sets where findings are written, relative to `--cwd` (default
  `.codex-review/reviews/`, or `CODEX_REVIEW_OUT_DIR`).
- `--usage [text|json]` prints a fuller token-usage breakdown after the one-line summary.

## Check subscription usage (no review, no spend)

```bash
python3 scripts/run_review.py limits          # human summary
python3 scripts/run_review.py limits --json    # machine-readable
python3 scripts/run_review.py limits --strict  # also exit non-zero on NEAR
```

Reads codex's last persisted rate-limit snapshot off disk, so it costs **no tokens and makes no
network call**; it is as fresh as your last codex activity (the output states the timestamp). It
prints the plan, each window's used-percent and reset time, and a status of `OK` / `NEAR` /
`REACHED`. Exit: `0` normally, `1` on REACHED, `3` when no snapshot exists yet; `--strict` also
exits `1` on NEAR, so an agent can gate a long paid review on remaining headroom.

## See and stop in-flight runs

```bash
python3 scripts/run_review.py runs               # list in-flight reviews
python3 scripts/run_review.py kill <pid|topic>   # stop one
```

Every round registers a marker while live. `runs` lists each (pid, topic, model, elapsed, cwd) and
prunes dead entries; `kill` stops the codex child (ending the spend) then the wrapper. A stop signal
to a backgrounded run also tears the codex child down, so cancelling the background task stops the
spend.

## The fix loop (iterate to zero)

Codex is a **second, independent reviewer** (one that did not write the code), so it is good at
catching oversights and reasoning about correctness the author's own pass misses. Get the value by
looping to zero findings, not by taking one round as the verdict:

1. Run a first round (`--kind implementation --topic my-feature`).
2. Read the findings, apply the ones you agree with, and **push back with technical reasoning where
   the reviewer is wrong**: a real fix or a justified rebuttal, never a silent skip.
3. Run `--kind fix-round --topic my-feature --ask "fixed 1,3,4; pushed back on 2 because…"`. The
   reviewer re-checks each earlier finding against the current tree and surfaces anything new.
4. Repeat until a round returns nothing. In practice a topic converges in **roughly 6 rounds**;
   each round is cheaper as findings shrink.

Under spec-driven development, review each stage on its own topic as it is ready: the **plan**
(`--kind plan`), then the **design** (`--kind design`), then the **implementation**
(`--kind implementation`), looping each to zero before moving on.

**Keep reasoning high.** The default `gpt-5.6-sol` at `xhigh` effort is slow (tens of minutes a
round) on purpose: the extra reasoning is what makes the reviewer range across more areas and press
harder on edge cases. Lower `--effort` only for a deliberately quick, lighter pass.

## Exit codes

- `0` review completed; findings written
- `1` the Codex call failed (error on stderr; an auth error names `codex login`)
- `2` bad arguments (e.g. the topic is not a valid kebab slug)
- `3` no thread exists for this topic; start with a non-`fix-round` kind

## Environment variables

- `CODEX_BIN`: path to the `codex` binary (default `codex`)
- `CODEX_REVIEW_MODEL` / `CODEX_REVIEW_EFFORT`: model / effort fallback (below a CLI flag)
- `CODEX_REVIEW_OUT_DIR`: findings directory (below `--out-dir`)
- `CODEX_REVIEW_STATE_DIR`: where thread state lives (default `~/.config/codex-review/state`)
- `CODEX_REVIEW_TIMEOUT`: per-round timeout in seconds (default 3600)
