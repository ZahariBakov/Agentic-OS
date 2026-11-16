"""Ask Claude Code one question and print the answer, the cost and the session id.

Usage (from the project folder):
    python -m agentic_os.ask "your question"
    python -m agentic_os.ask --model sonnet "your question"
    python -m agentic_os.ask --resume <session id> "your next question"
"""

import argparse
import json
import os
import signal
import subprocess
import tempfile
import threading

from agentic_os import limits

AGENT_TIMEOUT = 1800  # seconds; a longer agent call is stopped, so it cannot block a worker forever


class AgentError(RuntimeError):
    """An agent call that failed. `cost` is what it cost before it failed (0 when unknown)."""

    def __init__(self, message, cost=0.0):
        super().__init__(message)
        self.cost = cost


def kill_group(pid, sig=signal.SIGKILL):
    """Send a signal to an agent and everything it started (its tools, their children).

    The agent starts in its own process group (start_new_session), so the group id
    is its pid. A process without its own group (an agent started before we had
    groups) gets the signal alone. Return False when the process does not exist.
    """
    for send in (os.killpg, os.kill):
        try:
            send(pid, sig)
            return True
        except ProcessLookupError:
            continue  # no group with this id: try the process alone
        except PermissionError:
            return False  # not ours (an old pid that another user's process has now)
    return False


def ask(
    question,
    model="haiku",
    session_id=None,
    on_start=None,
    effort=None,
    extra_args=(),
    cwd=None,
    timeout=AGENT_TIMEOUT,
    env=None,
    on_session=None,
    on_event=None,
):
    """Run `claude -p` with the question and return its JSON reply (the result event).

    We read the stream of events, not one JSON reply, because only the stream
    has the rate_limit_event with the usage limits. We save them in limits.

    If session_id is given, continue that conversation instead of starting a new one.
    If on_start is given, call it with the process id when the agent starts.
    If on_session is given, call it with the session id as soon as the stream
    shows it (the first "system" event), long before the result. Then a job that
    is cut in the middle (the server stops) still knows its session and can resume.
    If effort is given (low, medium, high, xhigh), use it for this call.
    extra_args are more `claude` options. cwd is the folder where the agent runs.
    After `timeout` seconds the agent is stopped and we raise AgentError (a RuntimeError).
    The agent and its tools run in their own process group, so a stop or a
    timeout ends them all (kill_group), not only the `claude` process.
    env are more environment variables for the agent (and its hooks).
    If on_event is given, call it with every event of the stream (stream.py shows them live).
    """
    # The question goes through stdin, not the command line:
    #   - on the command line, a question that starts with "-" is read as an option
    #   - one command line argument can be at most 128 KB; stdin has no limit
    command = ["claude", "-p", "--model", model, "--output-format", "stream-json", "--verbose"]
    if session_id is not None:
        command += ["--resume", session_id]
    if effort is not None:
        command += ["--effort", effort]
    command += list(extra_args)
    reply = None
    timed_out = threading.Event()
    # stderr goes to a temporary file. A pipe that nobody reads can get full
    # and stop the agent, because we read only stdout while it runs.
    with tempfile.TemporaryFile("w+") as errors:
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True, cwd=cwd,
            env={**os.environ, **env} if env else None,  # None: the same environment as ours
            start_new_session=True,  # its own process group: see kill_group
        )

        def stop_agent():
            timed_out.set()
            kill_group(process.pid)  # the read loop below then sees the end of stdout

        timer = threading.Timer(timeout, stop_agent)
        timer.start()
        try:
            if on_start is not None:
                on_start(process.pid)
            try:
                process.stdin.write(question)
                process.stdin.close()  # the end of stdin tells claude that the question is complete
            except BrokenPipeError:
                pass  # claude ended before it read the question: stderr below says why
            for line in process.stdout:
                event = read_event(line)
                if event is None:
                    continue
                if on_event is not None:
                    safely(on_event, event)
                if event.get("type") == "result":
                    reply = event
                else:
                    # Our bookkeeping must never kill a working agent (and lose its cost).
                    keep_books(event, on_session)
            process.wait()
        finally:
            timer.cancel()
            # After any error (a bad line, an error in on_start) the agent must
            # not keep running without us: nobody would read its output or stop it.
            if process.poll() is None:
                kill_group(process.pid)
                process.wait()
            process.stdout.close()
        errors.seek(0)
        stderr = errors.read()
    cost = (reply.get("total_cost_usd") or 0.0) if reply else 0.0
    # The timer can fire just after the result came: then the call has succeeded.
    if timed_out.is_set() and reply is None:
        raise AgentError(f"claude took more than {timeout} seconds and was stopped")
    if process.returncode != 0 and not timed_out.is_set():
        why = (error_text(reply) + "\n" + stderr.strip()).strip() if reply else stderr.strip()
        raise AgentError(f"claude failed (exit code {process.returncode}): {why}", cost)
    if reply is None:
        raise AgentError("claude ended without a result")
    if reply.get("is_error"):
        raise AgentError(f"claude returned an error: {error_text(reply)}", cost)
    return reply


def read_event(line):
    """Return one stream line as a dict, or None for an empty or non-JSON line (it is skipped)."""
    if not line.strip():
        return None
    try:
        event = json.loads(line)
    except ValueError:
        print(f"claude wrote a line that is not JSON (skipped): {line[:200].rstrip()}")
        return None
    return event if isinstance(event, dict) else None


def keep_books(event, on_session):
    """Save what a stream event tells us: the session id, the usage limits."""
    if event.get("type") == "system" and on_session is not None and "session_id" in event:
        safely(on_session, event["session_id"])
    elif event.get("type") == "rate_limit_event":
        safely(limits.record, event["rate_limit_info"])


def safely(function, value):
    """Call function(value). An error (a broken data/limits.json, a failed save of the job) is
    printed, not raised: the agent goes on working, and its result and cost are kept."""
    try:
        function(value)
    except Exception as error:  # noqa: BLE001 - any bookkeeping error; the agent must go on
        print(f"Could not use a stream event ({getattr(function, '__name__', function)}): {error!r}")


def error_text(reply):
    """Return the error text of a result event. Error results have "errors" (a list), not "result"."""
    parts = [str(reply["result"])] if reply.get("result") else []
    parts += [str(error) for error in reply.get("errors") or []]
    return "; ".join(parts) or f"no error text ({reply.get('subtype', 'unknown')})"


def main():
    parser = argparse.ArgumentParser(description="Ask Claude Code one question.")
    parser.add_argument("question", help="the question to ask")
    parser.add_argument(
        "--model",
        choices=["haiku", "sonnet", "opus"],
        default="haiku",
        help="the model to use (default: haiku)",
    )
    parser.add_argument("--resume", metavar="SESSION_ID", help="continue this session")
    args = parser.parse_args()

    reply = ask(args.question, args.model, args.resume)
    print(reply["result"])
    print()
    print(f"Model:   {', '.join(reply['modelUsage'])}")
    print(f"Cost:    ${reply['total_cost_usd']:.4f}")
    print(f"Time:    {reply['duration_ms'] / 1000:.1f} s")
    print(f"Session: {reply['session_id']}")


if __name__ == "__main__":
    main()
