"""Tests for the access token of the web server (access.py)."""

import logging
import stat
from concurrent.futures import ThreadPoolExecutor

import pytest

from agentic_os import access

PORT = 8000
TOKEN = "a" * 48
HOST = {"host": f"127.0.0.1:{PORT}"}


def decide(method="GET", path="/api/jobs", headers=None, query=None, cookies=None):
    return access.decide(method, path, {**HOST, **(headers or {})}, query or {}, cookies or {}, PORT, TOKEN)


@pytest.fixture
def token_file(tmp_path, monkeypatch):
    path = tmp_path / "data" / "app-token"
    monkeypatch.setattr(access, "TOKEN_FILE", path)
    return path


def test_token_is_made_once_and_only_you_can_read_it(token_file, open_umask):
    first = access.token()

    assert len(first) == 48 and access.token() == first
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600


def test_no_token_is_refused():
    assert decide() == ("deny", access.NO_TOKEN)


def test_cookie_or_header_lets_the_request_through():
    assert decide(cookies={access.cookie_name(PORT): TOKEN}) == ("ok", None)
    assert decide(headers={access.HEADER: TOKEN}) == ("ok", None)


def test_wrong_token_or_other_port_cookie_is_refused():
    assert decide(headers={access.HEADER: "b" * 48})[0] == "deny"
    assert decide(cookies={access.cookie_name(8001): TOKEN})[0] == "deny"


def test_login_link_needs_the_right_token():
    assert decide(path=access.LOGIN_PATH, query={"t": TOKEN}) == ("login", None)
    assert decide(path=access.LOGIN_PATH, query={"t": "wrong"}) == ("deny", "wrong token")
    assert decide(path=access.LOGIN_PATH) == ("deny", "wrong token")


def test_host_and_origin_are_checked_before_the_token():
    ok = {access.HEADER: TOKEN}
    assert decide(headers={**ok, "host": "evil.example:8000"}) == ("deny", "wrong host")
    assert decide("POST", headers={**ok, "origin": "http://evil.example"}) == ("deny", "wrong origin")
    assert decide("POST", headers={**ok, "origin": f"http://localhost:{PORT}"}) == ("ok", None)


def test_empty_expected_token_lets_nothing_through():
    assert access.matches("", "") is False


def test_access_log_hides_the_token():
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s"', (
        "127.0.0.1:5000", "GET", f"/__app?t={TOKEN}&x=1"), None)

    access.HideToken().filter(record)

    assert TOKEN not in record.getMessage() and "t=<hidden>&x=1" in record.getMessage()


def test_access_log_keeps_other_parameters():
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, "%s", ("/api/costs?limit=5&at=3",), None)

    access.HideToken().filter(record)

    assert record.getMessage() == "/api/costs?limit=5&at=3"


def test_empty_token_file_gets_a_new_token(token_file):
    token_file.parent.mkdir(parents=True)
    token_file.write_text("\n")

    new = access.token()

    assert len(new) == 48 and token_file.read_text().strip() == new
    assert [path.name for path in token_file.parent.iterdir()] == ["app-token"]  # no temp file left


def test_deleted_token_file_means_a_new_token(token_file):
    old = access.token()
    token_file.unlink()

    assert access.token() != old


def test_many_threads_get_one_token(token_file):
    """The web server's threads after data/app-token was deleted (before: one temp name per process)."""
    with ThreadPoolExecutor(max_workers=16) as pool:
        tokens = list(pool.map(lambda _: access.token(), range(64)))

    assert len(set(tokens)) == 1 and len(tokens[0]) == 48
    assert token_file.read_text().strip() == tokens[0]
    assert [path.name for path in token_file.parent.iterdir()] == ["app-token"]


def test_a_file_system_without_hard_links(token_file, monkeypatch, open_umask):
    def no_links(source, target):
        raise PermissionError("Operation not permitted")

    monkeypatch.setattr(access.os, "link", no_links)

    made = access.token()

    assert len(made) == 48 and token_file.read_text().strip() == made
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600


def test_an_unreadable_token_file_is_a_clear_refusal(token_file, monkeypatch):
    def broken():
        raise PermissionError("Permission denied")

    monkeypatch.setattr(access, "read_token", broken)

    assert access.token_or_none() is None
    assert decide_with(None) == ("deny", access.UNREADABLE)
    assert access.still_valid(TOKEN) is False


def test_an_open_stream_ends_when_the_token_file_is_deleted(token_file):
    current = access.token()
    assert access.still_valid(current)

    token_file.unlink()

    assert not access.still_valid(current)


def test_the_sent_token_is_the_header_or_the_cookie():
    assert access.sent({access.HEADER: "h"}, {access.cookie_name(PORT): "c"}, PORT) == "h"
    assert access.sent({}, {access.cookie_name(PORT): "c"}, PORT) == "c"
    assert access.sent({}, {}, PORT) is None


def decide_with(expected):
    return access.decide("GET", "/api/jobs", {**HOST, access.HEADER: TOKEN}, {}, {}, PORT, expected)


# --- the local_only middleware (review 2, minor list: it had no test) ---
# Called directly with a Starlette Request: no extra package (httpx) is needed.

def call_middleware(path, headers=(), query=b""):
    """Run web.local_only for one GET request. Return (response or "passed")."""
    import asyncio

    from starlette.requests import Request

    from agentic_os import web

    scope = {"type": "http", "method": "GET", "path": path, "query_string": query, "scheme": "http",
             "server": ("127.0.0.1", web.app.state.port), "client": ("127.0.0.1", 5000), "root_path": "",
             "headers": [(name.encode(), value.encode()) for name, value in headers]}

    async def call_next(request):
        return "passed"

    return asyncio.run(web.local_only(Request(scope), call_next))


def test_the_middleware_refuses_a_request_without_the_token(token_file):
    host = [("host", "127.0.0.1:8000")]

    api = call_middleware("/api/jobs", host)
    page = call_middleware("/", host)

    assert api.status_code == 403 and api.media_type == "application/json"  # ui.js shows "detail"
    assert page.status_code == 403 and page.media_type == "text/plain"


def test_the_login_link_sets_the_cookie_and_the_cookie_lets_in(token_file):
    token = access.token()
    host = [("host", "127.0.0.1:8000")]

    login = call_middleware("/__app", host, f"t={token}".encode())

    assert login.status_code == 302 and login.headers["location"] == "/"
    cookie = login.headers["set-cookie"]
    assert cookie.startswith(f"{access.cookie_name(8000)}={token}") and "HttpOnly" in cookie
    assert call_middleware("/api/jobs", host + [("cookie", f"{access.cookie_name(8000)}={token}")]) == "passed"
    assert call_middleware("/api/jobs", host + [(access.HEADER.lower(), token)]) == "passed"
    assert call_middleware("/api/jobs", [("host", "evil.example:8000"), (access.HEADER.lower(), token)]) != "passed"
