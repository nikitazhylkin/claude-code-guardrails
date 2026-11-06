# claude-code-guardrails

Five small hooks for [Claude Code](https://docs.claude.com/en/docs/claude-code/hooks) that stop an agent from saying things it has not checked, and from doing a few things it should not do without a look first.

Each hook is one Python file with no dependencies beyond the standard library. They come from a set I run on my own agent work, rewritten here so they stand alone.

The idea behind them: a rule written in a markdown file does not stop an agent that is tired or in a hurry. A hook that reads the transcript and exits with code 2 does.

## What each hook stops

| Hook | Event | Stops |
|---|---|---|
| `claim_evidence.py` | Stop | "All tests pass", "deploy verified", "3x faster", "no bugs found" with no matching tool run in the same turn. Also stops "tests pass" when the last test output shows failures. Also stops a test count that was copied from a subagent report and printed by no shell of your own. |
| `served_artifact.py` | Stop | "It is live at https://..." without a fingerprint fetched from that URL (ETag, build id, bundle hash, a string only the new code has) and a negative control (the same check failing on the old build). A 200 and a screenshot are equally true of the old bytes. |
| `negative_claim.py` | Stop | "X is not installed" when a tool result in the same turn was paginated or truncated. Also "there is no way to..." about the outside world with no search scope and no reason given. |
| `model_ladder_gate.py` | PreToolUse (Agent) | A subagent dispatch that inherits the expensive chat model by accident, a reader or reviewer running on an expensive model, and a model from the "no implementation" list (default `fable`) on an implementation prompt. |
| `deploy_guard.py` | PreToolUse (Bash) | A production deploy (`vercel --prod`, `railway up`, `shopify theme push --live`) from a tree that is not a git repo, has no remote, has an unpushed HEAD, has modified tracked files, or has untracked files in the deployed path. |

What the Stop hooks ignore on purpose:

- Planning words ("make sure tests pass before you merge").
- Quoted or backticked mentions of a claim phrase.
- Markdown table cells (negative claims).
- Local URLs such as `localhost` (served artifact).
- Statements about the agent's own reach ("I have no way to write to disk here").

## Example of a blocked turn

The agent edits a file, never runs the suite, and ends its turn:

```
assistant: Fixed the parser. All tests pass now.
```

The Stop hook exits with code 2 and writes this to stderr, which Claude Code feeds back to the model:

```
[claim-evidence] BLOCKED: claimed "tests pass" with no matching tool run in this turn (tests_pass).
Run the check now and report its real output, or take the claim back. Re-run any number a subagent gave you in your own shell before you repeat it. Justified exception: GUARDRAILS_BYPASS=1.
```

The model then runs `pytest`, reads the result, and either reports the real output or takes the claim back. The hook only forces one extra round: when Claude Code sets `stop_hook_active`, the hooks let the turn end, so there is no loop.

## Install

1. Copy the `hooks/` folder into your project, for example to `.claude/guardrails/`. Keep `_common.py` next to the hooks, because they all import it.
2. Merge [`examples/settings.json`](examples/settings.json) into your `.claude/settings.json`. It wires the three Stop hooks, the Agent hook and the Bash hook. Use only the ones you want.
3. Optional: copy [`examples/guardrails.json`](examples/guardrails.json) somewhere and set `GUARDRAILS_CONFIG` to its path.

## Requirements

- Python 3 with the standard library only. The tests ran on Python 3.12 (Ubuntu). I also ran the hook scripts by hand on Python 3.14 (macOS). I have not run anything on older versions.
- `git` on the PATH, for `deploy_guard.py` and its tests.
- `pytest`, only to run the tests.

## Configuration reference

Everything is optional. Defaults apply when a setting is absent.

Environment variables:

| Variable | Used by | Default | Effect |
|---|---|---|---|
| `GUARDRAILS_BYPASS` | all hooks | unset | Set to exactly `1` and the hook exits 0 without checking. |
| `GUARDRAILS_CONFIG` | all hooks | unset | Path to a JSON config file (keys below). A missing, unreadable or invalid file means defaults. |
| `GUARDRAILS_LOG_DIR` | all hooks | unset (no logging) | Directory for one log file per hook. |
| `CLAIM_EVIDENCE_MODE` | `claim_evidence.py` | `block` | Any other value prints a WARN line and allows. |
| `SERVED_ARTIFACT_MODE` | `served_artifact.py` | `block` | Same. |
| `NEGATIVE_CLAIM_MODE` | `negative_claim.py` | `block` | Same. |
| `MODEL_LADDER_MODE` | `model_ladder_gate.py` | `block` | Same. |

`deploy_guard.py` has no warn mode.

JSON config keys:

| Section | Key | Default | Effect |
|---|---|---|---|
| `claim_evidence` | `extra_test_runners` | `[]` | Extra lowercase strings that count as a test run. They also count for "no bugs found". |
| `claim_evidence` | `extra_deploy_tokens` | `[]` | Extra strings that count as deploy evidence. |
| `claim_evidence` | `extra_perf_tokens` | `[]` | Extra strings that count as a timing or benchmark run. |
| `model_ladder` | `expensive_models` | `["opus", "fable"]` | Substrings (case-insensitive) of model names treated as expensive for readers and reviewers. |
| `model_ladder` | `no_implementation_models` | `["fable"]` | Substrings of model names that must not get an implementation prompt. |
| `model_ladder` | `cheap_model` | `"sonnet"` | Name shown in the block message. |
| `model_ladder` | `override_marker` | `"why-expensive:"` | A prompt line starting with this text, followed by a reason, keeps an expensive reader or reviewer. |
| `deploy_guard` | `extra_patterns` | `[]` | Regexes. A command segment that matches one is treated as a production deploy. Invalid regexes are skipped. |

Exit codes: `0` allows, `2` blocks (stderr goes back to the model). The Stop hooks always allow when Claude Code sets `stop_hook_active`, so a block costs at most one extra round.

For the full behaviour of each hook see [docs/HOOKS.md](docs/HOOKS.md).

## How the Stop hooks decide

Claude Code passes each hook a `transcript_path`. The hook reads the JSONL transcript and takes everything after the last real user message. Tool results and injected meta messages are also stored as `user` entries, and the hooks skip them, otherwise a turn that used tools would look like it used none.

From that turn the hook builds three things: the assistant text, a signature for every tool call (name plus arguments), and the text of every tool result. A claim pattern in the text needs a matching token in the tool signatures. Bad input or a missing transcript returns 0 (allow). `model_ladder_gate.py` and `deploy_guard.py` also catch any error of their own and allow. The three Stop hooks do not wrap their whole body, so an unexpected bug there would exit with a non-zero code other than 2, which Claude Code treats as a non-blocking error and does not block the turn.

## How to run the tests

From the repository root:

```
pip install pytest
python3 -m pytest -q
```

Last full run: 87 passed on Python 3.12.

The suite has 87 tests. Each hook has at least one input that must block and one that must pass, run through the real script as a subprocess. The Stop hooks use JSONL transcript fixtures in `tests/fixtures/`. The deploy guard tests build real git repositories with a bare remote.

## Known limits

- These are pattern checks, not proof. A model can write a claim in words the patterns do not know. `claim_evidence.py` checks that a matching tool ran, not that it tested the right thing.
- The patterns are English only.
- `deploy_guard.py` reads the command text. A heredoc or a multi-line string that has a line starting with a deploy command (for example a `vercel` line with `--prod`) is read as a deploy, because the command is split on newlines. Quoted one-line spans are stripped, but heredoc bodies are not. Write such files with an editor tool, or use `GUARDRAILS_BYPASS=1`.
- `deploy_guard.py` only knows the deploy commands listed in `docs/HOOKS.md` plus your `extra_patterns`. Other deploy tools pass.
- `claim_evidence.py` reads test output across the whole turn. A failure line from one command can mark a "tests pass" claim as failing even when a different command passed.
- The `tests_pass` evidence check is substring based: any tool call whose name or arguments contain a runner name (for example the word `jest` in a file path) counts as a test run.
- `negative_claim.py` treats any pagination marker in any tool result of the turn as partial data, even if it came from a different call than the one the claim is about.
- The hooks have not been tested against every Claude Code release. If a release changes the hook input or transcript format, check `_common.py`.

## License

MIT. See [LICENSE](LICENSE).
