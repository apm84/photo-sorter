"""Entry point. No arguments -> GUI. Subcommands: run, web, download-models, providers."""
from __future__ import annotations

import argparse
import json
import logging
import sys


def _early_thread_caps():
    """Cap BLAS/OpenMP thread pools before numpy is imported (Run in background, v0.4)."""
    try:
        from .background import set_thread_env
        from .config import app_data_dir

        mode = json.loads((app_data_dir() / "gui.json").read_text(encoding="utf-8")).get("run_mode", "background")
    except Exception:
        mode = "background"
    try:
        set_thread_env(mode)
    except Exception:
        pass


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    _early_thread_caps()
    if not argv:
        from .gui import main as gui_main

        return gui_main()
    ap = argparse.ArgumentParser(prog="photosorter")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="process photos without the window")
    r.add_argument("--src", action="append", required=True, help="source folder (repeatable)")
    r.add_argument("--out", required=True, help="output folder for the sorted copy + index")
    r.add_argument("--no-ai", action="store_true", help="skip image AI (types/tags/search use heuristics only)")
    r.add_argument("--embedder", default="siglip2", choices=["siglip2", "clip"])
    r.add_argument("--faces", default="yunet_sface", choices=["yunet_sface", "buffalo_l", "buffalo_s"])
    r.add_argument("--no-faces", action="store_true")
    r.add_argument("--cpu", action="store_true", help="do not use the GPU")
    r.add_argument("--copy-exact-duplicates", action="store_true")
    r.add_argument("--mtime-fallback", action="store_true", help="use file modified date when no other date")
    r.add_argument("--speed", default="background", choices=["background", "lowest", "full"],
                   help="background (default): low priority, half the cores; full: every core")
    w = sub.add_parser("web", help="open the search web app")
    w.add_argument("--out", required=True)
    w.add_argument("--port", type=int, default=8765)
    w.add_argument("--no-browser", action="store_true")
    w.add_argument("--page", default="", choices=["", "people", "cleanup", "albums", "verify", "sync"],
                   help="page to open in the browser")
    sy = sub.add_parser("sync", help="family sync to the Pi in the background (keeps running; see the Sync page)")
    sy.add_argument("--out", required=True)
    sy.add_argument("--once", action="store_true", help="sync once now and exit")
    sa = sub.add_parser("sync-autostart", help="start family sync when you sign in to Windows")
    sa.add_argument("--out", required=True)
    sa.add_argument("--remove", action="store_true")
    vf = sub.add_parser("verify", help="check that every original has a safe copy (read-only)")
    vf.add_argument("--out", required=True)
    vf.add_argument("--src", action="append", help="original folder (default: the folders this index was built from)")
    d = sub.add_parser("download-models")
    d.add_argument("--embedder", default="siglip2", choices=["siglip2", "clip", "none"])
    d.add_argument("--faces", default="yunet_sface", choices=["yunet_sface", "buffalo_l", "buffalo_s", "none"],
                   help="buffalo_* = InsightFace weights, non-commercial use only")
    sub.add_parser("providers", help="show ONNX Runtime providers (DirectML = GPU)")
    gp = sub.add_parser("gpu-probe", help="list graphics adapters and ONNX Runtime providers (used by setup)")
    gp.add_argument("--json", help="write the result to this file")
    st = sub.add_parser("selftest", help="load the models and run them once on a made-up picture")
    st.add_argument("--cpu", action="store_true")
    st.add_argument("--no-ai", action="store_true")
    st.add_argument("--no-faces", action="store_true")
    st.add_argument("--json", help="write the result to this file")
    sub.add_parser("check-update", help="check GitHub Releases for a newer version")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    if a.cmd == "run":
        from .config import Settings
        from .pipeline import Pipeline

        s = Settings(sources=a.src, output=a.out, use_ai=not a.no_ai, embedder=a.embedder,
                     use_faces=not a.no_faces, face_pack=a.faces,
                     use_gpu=not a.cpu, copy_exact_duplicates=a.copy_exact_duplicates,
                     mtime_date_fallback=a.mtime_fallback, run_mode=a.speed)

        def ev(e):
            if e["kind"] == "progress" and e.get("total"):
                print(f"\r{e['stage']}: {e['done']}/{e['total']}   ", end="", flush=True)
            elif e["kind"] in ("stage",):
                print()
        print(json.dumps(Pipeline(s, ev).run(), indent=2))
    elif a.cmd == "web":
        from .web.app import serve

        print(f"Photo Sorter search: http://127.0.0.1:{a.port}/  (Ctrl+C to stop)")
        serve(a.out, a.port, open_browser=not a.no_browser, page=a.page)
    elif a.cmd == "sync":
        from .sync import daemon_main

        return daemon_main(a.out, once=a.once)
    elif a.cmd == "sync-autostart":
        from . import sync

        if a.remove:
            print("Removed." if sync.remove_autostart() else "It wasn't set up.")
        else:
            print(f"Family sync will start when you sign in ({sync.install_autostart(a.out)}).")
            sync.spawn_worker(a.out)
    elif a.cmd == "verify":
        from .verify import run_verify

        res = run_verify(a.out, sources=a.src)
        print(json.dumps(res["summary"], indent=2))
    elif a.cmd == "download-models":
        from .models import ensure_models

        def prog(label, done, total):
            print(f"\r{label}: {done // (1 << 20)} / {total // (1 << 20)} MB", end="", flush=True)
        if a.embedder != "none":
            ensure_models(a.embedder, prog); print()
        if a.faces != "none":
            ensure_models(a.faces, prog); print()
        print("Models ready.")
    elif a.cmd == "providers":
        from .models import available_providers

        print(available_providers())
    elif a.cmd in ("gpu-probe", "selftest"):
        from . import onboarding

        if a.cmd == "gpu-probe":
            res = onboarding.gpu_probe()
            res["recommendation"] = onboarding.recommend(res)
        else:
            res = onboarding.self_test(use_gpu=not a.cpu, use_ai=not a.no_ai, use_faces=not a.no_faces)
        txt = json.dumps(res, indent=2)
        if a.json:
            with open(a.json, "w", encoding="utf-8") as f:
                f.write(txt)
        if sys.stdout:
            print(txt)
        return 0 if res.get("ok", True) else 1
    elif a.cmd == "check-update":
        from .updater import check

        print(json.dumps(check(), indent=2))


if __name__ == "__main__":
    sys.exit(main())
