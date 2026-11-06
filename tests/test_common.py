import sys

from conftest import HOOKS, assistant_text, tool_result, tool_use, user

sys.path.insert(0, str(HOOKS))
import _common as c  # noqa: E402


def test_turn_starts_at_last_real_user_message_not_at_a_tool_result():
    transcript = [user("one"), tool_use("a", "Bash", command="pytest"), tool_result("a", "1 passed"),
                  assistant_text("done")]
    turn = c.extract_turn(transcript)
    assert turn.tool_sigs == ["Bash command=pytest"]
    assert turn.tool_results == ["1 passed"]
    assert turn.assistant_texts == ["done"]


def test_earlier_turns_are_excluded():
    transcript = [user("one"), tool_use("a", "Bash", command="pytest"), tool_result("a", "1 passed"),
                  assistant_text("done"), user("two"), assistant_text("hi")]
    assert c.extract_turn(transcript).tool_sigs == []


def test_meta_messages_do_not_start_a_turn():
    transcript = [user("one"), tool_use("a", "Bash", command="ls"),
                  {"isMeta": True, "message": {"role": "user", "content": "hook feedback"}},
                  assistant_text("ok")]
    assert c.extract_turn(transcript).tool_sigs == ["Bash command=ls"]


def test_broken_config_file_falls_back_to_defaults(tmp_path, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    monkeypatch.setenv("GUARDRAILS_CONFIG", str(bad))
    assert c.load_config("claim_evidence") == {}


def test_logging_is_off_unless_a_directory_is_set(tmp_path, monkeypatch):
    monkeypatch.delenv("GUARDRAILS_LOG_DIR", raising=False)
    c.log("x", "nothing")
    monkeypatch.setenv("GUARDRAILS_LOG_DIR", str(tmp_path / "logs"))
    c.log("x", "something")
    assert "something" in (tmp_path / "logs" / "x.log").read_text()
