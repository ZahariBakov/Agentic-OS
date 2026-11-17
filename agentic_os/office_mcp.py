"""Office tools: an MCP server that Agentic OS gives to every agent with tools.

Claude Code starts it (stdio) for each agent call; `safety.mcp_args(job_id)` writes the
config. It speaks MCP's JSON-RPC by hand (one JSON object per line):
no new package.

The tools read what Agentic OS knows; they never call a model and never write memory
(agents still save notes with tagged blocks, which Python checks):

    memory_search    the notes in memory/wiki that fit a query, best first
    project_recall   what Agentic OS remembers about the job's project
    run_command      a shell command in the job's folder, after your OK (commands.py)
    delegate         sub-agents that read and report, in parallel (specialists.py)

The job id comes from the AGENTIC_OS_JOB_ID environment variable (set in the config).

Usage (Claude Code starts it; to try it by hand, type JSON lines):
    python -m agentic_os.office_mcp
"""

import json
import math
import os
import sys
from pathlib import Path

from agentic_os import commands, jobs, memory, project_memory, specialists

PROJECT_DIR = Path(__file__).resolve().parent.parent
SERVER = "office"  # the tools are mcp__office__<name> for Claude Code
PROTOCOL = "2025-06-18"  # used when the client does not say which version it speaks
MAX_LIMIT = 10
SNIPPET_CHARS = 300


class ToolError(Exception):
    """A clear message for the agent: a wrong argument, or "not possible now"."""


def limit_of(arguments, default=5):
    limit = arguments.get("limit", default)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIMIT:
        raise ToolError(f"limit must be a whole number from 1 to {MAX_LIMIT}")
    return limit


def query_of(arguments):
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ToolError("query must be a non-empty string")
    return query


def memory_search(arguments, job_id=None):
    """The notes that share the most rare words with the query (like project_memory.rank).

    Searched: the area (folder) name, the title, the description and the text.
    """
    query, limit = query_of(arguments), limit_of(arguments)
    notes = []
    for path in sorted(memory.WIKI_DIR.glob("*/*.md")):
        meta, body = memory.read_note(path)
        title = meta.get("title", path.stem)
        notes.append((path, title, meta.get("description", ""), body))
    wanted = set(project_memory.words(query))
    # The area (the folder, "second-brain") counts too: a note there may never say "second brain".
    sets = [set(project_memory.words(f"{path.parent.name.replace('-', ' ')} {title} {description} {body}"))
            for path, title, description, body in notes]
    count = {}
    for found in sets:
        for word in found:
            count[word] = count.get(word, 0) + 1
    scored = []
    for (path, title, description, body), found in zip(notes, sets):
        score = sum(math.log(1 + len(notes) / count[word]) for word in found & wanted)
        if score > 0:
            scored.append((score, title, path, description, body))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return {"notes": [
        {"title": title, "path": str(path.relative_to(memory.MEMORY_DIR)), "description": description,
         "snippet": " ".join(body.split())[:SNIPPET_CHARS]}
        for _, title, path, description, body in scored[:limit]
    ], "searched": len(notes)}


def project_recall(arguments, job_id=None):
    """The job's project: its instructions, and the other entries that fit the query."""
    query, limit = query_of(arguments), limit_of(arguments)
    if not job_id or not jobs.exists(job_id):
        raise ToolError("this agent has no job, so it has no project")
    project = project_memory.project_of(jobs.load(job_id).folder)
    if project is None:
        raise ToolError("this job has no project folder")
    instructions, related = project_memory.relevant(project, query, limit)
    shown = lambda entry: {"kind": entry["kind"], "text": entry["text"], "job": entry.get("job"), "at": entry["at"]}
    return {"project": project.name, "instructions": [shown(entry) for entry in instructions],
            "related": [shown(entry) for entry in related]}


