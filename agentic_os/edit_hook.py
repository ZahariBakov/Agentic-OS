"""Edit hook: Claude Code runs this before every Edit, Write, MultiEdit or NotebookEdit of an agent.

Claude Code sends the event as JSON on stdin, for example:
    {"hook_event_name": "PreToolUse", "tool_name": "Edit", "cwd": "/work",
     "tool_input": {"file_path": "/work/app.py", ...}}
The job id comes from the environment variable AGENTIC_OS_JOB_ID, and the folder
where the agent may edit from AGENTIC_OS_EDIT_DIR (jobs.call_agent sets both, see
environment()). We save a copy of the file before the job's first edit of it
(changes.py), but only inside that folder: the hook runs before Claude Code's
permission check, so a refused edit (outside the folder, or a read-only agent)
must not leave a copy. A later undo would write that copy over your own file.

This hook must never stop the agent: on any error it writes a short message
to stderr and exits with 0. Exit code 0 with no output means "go on".
The command is set up in safety.agent_args().
"""

import json
import os
import sys
from pathlib import Path

from agentic_os import changes

JOB_ID_VARIABLE = "AGENTIC_OS_JOB_ID"
EDIT_DIR_VARIABLE = "AGENTIC_OS_EDIT_DIR"


def environment(job_id, edit_dir):
    """Return the environment variables for a job's agent. edit_dir None: it may not edit."""
    env = {JOB_ID_VARIABLE: job_id}
    if edit_dir is not None:
        env[EDIT_DIR_VARIABLE] = str(Path(edit_dir).resolve())
    return env


def handle(event, job_id, edit_dir=None):
    """Save the file of one event. Return True if a copy was saved."""
    if not job_id or not edit_dir or event.get("hook_event_name") != "PreToolUse":
        return False
    tool_input = event.get("tool_input") or {}
    file = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not file:
        return False
    # A relative path is relative to the agent's folder.
    path = (Path(event.get("cwd") or ".") / file).resolve()
    if not path.is_relative_to(Path(edit_dir).resolve()):
        return False  # outside the folder: Claude Code refuses the edit
    return changes.snapshot_before(job_id, path)


def main():
    try:
        handle(json.load(sys.stdin), os.environ.get(JOB_ID_VARIABLE), os.environ.get(EDIT_DIR_VARIABLE))
    except Exception as error:  # never block the agent because of our hook
        print(f"edit_hook: {error}", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()
