"""Run in background (v0.4): lower process priority, fewer threads, and yield to the person at the PC.

Modes (Settings.run_mode):
  "background" (default)  below-normal priority, about half the CPU cores, thumbnail/sync work waits
                          while the keyboard or mouse is in use (resumes after `idle_resume_s` idle)
  "lowest"                idle priority (only spare CPU), otherwise like "background"
  "full"                  normal priority, every core, never waits (the 0.3 behaviour)

Standard library only (ctypes on Windows). On Linux/macOS priority uses os.nice and idle detection is off.
"""
from __future__ import annotations

import os
import sys
import threading
import time

MODES = ("background", "lowest", "full")
MODE_LABELS = {"background": "Run in background (recommended)", "lowest": "Lowest priority (only spare CPU)",
               "full": "Full speed"}
THREAD_ENV = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS",
              "VECLIB_MAXIMUM_THREADS")

# Windows priority classes (winbase.h)
NORMAL_PRIORITY_CLASS = 0x00000020
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
IDLE_PRIORITY_CLASS = 0x00000040

_state = {"mode": "full", "threads": 0}


def norm_mode(mode: str | None) -> str:
    return mode if mode in MODES else "background"


def cpu_count() -> int:
    return os.cpu_count() or 4


def thread_budget(mode: str, cores: int | None = None) -> int:
    """Worker threads for a mode: every core at full speed, about half of them otherwise (at least 1)."""
    cores = cores or cpu_count()
    return cores if norm_mode(mode) == "full" else max(1, cores // 2)


def worker_count(settings) -> int:
    """Thread-pool size for pipeline stages: the user's `workers` setting, capped by the mode's budget."""
    return max(1, min(int(getattr(settings, "workers", 4) or 4),
                      thread_budget(getattr(settings, "run_mode", "background"))))


def ort_threads() -> int:
    """intra_op threads for ONNX Runtime CPU sessions (0 = ORT default, all cores)."""
    return _state["threads"] if _state["mode"] != "full" else 0


def set_thread_env(mode: str, environ=None) -> dict:
    """Cap BLAS/OpenMP pools. Only effective before numpy/onnxruntime are imported, so the CLI and GUI call
    this first thing. Returns the values set."""
    environ = os.environ if environ is None else environ
    mode = norm_mode(mode)
    vals = {}
    if mode != "full":
        n = str(thread_budget(mode))
        for k in THREAD_ENV:
            if k not in environ or environ.get("PHOTOSORTER_SET_THREADS") == "1":
                environ[k] = n
                vals[k] = n
        environ["PHOTOSORTER_SET_THREADS"] = "1"
    return vals


def set_priority(mode: str) -> str:
    """Lower (or restore) this process's CPU priority. Returns a short description; never raises."""
    mode = norm_mode(mode)
    try:
        if sys.platform == "win32":
            import ctypes

            k32 = ctypes.windll.kernel32
            cls = {"background": BELOW_NORMAL_PRIORITY_CLASS, "lowest": IDLE_PRIORITY_CLASS}.get(
                mode, NORMAL_PRIORITY_CLASS)
            k32.SetPriorityClass(k32.GetCurrentProcess(), cls)
            return {BELOW_NORMAL_PRIORITY_CLASS: "below normal", IDLE_PRIORITY_CLASS: "idle"}.get(cls, "normal")
        target = {"background": 10, "lowest": 19}.get(mode, 0)
        cur = os.nice(0)
        if target > cur:
            os.nice(target - cur)          # can only go down without privileges
        return f"nice {os.nice(0)}"
    except Exception as e:  # pragma: no cover - platform specific
        return f"unchanged ({e})"


def apply(settings_or_mode) -> dict:
    """Apply a run mode to this process: priority, thread caps (ORT + OpenCV + env). Safe to call repeatedly."""
    mode = norm_mode(settings_or_mode if isinstance(settings_or_mode, str)
                     else getattr(settings_or_mode, "run_mode", "background"))
    _state["mode"] = mode
    _state["threads"] = thread_budget(mode)
    set_thread_env(mode)
    prio = set_priority(mode)
    try:
        import cv2

        cv2.setNumThreads(_state["threads"] if mode != "full" else -1)
    except Exception:
        pass
    return {"mode": mode, "threads": _state["threads"], "priority": prio}


def lower_thread_priority(mode: str = "background") -> None:
    """Lower just the calling thread (e.g. the Verify check inside the web server). Windows only; no-op elsewhere."""
    if sys.platform != "win32" or norm_mode(mode) == "full":
        return
    try:  # pragma: no cover - Windows only
        import ctypes

        k32 = ctypes.windll.kernel32
        k32.SetThreadPriority(k32.GetCurrentThread(), -15 if mode == "lowest" else -1)   # IDLE / BELOW_NORMAL
    except Exception:
        pass


def boost_thread_priority() -> None:
    """Raise just the calling thread so the search page stays responsive during a background scan.

    Windows only (THREAD_PRIORITY_HIGHEST, relative to this process's priority class). No-op elsewhere.
    """
    if sys.platform != "win32":
        return
    try:  # pragma: no cover - Windows only
        import ctypes

        ctypes.windll.kernel32.SetThreadPriority(ctypes.windll.kernel32.GetCurrentThread(), 2)
    except Exception:
        pass


def saved_mode() -> str:
    """The speed chosen in the Photo Sorter window (gui.json), for helper processes and threads."""
    try:
        import json

        from .config import app_data_dir

        return norm_mode(json.loads((app_data_dir() / "gui.json").read_text(encoding="utf-8")).get("run_mode"))
    except Exception:
        return "background"


def current_mode() -> str:
    return _state["mode"]


# ---- idle detection --------------------------------------------------------------------------------------
def seconds_since_input() -> float | None:
    """Seconds since the last keyboard/mouse input in this Windows session; None where unknown."""
    if sys.platform != "win32":
        return None
    try:  # pragma: no cover - Windows only
        import ctypes
        from ctypes import wintypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            return None
        now = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
        return ((now - lii.dwTime) & 0xFFFFFFFF) / 1000.0
    except Exception:
        return None


class IdleGate:
    """Call wait() between units of light background work (thumbnails, sync uploads).

    In background/lowest mode, while someone is using the PC (input in the last `resume_s` seconds) it sleeps
    in short steps until they have been idle for `resume_s`, but never longer than `max_wait` per call, so
    work still trickles along (throttled) during long sessions. Full speed never waits."""

    def __init__(self, mode: str = "background", resume_s: float = 30.0, max_wait: float = 20.0,
                 idle_fn=seconds_since_input, sleep=time.sleep, stop: threading.Event | None = None):
        self.mode = norm_mode(mode)
        self.resume_s = float(resume_s)
        self.max_wait = float(max_wait)
        self.idle_fn = idle_fn
        self.sleep = sleep
        self.stop = stop
        self.waited = 0.0

    def user_active(self) -> bool:
        if self.mode == "full":
            return False
        idle = self.idle_fn()
        return idle is not None and idle < self.resume_s

    def wait(self) -> float:
        """Returns seconds waited."""
        waited = 0.0
        while self.user_active() and waited < self.max_wait:
            if self.stop is not None and self.stop.is_set():
                break
            step = min(1.0, self.max_wait - waited)
            self.sleep(step)
            waited += step
        self.waited += waited
        return waited
