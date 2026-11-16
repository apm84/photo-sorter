"""v0.6.1: the search-page server starts reliably (no fixed delay, retries if the port is taken)."""
from __future__ import annotations

import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from photosorter import __version__, updater
from photosorter.config import db_path, index_dir
from photosorter.db import DB
from photosorter.webserver import (DEFAULT_PORT, HOST, SearchServer, find_free_port, port_accepts,
                                   wait_until_ready)


def _index(out: Path):
    index_dir(out)
    DB(db_path(out))
    return out


def _free_start(host=HOST) -> int:
    """A high free port to use as preferred_port in tests, so we don't collide with a real session."""
    return find_free_port(host, start=18000, tries=200)


def _http(url: str, timeout: float = 5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, r.read()


def test_version_and_updater_offers_0_6_1():
    assert __version__ == "0.6.1"
    assert updater.is_newer("v0.6.1", "0.6.0")
    assert not updater.is_newer("v0.6.1", "0.6.1")


def test_find_free_port_skips_occupied():
    start = _free_start()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((HOST, start))
    sock.listen(1)
    try:
        assert find_free_port(HOST, start, tries=5) == start + 1
    finally:
        sock.close()


def test_wait_until_ready_times_out_and_succeeds():
    start = _free_start()
    assert wait_until_ready(HOST, start, timeout=0.25, interval=0.05) is False
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((HOST, start))
    sock.listen(5)
    try:
        # One successful probe is enough. A second connect against a dummy listener
        # that never accept()s can fail on Windows (the listen backlog is left full).
        assert wait_until_ready(HOST, start, timeout=2, interval=0.05) is True
    finally:
        sock.close()


def test_search_server_serves_and_stops(tmp_path):
    out = _index(tmp_path / "out")
    server = SearchServer(out, preferred_port=_free_start())
    try:
        port = server.start(timeout=15)
        assert server.is_running() and port == server.port
        status, body = _http(server.url())
        assert status == 200 and b"Photo Sorter" in body
        # starting again is a no-op that keeps the same port
        assert server.start(timeout=5) == port
    finally:
        server.stop()
    assert not server.is_running()
    assert not port_accepts(HOST, port, timeout=0.2)


def test_search_server_uses_next_port_when_busy(tmp_path):
    out = _index(tmp_path / "out")
    start = _free_start()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((HOST, start))
    sock.listen(1)
    server = SearchServer(out, preferred_port=start)
    try:
        port = server.start(timeout=15)
        assert port == start + 1
        status, _ = _http(server.url())
        assert status == 200
    finally:
        server.stop()
        sock.close()


def test_search_server_fails_without_index_then_retries(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    start = _free_start()
    server = SearchServer(out, preferred_port=start)
    with pytest.raises(RuntimeError, match="No index found|did not start|Traceback|Search page"):
        server.start(timeout=10)
    assert not server.is_running()
    # State is reset so a later attempt, once the index exists, works
    _index(out)
    server = SearchServer(out, preferred_port=start)
    try:
        port = server.start(timeout=15)
        assert server.is_running()
        assert _http(server.url())[0] == 200
        assert port >= start
    finally:
        server.stop()


def test_search_server_starts_under_busy_peer_threads(tmp_path):
    """The 0.6.0 window opened the browser after a fixed 1.2 s; we wait until the port accepts."""
    out = _index(tmp_path / "out")
    stop = threading.Event()

    def burn():
        n = 0
        while not stop.is_set():
            n += sum(i * i for i in range(3000))

    burners = [threading.Thread(target=burn, daemon=True) for _ in range(4)]
    for t in burners:
        t.start()
    server = SearchServer(out, preferred_port=_free_start())
    try:
        t0 = time.perf_counter()
        port = server.start(timeout=30)
        elapsed = time.perf_counter() - t0
        assert server.is_running()
        assert _http(server.url())[0] == 200
        # Must succeed by waiting, not by assuming a 1.2 s delay is enough
        assert elapsed < 30
        assert port_accepts(HOST, port)
    finally:
        stop.set()
        server.stop()


def test_find_free_port_exhausted():
    start = _free_start()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((HOST, start))
    sock.listen(1)
    try:
        with pytest.raises(RuntimeError, match="No free port"):
            find_free_port(HOST, start, tries=1)
    finally:
        sock.close()


def test_default_preferred_port_is_8765():
    assert DEFAULT_PORT == 8765
