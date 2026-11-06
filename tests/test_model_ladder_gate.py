import json

import pytest

from conftest import run_hook


def dispatch(prompt, model=None, subagent_type=None, description="", env_extra=None):
    tool_input = {"prompt": prompt, "description": description}
    if model:
        tool_input["model"] = model
    if subagent_type:
        tool_input["subagent_type"] = subagent_type
    return run_hook("model_ladder_gate.py", {"tool_name": "Agent", "tool_input": tool_input}, env_extra)


def test_blocks_dispatch_with_no_model_and_no_planning_marker():
    r = dispatch("Update the config loader to read YAML and add the new field.")
    assert r.returncode == 2
    assert "no model field" in r.stderr


def test_allows_no_model_when_the_prompt_is_planning():
    r = dispatch("Plan the migration order for the three services before anything is changed.")
    assert r.returncode == 0, r.stderr


def test_blocks_fable_on_an_implementation_prompt():
    r = dispatch("Implement the retry wrapper and refactor the callers.", model="claude-fable-5-1")
    assert r.returncode == 2
    assert "implementation-shaped" in r.stderr


def test_allows_sonnet_on_an_implementation_prompt():
    r = dispatch("Implement the retry wrapper and refactor the callers.", model="sonnet")
    assert r.returncode == 0, r.stderr


def test_blocks_reader_on_opus():
    r = dispatch("Read-only task: find all callers of parse_config and list them.", model="opus")
    assert r.returncode == 2
    assert "reader on an expensive model" in r.stderr


def test_blocks_reviewer_that_inherits_the_chat_model():
    r = dispatch("Do a code review of the diff in src/billing and list defects. Planning is done.")
    assert r.returncode == 2


def test_allows_reader_on_sonnet_and_explore_agent_on_sonnet():
    assert dispatch("Read-only: find all callers of parse_config.", model="sonnet").returncode == 0
    assert dispatch("Where is auth handled?", model="sonnet", subagent_type="Explore").returncode == 0


def test_blocks_explore_agent_on_opus():
    assert dispatch("Where is auth handled?", model="opus", subagent_type="Explore").returncode == 2


def test_override_line_keeps_an_expensive_reviewer():
    prompt = "Do a code review of the diff.\nwhy-expensive: the diff touches a custom allocator."
    assert dispatch(prompt, model="opus").returncode == 0


def test_hard_reasoning_builder_on_opus_passes():
    r = dispatch("Role: implementer. Implement the lock-free queue.", model="opus")
    assert r.returncode == 0, r.stderr


def test_unclear_dispatch_on_opus_passes():
    assert dispatch("Work out why the cache hit rate dropped after the last release.",
                    model="opus").returncode == 0


def test_warn_mode_allows_with_message():
    r = dispatch("Update the config loader.", env_extra={"MODEL_LADDER_MODE": "warn"})
    assert r.returncode == 0 and "WARN" in r.stderr


def test_ignores_other_tools_and_survives_garbage_input():
    assert run_hook("model_ladder_gate.py", {"tool_name": "Bash", "tool_input": {"command": "ls"}}).returncode == 0
    import subprocess, sys
    from conftest import HOOKS
    p = subprocess.run([sys.executable, str(HOOKS / "model_ladder_gate.py")], input="not json",
                       capture_output=True, text=True)
    assert p.returncode == 0


def test_config_changes_the_expensive_list_and_override(tmp_path):
    cfg = tmp_path / "g.json"
    cfg.write_text(json.dumps({"model_ladder": {"expensive_models": ["big"], "cheap_model": "small",
                                                "override_marker": "why-big:"}}))
    env = {"GUARDRAILS_CONFIG": str(cfg)}
    blocked = dispatch("Read-only: find all callers of parse_config.", model="big-1", env_extra=env)
    assert blocked.returncode == 2 and "small" in blocked.stderr
    ok = dispatch("Read-only: find all callers.\nwhy-big: huge repo", model="big-1", env_extra=env)
    assert ok.returncode == 0
    # opus is no longer in the expensive list under this config
    assert dispatch("Read-only: find all callers.", model="opus", env_extra=env).returncode == 0
