# Agentic OS

A small "operating system" for Claude Code agents, written in Python.

Every task is a **job**. Each job runs as a `claude -p` process. Python chooses the model,
gives the agent the right memory, checks the result and keeps the cost. You watch and
steer everything from a local web page.

This is a learning project: the code and the notes are in simple English, and the
project grows in small steps.

## What it does

- **Jobs with live output.** Start a task from the web page or the command line. You see
  the agent's text and tool calls as they happen, the model, the time and the cost.
- **The right model for each job.** Free keyword rules first, then a cheap Haiku call that
  chooses the model (Haiku, Sonnet or Opus) and the effort.
- **Leads.** Long-running agents with a name, an area and their own session (`roster.json`).
  A lead keeps its context between jobs and writes a short handoff for the next session.
- **"Done" means checked.** A job can have a check (for example `pytest`). The job is done
  only when the check passes. A failure goes back to the same agent with the output.
- **A job is a conversation.** The agent can ask you a question, propose a plan with a cost
  estimate, and you can write to it during and after the job.
- **Review and lessons.** Approve or reject a result, comment on a file, undo a job's file
  changes. Bad results turn into lesson drafts that you keep or drop.
- **Memory (a "second brain").** Plain Markdown notes in `memory/` with a map
  (`MEMORY.md`). Raw sources (also PDF and Office files) are turned into wiki pages.
- **Skills and routines.** Saved instructions you run with one button, and scheduled jobs
  (a morning briefing, weekly health checks, daily backups) with cost caps.
- **Safety.** Agents work only in their folder, cannot change memory, skills or settings,
  and never commit, push or delete. Text that looks like a secret is never saved.
- **Costs and limits.** Cost per job, per lead and per successful job; new jobs pause near
  the 5-hour and weekly usage limits.

## How it works

```
 you (web page or CLI)
        |
        v
   jobs.add  ---> router: rules, then Haiku -> model + effort
        |
        v
   queue (data/jobs/<id>.json)  ---> a lead? use its session (leads.py)
        |
        v
   ask.py: claude -p --output-format stream-json ...
        |        (memory in the prompt, allowed tools, folder limits)
        v
   the agent's answer
        |
        +--> tagged blocks: <question>, <plan>, <note>, <handoff>, ...
        |    Python checks them and saves them (agents never write state)
        v
   the check (exit code 0?) --no--> the error goes back to the same session
        |
       yes
        v
   done: result, cost, artifact, project memory
```

The main rule of the design: **exact work is Python, AI is only for language and
decisions.** Dates, file lists, schedules, checks and costs are plain Python functions.

| Folder | What is in it |
|---|---|
| `agentic_os/` | the Python package (one module per area: `jobs.py` and its parts `job_*.py`, `router.py`, `leads.py`, `memory.py`, `web.py`, ...) |
| `agentic_os/static/` | the web page (plain HTML, CSS and JavaScript) |
| `memory/` | the notes: `MEMORY.md` (the map), `wiki/`, `raw/`, `identity/`, `log.md` (your notes are not in git) |
| `skills/`, `routines/` | the skills and the scheduled jobs that Agentic OS runs (Markdown) |
| `roster.json` | the leads |
| `tests/` | pytest tests (no real agent runs in the tests) |
| `data/` | your jobs, logs and results (personal, not in git) |

## Install

You need:

- Linux (it is built and tested on Ubuntu). On Windows, use WSL2 (see below).
- [Claude Code](https://claude.com/claude-code), logged in (`claude` on your `PATH`).
  Agentic OS uses your Claude Code plan; it needs no API key.
- Python 3.13 and [uv](https://docs.astral.sh/uv/)

```bash
git clone https://github.com/ZahariBakov/Agentic-OS.git agentic-os
cd agentic-os
uv venv --python 3.13
uv pip install -r requirements.txt
.venv/bin/pytest -q          # optional: all tests should pass
cp memory/identity/user.example.md memory/identity/user.md   # then write about yourself
git config core.hooksPath .githooks   # stop commits that hold personal text
```

Your own notes stay on your computer: `memory/` notes, `memory/identity/user.md` and
`CLAUDE.local.md` (your personal rules for Claude Code) are not in git.

### Windows (WSL2)

Agentic OS uses Linux features (file locks, process groups), so on Windows it runs inside
WSL2, a real Linux in Windows. The code needs no changes.

1. Open PowerShell as administrator and run `wsl --install`. It installs Ubuntu.
   Restart Windows, then open "Ubuntu" from the Start menu and create a Linux user.
2. In Ubuntu, install uv and Claude Code, then log in to Claude Code:

   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh    # then open a new terminal
   # Claude Code: use the Linux installer from https://claude.com/claude-code
   claude                                              # log in once, then exit
   ```

3. Follow the install steps above, inside Ubuntu. `uv venv --python 3.13` downloads
   Python 3.13 if Ubuntu has an older one.
4. Start the web page as in "Run" below and open the printed link in your Windows
   browser. WSL2 forwards `127.0.0.1` to Windows.

Tips:

- Keep the project in your Linux home (`~/workspace/agentic-os`), not under `/mnt/c/`.
  Git and the tests are much slower on Windows folders.
- GitHub: clone over HTTPS. For a push, use a personal access token as the password.
- `data/` and your memory notes are not in git, so each computer has its own jobs,
  settings, logs and notes. Git moves the code, `skills/`, `routines/` and `roster.json`.

## Run

Start the web page:

```bash
.venv/bin/python -m agentic_os.web --port 8000
```

It prints a link like `http://127.0.0.1:8000/__app?t=...`. Open it in your browser. The
server listens only on `127.0.0.1` and answers only requests with its token: the link puts
the token in a cookie, so later you can open `http://127.0.0.1:8000` directly. To print the
link again:

```bash
.venv/bin/python -m agentic_os.access link --port 8000
```

The server also starts the routines that are due, every minute (`--no-routines` turns this off).

Or use the command line:

```bash
.venv/bin/python -m agentic_os.jobs add "Summarize memory/raw/notes.md in 5 lines"
.venv/bin/python -m agentic_os.jobs run       # run the queued jobs
.venv/bin/python -m agentic_os.jobs list
.venv/bin/python -m agentic_os.briefing       # the morning facts (free, no agent)
```

Every module runs on its own: `python -m agentic_os.<module> --help`. The full list of
commands is in `AGENTS.md`.

## Costs

Every job is a real Claude Code call and uses your plan's limits. Cheap models go first,
and many things are free because they are plain Python (the briefing facts, memory health,
the cost reports, backups). Opus jobs always start with a plan and an estimate that you
approve.

## For developers

- `AGENTS.md` - the commands, the restrictions and the skills for coding agents
- Tests: `.venv/bin/pytest -q`. Syntax check: `.venv/bin/python -m compileall -q agentic_os`
- All code and files are ASCII only

## License

MIT, see `LICENSE`.
