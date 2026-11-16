"""Approved shell commands: an agent asks to run a command, you allow or deny it, Python runs it.

The agent calls the office tool `run_command`
(office_mcp.py); this module holds the rules and does the work:

    - blocked commands are refused at once (rm, sudo, git push, ...), and so is any
      command that names a secret file (the mail password, the web token, ~/.ssh, ...)
    - mode "off" (data/settings.json "commands_mode"): no agent may run commands
    - mode "ask" (the default): the command waits for you on the web page (Allow,
      Always allow, Deny), at most WAIT_SECONDS; then the agent hears "no answer"
    - "Always allow" saves the command on your list ("commands_allow"); a simple
      command that starts with an entry runs without asking. Never a command with
      ; | & $ ` > < (a chain could hide anything after the allowed start), and never
      one that reaches the network
    - the command runs in the job's folder (its isolated workspace when it has one),
      in its own process group, with a time limit; the agent gets the exit code and
      the end of the output

A request is a JSON file data/commands/<id>.json; the web page writes your decision
into it (decide), the waiting tool reads it.

Usage (from the project folder):
    python -m agentic_os.commands list          the waiting commands
    python -m agentic_os.commands allow|always|deny <id>
"""

import argparse
import json
import re
import shlex
import signal
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path

from agentic_os import memory
from agentic_os.ask import kill_group

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
COMMANDS_DIR = DATA_DIR / "commands"
MODES = ("ask", "off")
WAIT_SECONDS = 600  # how long a command waits for your answer
DEFAULT_TIMEOUT = 300  # how long a command may run, in seconds (the agent may ask for less or more)
MAX_TIMEOUT = 900
OUTPUT_CHARS = 6000  # the end of the output that goes back to the agent
POLL_SECONDS = 1.0
# Claude Code's limit for one MCP tool call, in ms: the wait, the run, and a margin.
TOOL_TIMEOUT_MS = (WAIT_SECONDS + MAX_TIMEOUT + 60) * 1000

# Refused anywhere in the command line, not only at its start ("cd x && rm -rf y").
BLOCKED = (
    "rm", "rmdir", "shred", "sudo", "su", "doas", "pkexec", "mkfs", "dd", "chown",
    "git commit", "git push", "git pull", "git reset", "git checkout", "git stash", "git merge",
    "git rebase", "git clean", "git switch", "git restore", "git worktree",
    "shutdown", "reboot", "systemctl", "crontab",
)
# Files no command may name: they hold secrets (safety.SECRET_FILES and agent_settings.json).
SECRET_PARTS = ("app-token", "mail.json", ".ssh", ".git-credentials", ".netrc", ".credentials.json",
                ".aws", ".gnupg", ".config/gh", ".config/agentic-os", ".docker/config.json", ".env")
# Commands that reach the network always ask, even when they are on your list.
NETWORK = re.compile(
    r"(^|[\s;&|(])(curl|wget|ssh|scp|sftp|ftp|nc|ncat|telnet|rsync|"
    r"git\s+(clone|fetch|ls-remote)|pip3?\s+install|uv\s+(pip\s+install|add|sync)|"
    r"npm\s+(i|install|ci|publish|update)|pnpm\s+(i|install|add)|yarn|cargo\s+(install|fetch)|"
    r"apt(-get)?|docker\s+(pull|push|login))(?=[\s;&|)]|$)")
CHAIN = re.compile(r"[;&|`$<>\n]")  # a command with these is never "simple"


def tidy(command):
    return " ".join(command.split())


def blocked(command):
    """Return the blocked word or secret file the command names, or None."""
    text = f" {tidy(command)} "
    for word in BLOCKED:
        pattern = r"\s+".join(re.escape(part) for part in word.split())
        if re.search(rf"(^|[\s;&|(`'\"/]){pattern}(?=[\s;&|)`'\"]|$)", text):
            return word
    for part in SECRET_PARTS:
        if part in text:
            return part
    return None


def allowed_by(command, allow_list):
    """Return the entry of your list that lets the command run without asking, or None.

    An entry matches the start of the command at a word end ("pytest" matches "pytest -q",
    not "pytestx"); a "*" in an entry matches anything. Only simple commands: no chain,
    no network.
    """
    text = tidy(command)
    if CHAIN.search(command) or NETWORK.search(text):
        return None
    for entry in allow_list:
        entry = tidy(str(entry))
        if not entry:
            continue
        pattern = ".*".join(re.escape(part) for part in entry.split("*"))
        if re.match(rf"{pattern}(\s|$)", text):
            return entry
    return None


def path_of(command_id):
    if not re.fullmatch(r"[0-9a-f]{12}", command_id or ""):
        raise ValueError(f"No command {command_id}.")
    return COMMANDS_DIR / f"{command_id}.json"


def lock():
    return memory.file_lock(DATA_DIR / ".commands.lock")


def load(command_id):
    path = path_of(command_id)
    if not path.exists():
        raise ValueError(f"No command {command_id}.")
    return json.loads(path.read_text())


def save(record):
    COMMANDS_DIR.mkdir(parents=True, exist_ok=True)
    memory.write_atomic(path_of(record["id"]), json.dumps(record, indent=2))