def run_command(arguments, job_id=None):
    """A shell command, after the user's OK (or on their list). The rules are in commands.py."""
    command, reason = arguments.get("command"), arguments.get("reason")
    if not isinstance(command, str) or not command.strip():
        raise ToolError("command must be a non-empty string")
    if not isinstance(reason, str) or not reason.strip():
        raise ToolError("reason must say in one sentence why you need this command")
    timeout = arguments.get("timeout_seconds", commands.DEFAULT_TIMEOUT)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= commands.MAX_TIMEOUT:
        raise ToolError(f"timeout_seconds must be a whole number from 1 to {commands.MAX_TIMEOUT}")
    if not job_id or not jobs.exists(job_id):
        raise ToolError("this agent has no job, so it cannot run commands")
    job = jobs.load(job_id)
    if not jobs.can_edit(job):
        raise ToolError("this job cannot change files, so it cannot run commands either")
    settings = jobs.settings()
    if settings["commands_mode"] == "off":
        raise ToolError("the user turned shell commands off; tell them what you would run and why")
    bad = commands.blocked(command)
    if bad is not None:
        raise ToolError(f'not allowed: "{bad}" is blocked for every agent; tell the user what to run themselves')
    allowed = commands.allowed_by(command, settings["commands_allow"])
    record = commands.request(job_id, command.strip(), reason.strip(), jobs.edit_folder(job), timeout,
                              status="allowed" if allowed else "pending", allowed=allowed)
    stopped = lambda: jobs.is_stopped(job)
    if not allowed:
        answer = commands.wait(record, stopped)
        if answer != "allowed":
            raise ToolError({"denied": "the user denied this command; do not ask for it again, find another way "
                                       "or tell them what you need",
                             "no answer": f"the user did not answer in {commands.WAIT_SECONDS // 60} minutes; "
                                          "go on without it, and say in your answer what you wanted to run",
                             "stopped": "the job was stopped"}.get(answer, answer))
    done = commands.run(record, stopped)
    return {"exit_code": done["exit_code"], "seconds": done["seconds"], "output": done["output"],
            "folder": done["cwd"]}


def delegate(arguments, job_id=None):
    """Sub-jobs that read and report, at the same time. The rules are in specialists.py."""
    if not job_id or not jobs.exists(job_id):
        raise ToolError("this agent has no job, so it cannot delegate")
    job = jobs.load(job_id)
    try:
        return specialists.delegate(job, arguments.get("assignments"), stopped=lambda: jobs.is_stopped(job))
    except ValueError as error:
        raise ToolError(str(error)) from error


TOOLS = {
    "memory_search": (memory_search, {
        "description": "Search the user's notes (memory/wiki) for a query. Returns up to `limit` notes, best "
                       "first: title, path (relative to the memory folder, open it with Read for the full "
                       "text), description and the first 300 characters. Free; no model is called.",
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Words to look for, for example \"git branches\"."},
            "limit": {"type": "integer", "description": f"How many notes, 1 to {MAX_LIMIT}. Default 5."},
        }, "required": ["query"]},
    }),
    "project_recall": (project_recall, {
        "description": "What Agentic OS remembers about this job's project (its folder or git repository): "
                       "the user's instructions (always), and the decisions, known failures and past jobs "
                       "that fit the query, best first. An error when the job has no project folder.",
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string", "description": "What you are about to do, for example \"fix the cart total test\"."},
            "limit": {"type": "integer", "description": f"How many related entries, 1 to {MAX_LIMIT}. Default 5."},
        }, "required": ["query"]},
    }),
    "run_command": (run_command, {
        "description": "Run one shell command in the job's folder (bash), for example the tests. The user "
                       "sees it on the web page and allows or denies it; you wait (up to 10 minutes) for "
                       "the answer. Returns exit_code, seconds and the last 6000 characters of the output "
                       "(stdout and stderr together). Blocked for everyone: rm, sudo, git commit/push/reset/"
                       "checkout and other git changes, and any secret file. Only for jobs that may edit files. "
                       "Ask for the commands you really need, one at a time.",
        "inputSchema": {"type": "object", "properties": {
            "command": {"type": "string", "description": "The command line, for example \"python -m pytest -q\"."},
            "reason": {"type": "string", "description": "One sentence for the user: why you need it."},
            "timeout_seconds": {"type": "integer",
                                "description": f"Stop it after this many seconds, 1 to {commands.MAX_TIMEOUT}. "
                                               f"Default {commands.DEFAULT_TIMEOUT}."},
        }, "required": ["command", "reason"]},
    }),
    "delegate": (delegate, {
        "description": f"Give 1 to {specialists.MAX_AT_ONCE} parts of your work to sub-agents that run at the same "
                       "time and return when all are done, with each one's report (title, job, model, status, "
                       "cost_usd, report, error) and budget_left_usd. They can only READ files in your folder "
                       "(no edits, no commands, no questions to the user, no further delegation). Python picks "
                       f"each one's model (haiku or sonnet). All sub-agents of your job together may cost at "
                       f"most ${specialists.CAP_USD:.2f}. Use it for independent reading work (search, review, "
                       "compare); a sub-agent cannot see your conversation, so write each task as a complete brief.",
        "inputSchema": {"type": "object", "properties": {
            "assignments": {"type": "array", "description": f"1 to {specialists.MAX_AT_ONCE} items.", "items": {
                "type": "object", "properties": {
                    "title": {"type": "string", "description": "3 to 8 words, shown on the web page."},
                    "task": {"type": "string", "description": "The full brief: goal, files, what to report."},
                }, "required": ["title", "task"]}},
        }, "required": ["assignments"]},
    }),
}


