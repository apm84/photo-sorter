"""First-run setup wizard (v0.5), inside the app window: this PC's graphics card, folders and free space,
model downloads (progress, resume, SHA-256 check, retry), a self-test, and update checks. No console."""
from __future__ import annotations

import json
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import __version__, onboarding
from .config import Settings, app_data_dir, save_app_config

GUI_STATE = app_data_dir() / "gui.json"
STEPS = ("Welcome", "This PC", "Folders", "AI models", "Self-test", "Done")


def _gb(n: int) -> str:
    return "?" if n is None or n < 0 else f"{n / 2**30:.1f} GB"


class SetupWizard:
    """Modal Toplevel. Calls `on_finish(settings_dict)` (or `on_finish(None)` if skipped)."""

    def __init__(self, root: tk.Tk, on_finish, rerun: bool = False):
        self.root, self.on_finish, self.rerun = root, on_finish, rerun
        self.w = tk.Toplevel(root)
        self.w.title(f"Photo Sorter {__version__} setup")
        self.w.geometry("760x560")
        self.w.protocol("WM_DELETE_WINDOW", self.skip)
        self.q: queue.Queue = queue.Queue()
        self.cancel_flag = threading.Event()
        self.busy = False
        d = onboarding.first_run_defaults()
        try:
            self.prev = json.loads(GUI_STATE.read_text(encoding="utf-8"))
        except Exception:
            self.prev = {}
        self.sources = list(d["sources"])
        self.v_out = tk.StringVar(value=d["output"])
        self.v_models = tk.StringVar(value=d["models_dir"])
        self.v_gpu = tk.BooleanVar(value=bool(self.prev.get("use_gpu", True)))
        self.v_ai = tk.BooleanVar(value=bool(self.prev.get("use_ai", True)))
        self.v_faces = tk.BooleanVar(value=bool(self.prev.get("use_faces", True)))
        self.v_updates = tk.BooleanVar(value=bool(d["update_check"]))
        self.from_earlier = d["from_earlier_install"]
        self.probe: dict | None = None
        self.test_result: dict | None = None
        self.step = 0

        self.head = ttk.Label(self.w, font=("Segoe UI", 14, "bold")); self.head.pack(anchor="w", padx=14, pady=(12, 2))
        self.crumbs = ttk.Label(self.w, foreground="#666"); self.crumbs.pack(anchor="w", padx=14)
        self.body = ttk.Frame(self.w); self.body.pack(fill="both", expand=True, padx=14, pady=8)
        nav = ttk.Frame(self.w); nav.pack(fill="x", padx=14, pady=(0, 12))
        self.b_skip = ttk.Button(nav, text="Skip setup", command=self.skip); self.b_skip.pack(side="left")
        self.b_next = ttk.Button(nav, text="Next", command=self.next); self.b_next.pack(side="right")
        self.b_back = ttk.Button(nav, text="Back", command=self.back); self.b_back.pack(side="right", padx=6)
        self.show()
        self.w.after(120, self.poll)
        if rerun:                       # a transient of the hidden first-run root would be hidden too
            self.w.transient(root)
            self.w.grab_set()
        self.w.focus_force()

    # ---- navigation --------------------------------------------------------------------------------------
    def clear(self):
        for c in self.body.winfo_children():
            c.destroy()

    def show(self):
        self.clear()
        self.head.config(text=STEPS[self.step])
        self.crumbs.config(text="  >  ".join(f"[{s}]" if i == self.step else s for i, s in enumerate(STEPS)))
        self.b_back.config(state="normal" if 0 < self.step and not self.busy else "disabled")
        self.b_next.config(text="Finish" if self.step == len(STEPS) - 1 else "Next", state="normal")
        getattr(self, f"page_{self.step}")()

    def next(self):
        if self.busy:
            return
        if self.step == 2 and not self.save_folders():
            return
        if self.step == len(STEPS) - 1:
            return self.finish()
        self.step += 1
        self.show()

    def back(self):
        if not self.busy and self.step > 0:
            self.step -= 1
            self.show()

    def skip(self):
        if self.busy:
            if not messagebox.askyesno("Photo Sorter", "A download or test is running. Stop it and leave setup?",
                                       parent=self.w):
                return
            self.cancel_flag.set()
        elif not messagebox.askyesno("Photo Sorter", "Leave setup? You can run it again from Help > Run setup again. "
                                     "Without the AI models, sorting still works but uses simple rules only.",
                                     parent=self.w):
            return
        self.close(None)

    def close(self, result):
        try:
            self.w.grab_release()
        except tk.TclError:
            pass
        self.w.destroy()
        self.on_finish(result)

    def text(self, parent, s, **kw):
        lbl = ttk.Label(parent, text=s, wraplength=700, justify="left", **kw)
        lbl.pack(anchor="w", pady=3)
        return lbl

    # ---- pages -------------------------------------------------------------------------------------------
    def page_0(self):
        self.text(self.body, "Photo Sorter sorts copies of your photos by date, place, people and type, finds "
                             "duplicates, and lets you search them. Setup takes a few minutes:")
        for s in ("1. Check this PC's graphics card, so the AI can use it if possible.",
                  "2. Pick the photo folders to read (never changed) and where the sorted copy goes.",
                  "3. Download the AI models once (about 950 MB) with a progress bar. Interrupted downloads resume.",
                  "4. Run a quick self-test."):
            self.text(self.body, s)
        self.text(self.body, "Everything stays on this PC. Your photos are never uploaded; the only internet use is "
                             "downloading the models and checking GitHub for app updates.", foreground="#256029")
        if self.from_earlier:
            self.text(self.body, "Found the settings of an earlier Photo Sorter on this PC: your folders and index "
                                 "will be picked up as they are.", foreground="#1d4ed8")

    def page_1(self):
        self.pc_lbl = self.text(self.body, "Checking the graphics card..." if self.probe is None else "")
        self.pc_frame = ttk.Frame(self.body); self.pc_frame.pack(fill="x", pady=6)
        box = ttk.LabelFrame(self.body, text="Run the image AI on"); box.pack(fill="x", pady=8)
        ttk.Radiobutton(box, text="Graphics card (DirectML), falls back to the CPU on its own if it fails",
                        variable=self.v_gpu, value=True).pack(anchor="w", padx=6, pady=2)
        ttk.Radiobutton(box, text="CPU only (slower, works everywhere)", variable=self.v_gpu,
                        value=False).pack(anchor="w", padx=6, pady=2)
        ttk.Button(self.body, text="Check again", command=self.start_probe).pack(anchor="w")
        if self.probe is None:
            self.start_probe()
        else:
            self.render_probe()

    def start_probe(self):
        self.pc_lbl.config(text="Checking the graphics card...")
        threading.Thread(target=lambda: self.q.put(("probe", onboarding.detect_gpus())), daemon=True).start()

    def render_probe(self):
        if self.step != 1:
            return
        p = self.probe or {}
        rec = onboarding.recommend(p)
        for c in self.pc_frame.winfo_children():
            c.destroy()
        ads = p.get("adapters") or []
        if ads:
            for a in ads:
                kind = "software, not used" if onboarding.is_software(a) else f"{a.get('vram_mb', 0) // 1024} GB video memory"
                ttk.Label(self.pc_frame, text=f"  - {a.get('name')} ({kind})").pack(anchor="w")
        else:
            ttk.Label(self.pc_frame, text="  - No graphics adapters reported.").pack(anchor="w")
        dml = "DmlExecutionProvider" in p.get("providers", [])
        ttk.Label(self.pc_frame, text=f"  DirectML GPU acceleration: {'available' if dml else 'not available'}").pack(anchor="w")
        self.pc_lbl.config(text=("Recommended: graphics card. " if rec["use_gpu"] else "Recommended: CPU. ") + rec["why"])
        if "pc_set" not in p:
            self.v_gpu.set(rec["use_gpu"])
            p["pc_set"] = True

    def page_2(self):
        f = ttk.Frame(self.body); f.pack(fill="x")
        ttk.Label(f, text="Photo folders to read (never changed):").grid(row=0, column=0, sticky="w")
        self.lb = tk.Listbox(f, height=5)
        self.lb.grid(row=1, column=0, rowspan=2, sticky="nsew")
        for s in self.sources:
            self.lb.insert("end", s)
        ttk.Button(f, text="Add folder...", command=self.add_src).grid(row=1, column=1, sticky="ew", padx=4)
        ttk.Button(f, text="Remove", command=self.rm_src).grid(row=2, column=1, sticky="new", padx=4)
        ttk.Label(f, text="Output folder (the sorted copy and its index; pick a drive with room):").grid(
            row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(f, textvariable=self.v_out).grid(row=4, column=0, sticky="ew")
        ttk.Button(f, text="Browse...", command=lambda: self.pick(self.v_out, "Choose the output folder")).grid(
            row=4, column=1, sticky="ew", padx=4)
        ttk.Label(f, text="AI models folder (about 1 GB, kept when the app updates):").grid(
            row=5, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(f, textvariable=self.v_models).grid(row=6, column=0, sticky="ew")
        ttk.Button(f, text="Browse...", command=lambda: self.pick(self.v_models, "Choose the models folder")).grid(
            row=6, column=1, sticky="ew", padx=4)
        f.columnconfigure(0, weight=1)
        self.space_lbl = self.text(self.body, "")
        ttk.Button(self.body, text="Check free space", command=self.check_space).pack(anchor="w")
        if self.from_earlier:
            self.text(self.body, "These come from your earlier install. Keep the same output folder to carry on with "
                                 "its index, people, albums and Clean up decisions.", foreground="#1d4ed8")
        self.check_space()

    def add_src(self):
        d = filedialog.askdirectory(title="Choose a photo folder", parent=self.w)
        if d and d not in self.lb.get(0, "end"):
            self.lb.insert("end", d)
            self.sources = list(self.lb.get(0, "end"))
            self.check_space()

    def rm_src(self):
        for i in reversed(self.lb.curselection()):
            self.lb.delete(i)
        self.sources = list(self.lb.get(0, "end"))

    def pick(self, var, title):
        d = filedialog.askdirectory(title=title, parent=self.w)
        if d:
            var.set(d)
            self.check_space()

    def check_space(self):
        self.space_lbl.config(text="Checking free space...")
        args = (self.v_models.get().strip(), self.v_out.get().strip(), list(self.sources))
        threading.Thread(target=lambda: self.q.put(("space", onboarding.disk_check(*args))), daemon=True).start()

    def render_space(self, r):
        if self.step != 2:
            return
        lib = f"{_gb(r['library_bytes'])}{'' if r['library_complete'] else '+'}"
        txt = (f"Models drive: {_gb(r['models_free'])} free (needs about {_gb(r['need_models'])}).   "
               f"Output drive: {_gb(r['output_free'])} free. Photos found: {lib}; the sorted copy needs about the same.")
        if r["warnings"]:
            txt += "\nWarning: " + " ".join(r["warnings"])
        self.space_lbl.config(text=txt, foreground="#b45309" if r["warnings"] else "")

    def save_folders(self) -> bool:
        out, mdir = self.v_out.get().strip(), self.v_models.get().strip()
        if not out:
            messagebox.showinfo("Photo Sorter", "Choose an output folder for the sorted copy.", parent=self.w)
            return False
        bad = [s for s in self.sources if Path(out).resolve() == Path(s).resolve() or
               Path(s).resolve() in Path(out).resolve().parents]
        if bad:
            messagebox.showwarning("Photo Sorter", "The output folder can't be one of the photo folders (or inside "
                                   "one). Pick a separate folder.", parent=self.w)
            return False
        try:
            onboarding.set_models_dir(mdir or onboarding.default_models_dir())
        except OSError as e:
            messagebox.showerror("Photo Sorter", f"Can't use that models folder: {e}", parent=self.w)
            return False
        return True

    def page_3(self):
        st = onboarding.models_status()
        mb = st["missing_bytes"]
        ai_state = "installed" if st["siglip2"] else _gb(mb["siglip2"]) + " to download"
        face_state = "installed" if st["yunet_sface"] else f"{mb['yunet_sface'] >> 20} MB to download"
        ttk.Checkbutton(self.body, text=f"Image AI: types, tags, near-duplicates, text search (SigLIP 2, Apache-2.0) - "
                        f"{ai_state}", variable=self.v_ai).pack(anchor="w")
        ttk.Checkbutton(self.body, text=f"Face recognition (OpenCV YuNet + SFace) - {face_state}",
                        variable=self.v_faces).pack(anchor="w")
        self.text(self.body, f"Saved in: {st['models_dir']}", foreground="#666")
        self.import_frame = ttk.Frame(self.body); self.import_frame.pack(fill="x", pady=4)
        self.dl_lbl = self.text(self.body, "")
        self.file_bar = ttk.Progressbar(self.body, maximum=100); self.file_bar.pack(fill="x", pady=2)
        self.all_bar = ttk.Progressbar(self.body, maximum=100); self.all_bar.pack(fill="x", pady=2)
        bf = ttk.Frame(self.body); bf.pack(fill="x", pady=6)
        self.b_dl = ttk.Button(bf, text="Download", command=self.start_download); self.b_dl.pack(side="left")
        self.b_cancel = ttk.Button(bf, text="Cancel", command=self.cancel_flag.set, state="disabled")
        self.b_cancel.pack(side="left", padx=6)
        if st["siglip2"] and st["yunet_sface"]:
            self.dl_lbl.config(text="All models are installed.")
            self.b_dl.config(text="Check files", command=self.start_verify)
        threading.Thread(target=lambda: self.q.put(("legacy", onboarding.find_existing_model_folders())),
                         daemon=True).start()

    def render_legacy(self, found):
        if self.step != 3 or not found:
            return
        st = onboarding.models_status()
        if st["siglip2"] and st["yunet_sface"]:
            return
        for c in self.import_frame.winfo_children():
            c.destroy()
        ttk.Label(self.import_frame, text=f"Found models from an earlier install: {found[0]}").pack(side="left")
        ttk.Button(self.import_frame, text="Use these (no download)",
                   command=lambda: self.start_import(found[0])).pack(side="left", padx=6)

    def _set_busy(self, on: bool):
        self.busy = on
        self.b_next.config(state="disabled" if on else "normal")
        self.b_back.config(state="disabled" if on else "normal")
        if hasattr(self, "b_dl") and self.b_dl.winfo_exists():
            self.b_dl.config(state="disabled" if on else "normal")
            self.b_cancel.config(state="normal" if on else "disabled")

    def _groups(self):
        return [g for g, on in (("siglip2", self.v_ai.get()), ("yunet_sface", self.v_faces.get())) if on]

    def start_download(self):
        from .models import DownloadCancelled, ensure_models, missing_bytes

        groups = self._groups()
        self.cancel_flag.clear()
        self._set_busy(True)
        total = sum(missing_bytes(g) for g in groups) or 1
        per_file: dict[str, int] = {}

        def prog(label, done, tot):
            per_file[label] = done
            self.q.put(("dl", label, done, tot, sum(per_file.values()), total))

        def work():
            try:
                for g in groups:
                    ensure_models(g, prog, cancel=self.cancel_flag.is_set)   # skips files already there
                self.q.put(("dl_done", "Models downloaded and checked (SHA-256)."))
            except DownloadCancelled:
                self.q.put(("dl_done", "Download paused. Press Download to carry on where it stopped."))
            except Exception as e:
                self.q.put(("dl_done", f"Download failed: {e}. Check the internet connection and press Download "
                                       "to retry; finished files are kept."))
        threading.Thread(target=work, daemon=True).start()

    def start_import(self, folder):
        from .models import import_models_from

        self._set_busy(True)

        def work():
            try:
                got = import_models_from(folder, self._groups() or ("siglip2", "yunet_sface"),
                                         lambda label, d, t: self.q.put(("dl", f"Copying {label}", d, t, 0, 1)))
                st = onboarding.models_status()
                missing = [g for g in self._groups() if not st[g]]
                msg = f"Copied {len(got)} checked file(s)." + (" Press Download for the rest." if missing else "")
                self.q.put(("dl_done", msg))
            except Exception as e:
                self.q.put(("dl_done", f"Copy failed: {e}"))
        threading.Thread(target=work, daemon=True).start()

    def start_verify(self):
        from .models import verify_group

        self._set_busy(True)

        def work():
            bad = [b for g in self._groups() for b in verify_group(g)]
            if bad:
                from .models import model_path

                for b in bad:
                    model_path(b).unlink(missing_ok=True)
                self.q.put(("dl_done", f"{len(bad)} damaged file(s) removed; press Download to fetch them again."))
            else:
                self.q.put(("dl_done", "All model files match their checksums."))
        threading.Thread(target=work, daemon=True).start()

    def page_4(self):
        self.test_lbl = self.text(self.body, "")
        self.test_box = ttk.Frame(self.body); self.test_box.pack(fill="x")
        ttk.Button(self.body, text="Run the self-test", command=self.start_test).pack(anchor="w", pady=6)
        if self.test_result is None:
            self.start_test()
        else:
            self.render_test()

    def start_test(self):
        st = onboarding.models_status()
        ai, faces = self.v_ai.get() and st["siglip2"], self.v_faces.get() and st["yunet_sface"]
        if not (ai or faces):
            self.test_lbl.config(text="No AI models installed, so there is nothing to test. Sorting by date, place "
                                      "and duplicates still works.")
            return
        self._set_busy(True)
        self.test_lbl.config(text="Testing the models on a made-up picture (up to a minute)...")
        gpu = self.v_gpu.get()
        threading.Thread(target=lambda: self.q.put(("test", onboarding.run_self_test(gpu, ai, faces))),
                         daemon=True).start()

    def render_test(self):
        if self.step != 4:
            return
        r = self.test_result or {}
        for c in self.test_box.winfo_children():
            c.destroy()
        for s in r.get("steps", []):
            where = (" on the graphics card" if s.get("gpu") else " on the CPU") if "gpu" in s else ""
            line = f"{'OK' if s.get('ok') else 'FAILED'}  {s['step']}{where}" + \
                (f" ({s['ms']} ms)" if "ms" in s else "") + (f": {s['error']}" if s.get("error") else "")
            ttk.Label(self.test_box, text=line, wraplength=700).pack(anchor="w")
        ttk.Label(self.test_box, text=f"HEIC/HEIF (iPhone) photos: {'supported' if r.get('heic') else 'not supported'}"
                  ).pack(anchor="w")
        if "gpu_failed" in r:
            self.v_gpu.set(False)
            msg = "The graphics card test failed, so the app will use the CPU (you can switch back later)."
        elif r.get("ok"):
            gpu_steps = [s for s in r.get("steps", []) if s.get("gpu")]
            msg = "Everything works." + (" The image AI runs on the graphics card." if gpu_steps else "")
        else:
            msg = r.get("error") or "Something failed. Go back to AI models and press Check files, or try CPU only."
        self.test_lbl.config(text=msg)

    def page_5(self):
        self.text(self.body, "Setup is done. Press Finish to open Photo Sorter, then Start / Resume to sort.")
        self.text(self.body, f"Output: {self.v_out.get()}\nRuns on: {'graphics card' if self.v_gpu.get() else 'CPU'}"
                             f"\nImage AI: {'on' if self.v_ai.get() else 'off'}   Faces: {'on' if self.v_faces.get() else 'off'}")
        ttk.Checkbutton(self.body, text="Check GitHub for Photo Sorter updates once a day (you choose when to install)",
                        variable=self.v_updates).pack(anchor="w", pady=8)

    def finish(self):
        st = onboarding.models_status()
        prev = dict(self.prev)
        s = Settings.from_dict(prev)
        s.sources, s.output = list(self.sources), self.v_out.get().strip()
        s.use_gpu = bool(self.v_gpu.get())
        s.use_ai = bool(self.v_ai.get() and st["siglip2"])
        s.use_faces = bool(self.v_faces.get() and st["yunet_sface"])
        if s.use_faces and s.face_pack not in ("yunet_sface",) and not prev.get("face_pack"):
            s.face_pack = "yunet_sface"
        d = {**prev, **s.to_dict()}
        GUI_STATE.write_text(json.dumps(d, indent=2), encoding="utf-8")
        save_app_config(update_check=bool(self.v_updates.get()))
        onboarding.mark_done(use_ai=s.use_ai, use_faces=s.use_faces, use_gpu=s.use_gpu,
                             gpu=(onboarding.recommend(self.probe or {}).get("gpu")))
        self.close(d)

    # ---- worker messages ---------------------------------------------------------------------------------
    def poll(self):
        try:
            while True:
                m = self.q.get_nowait()
                k = m[0]
                if k == "probe":
                    self.probe = m[1]
                    self.render_probe()
                elif k == "space":
                    self.render_space(m[1])
                elif k == "legacy":
                    self.render_legacy(m[1])
                elif k == "dl" and self.step == 3:
                    _, label, done, tot, all_done, all_tot = m
                    self.dl_lbl.config(text=f"{label}: {done >> 20} / {tot >> 20} MB   (all: {all_done >> 20} / "
                                            f"{all_tot >> 20} MB)")
                    self.file_bar.config(value=100 * done / (tot or 1))
                    self.all_bar.config(value=min(100, 100 * all_done / (all_tot or 1)))
                elif k == "dl_done":
                    self._set_busy(False)
                    if self.step == 3:
                        self.show()
                        self.dl_lbl.config(text=m[1])
                elif k == "test":
                    self._set_busy(False)
                    self.test_result = m[1]
                    self.render_test()
        except queue.Empty:
            pass
        if self.w.winfo_exists():
            self.w.after(120, self.poll)
