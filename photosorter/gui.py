"""Simple Tk window: pick folders, Start/Pause/Stop, progress per stage, log."""
from __future__ import annotations

import json
import logging
import queue
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from . import __version__, background
from .config import Settings, app_data_dir
from .pipeline import STAGE_LABELS, STAGES, Pipeline

GUI_STATE = app_data_dir() / "gui.json"
log = logging.getLogger("photosorter")


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(f"Photo Sorter {__version__}")
        root.geometry("900x680")
        self.q: queue.Queue = queue.Queue()
        self.pipeline: Pipeline | None = None
        self.thread: threading.Thread | None = None
        self.web = None  # SearchServer, started once an index exists
        self._web_lock = threading.Lock()
        st = self._load_state()
        self.s = Settings.from_dict(st)

        pad = {"padx": 6, "pady": 3}
        top = ttk.Frame(root); top.pack(fill="x", **pad)
        ttk.Label(top, text="Photo folders to read (never changed):").grid(row=0, column=0, sticky="w")
        self.src_list = tk.Listbox(top, height=4)
        self.src_list.grid(row=1, column=0, rowspan=2, sticky="nsew")
        for s in self.s.sources:
            self.src_list.insert("end", s)
        ttk.Button(top, text="Add folder...", command=self.add_src).grid(row=1, column=1, sticky="ew", padx=4)
        ttk.Button(top, text="Remove", command=self.remove_src).grid(row=2, column=1, sticky="new", padx=4)
        ttk.Label(top, text="Output folder (sorted copies + index):").grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.out_var = tk.StringVar(value=self.s.output)
        ttk.Entry(top, textvariable=self.out_var).grid(row=4, column=0, sticky="ew")
        ttk.Button(top, text="Browse...", command=self.pick_out).grid(row=4, column=1, sticky="ew", padx=4)
        top.columnconfigure(0, weight=1)

        opts = ttk.LabelFrame(root, text="Options"); opts.pack(fill="x", **pad)
        self.v_ai = tk.BooleanVar(value=self.s.use_ai)
        self.v_faces = tk.BooleanVar(value=self.s.use_faces)
        self.v_gpu = tk.BooleanVar(value=self.s.use_gpu)
        self.v_exact = tk.BooleanVar(value=self.s.copy_exact_duplicates)
        self.v_mtime = tk.BooleanVar(value=self.s.mtime_date_fallback)
        self.v_pack = tk.StringVar(value=self.s.face_pack)
        ttk.Checkbutton(opts, text="Image AI: type sorting, tags, text search (SigLIP 2)", variable=self.v_ai).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(opts, text="Face recognition", variable=self.v_faces).grid(row=0, column=1, sticky="w")
        cb = ttk.Combobox(opts, textvariable=self.v_pack, values=["yunet_sface", "buffalo_l", "buffalo_s"], width=12, state="readonly")
        cb.grid(row=0, column=2)
        cb.bind("<<ComboboxSelected>>", lambda e: self.refresh_model_status())
        ttk.Checkbutton(opts, text="Use GPU (DirectML)", variable=self.v_gpu).grid(row=0, column=3, sticky="w")
        ttk.Checkbutton(opts, text="Also copy byte-identical duplicates", variable=self.v_exact).grid(row=1, column=0, sticky="w")
        ttk.Checkbutton(opts, text="Use file modified date if no photo date", variable=self.v_mtime).grid(row=1, column=1, columnspan=2, sticky="w")
        ttk.Label(opts, text="Speed:").grid(row=1, column=3, sticky="e")
        self.v_mode = tk.StringVar(value=background.MODE_LABELS[background.norm_mode(self.s.run_mode)])
        mode_cb = ttk.Combobox(opts, textvariable=self.v_mode, values=list(background.MODE_LABELS.values()),
                               width=32, state="readonly")
        mode_cb.grid(row=1, column=4, sticky="w")
        mode_cb.bind("<<ComboboxSelected>>", lambda e: self.change_mode())
        # v0.6: videos (copied, previews, faces and search; needs the bundled ffmpeg)
        self.v_videos = tk.BooleanVar(value=self.s.use_videos)
        ttk.Checkbutton(opts, text="Include videos", variable=self.v_videos).grid(row=0, column=4, sticky="w")
        self.model_lbl = ttk.Label(opts, text=""); self.model_lbl.grid(row=2, column=0, columnspan=5, sticky="w")

        btns = ttk.Frame(root); btns.pack(fill="x", **pad)
        self.b_models = ttk.Button(btns, text="Download models", command=self.download_models)
        self.b_start = ttk.Button(btns, text="Start / Resume", command=self.start)
        self.b_pause = ttk.Button(btns, text="Pause", command=self.toggle_pause, state="disabled")
        self.b_stop = ttk.Button(btns, text="Stop", command=self.stop, state="disabled")
        self.b_web = ttk.Button(btns, text="Open search page", command=self.open_web)
        self.b_clean = ttk.Button(btns, text="Clean up duplicates", command=lambda: self.open_web("cleanup"))
        self.b_verify = ttk.Button(btns, text="Verify originals", command=lambda: self.open_web("verify"))
        self.b_sync = ttk.Button(btns, text="Family sync", command=lambda: self.open_web("sync"))
        for b in (self.b_models, self.b_start, self.b_pause, self.b_stop, self.b_web, self.b_clean, self.b_verify,
                  self.b_sync):
            b.pack(side="left", padx=3)

        # v0.5: update banner (hidden until a newer release is found)
        self.update_bar = ttk.Frame(root)
        self.update_lbl = ttk.Label(self.update_bar, text="", foreground="#1d4ed8")
        self.update_lbl.pack(side="left")
        self.b_update = ttk.Button(self.update_bar, text="Install update", command=self.install_update)
        self.b_update.pack(side="left", padx=6)
        ttk.Button(self.update_bar, text="What's new", command=self.show_update_notes).pack(side="left")
        self.update_info: dict | None = None

        prog = ttk.Frame(root); prog.pack(fill="x", **pad)
        self.stage_lbl = ttk.Label(prog, text="Idle"); self.stage_lbl.pack(anchor="w")
        self.bar = ttk.Progressbar(prog, mode="determinate", maximum=100); self.bar.pack(fill="x")

        self.tree = ttk.Treeview(root, columns=("done", "total", "notes"), height=len(STAGES))
        self.tree.heading("#0", text="Stage"); self.tree.heading("done", text="Done")
        self.tree.heading("total", text="Total"); self.tree.heading("notes", text="Notes")
        self.tree.column("#0", width=200); self.tree.column("done", width=80, anchor="e")
        self.tree.column("total", width=80, anchor="e"); self.tree.column("notes", width=380)
        for s in STAGES:
            self.tree.insert("", "end", iid=s, text=STAGE_LABELS[s], values=("", "", ""))
        self.tree.pack(fill="x", **pad)

        self.log = ScrolledText(root, height=12, state="disabled"); self.log.pack(fill="both", expand=True, **pad)
        self.refresh_model_status()
        self._build_menu()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(150, self.poll)
        root.after(4000, self.auto_update_check)
        root.after(200, self.ensure_web_server)
        try:                                   # v0.5: move a source-install sync autostart over to the .exe
            from . import sync as _sync

            if _sync.refresh_autostart():
                self.write_log("Family sync at sign-in now uses the installed app.")
        except Exception:
            pass

    # ---- state ------------------------------------------------------------
    def _load_state(self) -> dict:
        try:
            return json.loads(GUI_STATE.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def collect(self) -> Settings:
        s = self.s
        s.sources = list(self.src_list.get(0, "end"))
        s.output = self.out_var.get().strip()
        s.use_ai, s.use_faces, s.use_gpu = self.v_ai.get(), self.v_faces.get(), self.v_gpu.get()
        s.copy_exact_duplicates, s.mtime_date_fallback = self.v_exact.get(), self.v_mtime.get()
        s.face_pack = self.v_pack.get()
        s.use_videos = self.v_videos.get()
        s.run_mode = self.mode_value()
        GUI_STATE.write_text(json.dumps(s.to_dict(), indent=2), encoding="utf-8")
        return s

    def mode_value(self) -> str:
        label = self.v_mode.get()
        return next((k for k, v in background.MODE_LABELS.items() if v == label), "background")

    def change_mode(self):
        """Priority changes straight away; thread counts apply from the next Start."""
        self.collect()
        info = background.apply(self.s.run_mode)
        self.write_log(f"Speed: {background.MODE_LABELS[info['mode']]} (priority {info['priority']}). "
                       "Thread limits apply from the next Start.")

    def refresh_model_status(self):
        from .embedders import embedder_available
        from .faces import faces_available
        from .models import NON_COMMERCIAL

        try:
            from .models import available_providers

            gpu = "DmlExecutionProvider" in available_providers()
        except Exception:
            gpu = False
        from .imaging import pillow_heif

        heif_ok = pillow_heif is not None
        pack = self.v_pack.get()
        nc = " - NON-COMMERCIAL license" if pack in NON_COMMERCIAL else ""
        self.model_lbl.config(text=f"Image AI ({self.s.embedder}): {'ready' if embedder_available(self.s.embedder) else 'not installed'}   "
                                   f"Faces ({pack}{nc}): {'ready' if faces_available(pack) else 'not downloaded'}   "
                                   f"GPU (DirectML): {'available' if gpu else 'not available - CPU only'}   "
                                   f"HEIC: {'on' if heif_ok else 'off'}")

    # ---- actions ------------------------------------------------------------
    def add_src(self):
        d = filedialog.askdirectory(title="Choose a photo folder")
        if d and d not in self.src_list.get(0, "end"):
            self.src_list.insert("end", d)

    def remove_src(self):
        for i in reversed(self.src_list.curselection()):
            self.src_list.delete(i)

    def pick_out(self):
        d = filedialog.askdirectory(title="Choose an output folder (preferably empty, on a drive with room)")
        if d:
            self.out_var.set(d)
            self.ensure_web_server()

    def write_log(self, text: str):
        self.log.config(state="normal"); self.log.insert("end", text + "\n"); self.log.see("end"); self.log.config(state="disabled")

    def download_models(self):
        s = self.collect()
        self.b_models.config(state="disabled")

        def work():
            from .models import NON_COMMERCIAL, ensure_models

            def prog(label, done, total):
                self.q.put({"kind": "dl", "label": label, "done": done, "total": total})
            try:
                if s.use_ai:
                    try:
                        ensure_models(s.embedder, prog)
                    except RuntimeError as e:  # SigLIP 2 not hosted yet -> Export-Models.bat
                        self.q.put({"kind": "log", "text": str(e)})
                if s.use_faces:
                    if s.face_pack in NON_COMMERCIAL:
                        self.q.put({"kind": "log", "text": "Note: InsightFace weights are licensed for non-commercial use only."})
                    ensure_models(s.face_pack, prog)
                self.q.put({"kind": "log", "text": "Models downloaded."})
            except Exception as e:
                self.q.put({"kind": "log", "text": f"Model download failed: {e}"})
            self.q.put({"kind": "dl_done"})
        threading.Thread(target=work, daemon=True).start()

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        s = self.collect()
        if not s.sources or not s.output:
            messagebox.showwarning("Photo Sorter", "Add at least one photo folder and choose an output folder.")
            return
        out = Path(s.output).resolve()
        for src in s.sources:
            if Path(src).resolve() == out:
                messagebox.showerror("Photo Sorter", "The output folder must be different from the photo folders.")
                return
        try:
            self.pipeline = Pipeline(s, self.q.put)
        except Exception as e:
            messagebox.showerror("Photo Sorter", str(e)); return
        self.b_start.config(state="disabled"); self.b_pause.config(state="normal", text="Pause"); self.b_stop.config(state="normal")
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        try:
            self.pipeline.run()
        except Exception as e:
            self.q.put({"kind": "log", "text": f"Error: {e}"})
        self.q.put({"kind": "finished"})

    def toggle_pause(self):
        if not self.pipeline:
            return
        if self.pipeline.paused:
            self.pipeline.resume(); self.b_pause.config(text="Pause")
        else:
            self.pipeline.pause(); self.b_pause.config(text="Resume")

    def stop(self):
        if self.pipeline:
            self.pipeline.stop()
            self.write_log("Stopping after the current batch...")

    def ensure_web_server(self):
        """Start the search page in the background once this output folder has an index (no browser)."""
        out = self.out_var.get().strip()
        from .config import db_path

        if not out or not db_path(Path(out)).exists():
            return
        if self.web is not None and self.web.same_root(out) and (self.web.is_running() or self.web.is_starting()):
            return
        threading.Thread(target=self._start_web, args=(out, False, ""), daemon=True, name="photosorter-web-start").start()

    def _start_web(self, out: str, open_browser: bool, page: str):
        from .webserver import SearchServer

        with self._web_lock:
            try:
                if self.web is not None and not self.web.same_root(out):
                    try:
                        self.web.stop()
                    except Exception:
                        pass
                    self.web = None
                if self.web is None:
                    self.web = SearchServer(out)
                already = self.web.is_running()
                self.web.start()
                if not already:
                    self.q.put({"kind": "log", "text": f"Search page ready at {self.web.url()}"})
                if open_browser:
                    webbrowser.open(self.web.url(page))
            except Exception as e:
                log.exception("Could not start the search page")
                detail = getattr(self.web, "error", None) or str(e)
                self.q.put({"kind": "web_error", "text": str(e), "detail": detail, "alert": open_browser})

    def open_web(self, page: str = ""):
        out = self.out_var.get().strip()
        from .config import db_path

        if not out or not db_path(Path(out)).exists():
            messagebox.showinfo("Photo Sorter", "Run the sorter on this output folder first."); return
        if self.web is not None and self.web.same_root(out) and self.web.is_running():
            webbrowser.open(self.web.url(page)); return
        self.write_log("Starting search page...")
        threading.Thread(target=self._start_web, args=(out, True, page), daemon=True, name="photosorter-web-open").start()

    def after_run_suggest(self):
        """Suggested albums (v0.8): look for new events after each sort run, in a low-priority thread.
        It only reads the index (no AI model runs), so it takes seconds; problems never break the sorter."""
        from .config import db_path
        out = self.out_var.get().strip()
        if not out or not db_path(Path(out)).exists():
            return

        def work():
            try:
                from . import background, suggest
                from .db import DB
                background.lower_thread_priority("background")
                db = DB(db_path(Path(out)))
                try:
                    r = suggest.find(db)
                finally:
                    db.close()
                if r["added"]:
                    self.q.put({"kind": "log", "text": f"Suggested albums: {r['added']} new (Albums page, Suggested)."})
            except Exception as e:  # noqa: BLE001
                self.q.put({"kind": "log", "text": f"Suggested albums could not be made: {e}"})
        threading.Thread(target=work, daemon=True, name="photosorter-suggest").start()

    def after_run_sync(self):
        """Family sync (v0.4): start the background sync after each sort run, if it is set up."""
        out = self.out_var.get().strip()
        if not out:
            return
        try:
            from . import sync

            if sync.request_after_run(Path(out)):
                self.write_log("Family sync started in the background (it keeps going if you close this window).")
        except Exception as e:  # never let sync problems break the sorter
            self.write_log(f"Family sync could not start: {e}")

    # ---- v0.5 menu, setup and updates ----------------------------------------------------------------
    def _build_menu(self):
        from . import updater

        m = tk.Menu(self.root)
        h = tk.Menu(m, tearoff=0)
        self.v_autoupd = tk.BooleanVar(value=updater.auto_check_enabled())
        h.add_command(label="Check for updates...", command=lambda: self.check_updates(manual=True))
        h.add_checkbutton(label="Check for updates daily", variable=self.v_autoupd,
                          command=lambda: updater.set_auto_check(self.v_autoupd.get()))
        h.add_separator()
        h.add_command(label="Run setup again...", command=self.rerun_setup)
        h.add_command(label="Open the app data folder", command=lambda: webbrowser.open(app_data_dir().as_uri()))
        h.add_command(label="Release notes on GitHub", command=lambda: webbrowser.open(updater.RELEASES_PAGE))
        h.add_command(label="About Photo Sorter", command=lambda: messagebox.showinfo(
            "Photo Sorter", f"Photo Sorter {__version__}\nOpen source (MIT): https://github.com/{updater.REPO}\n"
            "Your photos never leave this PC."))
        m.add_cascade(label="Help", menu=h)
        self.root.config(menu=m)

    def rerun_setup(self):
        if self.thread and self.thread.is_alive():
            messagebox.showinfo("Photo Sorter", "Stop the current run first."); return
        from .gui_setup import SetupWizard

        def done(d):
            if d:
                self.s = Settings.from_dict(d)
                self.src_list.delete(0, "end")
                for x in self.s.sources:
                    self.src_list.insert("end", x)
                self.out_var.set(self.s.output)
                self.v_ai.set(self.s.use_ai); self.v_faces.set(self.s.use_faces); self.v_gpu.set(self.s.use_gpu)
                self.v_videos.set(self.s.use_videos)
            self.refresh_model_status()
        SetupWizard(self.root, done, rerun=True)

    def auto_update_check(self):
        from . import updater

        if updater.due():
            self.check_updates(manual=False)

    def check_updates(self, manual: bool = False):
        from . import updater

        def work():
            try:
                info = updater.check()
                updater.record_check(info)
                self.q.put({"kind": "update", "info": info, "manual": manual})
            except Exception as e:
                self.q.put({"kind": "update", "info": {"error": f"Couldn't reach GitHub ({e})"}, "manual": manual})
        threading.Thread(target=work, daemon=True).start()

    def on_update_info(self, info: dict, manual: bool):
        from . import updater

        if info.get("available"):
            self.update_info = info
            self.update_lbl.config(text=f"Photo Sorter {info['version']} is available (you have {__version__}).")
            self.b_update.config(text="Install update" if updater.can_self_install() else "Download installer")
            self.update_bar.pack(fill="x", padx=6, pady=3, before=self.tree)
            if manual:
                self.show_update_notes()
        elif manual:
            if info.get("error"):
                messagebox.showwarning("Photo Sorter", info["error"])
            else:
                messagebox.showinfo("Photo Sorter", f"You have the latest version ({__version__}).")

    def show_update_notes(self):
        info = self.update_info or {}
        w = tk.Toplevel(self.root); w.title(f"What's new in {info.get('version', '')}"); w.geometry("640x420")
        t = ScrolledText(w, wrap="word"); t.pack(fill="both", expand=True, padx=6, pady=6)
        t.insert("end", info.get("notes") or "No release notes."); t.config(state="disabled")
        ttk.Button(w, text="Close", command=w.destroy).pack(pady=4)

    def install_update(self):
        from . import updater

        info = self.update_info
        if not info:
            return
        if not updater.can_self_install():
            webbrowser.open(info.get("url") or updater.RELEASES_PAGE)
            return
        if self.thread and self.thread.is_alive():
            messagebox.showinfo("Photo Sorter", "Stop or finish the current run first (progress is saved)."); return
        if not messagebox.askyesno("Photo Sorter", f"Download Photo Sorter {info['version']} "
                                   f"({info.get('size', 0) >> 20} MB), check it, then close and update? "
                                   "Your photos, index, settings and models are kept."):
            return
        self.b_update.config(state="disabled")

        def prog(label, done, total):
            self.q.put({"kind": "dl", "label": label, "done": done, "total": total})

        def work():
            try:
                path = updater.download(info, prog)
                from .config import save_app_config

                save_app_config(update_pending_sha256=info["sha256"])
                self.q.put({"kind": "update_ready", "path": str(path)})
            except Exception as e:
                self.q.put({"kind": "log", "text": f"Update download failed: {e}. Try again later."})
                self.q.put({"kind": "update_failed"})
        threading.Thread(target=work, daemon=True).start()

    def run_installer(self, path: str):
        from . import updater

        try:
            updater.launch_installer(Path(path))
        except Exception as e:
            messagebox.showerror("Photo Sorter", f"Couldn't start the update: {e}")
            self.b_update.config(state="normal")
            return
        self.root.destroy()

    def on_close(self):
        if self.thread and self.thread.is_alive():
            if not messagebox.askyesno("Photo Sorter", "Processing is running. Stop and close? Progress is saved."):
                return
            self.pipeline.stop()
        if self.web is not None:
            try:
                self.web.stop()
            except Exception:
                pass
        self.root.destroy()

    # ---- events -------------------------------------------------------------
    def poll(self):
        try:
            while True:
                self.handle(self.q.get_nowait())
        except queue.Empty:
            pass
        self.root.after(150, self.poll)

    def handle(self, e: dict):
        k = e["kind"]
        if k == "log":
            self.write_log(e["text"])
        elif k == "stage":
            self.stage_lbl.config(text=f"{e['label']} ({STAGES.index(e['stage']) + 1}/{len(STAGES)})")
            self.bar.config(mode="indeterminate"); self.bar.start(15)
        elif k == "progress":
            done, total = e["done"], e["total"]
            notes = ", ".join(f"{a}: {b}" for a, b in e.items() if a not in ("kind", "stage", "done", "total"))
            self.tree.item(e["stage"], values=(done, total or "", notes))
            if total:
                self.bar.stop(); self.bar.config(mode="determinate", value=100 * done / total)
        elif k == "dl":
            t = e["total"] or 1
            self.stage_lbl.config(text=f"Downloading {e['label']}: {e['done'] >> 20} / {e['total'] >> 20} MB")
            self.bar.config(mode="determinate", value=100 * e["done"] / t)
        elif k == "update":
            self.on_update_info(e["info"], e["manual"])
        elif k == "update_ready":
            self.stage_lbl.config(text="Update downloaded and checked. Closing to install...")
            self.root.after(800, lambda: self.run_installer(e["path"]))
        elif k == "update_failed":
            self.b_update.config(state="normal"); self.stage_lbl.config(text="Idle")
        elif k == "dl_done":
            self.b_models.config(state="normal"); self.refresh_model_status(); self.stage_lbl.config(text="Idle")
        elif k in ("done", "stopped", "error"):
            if k == "done":
                s = e["summary"]
                self.write_log(f"Done: {s['files']} files, {s['copied']} copied, {s['dup_groups']} duplicate groups, "
                               f"{s['people']} people groups. Types: {s['types']}")
            if k == "error":
                self.write_log("Error: " + e["text"])
        elif k == "finished":
            self.after_run_sync()
            self.after_run_suggest()
            self.ensure_web_server()
            self.bar.stop(); self.bar.config(mode="determinate", value=100)
            self.stage_lbl.config(text="Idle")
            self.b_start.config(state="normal"); self.b_pause.config(state="disabled"); self.b_stop.config(state="disabled")
        elif k == "web_error":
            self.write_log("Search page failed to start: " + e["text"])
            if e.get("alert", True):
                from .config import app_data_dir as _app_data_dir
                logf = _app_data_dir() / "photosorter.log"
                messagebox.showerror(
                    "Photo Sorter",
                    "Couldn't open the search page.\n\n" + e["text"]
                    + "\n\nDetails were written to:\n" + str(logf))
        elif k == "paused":
            self.write_log("Paused.")
        elif k == "resumed":
            self.write_log("Resumed.")


def main():
    logf = app_data_dir() / "photosorter.log"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(logf, encoding="utf-8")])
    try:
        background.apply(Settings.from_dict(json.loads(GUI_STATE.read_text(encoding="utf-8"))))
    except Exception:
        background.apply("background")
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    from . import onboarding

    if onboarding.needs_onboarding():
        from .gui_setup import SetupWizard

        root.withdraw()

        def done(_d):
            root.deiconify()
            App(root)
        SetupWizard(root, done)
    else:
        App(root)
    root.mainloop()
