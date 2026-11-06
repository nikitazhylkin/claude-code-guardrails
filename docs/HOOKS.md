# Hook reference

One section per hook. For install steps and the settings table see the [README](../README.md).

All hooks share these rules:

- Input is the JSON Claude Code sends on stdin. Stop hooks use `transcript_path` and `stop_hook_active`. PreToolUse hooks use `tool_name`, `tool_input` and `cwd`.
- Exit `0` allows. Exit `2` blocks, and stderr goes back to the model.
- `GUARDRAILS_BYPASS=1` turns a hook off. This is the only bypass.
- Stop hooks allow the turn to end when `stop_hook_active` is true, so a block forces at most one more round.
- A "turn" is everything after the last real user message. Tool results and injected meta messages are stored as `user` entries and are not turn boundaries.
- Matching is case-insensitive and English only.

## claim_evidence.py (Stop)

**Reads:** the final assistant text of the turn, the tool calls of the turn (name plus arguments, lowercased), the text of the tool results of the turn, and for pasted numbers the whole transcript.

**Blocks when:**

| Class | Claim phrases (examples) | Needs in the turn |
|---|---|---|
| `tests_pass` | "tests pass", "tests are green", "all green", "tests passing" | A tool call containing a test runner name: pytest, npm test, pnpm test, yarn test, vitest, jest, go test, cargo test, bun test, mocha, playwright test, node --test, unittest, rspec, phpunit, mvn test, gradle test, dotnet test, plus `extra_test_runners`. Also blocks if the last pass or fail line in the tool output shows `N failed` with N above zero. |
| `no_issues` | "no bugs found", "no errors detected", "no regressions present" | A test runner (above) or playwright, puppeteer, cypress, selenium. |
| `deploy_verified` | "deployment verified", "deployment succeeded", "deployment is live" | A call containing vercel, netlify, railway, fly deploy, kubectl, curl, wget, fetch, git push or deploy, plus `extra_deploy_tokens`. |
| `perf_measured` | "3x faster", "latency dropped", "performance improved", "measured faster" | A call containing hyperfine, timeit, perf_counter, pytest-benchmark, benchmark, console.time, lighthouse, autocannon, /usr/bin/time, time -p, performance.now, cprofile, --profile, py-spy, plus `extra_perf_tokens`. |
| pasted number | A count like "63 passed", "12 tests" or "63/63" | The count must not appear only in a subagent report (an Agent tool result or a task notification) while no Bash output in the session contains the same number. |

**Ignores:**

- Planning or conditional wording right before the phrase: "make sure", "ensure", "until", "unless", "should", "verify", "check that", "once", "when", "if", "before", "I will", "we will", "need to", "going to".
- A phrase inside quotes or backticks on the same sentence.
- A claim in an earlier turn.

**Example block message:**

```
[claim-evidence] BLOCKED: claimed "Tests pass" but the last test output shows failures.
Run the check now and report its real output, or take the claim back. Re-run any number a subagent gave you in your own shell before you repeat it. Justified exception: GUARDRAILS_BYPASS=1.
```

**Bypass:** `GUARDRAILS_BYPASS=1`. For a trial run use `CLAIM_EVIDENCE_MODE=warn`.

## served_artifact.py (Stop)

**Reads:** the final assistant text, the tool call signatures of the turn and the tool results of the turn.

**Blocks when** the message contains a non-local `http(s)` URL within 300 characters of words like "is live", "now live", "went live", "published", "deployed", "shipped", "is up", "back up", "is fixed", "fixed on", and either of these is missing:

1. A fingerprint. Some tool call in the turn must have fetched that URL (curl, wget, fetch, playwright, puppeteer, navigate, screenshot and similar), and the message, the fetch command or the tool results must contain a build identity: etag, last-modified, build id, bundle hash, content hash, sha256, deployment id, a `?v=` token, a hex string of 8 to 64 characters with at least one letter, or wording like "only in the new build".
2. A negative control, stated in the message: "old build", "previous version", "control URL", "negative control", "before the deploy", "same check fails" and similar.

**Ignores:** URLs on localhost, 127.0.0.1, 0.0.0.0, `[::1]` and `*.local`, and URLs with no live-claim wording near them. Each URL is reported once.

**Example block message:**

```
[served-artifact] BLOCKED: you called a URL live, deployed or fixed without proving the served build is the new one. https://docs.example.org/guide is missing the served build's own fingerprint (nothing in this turn fetched that URL) and a negative control (the same check failing on the old build or on a control URL). A 200 and a screenshot are equally true of the old bytes. Fetch the URL now and quote its fingerprint (build id, bundle hash, ETag, or a string only the new code contains), then state the negative control: the same check failing on the old build or on a control URL. Honest alternative: say it is deployed and unverified. localhost URLs are exempt. Justified exception: GUARDRAILS_BYPASS=1.
```

**Bypass:** `GUARDRAILS_BYPASS=1`, or `SERVED_ARTIFACT_MODE=warn` to only print.

## negative_claim.py (Stop)

**Reads:** the final assistant text and the tool results of the turn.

**Blocks when:**