def tool_names():
    """The names Claude Code gives these tools (for --allowedTools)."""
    return [f"mcp__{SERVER}__{name}" for name in TOOLS]


def config_args(job_id):
    """Return the `claude` options that start this server for one job's agent, and no other MCP server.

    -I (isolated mode): Python adds no folder of the agent to sys.path (an agent could have
    written a json.py there); we add only the project folder (like safety.settings for the hook).
    """
    code = f"import sys; sys.path.insert(0, {str(PROJECT_DIR)!r}); from agentic_os.office_mcp import main; main()"
    config = {"mcpServers": {SERVER: {
        "type": "stdio", "command": sys.executable, "args": ["-I", "-c", code],
        "env": {"AGENTIC_OS_JOB_ID": job_id or ""},
    }}}
    # --mcp-config takes a list: the next option (a flag) ends it.
    return ["--mcp-config", json.dumps(config), "--strict-mcp-config"]


def text_result(data, error=False):
    return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}], "isError": error}


def call_tool(params, job_id):
    name, arguments = params.get("name"), params.get("arguments") or {}
    if name not in TOOLS:
        return text_result({"error": f"unknown tool {name!r}; known: {', '.join(TOOLS)}"}, error=True)
    if not isinstance(arguments, dict):
        return text_result({"error": "arguments must be an object"}, error=True)
    try:
        return text_result(TOOLS[name][0](arguments, job_id))
    except ToolError as error:
        return text_result({"error": str(error)}, error=True)
    except Exception as error:  # never crash the server: the agent gets the reason instead
        return text_result({"error": f"{type(error).__name__}: {error}"}, error=True)


def handle(message, job_id=None):
    """Answer one JSON-RPC message. Return the reply, or None for a notification."""
    method, params, message_id = message.get("method"), message.get("params") or {}, message.get("id")
    if message_id is None:
        return None  # a notification ("notifications/initialized"): no reply
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion") or PROTOCOL,
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": SERVER, "version": "1.0.0"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [{"name": name, **spec} for name, (_, spec) in TOOLS.items()]}
    elif method == "tools/call":
        result = call_tool(params, job_id)
    else:
        return {"jsonrpc": "2.0", "id": message_id, "error": {"code": -32601, "message": f"unknown method {method}"}}
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def main():
    job_id = os.environ.get("AGENTIC_OS_JOB_ID") or None
    # stdout carries the JSON-RPC replies only. Our modules print progress ("start <job>"):
    # those lines go to stderr, which Claude Code keeps as the server's log.
    replies, sys.stdout = sys.stdout, sys.stderr
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "not JSON"}}
        else:
            reply = handle(message, job_id) if isinstance(message, dict) else None
        if reply is not None:
            replies.write(json.dumps(reply) + "\n")
            replies.flush()


if __name__ == "__main__":
    main()
