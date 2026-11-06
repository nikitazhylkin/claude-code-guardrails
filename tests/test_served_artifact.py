import sys

import pytest

from conftest import (FIXTURES, HOOKS, assistant_text, run_stop_hook, tool_result, tool_use, user,
                      write_transcript)

sys.path.insert(0, str(HOOKS))
import served_artifact as sa  # noqa: E402


def fixture(name):
    return str(FIXTURES / name)


def test_blocks_live_claim_with_no_fetch_and_no_control():
    r = run_stop_hook("served_artifact.py", fixture("served_live_no_proof.jsonl"))
    assert r.returncode == 2
    assert "docs.example.org/guide" in r.stderr
    assert "fingerprint" in r.stderr and "negative control" in r.stderr


def test_passes_with_fetched_fingerprint_and_negative_control():
    r = run_stop_hook("served_artifact.py", fixture("served_live_with_proof.jsonl"))
    assert r.returncode == 0, r.stderr


def test_fingerprint_without_control_still_blocks(tmp_path):
    entries = [user("ship"), tool_use("a", "Bash", command="curl -sI https://docs.example.org/guide"),
               tool_result("a", 'etag: "9f3a1c7be2d4"'),
               assistant_text("It is live at https://docs.example.org/guide, etag 9f3a1c7be2d4.")]
    r = run_stop_hook("served_artifact.py", write_transcript(tmp_path, entries))
    assert r.returncode == 2
    assert "negative control" in r.stderr
    assert "fingerprint" not in r.stderr.split("is missing")[1].split("negative control")[0]


def test_control_without_fetch_still_blocks(tmp_path):
    entries = [user("ship"),
               assistant_text("It is live at https://docs.example.org/guide. "
                              "Negative control: the check fails on the old build.")]
    r = run_stop_hook("served_artifact.py", write_transcript(tmp_path, entries))
    assert r.returncode == 2
    assert "nothing in this turn fetched" in r.stderr


@pytest.mark.parametrize("url", ["http://localhost:3000/", "http://127.0.0.1:8080/x",
                                 "http://[::1]:9000/", "http://printer.local/status"])
def test_local_urls_are_exempt(tmp_path, url):
    entries = [user("run it"), assistant_text(f"The app is live at {url} now.")]
    r = run_stop_hook("served_artifact.py", write_transcript(tmp_path, entries))
    assert r.returncode == 0


def test_url_without_a_live_claim_is_ignored(tmp_path):
    entries = [user("docs?"), assistant_text("The guide is described at https://docs.example.org/guide.")]
    r = run_stop_hook("served_artifact.py", write_transcript(tmp_path, entries))
    assert r.returncode == 0


def test_stop_hook_active_and_bypass_and_warn():
    path = fixture("served_live_no_proof.jsonl")
    assert run_stop_hook("served_artifact.py", path, stop_hook_active=True).returncode == 0
    assert run_stop_hook("served_artifact.py", path, env_extra={"GUARDRAILS_BYPASS": "1"}).returncode == 0
    warn = run_stop_hook("served_artifact.py", path, env_extra={"SERVED_ARTIFACT_MODE": "warn"})
    assert warn.returncode == 0 and "WARN" in warn.stderr


def test_unit_unmet_lists_each_url_once():
    msg = "https://a.example.org is live. Also https://a.example.org/ is live."
    assert len(sa.served_artifact_unmet(msg, [], [])) == 1
