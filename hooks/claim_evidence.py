#!/usr/bin/env python3
"""Stop hook: block a final message that claims a result no tool run backs up.

The model says "all tests pass" or "the deploy is verified". The hook reads the
transcript for the current turn and asks: did a matching tool run in this turn?
If not, the turn is blocked (exit 2) and the reason goes to stderr, so the model
must run the check or take the claim back.

Claim classes:
  tests_pass       "tests pass", "all green"        needs a test runner in this turn,
                                                    and the last result must not be a failure
  no_issues        "no bugs found", "no errors"     needs a test runner or a browser tool
  deploy_verified  "deploy verified", "is live"     needs a deploy or fetch command
  perf_measured    "2x faster", "latency dropped"   needs a timing or benchmark tool
  pasted_number    "63 passed"                      the count came from a subagent report
                                                    and from no shell run of your own

Planning phrasing ("make sure tests pass") and quoted mentions are not claims.

Config (optional JSON file named by GUARDRAILS_CONFIG, section "claim_evidence"):
  extra_test_runners, extra_deploy_tokens, extra_perf_tokens: lists of lowercase strings.
Env: GUARDRAILS_BYPASS=1 turns the hook off. CLAIM_EVIDENCE_MODE=warn prints but does not block.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as c  # noqa: E402

HOOK = "claim_evidence"

TEST_RUNNERS = [
    "pytest", "npm test", "npm run test", "pnpm test", "yarn test", "vitest", "jest",
    "go test", "cargo test", "bun test", "mocha", "playwright test", "node --test",
    "node:test", "unittest", "rspec", "phpunit", "mvn test", "gradle test", "dotnet test",
]
BROWSER_TOOLS = ["playwright", "puppeteer", "cypress", "selenium"]
DEPLOY_TOKENS = ["vercel", "netlify", "railway", "fly deploy", "kubectl", "curl", "wget", "fetch",
                 "git push", "deploy"]
PERF_TOKENS = ["hyperfine", "timeit", "perf_counter", "pytest-benchmark", "benchmark",
               "console.time", "lighthouse", "autocannon", "/usr/bin/time", "time -p",
               "performance.now", "cprofile", "--profile", "py-spy"]

PATTERNS = [
    ("tests_pass",
     re.compile(r"\btests?\s+(?:pass(?:ed|es|ing)?|are\s+green|all\s+green|clean)\b", re.I)),
    ("no_issues",
     re.compile(r"\bno\s+(?:bugs|errors|issues|regressions)\s+"
                r"(?:found|present|detected|in\s+the\s+browser)\b", re.I)),
    ("deploy_verified",
     re.compile(r"\bdeploy(?:ment)?\s+(?:verified|succeeded|works|is\s+live|confirmed)", re.I)),
    ("perf_measured",
     re.compile(
         r"\b(?:measured|measurably|benchmarked)\s+(?:it\s+)?(?:as\s+|to\s+be\s+)?"
         r"(?:faster|quicker|slower|cheaper)\b"
         r"|\b(?:\d+(?:\.\d+)?\s*(?:x|%)|twice|half)\s+(?:as\s+)?(?:faster|quicker|slower)\b"
         r"|\bperformance\s+(?:improved|is\s+better|went\s+up|increased)\b"
         r"|\b(?:improved|boosted|increased)\s+(?:the\s+)?performance\b"
         r"|\b(?:latency|runtime|load\s+time|response\s+time|build\s+time|execution\s+time)\s+"
         r"(?:dropped|fell|went\s+down|decreased|improved|is\s+down)\b"
         r"|\b(?:runs?|loads?|executes?|renders?)\s+(?:\d+(?:\.\d+)?\s*(?:x|%)\s+)?"
         r"(?:faster|quicker)\s+now\b", re.I)),
]


def expected_tokens(category: str, cfg: dict) -> list[str]:
    extra = lambda key: [str(x).lower() for x in cfg.get(key, [])]  # noqa: E731
    runners = TEST_RUNNERS + extra("extra_test_runners")
    if category == "tests_pass":
        return runners
    if category == "no_issues":
        return runners + BROWSER_TOOLS
    if category == "deploy_verified":
        return DEPLOY_TOKENS + extra("extra_deploy_tokens")
    if category == "perf_measured":
        return PERF_TOKENS + extra("extra_perf_tokens")
    return []


def tests_verdict(tool_output: str) -> str:
    """'pass', 'fail' or 'unknown' from test-runner output. The LAST result wins,
    because a red run followed by a green run is an honest TDD turn."""
    verdict = "unknown"
    for m in re.finditer(
            r"(\d+)\s+failed(?:,\s*\d+\s+errors?)?(?:,\s*\d+\s+passed)?|\b\d+\s+passed\b",
            tool_output.lower()):
        verdict = "fail" if (m.group(1) and int(m.group(1)) > 0) else "pass"
    return verdict


def scan_claims(turn: c.Turn, cfg: dict | None = None) -> list[dict]:
    cfg = cfg or {}
    text = turn.text
    tools_blob = "\n".join(turn.tool_sigs).lower()
    results_blob = "\n".join(turn.tool_results)
    out: list[dict] = []
    for category, rx in PATTERNS:
        for m in rx.finditer(text):
            lead = text[max(0, m.start() - 48):m.start()]
            if c.NON_ASSERTION_RX.search(lead) or c.is_quoted_mention(text, m.start(), m.end()):
                continue
            excerpt = text[max(0, m.start() - 30):m.end() + 60].replace("\n", " ").strip()[:200]
            tokens = expected_tokens(category, cfg)
            if not any(t in tools_blob for t in tokens):
                out.append({"category": category, "kind": "no_evidence",
                            "matched": m.group(0), "excerpt": excerpt})
            elif category == "tests_pass" and tests_verdict(results_blob) == "fail":
                out.append({"category": category, "kind": "verdict_fail",
                            "matched": m.group(0), "excerpt": excerpt})
    return out


# --- pasted numbers: a count that came from a subagent, not from your shell ------

COUNT_WORD_RX = re.compile(
    r"\b(\d{1,6})\s+(?:passed|passing|green|tests?|specs?|checks?|assertions?)\b", re.I)
COUNT_RATIO_RX = re.compile(r"\b(\d{1,6})\s*/\s*(\d{1,6})\b")


def count_numbers(text: str) -> set[int]:
    out: set[int] = set()
    for m in COUNT_WORD_RX.finditer(text or ""):
        out.add(int(m.group(1)))
    for m in COUNT_RATIO_RX.finditer(text or ""):
        if m.group(1) == m.group(2):  # "63/63" counts; a date like 14/09 does not
            out.add(int(m.group(1)))
    return out


def collect_session_blocks(transcript: list[dict]) -> tuple[list[str], list[str]]:
    """(subagent report texts, shell output texts) over the whole transcript."""
    id_to_name: dict = {}
    subagent: list[str] = []
    bash: list[str] = []
    for entry in transcript:
        msg = entry.get("message", entry)
        content = msg.get("content", "")
        if isinstance(content, str):
            if "<task-notification>" in content:
                subagent.append(content)
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_use":
                id_to_name[block.get("id")] = block.get("name", "")
            elif btype == "text" and "<task-notification>" in block.get("text", ""):
                subagent.append(block.get("text", ""))
            elif btype == "tool_result":
                name = id_to_name.get(block.get("tool_use_id"), "")
                text = c.flatten_tool_result(block.get("content"))
                if not text:
                    continue
                if name in ("Agent", "Task"):
                    subagent.append(text)
                elif name == "Bash":
                    bash.append(text)
    return subagent, bash


def pasted_numbers_unmet(message: str, subagent_texts: list[str], bash_outputs: list[str]) -> list[int]:
    claimed = count_numbers(message)
    sub_counts: set[int] = set()
    for blob in subagent_texts:
        sub_counts |= count_numbers(blob)
    bash_blob = "\n".join(bash_outputs)
    return [n for n in sorted(claimed & sub_counts)
            if not re.search(r"(?<!\d)%d(?!\d)" % n, bash_blob)]


# --- entry point ------------------------------------------------------------------

def main() -> int:
    if c.bypassed():
        return 0
    payload = c.read_hook_input()
    transcript = c.load_transcript(payload.get("transcript_path", ""))
    if not transcript:
        return 0
    turn = c.extract_turn(transcript)
    if not turn.assistant_texts:
        return 0

    cfg = c.load_config("claim_evidence")
    problems: list[str] = []

    for u in scan_claims(turn, cfg):
        if u["kind"] == "verdict_fail":
            problems.append(f'claimed "{u["matched"]}" but the last test output shows failures')
        else:
            problems.append(f'claimed "{u["matched"]}" with no matching tool run in this turn '
                            f'({u["category"]})')

    sub, bash = collect_session_blocks(transcript)
    pasted = pasted_numbers_unmet(turn.text, sub, bash)
    if pasted:
        problems.append("the count(s) " + ", ".join(map(str, pasted)) + " appear in a subagent "
                        "report and in no shell output of this session")

    if not problems:
        c.log(HOOK, "ok")
        return 0

    c.log(HOOK, "flagged: " + "; ".join(problems))
    if payload.get("stop_hook_active"):
        return 0  # one forced continuation, never a loop
    if os.environ.get("CLAIM_EVIDENCE_MODE", "block").strip().lower() != "block":
        sys.stderr.write("[claim-evidence] WARN: " + "; ".join(problems) + "\n")
        return 0
    sys.stderr.write(
        "[claim-evidence] BLOCKED: " + "; ".join(problems) + ".\n"
        "Run the check now and report its real output, or take the claim back. "
        "Re-run any number a subagent gave you in your own shell before you repeat it. "
        "Justified exception: GUARDRAILS_BYPASS=1.\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
