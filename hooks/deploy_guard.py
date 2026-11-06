#!/usr/bin/env python3
"""PreToolUse hook on Bash: refuse a production deploy from a tree that is not on a remote.

A deploy run from a working directory ships whatever sits on one machine, including the
absence of files a teammate added. Nobody can review it later or roll back to a known
commit. This hook looks at the command, and if it is a real production deploy, it checks
the git state of the deploy directory first.

Deploy commands matched (real invocations only, quoted spans are stripped, and a segment
that starts with grep/rg/echo/cat is not a deploy):
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
QUOTED_SPAN_RE = re.compile(r"'[^']*'|\"[^\"]*\"")
# Cut the command into segments first: `echo hi && <deploy>` is a deploy, not an echo.
SEGMENT_RE = re.compile(r"&&|\|\||;|\||\n")


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
    stripped = QUOTED_SPAN_RE.sub(" ", command)
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
