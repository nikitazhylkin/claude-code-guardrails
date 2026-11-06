#!/usr/bin/env python3
"""Stop hook: block "X is not there" claims built on partial data.

Two failure classes:

  partial_data      The message says "Y is not installed" / "missing from the list" and a
                    tool result in the same turn shows the data was paginated, truncated or
                    cursor-based. Page one of N cannot prove that something is absent.

  research_negative The message says "there is no way to ..." / "no such program exists"
                    about the outside world, without saying what was searched (scope) and
                    why the answer is no (reason). A negative with both is falsifiable and
                    passes.

Quoted mentions, table cells, and statements about the agent's own reach ("I have no way to
write to disk") are not claims.

Env: GUARDRAILS_BYPASS=1 turns it off. NEGATIVE_CLAIM_MODE=warn prints but does not block.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as c  # noqa: E402

HOOK = "negative_claim"

NEGATIVE_CLAIM_RE = re.compile("|".join([
    r"\b(?:is|are|was|were)\s+(?:not|n[o'’]t)\s+(?:installed|active|configured|enabled|present|in\s+use|set\s+up|deployed)\b",
    r"\b(?:doesn[''’]t|does\s+not)\s+(?:appear|exist|show\s+up|seem\s+to\s+be)\b",
    r"\bno\s+(?:longer\s+)?(?:installed|active|in\s+use|configured|enabled|present)\b",
    r"\b(?:not\s+)?(?:found|present|installed|active)\s+in\s+the\s+(?:list|admin|store|account)\b",
    r"\b(?:we\s+)?don[''’]t\s+(?:have|see|use)\s+\w+\s+(?:installed|active|enabled|configured)\b",
    r"\bmissing\s+from\s+(?:the\s+)?(?:list|admin|account|store|setup)\b",
    r"\bno\s+(?:trace|evidence|sign|record)\s+of\s+\w+\s+(?:in|on|at)\b",
    r"\bnot\s+(?:installed|active|configured|enabled|present)\s+(?:on|in|at)\s+(?:the\s+)?(?:store|account|admin|workspace)\b",
]), re.I)

PAGINATION_RE = re.compile("|".join([
    r'"nextPageToken"\s*:\s*"',
    r'"next_page_token"\s*:\s*"',
    r'"has_next_page"\s*:\s*true',
    r'"has_more"\s*:\s*true',
    r'"hasNextPage"\s*:\s*true',
    r'"last_page"\s*:\s*false',
    r'\bPage\s+\d+\s+of\s+\d+\b',
    r'\bShowing\s+\d+\s+of\s+\d+\b',
    r'\bDisplaying\s+\d+\s+of\s+\d+\b',
    r'"cursor"\s*:\s*"[^"]{10,}"',
    r'\(truncated\)',
    r'\b\.\.\.\s*\(more\b',
    r'Link:\s*<[^>]*>;\s*rel="next"',
    r'"page_info"\s*:\s*"',
]), re.I | re.M)

RESEARCH_NEGATIVE_RE = re.compile("|".join([
    r"\bthere\s+(?:is|are)\s+no\s+(?:way|option|options|path|program|programs|"
    r"provider|providers|scheme|schemes|product|products|service|services|"
    r"alternative|alternatives)\b",
    r"\bno\s+way\s+to\s+\w+",
    r"\b(?:nothing|no\s+options?|no\s+programs?|no\s+providers?)\s+(?:is\s+|are\s+)?"
    r"(?:available|exists?|out\s+there)\b",
    r"\bit\s+is\s+not\s+possible\s+to\b",
    r"\bnot\s+possible\s+(?:in|for|with)\s+\w+",
    r"\bdoes\s+not\s+exist\s+(?:in|for|on)\b",
]), re.I)

# A statement about the agent's own reach, or about local code and artifacts, is not a
# research claim: "I have no way to write them", "does not exist in the diff".
LOCAL_BEFORE_RE = re.compile(
    r"(?:\b(?:I|we|you)\s+(?:have|had|see|saw|found)\s*$"
    r"|\b(?:path|code|codebase|script|repo|module|function|file|branch|endpoint)\s+"
    r"[\w\s]{0,20}(?:contains|has|provides)\s*$)", re.I)
LOCAL_AFTER_RE = re.compile(
    r"^\s*(?:in|on|for)?\s*(?:the\s+)?(?:evidence|span|transcript|diff|screen|"
    r"site|page|repo|code|codebase|file|doc|ledger|layer|manifest|workspace)\b", re.I)

SCOPE_RE = re.compile(
    r"\b(?:I\s+(?:checked|searched|reviewed|swept|read|called|queried)|checked\s+\d+|"
    r"searched\s+\d+|scope\s*[:=]|of\s+\d+\s+(?:providers?|programs?|options?|files?)|"
    r"\d+\s+(?:providers?|programs?|options?|sources?|files?|sites?)\s+"
    r"(?:checked|searched|reviewed))\b", re.I)
REASON_RE = re.compile(
    r"\b(?:because|the\s+reason\s+is|below\s+(?:the\s+|every\s+)?(?:floor|minimum|threshold)|"
    r"above\s+(?:the\s+)?(?:cap|ceiling)|minimum\s+is|only\s+applies|excluded\s+because|"
    r"too\s+(?:small|low|large))\b", re.I)


def find_claims(text: str, rx: re.Pattern, *, local_guards: bool = False) -> list[tuple[str, int]]:
    out = []
    for m in rx.finditer(text):
        if c.is_quoted_mention(text, m.start(), m.end()) or c.on_table_row(text, m.start()):
            continue
        if local_guards:
            if LOCAL_BEFORE_RE.search(text[max(0, m.start() - 60):m.start()]):
                continue
            if LOCAL_AFTER_RE.search(text[m.end():m.end() + 40]):
                continue
        out.append((m.group(0), c.line_of(text, m.start())))
    return out


def partial_data_claims(text: str, tool_results: list[str]) -> list[tuple[str, int]]:
    claims = find_claims(text, NEGATIVE_CLAIM_RE)
    if not claims:
        return []
    if any(PAGINATION_RE.search(r) for r in tool_results):
        return claims
    return []


def research_negative_unmet(text: str) -> list[tuple[str, int]]:
    claims = find_claims(text, RESEARCH_NEGATIVE_RE, local_guards=True)
    if not claims:
        return []
    if SCOPE_RE.search(text) and REASON_RE.search(text):
        return []
    return claims


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
    text = turn.text

    flagged: list[str] = []
    partial = partial_data_claims(text, turn.tool_results)
    if partial:
        flagged.append('"' + partial[0][0] + '" was claimed from data that a tool result '
                       "shows is paginated or truncated")
    research = research_negative_unmet(text)
    if research:
        flagged.append('"' + research[0][0] + '" states no search scope and no reason')

    if not flagged:
        c.log(HOOK, "ok")
        return 0
    c.log(HOOK, "flagged: " + "; ".join(flagged))
    if payload.get("stop_hook_active"):
        return 0
    if os.environ.get("NEGATIVE_CLAIM_MODE", "block").strip().lower() != "block":
        sys.stderr.write("[negative-claim] WARN: " + "; ".join(flagged) + "\n")
        return 0
    sys.stderr.write(
        "[negative-claim] BLOCKED: " + "; ".join(flagged) + ". Page through the full data before "
        "you say something is absent, or say exactly how much you checked (\"I checked 50 of 212\"). "
        "For an outside-world negative, state what you searched and why the answer is no, or soften "
        "the claim. Justified exception: GUARDRAILS_BYPASS=1.\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
