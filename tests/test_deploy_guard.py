import json
import subprocess

import pytest

from conftest import run_hook


def sh(cwd, *args):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.org",
                        "HOME": str(cwd), "PATH": "/usr/bin:/bin:/usr/local/bin"})


@pytest.fixture
def pushed_repo(tmp_path):
    """A repo whose HEAD is on a remote branch, clean tree."""
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    work.mkdir()
    sh(tmp_path, "git", "init", "--bare", "-b", "main", str(remote))
    sh(work, "git", "init", "-b", "main")
    (work / "app.txt").write_text("v1\n")
    sh(work, "git", "add", "-A")
    sh(work, "git", "commit", "-m", "first")
    sh(work, "git", "remote", "add", "origin", str(remote))
    sh(work, "git", "push", "-u", "origin", "main")
    return work


def deploy(cwd, command="npx vercel --prod", env_extra=None):
    return run_hook("deploy_guard.py", {"tool_name": "Bash", "cwd": str(cwd),
                                        "tool_input": {"command": command}}, env_extra)


def test_allows_deploy_from_clean_pushed_tree(pushed_repo):
    r = deploy(pushed_repo)
    assert r.returncode == 0, r.stderr


def test_blocks_deploy_with_modified_tracked_file(pushed_repo):
    (pushed_repo / "app.txt").write_text("v2 not committed\n")
    r = deploy(pushed_repo)
    assert r.returncode == 2
    assert "tracked files are modified" in r.stderr and "app.txt" in r.stderr


def test_blocks_deploy_with_untracked_file(pushed_repo):
    (pushed_repo / "api.ts").write_text("export {}\n")
    r = deploy(pushed_repo)
    assert r.returncode == 2
    assert "untracked files" in r.stderr and "api.ts" in r.stderr


def test_blocks_deploy_when_head_is_not_pushed(pushed_repo):
    (pushed_repo / "app.txt").write_text("v2\n")
    sh(pushed_repo, "git", "commit", "-am", "second, not pushed")
    r = deploy(pushed_repo)
    assert r.returncode == 2
    assert "HEAD is on no remote branch" in r.stderr


def test_blocks_deploy_from_repo_with_no_remote(tmp_path):
    sh(tmp_path, "git", "init", "-b", "main")
    (tmp_path / "a.txt").write_text("x")
    sh(tmp_path, "git", "add", "-A")
    sh(tmp_path, "git", "commit", "-m", "x")
    r = deploy(tmp_path)
    assert r.returncode == 2 and "no remote" in r.stderr


def test_blocks_deploy_from_directory_that_is_not_a_repo(tmp_path):
    r = deploy(tmp_path)
    assert r.returncode == 2 and "not a git repository" in r.stderr


def test_other_deploy_commands_are_matched(tmp_path):
    for cmd in ["railway up", "shopify theme push --live", "shopify theme publish"]:
        assert deploy(tmp_path, cmd).returncode == 2, cmd


def test_deploy_hidden_behind_operators_is_still_a_deploy(tmp_path):
    assert deploy(tmp_path, "echo starting && vercel --prod").returncode == 2


@pytest.mark.parametrize("cmd", [
    "vercel", "vercel --prebuilt", "vercel dev", "railway status",
    "shopify theme push", "git push origin main",
    'echo "vercel --prod"', "grep -r 'railway up' docs/", "cat notes-about-vercel-prod.md",
])
def test_non_deploys_are_ignored(tmp_path, cmd):
    assert deploy(tmp_path, cmd).returncode == 0, cmd


def test_cwd_flag_picks_the_deploy_directory(pushed_repo, tmp_path):
    (pushed_repo / "api.ts").write_text("x")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    r = deploy(elsewhere, f"vercel --prod --cwd {pushed_repo}")
    assert r.returncode == 2 and "api.ts" in r.stderr


def test_bypass_env(tmp_path):
    assert deploy(tmp_path, env_extra={"GUARDRAILS_BYPASS": "1"}).returncode == 0


def test_extra_patterns_from_config(tmp_path):
    cfg = tmp_path / "g.json"
    cfg.write_text(json.dumps({"deploy_guard": {"extra_patterns": [r"\bfly\s+deploy\b", "[bad("]}}))
    env = {"GUARDRAILS_CONFIG": str(cfg)}
    assert deploy(tmp_path, "fly deploy", env_extra=env).returncode == 2
    assert deploy(tmp_path, "fly deploy").returncode == 0  # not matched without the config


def test_non_bash_tool_and_garbage_input_pass(tmp_path):
    assert run_hook("deploy_guard.py", {"tool_name": "Edit", "tool_input": {}}).returncode == 0
