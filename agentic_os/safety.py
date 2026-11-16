"""Safety: the permissions of an agent.

Each agent gets:
    - agent_settings.json: a deny list of dangerous commands (no commit, no push,
      no rm, no internet), and Claude Code's auto memory turned off, so memory
      stays in our memory/ folder
    - a list of allowed tools; Edit and Write work only inside the job's folder
    - protected files: no agent may change them, not even in its own folder
      (the lead Ada works in the project folder, so she needs this)

A deny rule always wins over an allow rule. `claude -p` has nobody to ask,
so a tool that is not allowed is refused.
"""

import json
import shlex
import sys
from pathlib import Path

from agentic_os import memory

PROJECT_DIR = Path(__file__).resolve().parent.parent
SETTINGS_FILE = Path(__file__).with_name("agent_settings.json")
READ_TOOLS = ["Read", "Glob", "Grep"]
EDIT_TOOLS = ["Edit", "Write"]
FOLDER_TOOLS = ("Edit", "Write")  # these tools get the job's folder as a limit
WRITE_TOOLS = ("Edit", "Write", "NotebookEdit")  # denied for the protected files
# The project modules that the edit hook loads (tests/test_safety.py checks the list).
HOOK_MODULES = ("__init__", "edit_hook", "changes", "memory", "safety")
SECRET_FILES = ("app-token", "mail.json")  # in data/: no agent may read them


def protected():
    """Return (folders, files) that agents must never change.

    These files control the agents. An agent that could edit them could give
    itself more rights, or start jobs that run by themselves.
    A function, not a constant, so tests can move memory.MEMORY_DIR.
    """
    folders = [
        memory.MEMORY_DIR,  # notes: agents suggest them, Python writes them
        PROJECT_DIR / "routines",  # jobs that start by themselves
        PROJECT_DIR / "skills",  # changes go through calibrate and your accept
        PROJECT_DIR / "data",  # job state, lead sessions, proposals
        PROJECT_DIR / "backups",  # copies of data/
        PROJECT_DIR / ".claude",  # Claude Code settings and skills for this folder
        PROJECT_DIR / ".agents",
        PROJECT_DIR / ".git",  # git hooks and config run commands (core.fsmonitor, hooks/)
        PROJECT_DIR / ".venv",  # Python runs code from here (sitecustomize.py, packages)
    ]
    # The edit hook runs as you, with no deny list, before every edit of an agent.
    # These are the files it loads (HOOK_MODULES); a lead that works in this project
    # (Ada) could otherwise rewrite the hook and run any command through it.
    hook_files = [PROJECT_DIR / "agentic_os" / f"{name}.py" for name in HOOK_MODULES]
    files = [
        *hook_files,
        SETTINGS_FILE,  # the deny list itself
        PROJECT_DIR / "roster.json",  # the leads: model, tools, folder
        PROJECT_DIR / "CLAUDE.md",  # instructions for every Claude session here
        PROJECT_DIR / "AGENTS.md",  # the rules for every agent (CLAUDE.md imports it)
    ]
    return folders, files


def allow(tool, folder):
    """Return the allow rule for a tool. Edit and Write work only in the folder.

    "//" + an absolute path is how Claude Code writes an absolute path in a rule.
    """
    if tool in FOLDER_TOOLS:
        return f"{tool}(/{Path(folder).resolve()}/**)"
    return tool


def is_protected(path):
    """True when no agent may change this path (a protected file, or a file in a protected folder)."""
    folders, files = protected()
    path = Path(path)
    return path in files or any(path.is_relative_to(folder) for folder in folders)


def mirrored(root):
    """Return the protected folders and files of the project as they are inside a copy of it at root.

    A job's isolated workspace (workspaces.py) is a copy of the project. Without these
    rules Ada could change roster.json or the hook there, and Apply would copy it back.
    """
    folders, files = protected()
    return ([root / folder.relative_to(PROJECT_DIR) for folder in folders if folder.is_relative_to(PROJECT_DIR)],
            [root / file.relative_to(PROJECT_DIR) for file in files if file.is_relative_to(PROJECT_DIR)])


def deny_rules(workspace=None):
    """Return the rules that make the protected folders and files read-only.

    workspace: the job's active workspace dict ({"worktree", "repo", ...}) or None.
    "//" + an absolute path is how Claude Code writes an absolute path in a rule.
    """
    folders, files = protected()
    if workspace:
        root = Path(workspace["worktree"]).resolve()
        # .git in a worktree is a file that says where git's folder is. An agent that
        # changes it could point git to a config that runs commands (core.fsmonitor).
        files = [*files, root / ".git"]
        if Path(workspace["repo"]).resolve() == PROJECT_DIR:
            more_folders, more_files = mirrored(root)
            folders, files = [*folders, *more_folders], [*files, *more_files]
    paths = [f"/{folder}/**" for folder in folders] + [f"/{file}" for file in files]
    rules = [f"{tool}({path})" for path in paths for tool in WRITE_TOOLS]
    # The web page's token (access.py): an agent that reads it could approve its own plans.
    # The mail password (mail.py): the agent must never see it.
    # Absolute paths: a relative rule would only cover agents that run in this project.
    return [*rules, *[f"Read(/{PROJECT_DIR}/data/{name})" for name in SECRET_FILES]]


def settings():
    """Return the agent settings as a JSON string: agent_settings.json plus the edit hook.

    The hook (edit_hook.py) saves a file before an agent's first edit, so a
    job's changes can be undone (changes.py). The hook runs as you, in the
    agent's folder. `python -m` would put that folder first on sys.path, so a
    json.py that an agent wrote there would run as you. -I (isolated mode) adds
    no folder of the agent and ignores PYTHONPATH; we add only the project folder.
    """
    data = json.loads(SETTINGS_FILE.read_text())
    code = (f"import sys; sys.path.insert(0, {str(PROJECT_DIR)!r}); "
            "from agentic_os.edit_hook import main; main()")
    command = f"{shlex.quote(sys.executable)} -I -c {shlex.quote(code)}"
    data["hooks"] = {"PreToolUse": [{
        "matcher": "Edit|Write|MultiEdit|NotebookEdit",
        "hooks": [{"type": "command", "command": command, "timeout": 30}],
    }]}
    return json.dumps(data)


def agent_args(folder, tools, workspace=None):
    """Return the `claude` options with the permissions for one agent.

    All deny rules go into one --disallowedTools option: the option takes a list.
    --settings takes a file or a JSON string; we give a string with the hook added.
    workspace: the job's active workspace dict, or None (see deny_rules).
    """
    return [
        "--settings", settings(),
        "--allowedTools", *[allow(tool, folder) for tool in tools],
        "--disallowedTools", *deny_rules(workspace),
    ]


def no_tool_args():
    """Return the `claude` options for an agent with no tools: it only reads its task and answers.

    For text written by strangers (mail.py): a request hidden in a mail finds no tool to use.
    --tools "" turns off every built-in tool (an empty --allowedTools would not: read tools
    need no permission); --strict-mcp-config without --mcp-config loads no MCP server.
    The deny rules stay as a second lock.
    """
    return ["--settings", settings(), "--tools", "", "--strict-mcp-config",
            "--disallowedTools", *deny_rules()]
