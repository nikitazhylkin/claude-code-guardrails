"""Shared helpers for the guardrail hooks: transcript reading, config, logging.

Standard library only. Every hook imports this file from its own directory, so
copy the whole hooks/ folder when you install.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys
from pathlib import Path

QUOTE_CHARS = "\"'`«»“”‘’"


# --- hook input ---------------------------------------------------------------

def read_hook_input() -> dict:
    """Parse the JSON Claude Code sends on stdin. Never raises."""
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# --- config and logging ---------------------------------------------------------

def load_config(section: str) -> dict:
    """Read one section of the optional JSON config file.

    The file path comes from GUARDRAILS_CONFIG. A missing or broken file means
    "use the defaults": a config problem must never stop a turn.
    """
    path = os.environ.get("GUARDRAILS_CONFIG", "").strip()
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        sec = data.get(section, {})
        return sec if isinstance(sec, dict) else {}
    except Exception:
        return {}


def log(hook: str, message: str) -> None:
    """Append one line to $GUARDRAILS_LOG_DIR/<hook>.log. Off when the variable is unset."""
    d = os.environ.get("GUARDRAILS_LOG_DIR", "").strip()
    if not d:
        return
    try:
        p = Path(d)
        p.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        with (p / f"{hook}.log").open("a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {message}\n")
    except Exception:
        pass


def bypassed() -> bool:
    return os.environ.get("GUARDRAILS_BYPASS", "").strip() == "1"


# --- transcript ---------------------------------------------------------------

def load_transcript(path_str: str) -> list[dict]:
    """Read a Claude Code JSONL transcript. Bad lines are skipped."""
    if not path_str:
        return []
    p = Path(path_str)
    if not p.exists():
        return []
    out: list[dict] = []
    with p.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _role(entry: dict) -> str | None:
    return entry.get("role") or entry.get("message", {}).get("role")


def is_synthetic_user(entry: dict) -> bool:
    """True when a role=user entry is not a real human turn boundary.

    Tool results and injected meta messages are also stored as role=user.
    Treating them as the start of the turn hides every tool call before them.
    """
    if entry.get("isMeta") is True:
        return True
    msg = entry.get("message", entry)
    content = msg.get("content", "")
    if isinstance(content, list):
        types = {b.get("type") for b in content if isinstance(b, dict)}
        if types and types <= {"tool_result"}:
            return True
    return False


def flatten_tool_result(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text", ""))
            elif isinstance(c, str):
                parts.append(c)
        return "\n".join(parts)
    return ""


def tool_signature(block: dict) -> str:
    """`ToolName key=value key=value` for one tool_use block."""
    name = block.get("name", "")
    inp = block.get("input", {})
    if isinstance(inp, dict):
        flat = " ".join(f"{k}={v}" for k, v in inp.items() if isinstance(v, (str, int, float)))
    else:
        flat = str(inp)
    return f"{name} {flat}"


class Turn:
    """What happened since the last real user message."""

    def __init__(self) -> None:
        self.assistant_texts: list[str] = []
        self.tool_sigs: list[str] = []      # what was run: tool name plus arguments
        self.tool_results: list[str] = []   # what came back

    @property
    def text(self) -> str:
        return "\n".join(self.assistant_texts)


def extract_turn(transcript: list[dict]) -> Turn:
    turn = Turn()
    last_user = -1
    for i in range(len(transcript) - 1, -1, -1):
        e = transcript[i]
        if _role(e) == "user" and not is_synthetic_user(e):
            last_user = i
            break
    if last_user < 0:
        return turn
    for entry in transcript[last_user + 1:]:
        msg = entry.get("message", entry)
        role = msg.get("role")
        content = msg.get("content", [])
        if isinstance(content, str):
            if role == "assistant":
                turn.assistant_texts.append(content)
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text" and role == "assistant":
                turn.assistant_texts.append(block.get("text", ""))
            elif btype == "tool_use":
                turn.tool_sigs.append(tool_signature(block))
            elif btype == "tool_result":
                txt = flatten_tool_result(block.get("content"))
                if txt:
                    turn.tool_results.append(txt)
    return turn


# --- claim-matching guards ------------------------------------------------------

#: A mention inside planning or imperative phrasing is not an assertion:
#: "make sure tests pass before committing" must not block.
NON_ASSERTION_RX = re.compile(
    r"(?:make\s+sure|makes?\s+sure|ensure|ensuring|until|unless|should|"
    r"so\s+that|verify|check\s+that|confirm\s+that|once|when|if|before|"
    r"i\s+will|we\s+will|need\s+to|want\s+to|going\s+to|to\s+make)\s+"
    r"(?:the\s+|all\s+|that\s+)?[\w-]*\s*$", re.IGNORECASE)


def is_quoted_mention(text: str, start: int, end: int, window: int = 60) -> bool:
    """True when the match sits inside quotes or backticks: a message that
    DESCRIBES a claim phrase is not making the claim."""
    lead = text[max(0, start - window):start]
    tail = text[end:end + window]
    lead_q = max((lead.rfind(q) for q in QUOTE_CHARS), default=-1)
    if lead_q < 0 or any(c in lead[lead_q:] for c in ".!\n"):
        return False
    tail_positions = [tail.find(q) for q in QUOTE_CHARS if tail.find(q) >= 0]
    if not tail_positions:
        return False
    return "\n" not in tail[:min(tail_positions)]


def on_table_row(text: str, start: int) -> bool:
    line_start = text.rfind("\n", 0, start) + 1
    return text[line_start:start].lstrip().startswith("|")


def line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1
