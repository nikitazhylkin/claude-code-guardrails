#!/usr/bin/env python3
"""PreToolUse hook on Bash: refuse a production deploy from a tree that is not on a remote.

A deploy run from a working directory ships whatever sits on one machine, including the
absence of files a teammate added. Nobody can review it later or roll back to a known
commit. This hook looks at the command, and if it is a real production deploy, it checks
the git state of the deploy directory first.

Deploy commands matched (real invocations only, quoted spans are stripped, and a segment
that starts with grep/rg/echo/cat is not a deploy). Terminated heredoc bodies are data and
are not judged, unless the command runs code anywhere (bash, sh, ssh, eval, ./script, *.sh),
in which case the bodies are judged too. An unterminated body is judged. `#` comments are
judged verbatim; `<<` inside (( )) arithmetic is not a heredoc:
  "vercel" with --prod        "railway up"        "shopify theme push" with --live
  "shopify theme publish"     plus any regex in the config key "extra_patterns"

Checks, all reported at once:
  - the deploy directory is not a git repository
  - the repository has no remote
  - HEAD is on no remote branch (committed after the last push)
  - tracked files are modified in the deployed path
  - untracked files sit inside the deployed path

The directory comes from --cwd / --path on the command, else the hook payload's cwd.

Config (optional JSON, section "deploy_guard"): extra_patterns, a list of regexes that
mark additional commands as production deploys.
Env: GUARDRAILS_BYPASS=1 turns it off. The hook fails open on its own errors.
"""
from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as c  # noqa: E402

HOOK = "deploy_guard"
MAX_PATHS = 10
READER_COMMANDS = {"grep", "rg", "echo", "cat", "ripgrep", "ack"}
# Cut the command into segments first: `echo hi && <deploy>` is a deploy, not an echo.
SEGMENT_RE = re.compile(r"&&|\|\||;|\||\n")




SHELL_RUNNERS = {"bash", "sh", "zsh", "dash", "ksh", "ssh", "eval", "source", "."}
_HEREDOC_WORD_END = set(" \t\n;&|<>()`")


def _first_word(segment: str) -> str | None:
    for w in segment.split():
        if w in ("sudo", "env") or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w):
            continue
        return w
    return None


def _runs_code(text: str) -> bool:
    """True when any segment hands code to a shell/remote runner or runs a script file
    (first word is a runner, starts with ./ or /, or ends with .sh)."""
    for seg in SEGMENT_RE.split(text):
        w = _first_word(seg)
        if w and (w in SHELL_RUNNERS or w.startswith(("./", "/")) or w.endswith(".sh")):
            return True
    return False


def _read_heredoc_delim(s: str, i: int) -> tuple[str, bool, int] | None:
    """Parse the delimiter after `<<` at s[i:]. Returns (delimiter, strip_tabs, next index) or None."""
    n = len(s)
    strip_tabs = False
    if i < n and s[i] == "-":
        strip_tabs = True
        i += 1
    while i < n and s[i] in " \t":
        i += 1
    if i >= n:
        return None
    if s[i] in "'\"":
        q = s[i]
        j = s.find(q, i + 1)
        if j == -1:
            return None
        return s[i + 1:j], strip_tabs, j + 1
    j = i
    while j < n and s[j] not in _HEREDOC_WORD_END:
        j += 1
    if j == i:
        return None
    return s[i:j], strip_tabs, j


def strip_quotes(command: str) -> str:
    """Return the command text to judge: quoted spans dropped (replaced by a space) and
    terminated heredoc bodies dropped. If anything in the command runs code (a shell/remote
    runner, a script path, a pipe into a shell), the heredoc bodies are kept and judged too."""
    text = _scan(command, keep_bodies=False)
    if _runs_code(text):
        return _scan(command, keep_bodies=True)
    return text


def _matching_arith_end(s: str, i: int) -> int:
    """s[i:i+2] == '((' : index just past the matching '))', or -1."""
    depth = 0
    j = i
    n = len(s)
    while j < n:
        if s[j] == "(":
            depth += 1
        elif s[j] == ")":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return -1


def _scan(command: str, keep_bodies: bool) -> str:
    s = command
    n = len(s)
    out: list[str] = []
    pending: list[tuple[str, bool]] = []
    i = 0
    while i < n:
        c = s[i]
        if c == "\\":
            out.append(s[i:i + 2])
            i += 2
        elif c == "'":
            j = s.find("'", i + 1)
            if j == -1:  # unterminated: keep judging the rest
                out.append(s[i:])
                break
            out.append(" ")
            i = j + 1
        elif c == '"':
            j = i + 1
            while j < n and s[j] != '"':
                j += 2 if s[j] == "\\" else 1
            if j >= n:
                out.append(s[i:])
                break
            out.append(" ")
            i = j + 1
        elif c == "#" and (i == 0 or s[i - 1] in " \t\n;&|("):
            j = s.find("\n", i)
            j = n if j == -1 else j
            out.append(s[i:j])
            i = j
        elif s.startswith("((", i):
            j = _matching_arith_end(s, i)
            if j == -1:
                out.append("((")
                i += 2
            else:
                out.append(s[i:j])
                i = j
        elif s.startswith("<<<", i):
            out.append("<<<")
            i += 3
        elif s.startswith("<<", i):
            parsed = _read_heredoc_delim(s, i + 2)
            if parsed is None:
                out.append("<<")
                i += 2
            else:
                delim, strip_tabs, i = parsed
                pending.append((delim, strip_tabs))
                out.append(" ")
        elif c == "\n" and pending:
            out.append("\n")
            i += 1
            for delim, strip_tabs in pending:
                body: list[str] = []
                terminated = False
                while i < n:
                    j = s.find("\n", i)
                    line = s[i:] if j == -1 else s[i:j]
                    i = n if j == -1 else j + 1
                    cand = line.lstrip("\t") if strip_tabs else line
                    if cand == delim or line.strip() == delim:
                        terminated = True
                        break
                    body.append(line + "\n")
                # Dropped only when the terminator was found; an unterminated body is judged.
                if keep_bodies or not terminated:
                    out.extend(body)
            pending = []
        else:
            out.append(c)
            i += 1
    return "".join(out)


