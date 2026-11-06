import json
import sys

import pytest

from conftest import (FIXTURES, HOOKS, assistant_text, run_stop_hook, tool_result, tool_use, user,
                      write_transcript)

sys.path.insert(0, str(HOOKS))
import claim_evidence as ce  # noqa: E402


def fixture(name):
    return str(FIXTURES / name)


# --- end to end, with real transcript fixtures --------------------------------------

def test_blocks_tests_pass_claim_with_no_test_run():
    r = run_stop_hook("claim_evidence.py", fixture("claim_tests_pass_no_run.jsonl"))
    assert r.returncode == 2
    assert "BLOCKED" in r.stderr and "tests_pass" in r.stderr


def test_passes_when_pytest_ran_green():
    r = run_stop_hook("claim_evidence.py", fixture("claim_tests_pass_with_pytest.jsonl"))
    assert r.returncode == 0, r.stderr


def test_blocks_tests_pass_claim_when_last_run_was_red():
    r = run_stop_hook("claim_evidence.py", fixture("claim_tests_pass_but_red.jsonl"))
    assert r.returncode == 2
    assert "failures" in r.stderr


def test_stop_hook_active_allows_one_forced_continuation():
    r = run_stop_hook("claim_evidence.py", fixture("claim_tests_pass_no_run.jsonl"),
                      stop_hook_active=True)
    assert r.returncode == 0


def test_bypass_env_turns_hook_off():
    r = run_stop_hook("claim_evidence.py", fixture("claim_tests_pass_no_run.jsonl"),
                      env_extra={"GUARDRAILS_BYPASS": "1"})
    assert r.returncode == 0


def test_warn_mode_prints_but_does_not_block():
    r = run_stop_hook("claim_evidence.py", fixture("claim_tests_pass_no_run.jsonl"),
                      env_extra={"CLAIM_EVIDENCE_MODE": "warn"})
    assert r.returncode == 0
    assert "WARN" in r.stderr


def test_missing_transcript_is_a_no_op(tmp_path):
    r = run_stop_hook("claim_evidence.py", str(tmp_path / "nope.jsonl"))
    assert r.returncode == 0


# --- claim classes --------------------------------------------------------------------

def claims_for(text, tools=()):
    entries = [user("go")]
    for i, cmd in enumerate(tools):
        entries += [tool_use(f"t{i}", "Bash", command=cmd), tool_result(f"t{i}", "ok")]
    entries.append(assistant_text(text))
    return entries


def scan(tmp_path, text, tools=()):
    import _common as c
    path = write_transcript(tmp_path, claims_for(text, tools))
    turn = c.extract_turn(c.load_transcript(path))
    return ce.scan_claims(turn)


def test_deploy_verified_needs_a_deploy_or_fetch_command(tmp_path):
    assert scan(tmp_path, "Deployment verified.")
    assert not scan(tmp_path, "Deployment verified.", tools=["curl -s https://x.example/health"])


def test_perf_claim_needs_a_timing_tool(tmp_path):
    assert scan(tmp_path, "The parser is now 3x faster.")
    assert not scan(tmp_path, "The parser is now 3x faster.", tools=["hyperfine 'python parse.py'"])


def test_hedged_perf_wording_is_free(tmp_path):
    assert not scan(tmp_path, "This should improve performance, but I did not measure it.")


def test_no_issues_claim_needs_a_runner_or_browser(tmp_path):
    assert scan(tmp_path, "No bugs found in the checkout flow.")
    assert not scan(tmp_path, "No bugs found in the checkout flow.", tools=["npx playwright test"])


@pytest.mark.parametrize("text", [
    "Make sure tests pass before you merge.",
    "I will run the suite until tests pass.",
    'The old message said "tests pass" and that was wrong.',
    "Use `tests pass` as the exit marker.",
])
def test_planning_and_quoted_mentions_are_not_claims(tmp_path, text):
    assert not scan(tmp_path, text)


def test_extra_test_runner_from_config(tmp_path):
    import _common as c
    path = write_transcript(tmp_path, claims_for("Tests pass.", tools=["make check-all"]))
    turn = c.extract_turn(c.load_transcript(path))
    assert ce.scan_claims(turn)
    assert not ce.scan_claims(turn, {"extra_test_runners": ["make check-all"]})


def test_config_file_is_read(tmp_path):
    cfg = tmp_path / "g.json"
    cfg.write_text(json.dumps({"claim_evidence": {"extra_test_runners": ["make check-all"]}}))
    path = write_transcript(tmp_path, claims_for("Tests pass.", tools=["make check-all"]))
    r = run_stop_hook("claim_evidence.py", path, env_extra={"GUARDRAILS_CONFIG": str(cfg)})
    assert r.returncode == 0, r.stderr


def test_test_runner_in_earlier_turn_does_not_count(tmp_path):
    entries = [user("first"), tool_use("a", "Bash", command="pytest"), tool_result("a", "3 passed"),
               assistant_text("done"), user("second"), assistant_text("All tests pass.")]
    r = run_stop_hook("claim_evidence.py", write_transcript(tmp_path, entries))
    assert r.returncode == 2


# --- pasted numbers --------------------------------------------------------------------

def subagent_turn(subagent_report, shell_output, claim):
    return [user("run the suite with a helper"),
            tool_use("ag", "Agent", prompt="run tests"), tool_result("ag", subagent_report),
            tool_use("sh", "Bash", command="pytest -q"), tool_result("sh", shell_output),
            assistant_text(claim)]


def test_blocks_count_copied_from_subagent_report(tmp_path):
    entries = subagent_turn("Result: 63 passed", "5 passed in 0.1s", "Suite is healthy: 63 passed.")
    r = run_stop_hook("claim_evidence.py", write_transcript(tmp_path, entries))
    assert r.returncode == 2
    assert "63" in r.stderr and "subagent" in r.stderr


def test_allows_count_that_my_own_shell_printed(tmp_path):
    entries = subagent_turn("Result: 63 passed", "63 passed in 2.0s", "Suite is healthy: 63 passed.")
    r = run_stop_hook("claim_evidence.py", write_transcript(tmp_path, entries))
    assert r.returncode == 0, r.stderr


def test_ratio_counts_only_when_both_halves_match():
    assert ce.count_numbers("63/63 checks") == {63}
    assert ce.count_numbers("see 14/09 for the date") == set()
