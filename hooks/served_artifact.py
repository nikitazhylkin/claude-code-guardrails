#!/usr/bin/env python3
"""Stop hook: block "it is live" claims that only prove the OLD build is live.

A 200 status, a screenshot and "I deployed it" are all equally true of a build
that is two hours old. The only proof that the URL now serves the new build is:

  1. a fingerprint fetched from that URL in this turn (build id, bundle hash,
     ETag, or a string that only the new code contains), and
  2. a negative control: the same check failing on the old build or on a control URL.

One half alone proves nothing. A fingerprint with no control is a number nobody
compared. A control with no fingerprint is a comparison of nothing.

The hook looks at every non-local URL in the final message that sits near words
like "live", "deployed", "shipped", "fixed". localhost and *.local URLs are exempt.

Env: GUARDRAILS_BYPASS=1 turns it off. SERVED_ARTIFACT_MODE=warn prints but does not block.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as c  # noqa: E402

HOOK = "served_artifact"

# `]` stays in the class so an IPv6 loopback host (http://[::1]:9000/) survives
# and can be recognised as local.
HTTP_URL_RX = re.compile(r"https?://[^\s'\"<>)]+", re.I)

LOCAL_URL_RX = re.compile(
    r"^https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]|[\w.-]+\.local)"
    r"(?::\d+)?(?:[/?#]|$)", re.I)

SERVED_CLAIM_RX = re.compile(
    r"\b(?:is\s+live|now\s+live|went\s+live|live\s+(?:at|on|now)|goes\s+live|"
    r"published|deployed|shipped|is\s+up|back\s+up|"
    r"(?:is|are)\s+fixed|fixed\s+(?:on|at|in\s+production)|now\s+fixed)\b", re.I)

FETCH_VERB_RX = re.compile(
    r"\b(?:curl|wget|httpie|http\b|webfetch|web_fetch|fetch|playwright|puppeteer|"
    r"page\.goto|goto|navigate|screenshot|browser_navigate|lynx)\b", re.I)

FINGERPRINT_RX = re.compile(
    r"\betag\b|\bx-vercel-id\b|\bx-amz-(?:cf-)?id\b|\blast-modified\b|"
    r"\bbuild[\s_-]?id\b|\bbuildid\b|\bbundle\s+hash\b|\bcontent[\s_-]?hash\b|"
    r"\bsha-?256\b|\bdeployment[\s_-]?id\b|"
    r"\?v=[\w.-]{6,}|\b(?=[0-9a-f]*[a-f])[0-9a-f]{8,64}\b|"
    r"only\s+(?:in\s+)?the\s+new\s+(?:code|build|bytes|version|bundle)|"
    r"only\s+this\s+build|not\s+in\s+the\s+old\s+(?:build|code|version|bundle)", re.I)

NEG_CONTROL_RX = re.compile(
    r"\b(?:old|previous|prior|pre-?deploy(?:ment)?)\s+"
    r"(?:build|version|bundle|deploy(?:ment)?|url|bytes|page)\b"
    r"|\bcontrol\s+url\b|\bnegative\s+control\b|\bcontrol\s*[:=]"
    r"|\bbefore\s+the\s+(?:deploy|fix|change|push|release)\b"
    r"|\bsame\s+check\s+(?:fails|failed|returns|returned)\b"
    r"|\bfail(?:s|ed)?\s+on\s+the\s+(?:old|previous|control)\b"
    r"|\bnot\s+(?:present|found|there)\s+(?:in|on)\s+the\s+(?:old|previous)\b", re.I)


def url_key(url: str) -> str:
    return url.rstrip("/.,;:!?)").lower()


def sigs_that_fetched(url: str, tool_sigs: list[str]) -> list[str]:
    key = url_key(url)
    return [s for s in tool_sigs if key in s.lower() and FETCH_VERB_RX.search(s)]


def served_artifact_unmet(message: str, tool_sigs: list[str], tool_results: list[str]) -> list[dict]:
    """One entry per non-local URL called live without both halves of the proof."""
    if not message.strip():
        return []
    seen: set[str] = set()
    out: list[dict] = []
    for m in HTTP_URL_RX.finditer(message):
        url = m.group(0).rstrip(".,;:!?)")
        if url.endswith("]") and "[" not in url:
            url = url[:-1]
        key = url_key(url)
        if key in seen or LOCAL_URL_RX.match(url):
            continue
        window = message[max(0, m.start() - 300):m.end() + 300]
        if not SERVED_CLAIM_RX.search(window):
            continue
        seen.add(key)
        fetched = sigs_that_fetched(url, tool_sigs)
        proof_blob = message + "\n" + "\n".join(fetched) + "\n" + "\n".join(tool_results)
        missing: list[str] = []
        if not fetched:
            missing.append("the served build's own fingerprint (nothing in this turn fetched that URL)")
        elif not FINGERPRINT_RX.search(proof_blob):
            missing.append("the served build's own fingerprint (the fetch shows no build id, "
                           "bundle hash, ETag, or new-code-only string)")
        if not NEG_CONTROL_RX.search(message):
            missing.append("a negative control (the same check failing on the old build "
                           "or on a control URL)")
        if missing:
            out.append({"url": url, "missing": missing})
    return out


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
    unmet = served_artifact_unmet(turn.text, turn.tool_sigs, turn.tool_results)
    if not unmet:
        c.log(HOOK, "ok")
        return 0
    detail = "; ".join(f"{u['url']} is missing " + " and ".join(u["missing"]) for u in unmet)
    c.log(HOOK, "flagged: " + detail)
    if payload.get("stop_hook_active"):
        return 0
    if os.environ.get("SERVED_ARTIFACT_MODE", "block").strip().lower() != "block":
        sys.stderr.write("[served-artifact] WARN: " + detail + "\n")
        return 0
    sys.stderr.write(
        "[served-artifact] BLOCKED: you called a URL live, deployed or fixed without proving the "
        "served build is the new one. " + detail + ". A 200 and a screenshot are equally true of the "
        "old bytes. Fetch the URL now and quote its fingerprint (build id, bundle hash, ETag, or a "
        "string only the new code contains), then state the negative control: the same check failing "
        "on the old build or on a control URL. Honest alternative: say it is deployed and unverified. "
        "localhost URLs are exempt. Justified exception: GUARDRAILS_BYPASS=1.\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