def segment_kind(segment: str, extra: list[re.Pattern]) -> str | None:
    words = segment.split()
    if not words or words[0] in READER_COMMANDS:
        return None
    if re.search(r"\bvercel\b", segment) and re.search(r"(^|\s)--prod\b", segment):
        return "vercel"
    if re.search(r"\brailway\s+up\b", segment):
        return "railway"
    if re.search(r"\bshopify\s+theme\s+push\b", segment) and re.search(r"(^|\s)--live\b", segment):
        return "shopify"
    if re.search(r"\bshopify\s+theme\s+publish\b", segment):
        return "shopify"
    for rx in extra:
        if rx.search(segment):
            return "custom"
    return None


def deploy_kind(command: str, extra: list[re.Pattern] | None = None) -> str | None:
    stripped = strip_quotes(command)
    for segment in SEGMENT_RE.split(stripped):
        kind = segment_kind(segment, extra or [])
        if kind:
            return kind
    return None


def deploy_dir_from(command: str, payload_cwd: str) -> Path:
    base = Path(payload_cwd) if payload_cwd else Path.cwd()
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    for i, tok in enumerate(tokens):
        for flag in ("--cwd", "--path"):
            if tok == flag and i + 1 < len(tokens):
                return (base / tokens[i + 1]).expanduser().resolve()
            if tok.startswith(flag + "="):
                return (base / tok[len(flag) + 1:]).expanduser().resolve()
    return base.resolve()


def git(root: Path, *args: str) -> tuple[int, str]:
    try:
        p = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=5)
        return p.returncode, p.stdout
    except Exception as exc:
        return 1, str(exc)


def fmt_paths(paths: list[str]) -> str:
    shown = "\n".join(f"      {p}" for p in paths[:MAX_PATHS])
    return shown + (f"\n      +{len(paths) - MAX_PATHS} more" if len(paths) > MAX_PATHS else "")


def collect_failures(deploy_dir: Path) -> list[str]:
    code, out = git(deploy_dir, "rev-parse", "--show-toplevel")
    if code != 0 or not out.strip():
        return [f"{deploy_dir} is not a git repository, so the deploy cannot come from a commit "
                "that exists on a remote."]
    root = Path(out.strip())
    failures: list[str] = []

    _, remotes = git(root, "remote")
    if not remotes.strip():
        failures.append(f"this repository has no remote ({root}). Nothing here can be reviewed or "
                        "rolled back. Add a remote and push before deploying.")
    else:
        _, branches = git(root, "branch", "-r", "--contains", "HEAD")
        if not branches.strip():
            failures.append(f"HEAD is on no remote branch ({root}). Fix: git fetch, git push, then "
                            "deploy the pushed commit.")

    _, status = git(root, "status", "--porcelain", "--", str(deploy_dir))
    untracked, dirty = [], []
    for line in status.splitlines():
        if not line.strip():
            continue
        (untracked if line.startswith("??") else dirty).append(line[3:].strip())
    if dirty:
        failures.append("tracked files are modified in the deployed path. Commit and push first:\n"
                        + fmt_paths(dirty))
    if untracked:
        failures.append("untracked files sit inside the deployed path. No file should serve "
                        "production traffic while untracked in git:\n" + fmt_paths(untracked))
    return failures


def run() -> int:
    if c.bypassed():
        return 0
    payload = c.read_hook_input()
    if (payload.get("tool_name") or payload.get("tool") or "") != "Bash":
        return 0
    tool_input = payload.get("tool_input", {}) or {}
    command = str(tool_input.get("command") or "")
    if not command.strip():
        return 0

    cfg = c.load_config("deploy_guard")
    extra: list[re.Pattern] = []
    for pat in cfg.get("extra_patterns", []):
        try:
            extra.append(re.compile(str(pat)))
        except re.error:
            continue

    kind = deploy_kind(command, extra)
    if kind is None:
        return 0
    deploy_dir = deploy_dir_from(command, str(payload.get("cwd") or ""))
    failures = collect_failures(deploy_dir)
    if not failures:
        c.log(HOOK, f"ok: {kind} deploy allowed dir={deploy_dir}")
        return 0
    c.log(HOOK, f"BLOCKED {kind} deploy dir={deploy_dir} failures={len(failures)}")
    body = "\n\n".join(f"  {i}. {f}" for i, f in enumerate(failures, 1))
    sys.stderr.write(
        f"\n[deploy-guard] BLOCKED production deploy ({kind}).\n\n"
        f"  command: {command}\n  deploy dir: {deploy_dir}\n\n{body}\n\n"
        "  A production deploy should come from a commit that exists on a remote, so anyone can see\n"
        "  what shipped and roll back to it. Fix the tree, then deploy.\n"
        "  Override: GUARDRAILS_BYPASS=1 (the deploy then cannot be reviewed later).\n")
    return 2


def main() -> int:
    try:
        return run()
    except Exception as exc:
        c.log(HOOK, f"hook error, allowing: {exc!r}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