def now():
    # Milliseconds: two requests in one second must still sort in their order (of_job).
    return datetime.now().isoformat(timespec="milliseconds")


def request(job_id, command, reason, cwd, timeout, status="pending", allowed=None):
    """Save a new command request. Return it."""
    record = {"id": uuid.uuid4().hex[:12], "job": job_id, "command": command, "reason": reason,
              "cwd": str(cwd), "timeout": timeout, "status": status, "at": now(),
              "allowed_by": allowed, "decided_at": None, "exit_code": None, "output": None,
              "seconds": None}
    with lock():
        save(record)
    return record


def decide(command_id, decision, settings=None, save_setting=None):
    """Your answer to a waiting command: allow, always (allow and add it to your list) or deny.

    Return the record. Raise ValueError when it is not waiting any more (answered, or
    the agent stopped waiting). save_setting(name, value) saves your list (jobs.set_setting).
    """
    if decision not in ("allow", "always", "deny"):
        raise ValueError("The decision is allow, always or deny.")
    with lock():
        record = load(command_id)
        if record["status"] != "pending":
            raise ValueError(f"The command is not waiting any more ({record['status']}).")
        if decision == "always":
            if CHAIN.search(record["command"]) or NETWORK.search(tidy(record["command"])):
                raise ValueError("Only a simple command without ; | & $ ` > < and without the network can be "
                                 "always allowed. Use Allow for this one.")
            allow_list = list((settings or {}).get("commands_allow") or [])
            if tidy(record["command"]) not in allow_list:
                save_setting("commands_allow", [*allow_list, tidy(record["command"])])
        record["status"] = "denied" if decision == "deny" else "allowed"
        record["decided_at"] = now()
        save(record)
        return record


def set_status(command_id, status, **fields):
    """Change a record's status under the lock. Return the record."""
    with lock():
        record = load(command_id)
        record.update(status=status, **fields)
        save(record)
        return record


def wait(record, stopped=lambda: False, clock=time.monotonic, sleep=time.sleep):
    """Wait for your answer. Return "allowed", "denied", "no answer" or "stopped"."""
    end = clock() + WAIT_SECONDS
    while clock() < end:
        status = load(record["id"])["status"]
        if status != "pending":
            return status
        if stopped():
            break
        sleep(POLL_SECONDS)
    with lock():  # the answer may come in this moment: only a pending command gives up
        latest = load(record["id"])
        if latest["status"] != "pending":
            return latest["status"]
        latest["status"] = "stopped" if stopped() else "no answer"
        save(latest)
        return latest["status"]


def run(record, stopped=lambda: False):
    """Run an allowed command in its folder. Save and return the record with the result."""
    started = time.monotonic()
    set_status(record["id"], "running")
    # Its own process group: a timeout or a stop ends the shell and all it started.
    process = subprocess.Popen(record["command"], shell=True, cwd=record["cwd"], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
                               errors="replace", start_new_session=True)
    output, why = [], None
    reader = _reader(process, output)
    while process.poll() is None:
        if time.monotonic() - started > record["timeout"]:
            why = f"stopped after {record['timeout']} seconds"
        elif stopped():
            why = "stopped: the job was stopped"
        if why:
            kill_group(process.pid, signal.SIGKILL)
            break
        time.sleep(0.2)
    process.wait()
    reader.join(timeout=5)
    text = "".join(output)
    if len(text) > OUTPUT_CHARS:
        text = "...\n" + text[-OUTPUT_CHARS:]
    if why:
        text += f"\n[{why}]"
    return set_status(record["id"], "done", exit_code=process.returncode, output=text,
                      seconds=round(time.monotonic() - started, 1))


def _reader(process, output):
    """Read the output in a thread, so a full pipe never blocks the command."""
    import threading
    thread = threading.Thread(target=lambda: output.extend(iter(process.stdout.readline, "")), daemon=True)
    thread.start()
    return thread


def of_job(job_id):
    """Return the job's command requests, oldest first."""
    if not COMMANDS_DIR.is_dir():
        return []
    records = []
    for path in COMMANDS_DIR.glob("*.json"):
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if record.get("job") == job_id:
            records.append(record)
    return sorted(records, key=lambda record: record["at"])


def pending():
    """Return the commands that wait for your answer, oldest first."""
    if not COMMANDS_DIR.is_dir():
        return []
    records = []
    for path in COMMANDS_DIR.glob("*.json"):
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if record.get("status") == "pending":
            records.append(record)
    return sorted(records, key=lambda record: record["at"])


def main():
    from agentic_os import jobs  # here: jobs does not import this module at the top
    parser = argparse.ArgumentParser(description="Shell commands that agents ask to run.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="the commands that wait for your answer")
    for name in ("allow", "always", "deny"):
        commands.add_parser(name, help=f"{name} a waiting command").add_argument("id")
    args = parser.parse_args()
    if args.command == "list":
        for record in pending():
            print(f"{record['id']}  job {record['job']}  in {record['cwd']}\n    {record['command']}\n"
                  f"    why: {record['reason']}")
        if not pending():
            print("No command waits for you.")
        return
    try:
        record = decide(args.id, args.command, jobs.settings(), jobs.set_setting)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    print(f"{record['status']}: {shlex.quote(record['command'])}")


if __name__ == "__main__":
    main()
