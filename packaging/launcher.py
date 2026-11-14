import multiprocessing
import os
import sys


def _no_console_streams():
    """The windowed .exe has no console: send stray prints/log output to a file instead of None."""
    if sys.stdout is None or sys.stderr is None:
        try:
            base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "PhotoSorter")
            os.makedirs(base, exist_ok=True)
            f = open(os.path.join(base, "console.log"), "a", encoding="utf-8", buffering=1)
        except Exception:
            f = open(os.devnull, "w")
        if sys.stdout is None:
            sys.stdout = f
        if sys.stderr is None:
            sys.stderr = f


if __name__ == "__main__":
    multiprocessing.freeze_support()
    _no_console_streams()
    from photosorter.cli import main

    sys.exit(main())
