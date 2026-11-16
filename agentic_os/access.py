"""Access: the web server answers only requests that carry its token.

Listening on 127.0.0.1 and checking
Host and Origin stops other web sites. The token also stops other programs and
users on this computer that do not have it.

How it works:
  - The token is a random string in data/app-token (only you can read the file).
  - Open http://127.0.0.1:<port>/__app?t=<token> once. The server puts the token
    in a cookie and sends you to the jobs page.
  - After that, the browser sends the cookie with every request. A script can
    send the header X-Agentic-OS-Token instead.

The server prints the link when it starts. To print it again:
    python -m agentic_os.access link [--port 8000]
"""

import argparse
import hmac
import logging
import os
import re
import secrets
import tempfile
import threading

from pathlib import Path

from agentic_os.job_store import DATA_DIR

TOKEN_FILE = DATA_DIR / "app-token"
LOGIN_PATH = "/__app"
HEADER = "x-agentic-os-token"
COOKIE_DAYS = 365  # the token file lives in data/ anyway; no need to log in every day
NO_TOKEN = ("Agent Office needs its token. Open the link that the server printed at start, "
            "or run: python -m agentic_os.access link")
UNREADABLE = "Agent Office cannot read its token file data/app-token. Check that the file can be read."
TOKEN_LOCK = threading.Lock()  # the web server's threads make the token one at a time


def read_token():
    """The token in the file, or "" when there is no file or it is empty."""
    try:
        return TOKEN_FILE.read_text().strip()
    except FileNotFoundError:
        return ""


def token():
    """Return the token. Make it the first time (a file that only you can read).

    The new token is written to a temporary file with a unique name first, then
    os.link gives it the real name. os.link fails when the name exists, so two
    processes that start at the same time both end with the first one's token,
    and nobody ever reads a half-written file. The lock does the same for the
    threads of one process. An empty file (a crash) gets a new token; if two
    processes do that at the same time, the last one wins, which is fine: the
    server reads the file on every request.
    """
    with TOKEN_LOCK:
        text = read_token()
        if text:
            return text
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(prefix=f".{TOKEN_FILE.name}-", suffix=".tmp", dir=TOKEN_FILE.parent)
        temp = Path(name)  # mkstemp makes it with mode 0600
        try:
            with os.fdopen(handle, "w") as file:
                file.write(secrets.token_hex(24) + "\n")
            try:
                os.link(temp, TOKEN_FILE)
            except FileExistsError:
                if not read_token():
                    os.replace(temp, TOKEN_FILE)  # the file is there but empty
            except OSError:
                os.replace(temp, TOKEN_FILE)  # a file system without hard links (vfat, some network disks)
        finally:
            temp.unlink(missing_ok=True)
        return read_token()


def token_or_none():
    """The token, or None when the file cannot be read or written (decide then refuses with UNREADABLE)."""
    try:
        return token()
    except OSError:
        return None


def sent(headers, cookies, port):
    """Return the token that the request sent (the header, else the cookie), or None."""
    return headers.get(HEADER) or cookies.get(cookie_name(port))


def still_valid(given):
    """True while the token that opened a long request (the live updates) is still the token.

    Delete data/app-token and an open live-update stream ends at its next look.
    """
    try:
        return matches(given, read_token())
    except OSError:
        return False


def matches(given, expected):
    """True when the given text is the token. compare_digest takes the same time for any text."""
    return bool(given) and hmac.compare_digest(str(given).encode(), expected.encode())


def cookie_name(port):
    """One cookie per port: browsers share cookies between ports of the same host."""
    return f"agentic_os_{port}"


def link(port):
    return f"http://127.0.0.1:{port}{LOGIN_PATH}?t={token()}"


def decide(method, path, headers, query, cookies, port, expected):
    """Decide what to do with a request. Python only, no web objects, so tests are simple.

    headers: a dict with lower-case names. Returns one of:
      ("ok", None)          - let the request through
      ("login", None)       - a correct /__app link: set the cookie and go to "/"
      ("deny", "the text")  - answer 403 with this text
    expected: the token, or None when its file cannot be read (token_or_none).
    """
    ours = {f"127.0.0.1:{port}", f"localhost:{port}"}
    # Host: stops "DNS rebinding", where an evil domain name points to 127.0.0.1
    if headers.get("host") not in ours:
        return "deny", "wrong host"
    # Origin: a POST from another site has that site in its Origin header
    origin = headers.get("origin")
    if method not in ("GET", "HEAD") and origin is not None and origin not in {f"http://{host}" for host in ours}:
        return "deny", "wrong origin"
    if expected is None:
        return "deny", UNREADABLE
    if path == LOGIN_PATH:
        return ("login", None) if matches(query.get("t"), expected) else ("deny", "wrong token")
    if matches(headers.get(HEADER), expected) or matches(cookies.get(cookie_name(port)), expected):
        return "ok", None
    return "deny", NO_TOKEN


class HideToken(logging.Filter):
    """Hide the token in the access log of uvicorn ("GET /__app?t=... HTTP/1.1")."""

    def filter(self, record):
        if isinstance(record.args, tuple):
            record.args = tuple(re.sub(r"(?<=[?&])t=[^&\s]*", "t=<hidden>", arg) if isinstance(arg, str) else arg
                                for arg in record.args)
        return True


def main():
    parser = argparse.ArgumentParser(description="The token of the web page.")
    commands = parser.add_subparsers(dest="command", required=True)
    show = commands.add_parser("link", help="print the link that opens the web page")
    show.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.command == "link":
        print(link(args.port))


if __name__ == "__main__":
    main()
