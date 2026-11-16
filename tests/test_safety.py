"""Tests for the agent permissions: the files that control agents are read-only."""

import json
import subprocess
import sys

from agentic_os import safety


def test_protected_files_are_denied_for_every_write_tool(temp_memory, tmp_path):
    args = safety.agent_args(tmp_path, ["Read", "Edit", "Write"])
    denied = args[args.index("--disallowedTools") + 1:]

    project = safety.PROJECT_DIR
    for path in [f"/{temp_memory}/**", f"/{project}/routines/**", f"/{project}/skills/**",
                 f"/{safety.SETTINGS_FILE}", f"/{project}/roster.json"]:
        for tool in ("Edit", "Write", "NotebookEdit"):
            assert f"{tool}({path})" in denied


def test_all_deny_rules_are_in_one_option(temp_memory, tmp_path):
    args = safety.agent_args(tmp_path, ["Read"])

    assert args.count("--disallowedTools") == 1


def test_edit_is_allowed_only_in_the_folder(temp_memory, tmp_path):
    args = safety.agent_args(tmp_path, ["Read", "Edit"])

    assert f"Edit(/{tmp_path.resolve()}/**)" in args
    assert "Read" in args


def test_the_edit_hook_code_is_protected():
    """The hook runs as the user before every edit: every project module it loads must be protected."""
    loaded = subprocess.run(
        [sys.executable, "-c", "import sys, agentic_os.edit_hook; "
         "print(' '.join(sorted(m for m in sys.modules if m.startswith('agentic_os'))))"],
        cwd=safety.PROJECT_DIR, capture_output=True, text=True, check=True).stdout.split()
    folders, files = safety.protected()
    for module in loaded:
        name = module.split(".")[-1] if "." in module else "__init__"
        assert safety.PROJECT_DIR / "agentic_os" / f"{name}.py" in files, module
    assert safety.PROJECT_DIR / ".git" in folders and safety.PROJECT_DIR / ".venv" in folders


def test_secret_files_cannot_be_read():
    deny = json.loads(safety.settings())["permissions"]["deny"]
    for rule in ("Read(~/.git-credentials)", "Read(~/.config/gh/**)", "Read(~/.claude/.credentials.json)"):
        assert rule in deny


def test_the_token_is_unreadable_for_every_agent(temp_memory, tmp_path):
    args = safety.agent_args(tmp_path, ["Read"])

    assert f"Read(/{safety.PROJECT_DIR}/data/app-token)" in args[args.index("--disallowedTools") + 1:]


def test_a_workspace_of_this_project_has_the_same_protected_files(temp_memory, tmp_path):
    worktree = tmp_path / "worktree"
    args = safety.agent_args(worktree, ["Edit"], {"worktree": str(worktree), "repo": str(safety.PROJECT_DIR)})
    denied = args[args.index("--disallowedTools") + 1:]

    root = worktree.resolve()
    for path in (f"/{root}/roster.json", f"/{root}/agentic_os/edit_hook.py", f"/{root}/routines/**",
                 f"/{root}/.venv/**", f"/{root}/.git"):
        assert f"Edit({path})" in denied


def test_a_workspace_of_another_project_protects_only_its_git_link(temp_memory, tmp_path):
    worktree = tmp_path / "worktree"
    args = safety.agent_args(worktree, ["Edit"], {"worktree": str(worktree), "repo": str(tmp_path / "other")})
    denied = args[args.index("--disallowedTools") + 1:]

    assert f"Edit(/{worktree.resolve()}/.git)" in denied
    assert f"Edit(/{worktree.resolve()}/roster.json)" not in denied


def test_the_hook_ignores_python_files_in_the_agents_folder(tmp_path):
    """The hook runs as the user in the agent's folder: a json.py there must not run."""
    marker = tmp_path / "planted-code-ran"
    (tmp_path / "json.py").write_text(f"open({str(marker)!r}, 'w').close()\n")
    command = json.loads(safety.settings())["hooks"]["PreToolUse"][0]["hooks"][0]["command"]

    done = subprocess.run(command, shell=True, cwd=tmp_path, input="{}", capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(tmp_path)})

    assert done.returncode == 0 and done.stderr == ""
    assert not marker.exists()
