import sys

import pytest

from conftest import (FIXTURES, HOOKS, assistant_text, run_stop_hook, tool_result, tool_use, user,
                      write_transcript)

sys.path.insert(0, str(HOOKS))
import negative_claim as nc  # noqa: E402


def fixture(name):
    return str(FIXTURES / name)


def test_blocks_absence_claim_made_from_a_paginated_result():
    r = run_stop_hook("negative_claim.py", fixture("negative_partial_data.jsonl"))
    assert r.returncode == 2
    assert "paginated or truncated" in r.stderr


def test_passes_when_the_result_was_complete():
    r = run_stop_hook("negative_claim.py", fixture("negative_full_data.jsonl"))
    assert r.returncode == 0, r.stderr


def test_blocks_research_negative_without_scope_and_reason(tmp_path):
    entries = [user("can we get funding?"),
               assistant_text("There is no way to get funding for this purchase.")]
    r = run_stop_hook("negative_claim.py", write_transcript(tmp_path, entries))
    assert r.returncode == 2
    assert "no search scope and no reason" in r.stderr


def test_passes_research_negative_with_scope_and_reason(tmp_path):
    entries = [user("can we get funding?"),
               assistant_text("There is no way to get funding for this purchase. I checked 12 programs, "
                              "and all of them have a minimum amount above this purchase, because "
                              "the minimum is 50,000.")]
    r = run_stop_hook("negative_claim.py", write_transcript(tmp_path, entries))
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("text", [
    "I have no way to write these files to disk from here.",
    "The sync path contains no way to send anything.",
    "The flag does not exist in the diff.",
    'The report said "there is no way to roll back" and it was wrong.',
])
def test_own_reach_local_artifacts_and_quotes_are_not_research_claims(tmp_path, text):
    assert nc.research_negative_unmet(text) == []


@pytest.mark.parametrize("text", [
    'The old note said "the plugin is not installed" and that was wrong.',
    "| plugin | status |\n|---|---|\n| analytics | is not installed |",
])
def test_quoted_mentions_and_table_cells_are_not_absence_claims(text):
    assert nc.partial_data_claims(text, ['{"has_more": true}']) == []


def test_stop_hook_active_bypass_warn():
    path = fixture("negative_partial_data.jsonl")
    assert run_stop_hook("negative_claim.py", path, stop_hook_active=True).returncode == 0
    assert run_stop_hook("negative_claim.py", path, env_extra={"GUARDRAILS_BYPASS": "1"}).returncode == 0
    warn = run_stop_hook("negative_claim.py", path, env_extra={"NEGATIVE_CLAIM_MODE": "warn"})
    assert warn.returncode == 0 and "WARN" in warn.stderr


@pytest.mark.parametrize("result", ['Showing 20 of 212', "Link: <https://x.example/?p=2>; rel=\"next\"",
                                    "(truncated)", '"hasNextPage": true'])
def test_pagination_signals_are_recognised(result):
    assert nc.partial_data_claims("Billing is not installed.", [result])
