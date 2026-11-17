"""Tests for the port check of the web server (web.bind_port and web.main)."""

import socket

import pytest

from agentic_os import routines, web


@pytest.fixture
def busy_port():
    """A port that another server already listens on."""
    other = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    other.bind(("127.0.0.1", 0))
    other.listen()
    yield other.getsockname()[1]
    other.close()


def test_free_port_is_bound_on_loopback():
    sock = web.bind_port(0)  # 0: the system picks a free port
    try:
        assert sock.getsockname()[0] == "127.0.0.1"
    finally:
        sock.close()


def test_busy_port_stops_with_a_clear_message(busy_port):
    with pytest.raises(SystemExit) as stop:
        web.bind_port(busy_port)
    assert f"Port {busy_port} is busy" in str(stop.value)
    assert f"--port {busy_port + 1}" in str(stop.value)


def test_busy_port_starts_no_routines(busy_port, monkeypatch):
    def no_tick(*args, **kwargs):
        raise AssertionError("a routine started although the port was busy")

    monkeypatch.setattr(routines, "tick", no_tick)
    monkeypatch.setattr("sys.argv", ["web", "--port", str(busy_port)])
    with pytest.raises(SystemExit):
        web.main()
