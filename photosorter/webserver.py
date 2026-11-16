"""Local search-page server used by the Photo Sorter window.

In 0.6.0 the window started uvicorn only when you clicked Open search page, set the port
as "up" before it was listening, waited a fixed 1.2 s, and ignored failures in the server
thread (uvicorn exits with SystemExit when the port is taken; in the windowed .exe that
traceback is easy to miss). This starts the server once an index exists, waits until the
port accepts connections, tries the next port if 8765 is taken, and logs failures.
"""
from __future__ import annotations

import logging
import socket
import threading
import time
from pathlib import Path

log = logging.getLogger("photosorter")

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
READY_TIMEOUT = 30.0
PORT_TRIES = 20


def port_accepts(host: str, port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def find_free_port(host: str = HOST, start: int = DEFAULT_PORT, tries: int = PORT_TRIES) -> int:
    """First port in [start, start+tries) that is not already accepting connections and can be bound."""
    last = None
    for port in range(int(start), int(start) + int(tries)):
        if port_accepts(host, port, timeout=0.05):
            last = OSError(f"{host}:{port} is already in use")
            continue
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
            return port
        except OSError as e:
            last = e
        finally:
            sock.close()
    raise RuntimeError(f"No free port in {start}–{start + tries - 1} on {host}" + (f" ({last})" if last else ""))


def wait_until_ready(host: str, port: int, timeout: float = READY_TIMEOUT, interval: float = 0.1,
                     stop: threading.Event | None = None) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if stop is not None and stop.is_set():
            return False
        if port_accepts(host, port):
            return True
        time.sleep(interval)
    return False


def boost_io_thread() -> None:
    """Keep the search-page thread scheduled ahead of a background/lowest-priority scan."""
    from . import background

    background.boost_thread_priority()


class SearchServer:
    """One uvicorn thread serving the search page for a given output folder."""

    def __init__(self, output_root: str | Path, host: str = HOST, preferred_port: int = DEFAULT_PORT):
        self.output_root = Path(output_root)
        self.host = host
        self.preferred_port = int(preferred_port)
        self.port: int | None = None
        self.error: str | None = None
        self._thread: threading.Thread | None = None
        self._uvicorn = None
        self._failed = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def url(self, page: str = "") -> str:
        page = (page or "").lstrip("/")
        return f"http://{self.host}:{self.port}/{page}"

    def same_root(self, output_root: str | Path) -> bool:
        try:
            return self.output_root.resolve() == Path(output_root).resolve()
        except OSError:
            return self.output_root == Path(output_root)

    def is_running(self) -> bool:
        return (self.port is not None and self._thread is not None and self._thread.is_alive()
                and not self._failed.is_set() and port_accepts(self.host, self.port, timeout=0.2))

    def is_starting(self) -> bool:
        return (self._thread is not None and self._thread.is_alive()
                and not self.is_running() and not self._failed.is_set())

    def start(self, timeout: float = READY_TIMEOUT) -> int:
        """Start if needed and return the bound port. Raises RuntimeError on failure; safe to retry."""
        with self._lock:
            if self.is_running():
                return self.port  # type: ignore[return-value]
            if self._thread is None or not self._thread.is_alive():
                self._failed.clear()
                self._stop.clear()
                self.error = None
                self.port = None
                self._uvicorn = None
                self._thread = threading.Thread(target=self._run, name="photosorter-web", daemon=True)
                self._thread.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._failed.is_set():
                break
            if self.port and port_accepts(self.host, self.port):
                return self.port
            time.sleep(0.1)
        err = self.error or (
            f"The search page did not start within {timeout:.0f}s"
            + (f" (tried port {self.port})" if self.port else f" (starting at port {self.preferred_port})")
            + ".")
        log.error("Search page failed to start: %s", err)
        self.stop()
        raise RuntimeError(err)

    def stop(self, join_timeout: float = 5.0) -> None:
        self._stop.set()
        server = self._uvicorn
        if server is not None:
            server.should_exit = True
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(join_timeout)
        self.port = None
        self._uvicorn = None
        self._thread = None
        self._failed.clear()

    def _run(self) -> None:
        boost_io_thread()
        try:
            import uvicorn

            from .web.app import create_app

            app = create_app(self.output_root)
            last = None
            for port in range(self.preferred_port, self.preferred_port + PORT_TRIES):
                if self._stop.is_set():
                    return
                if port_accepts(self.host, port, timeout=0.05):
                    last = f"{self.host}:{port} is already in use"
                    log.info("Search page: %s, trying the next port", last)
                    continue
                config = uvicorn.Config(app, host=self.host, port=port, log_level="warning", log_config=None)
                server = uvicorn.Server(config)
                self._uvicorn = server
                self.port = port
                try:
                    server.run()
                    return
                except SystemExit as e:
                    last = f"could not bind {self.host}:{port} ({e})"
                    log.warning("Search page: %s, trying the next port", last)
                    self.port = None
                    self._uvicorn = None
                    continue
            self.error = (
                f"Could not bind a port in {self.preferred_port}–"
                f"{self.preferred_port + PORT_TRIES - 1} on {self.host}"
                + (f" ({last})" if last else "")
                + ".")
            log.error("Search page: %s", self.error)
            self._failed.set()
        except Exception:
            import traceback

            self.error = traceback.format_exc()
            log.exception("Search page server failed to start")
            self._failed.set()