- *Partial data.* The message makes an absence claim ("is not installed", "is not configured", "does not exist", "does not appear", "missing from the list", "no trace of X in", "not found in the list") and any tool result in the turn carries a pagination or truncation marker: `nextPageToken`, `next_page_token`, `has_more: true`, `hasNextPage: true`, `last_page: false`, "Page N of M", "Showing N of M", "Displaying N of M", a long `cursor`, "(truncated)", a `Link: <...>; rel="next"` header or `page_info`.
- *Research negative.* The message says "there is no way", "there are no options", "no way to X", "nothing is available", "it is not possible to", or "does not exist in/for/on", and the message does not state both a search scope ("I checked 12", "searched 40", "scope:") and a reason ("because", "the minimum is", "below the floor", "too small").

**Ignores:** phrases in quotes or backticks, phrases on a markdown table row, and for research negatives, statements about the agent's own reach ("I have no way to...") or about local artifacts ("does not exist in the diff", "in the repo", "in the file").

**Example block message:**

```
[negative-claim] BLOCKED: "is not installed" was claimed from data that a tool result shows is paginated or truncated. Page through the full data before you say something is absent, or say exactly how much you checked ("I checked 50 of 212"). For an outside-world negative, state what you searched and why the answer is no, or soften the claim. Justified exception: GUARDRAILS_BYPASS=1.
```

**Bypass:** `GUARDRAILS_BYPASS=1`, or `NEGATIVE_CLAIM_MODE=warn`.

## model_ladder_gate.py (PreToolUse, matcher Agent)

**Reads:** `tool_input` of the Agent call: `model`, `prompt`, `description`, `subagent_type`.

**Blocks when** any of these is true, checked in order:

1. `model` is empty and the first 400 characters of the prompt contain none of: brainstorm, plan, planning, review, reviewer, judge, audit, research, read-only, verify only, critique.
2. `model` contains a name from `no_implementation_models` (default `fable`) and the first 400 characters contain one of: implement, tdd, write the code, fix the, edit, refactor, stage, bundle, build, ship, migrate.
3. The dispatch is a reader or reviewer and `model` is empty, `inherit`, or contains a name from `expensive_models` (default `opus`, `fable`), and the prompt has no override line.

A dispatch is a reader or reviewer when: a prompt line `Role: ...` names a reviewer, verifier, QA, judge, reader, researcher, explorer, scanner or auditor; or `subagent_type` is `Explore`; or the first 800 characters read like a code, spec, plan or diff review, or like read-only research, search or inventory work. A `Role:` line naming a builder, implementer, executor, fixer, designer, developer or writer, or builder wording in the head of the prompt, makes it not a reader or reviewer.

**Ignores:** other tools, and any dispatch that does not clearly look like a reader or reviewer. A false block is treated as worse than a missed expensive reader. An error inside the hook allows.

**Example block message:**

```
[model-ladder] BLOCKED: Name the model for this dispatch. Use sonnet for routine work (search, reading, bulk edits, verification, review). Planning and hard reasoning may use the chat model; implementation should not. To keep an expensive reader or reviewer, add a line: 'why-expensive: <reason>'.
  reason: reader on an expensive model without why-expensive:
```

**Bypass:** a prompt line `why-expensive: <reason>` (rule 3 only), `MODEL_LADDER_MODE=warn`, or `GUARDRAILS_BYPASS=1`.

## deploy_guard.py (PreToolUse, matcher Bash)

**Reads:** `tool_input.command` and the payload `cwd`. It then runs `git` in the deploy directory.

**Treats as a production deploy:**

- `vercel` with `--prod`
- `railway up`
- `shopify theme push` with `--live`
- `shopify theme publish`
- any command segment that matches a regex in `extra_patterns`

The command is cut into segments at `&&`, `||`, `;`, `|` and newlines. Quoted one-line spans are removed first. A segment whose first word is `grep`, `rg`, `ripgrep`, `ack`, `echo` or `cat` is not a deploy.

**Deploy directory:** the value of `--cwd` or `--path` on the command, otherwise the payload `cwd`.

**Blocks when** the deploy directory:

- is not inside a git repository,
- belongs to a repository with no remote,
- has a HEAD that is on no remote branch,
- has modified tracked files under the deploy path, or
- has untracked files under the deploy path.

All failures are listed in one message.

**Ignores:** commands that are not deploys, a clean tree whose HEAD is on a remote branch, and any error inside the hook (it allows).

**Example block message:**

```
[deploy-guard] BLOCKED production deploy (vercel).

  command: vercel --prod
  deploy dir: /work/app

  1. this repository has no remote (/work/app). Nothing here can be reviewed or rolled back. Add a remote and push before deploying.

  A production deploy should come from a commit that exists on a remote, so anyone can see
  what shipped and roll back to it. Fix the tree, then deploy.
  Override: GUARDRAILS_BYPASS=1 (the deploy then cannot be reviewed later).
```

**Known false positive:** a heredoc or multi-line string with a line that starts a deploy command is read as a deploy, because heredoc bodies are not stripped.

**Bypass:** `GUARDRAILS_BYPASS=1`.
