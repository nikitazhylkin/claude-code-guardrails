#!/usr/bin/env python3
"""PreToolUse hook on the Agent tool: keep subagent spend on the cheapest model that fits.

The chat model is the manager and the cost ceiling. When it spawns subagents, expensive
models should do planning and hard reasoning, and cheap models should do routine work.
A rule in a markdown file does not stop a drifting agent. This hook does.

Rules (checked in this order, first block wins):

  a. A dispatch with no `model` field inherits the chat model. It is blocked unless the
     first 400 characters of the prompt carry a planning or review marker (nothing to
     execute yet, so the ceiling is fine).
  b. A dispatch to a model in `no_implementation_models` (default: fable) on an
     implementation-shaped prompt (implement, refactor, build, migrate, ...) is blocked.
  c. A READER (Explore agent, read-only research, search, inventory) or a REVIEWER (code,
     spec, plan or diff review, verification) running on an expensive model, or inheriting
     one, is blocked. A line "why-expensive: <reason>" in the prompt overrides it.
     When a dispatch is not clearly a reader or reviewer, it passes: a false block costs
     more than a missed expensive reader.

The hook fails open. Any parse error or bug in the hook returns 0 and is logged.

Config (optional JSON, section "model_ladder"):
  expensive_models          default ["opus", "fable"]   substrings, case-insensitive
  no_implementation_models  default ["fable"]
  cheap_model               default "sonnet"            named in the block message
  override_marker           default "why-expensive:"
Env: MODEL_LADDER_MODE=warn prints the message but allows. GUARDRAILS_BYPASS=1 turns it off.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as c  # noqa: E402

HOOK = "model_ladder_gate"
PROMPT_WINDOW = 400
HEAD_CHARS = 800

PLANNING_MARKERS = ["brainstorm", "plan", "planning", "review", "reviewer", "judge", "audit",
                    "research", "read-only", "verify only", "critique"]
IMPLEMENTATION_MARKERS = ["implement", "tdd", "write the code", "fix the", "edit", "refactor",
                          "stage", "bundle", "build", "ship", "migrate"]

ROLE_LINE_RE = re.compile(r"(?mi)^[ \t>*-]*role[ \t]*:[ \t]*(.+)$")
BUILDER_WORDS_RE = re.compile(
    r"\b(?:builder|implementer|implementor|executor|fixer|designer|developer|writer)\b", re.I)
READER_WORDS_RE = re.compile(r"\b(?:reader|researcher|explorer|scanner|auditor)\b", re.I)
REVIEWER_WORDS_RE = re.compile(r"\b(?:reviewer|verifier|qa|judge)\b", re.I)

BUILDER_MARKER_RE = re.compile(
    r"\b(?:implement|implementer|builder|executor|refactor|migrate|"
    r"write the code|build (?:the|a|an)|create (?:the|a|an)|stage (?:the|a|an))\b", re.I)

READER_RE = re.compile(
    r"\bread[- ]only\b|\bresearch(?:ing)?\b|\binventory\b|\blocate\b|"
    r"\bfind (?:all|every|where|out|the (?:files?|callers?|places?|usages?|code))\b|"
    r"\bsearch (?:for|the|through)\b|\bscan (?:the )?(?:logs?|files?|folders?)\b|"
    r"\bsummari[sz]e (?:the )?(?:files?|docs?|transcripts?|logs?)\b|"
    r"\bgather (?:the )?data\b|\bsurvey\b|\bgrep\b", re.I)

REVIEWER_RE = re.compile(
    r"\bcode[- ]review\b|\bspec[- ]review\b|\bplan[- ]review\b|\bdiff[- ]review\b|"
    r"\bre-?review\b|\bdo a review\b|"
    r"\b(?:a|the) review of (?:the|this|these) (?:\w+ ){0,2}"
    r"(?:diff|code|spec|plan|pr|patch|changes?|bundle)\b|\breview(?:ing)? (?:the|this|these) (?:\w+ ){0,2}"
    r"(?:diff|code|spec|plan|pr|patch|changes?|bundle)\b|"
    r"\bqa verif\w*|\bverify (?:the )?claims?\b|\bre-?run (?:the )?(?:checks?|tests?)\b|"
    r"\badversarial verif\w*|\bverification (?:pass|agent)\b|\bverifier\b|"
    r"\ba reviewer\b|\byou are (?:a |the )?reviewer\b", re.I)


def _s(v) -> str:
    return v if isinstance(v, str) else ""


def settings() -> dict:
    cfg = c.load_config("model_ladder")
    return {
        "expensive": [str(x).lower() for x in cfg.get("expensive_models", ["opus", "fable"])],
        "no_impl": [str(x).lower() for x in cfg.get("no_implementation_models", ["fable"])],
        "cheap": str(cfg.get("cheap_model", "sonnet")),
        "override": str(cfg.get("override_marker", "why-expensive:")),
    }


def has_marker(text: str, markers: list[str]) -> bool:
    return any(m in (text or "")[:PROMPT_WINDOW].lower() for m in markers)


def classify(tool_input: dict) -> str | None:
    """'reader', 'reviewer', or None (builder or unclear)."""
    description = _s(tool_input.get("description"))
    prompt = _s(tool_input.get("prompt"))
    head = description + "\n" + prompt[:HEAD_CHARS]

    m = ROLE_LINE_RE.search(prompt)
    if m:
        role = m.group(1)
        if BUILDER_WORDS_RE.search(role):
            return None
        if REVIEWER_WORDS_RE.search(role):
            return "reviewer"
        if READER_WORDS_RE.search(role):
            return "reader"
    if _s(tool_input.get("subagent_type")).strip().lower() == "explore":
        return "reader"
    if BUILDER_MARKER_RE.search(head) and not REVIEWER_RE.search(description):
        return None
    if REVIEWER_RE.search(head):
        return "reviewer"
    if READER_RE.search(head):
        return "reader"
    return None


def model_is_expensive(model, expensive: list[str]) -> bool:
    """True for no model, "inherit", or a model whose name contains an expensive name."""
    if not isinstance(model, str) or not model.strip():
        return True
    m = model.strip().lower()
    return m == "inherit" or any(e in m for e in expensive)


def decide(tool_input: dict, cfg: dict) -> tuple[str, str | None]:
    """Returns ("allow" | "block", reason)."""
    model = tool_input.get("model")
    prompt = _s(tool_input.get("prompt"))

    if not model:
        if not has_marker(prompt, PLANNING_MARKERS):
            return "block", ("no model field (would inherit the chat model) and no "
                             "planning or review marker in the prompt")
    elif (isinstance(model, str) and any(n in model.lower() for n in cfg["no_impl"])
          and has_marker(prompt, IMPLEMENTATION_MARKERS)):
        return "block", f"{model} on an implementation-shaped prompt"

    role = classify(tool_input)
    if role and model_is_expensive(model, cfg["expensive"]):
        override = re.compile(r"(?mi)^[ \t>*-]*" + re.escape(cfg["override"]) + r"[ \t]*\S")
        if not override.search(prompt):
            return "block", f"{role} on an expensive model without {cfg['override']}"
    return "allow", None


def main() -> int:
    if c.bypassed():
        return 0
    try:
        payload = c.read_hook_input()
        if (payload.get("tool_name") or payload.get("tool") or "") != "Agent":
            return 0
        tool_input = payload.get("tool_input", {})
        if not isinstance(tool_input, dict):
            return 0
        cfg = settings()
        verdict, reason = decide(tool_input, cfg)
        if verdict == "allow":
            c.log(HOOK, f"allow model={tool_input.get('model') or 'inherit'}")
            return 0
        c.log(HOOK, f"{verdict} ({reason})")
        message = (f"Name the model for this dispatch. Use {cfg['cheap']} for routine work (search, "
                   "reading, bulk edits, verification, review). Planning and hard reasoning may use the "
                   "chat model; implementation should not. To keep an expensive reader or reviewer, "
                   f"add a line: '{cfg['override']} <reason>'.")
        if os.environ.get("MODEL_LADDER_MODE", "block").strip().lower() != "block":
            sys.stderr.write(f"[model-ladder] WARN: {message}\n  reason: {reason}\n")
            return 0
        sys.stderr.write(f"[model-ladder] BLOCKED: {message}\n  reason: {reason}\n")
        return 2
    except Exception as exc:  # never block on the hook's own bug
        c.log(HOOK, f"error, allowing: {exc!r}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
