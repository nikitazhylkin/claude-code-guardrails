"""Test helpers: build Claude Code style JSONL transcripts and run a hook as a subprocess."""
import json
import os
import subprocess
import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def user(text):
    return {"message": {"role": "user", "content": text}}


def assistant_text(text):
    return {"message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def tool_use(tool_id, name, **inp):
    return {"message": {"role": "assistant",
                        "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": inp}]}}


def tool_result(tool_id, text):
    return {"message": {"role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": tool_id,
                                     "content": [{"type": "text", "text": text}]}]}}


def write_transcript(tmp_path, entries):
    p = tmp_path / "transcript.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return str(p)


def run_hook(name, payload, env_extra=None, cwd=None):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("GUARDRAILS_", "CLAIM_EVIDENCE_", "SERVED_ARTIFACT_",
                                "NEGATIVE_CLAIM_", "MODEL_LADDER_"))}
    if env_extra:
        env.update(env_extra)
    return subprocess.run([sys.executable, str(HOOKS / name)], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, cwd=cwd)


def run_stop_hook(name, transcript_path, **extra):
    env_extra = extra.pop("env_extra", None)
    payload = {"session_id": "test", "transcript_path": transcript_path, **extra}
    return run_hook(name, payload, env_extra)
