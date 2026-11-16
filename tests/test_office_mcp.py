"""Tests for the office MCP server (office_mcp.py): the JSON-RPC messages and the tools.

No agent runs. The tools are called through handle(), in this process, so the
temp_memory and temp_jobs fixtures apply. One test starts the real server as a
child process, and only lists the tools (a child process does not see monkeypatch).
"""

import io
import json
import subprocess

from agentic_os import jobs, memory, office_mcp, project_memory


def call(name, arguments, job_id=None):
    """Call a tool through handle(). Return (the data, isError)."""
    reply = office_mcp.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                               "params": {"name": name, "arguments": arguments}}, job_id)
    result = reply["result"]
    return json.loads(result["content"][0]["text"]), result["isError"]


def test_initialize_and_list_the_tools():
    hello = office_mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {"protocolVersion": "2025-03-26"}})
    assert hello["result"]["protocolVersion"] == "2025-03-26"
    assert hello["result"]["serverInfo"]["name"] == "office"
    assert office_mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None

    tools = office_mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]

    assert [tool["name"] for tool in tools] == ["memory_search", "project_recall", "run_command", "delegate"]
    assert tools[2]["inputSchema"]["required"] == ["command", "reason"]


def test_an_unknown_method_is_an_error_not_a_crash():
    reply = office_mcp.handle({"jsonrpc": "2.0", "id": 3, "method": "resources/list"})

    assert reply["error"]["code"] == -32601


def test_memory_search_finds_the_notes_with_the_rare_words(temp_memory):
    memory.save_note("Git branches", "Make a branch per feature. Merge with a pull request.", area="tech")
    memory.save_note("Coffee", "Two cups a day, no sugar.", area="personal")

    found, error = call("memory_search", {"query": "how do I merge a feature branch?"})

    assert not error and found["searched"] == 2
    assert [note["title"] for note in found["notes"]] == ["Git branches"]
    assert found["notes"][0]["path"] == "wiki/tech/git-branches.md"
    assert "Merge with a pull request" in found["notes"][0]["snippet"]


def test_memory_search_counts_the_area_name(temp_memory):
    memory.save_note("Three folders", "Raw, wiki and the rules file.", area="second-brain")
    memory.save_note("Brain food", "Fish and nuts.", area="personal")

    found, _ = call("memory_search", {"query": "second brain"})

    assert [note["title"] for note in found["notes"]] == ["Three folders", "Brain food"]


def test_wrong_arguments_get_a_clear_message(temp_memory):
    for arguments, message in [({}, "query"), ({"query": "  "}, "query"),
                               ({"query": "git", "limit": 0}, "limit"),
                               ({"query": "git", "limit": "3"}, "limit"),
                               ({"query": "git", "limit": True}, "limit")]:
        found, error = call("memory_search", arguments)
        assert error and message in found["error"], arguments

    found, error = call("delete_everything", {})
    assert error and "unknown tool" in found["error"]


def test_project_recall_gives_the_instructions_and_what_fits(temp_jobs, tmp_path):
    folder = tmp_path / "shop"
    folder.mkdir()
    project_memory.add(folder, "instruction", "Use pytest.")
    project_memory.add(folder, "failure", "The cart total test needs the TZ variable.")
    project_memory.add(folder, "decision", "Prices are in cents.")
    job = jobs.add("Fix the cart.", "haiku", use_memory=False, folder=str(folder))

    found, error = call("project_recall", {"query": "the cart total test fails"}, job.id)

    assert not error and found["project"] == "shop"
    assert [entry["text"] for entry in found["instructions"]] == ["Use pytest."]
    assert [entry["text"] for entry in found["related"]] == ["The cart total test needs the TZ variable."]


def test_project_recall_without_a_project_says_why(temp_jobs):
    job = jobs.add("Say hi.", "haiku", use_memory=False)

    assert "no project folder" in call("project_recall", {"query": "x"}, job.id)[0]["error"]
    assert "no job" in call("project_recall", {"query": "x"}, None)[0]["error"]


def test_every_agent_with_tools_gets_the_office_and_no_other_mcp_server(temp_jobs):
    job = jobs.add("Say hi.", "haiku", use_memory=False)

    _, args = jobs.first_call(job)

    config = json.loads(args[args.index("--mcp-config") + 1])["mcpServers"]
    assert list(config) == ["office"] and config["office"]["env"] == {"AGENTIC_OS_JOB_ID": job.id}
    assert config["office"]["args"][0] == "-I"  # no folder of the agent on sys.path
    assert "--strict-mcp-config" in args
    allowed = args[args.index("--allowedTools") + 1:args.index("--disallowedTools")]
    assert "mcp__office__memory_search" in allowed and "mcp__office__project_recall" in allowed


def test_the_real_server_answers_over_stdin():
    server = json.loads(office_mcp.config_args("")[1])["mcpServers"]["office"]  # what Claude Code starts
    lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
             {"jsonrpc": "2.0", "method": "notifications/initialized"},
             {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]

    done = subprocess.run([server["command"], *server["args"]], input="".join(json.dumps(line) + "\n" for line in lines)
                          + "not json\n", capture_output=True, text=True, timeout=30)

    replies = [json.loads(line) for line in done.stdout.splitlines()]
    assert [reply.get("id") for reply in replies] == [1, 2, None]
    assert len(replies[1]["result"]["tools"]) == 4 and replies[2]["error"]["code"] == -32700


def test_printing_in_a_tool_never_reaches_the_json_channel(monkeypatch, capsys):
    # run_job prints progress lines; on stdout they would break the JSON-RPC replies.
    def noisy(arguments, job_id=None):
        print("start    job 123")
        return {"ok": True}

    monkeypatch.setitem(office_mcp.TOOLS, "noisy", (noisy, {"description": "x", "inputSchema": {"type": "object"}}))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "noisy"}}) + "\n"))

    office_mcp.main()

    out, err = capsys.readouterr()
    assert [json.loads(line)["id"] for line in out.splitlines()] == [5]
    assert "start    job 123" in err
