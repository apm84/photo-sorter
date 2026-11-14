"""First-run setup (v0.5): hardware check, folders, model downloads and a self-test. Everything runs on this
PC; photos are never uploaded. The Tk wizard lives in `gui_setup.py`; this module is the testable logic."""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import __version__
from .config import app_data_dir, default_models_dir, install_dir, is_frozen, load_app_config, models_dir, save_app_config

log = logging.getLogger("photosorter")

ONBOARDING_JSON = "onboarding.json"
MODELS_NEED_MB = 1100            # SigLIP 2 (~950 MB) + faces (~40 MB) + room for the .part file of the biggest
LIBRARY_HINT_PCT = 1.05          # the sorted library is a full copy of the originals (+ a little index)
SOFTWARE_GPU_NAMES = ("microsoft basic render", "microsoft basic display", "remote display", "parsec", "virtual")


# ---- state -------------------------------------------------------------------------------------------------
def state_path() -> Path:
    return app_data_dir() / ONBOARDING_JSON


def load_state() -> dict:
    try:
        d = json.loads(state_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def mark_done(**extra) -> dict:
    d = load_state()
    d.update(extra, completed=True, version=__version__, completed_at=time.time())
    state_path().write_text(json.dumps(d, indent=2), encoding="utf-8")
    return d


def needs_onboarding() -> bool:
    """Show the setup wizard on first run of the installed app, or when the default models are missing."""
    if os.environ.get("PHOTOSORTER_SKIP_ONBOARDING"):
        return False
    if not load_state().get("completed"):
        return True
    from .embedders import embedder_available
    from .faces import faces_available

    st = load_state()
    want_ai, want_faces = st.get("use_ai", True), st.get("use_faces", True)
    return (want_ai and not embedder_available("siglip2")) or (want_faces and not faces_available("yunet_sface"))


# ---- hardware ----------------------------------------------------------------------------------------------
def _dxgi_adapters() -> list[dict]:  # pragma: no cover - Windows only
    """Graphics adapters from DXGI (the list DirectML chooses from). Runs in a child process (see detect_gpus)."""
    import ctypes
    from ctypes import POINTER, Structure, byref, c_long, c_size_t, c_uint, c_ulong, c_void_p, c_wchar

    class GUID(Structure):
        _fields_ = [("d1", c_ulong), ("d2", ctypes.c_ushort), ("d3", ctypes.c_ushort), ("d4", ctypes.c_ubyte * 8)]

    class LUID(Structure):
        _fields_ = [("low", c_ulong), ("high", c_long)]

    class DESC1(Structure):
        _fields_ = [("Description", c_wchar * 128), ("VendorId", c_uint), ("DeviceId", c_uint), ("SubSysId", c_uint),
                    ("Revision", c_uint), ("DedicatedVideoMemory", c_size_t), ("DedicatedSystemMemory", c_size_t),
                    ("SharedSystemMemory", c_size_t), ("AdapterLuid", LUID), ("Flags", c_uint)]

    iid = GUID(0x770AAE78, 0xF26F, 0x4DBA, (ctypes.c_ubyte * 8)(0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87))
    factory = c_void_p()
    hr = ctypes.windll.dxgi.CreateDXGIFactory1(byref(iid), byref(factory))
    if hr != 0 or not factory:
        raise OSError(f"CreateDXGIFactory1 failed (0x{hr & 0xFFFFFFFF:08X})")

    def method(obj, index, *argtypes):
        vtbl = ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents
        return ctypes.WINFUNCTYPE(c_long, c_void_p, *argtypes)(vtbl[index])

    out = []
    try:
        for i in range(16):
            adapter = c_void_p()
            hr = method(factory, 12, c_uint, POINTER(c_void_p))(factory, i, byref(adapter))   # EnumAdapters1
            if hr != 0 or not adapter:
                break                                                     # DXGI_ERROR_NOT_FOUND = no more
            try:
                d = DESC1()
                if method(adapter, 10, POINTER(DESC1))(adapter, byref(d)) == 0:                 # GetDesc1
                    out.append({"name": d.Description, "vendor_id": d.VendorId, "device_id": d.DeviceId,
                                "vram_mb": int(d.DedicatedVideoMemory) >> 20,
                                "shared_mb": int(d.SharedSystemMemory) >> 20,
                                "software": bool(d.Flags & 2), "source": "dxgi"})
            finally:
                method(adapter, 2)(adapter)                                                    # Release
    finally:
        method(factory, 2)(factory)
    return out


def _cim_adapters() -> list[dict]:  # pragma: no cover - Windows only
    ps = ("Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM,PNPDeviceID | ConvertTo-Json -Compress")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True,
                       text=True, timeout=20, creationflags=0x08000000)
    data = json.loads(r.stdout or "[]")
    data = [data] if isinstance(data, dict) else data
    return [{"name": d.get("Name") or "?", "vram_mb": int(d.get("AdapterRAM") or 0) >> 20, "software": False,
             "source": "wmi"} for d in data]


def self_command(*args: str) -> list[str]:
    """Command line that runs this app's CLI (the .exe when installed, python -m photosorter from source)."""
    if is_frozen():
        return [sys.executable, *args]
    exe = Path(sys.executable)
    if sys.platform == "win32" and exe.name.lower() == "python.exe" and (exe.parent / "pythonw.exe").exists():
        exe = exe.parent / "pythonw.exe"
    return [str(exe), "-m", "photosorter", *args]


def run_child_json(args: list[str], timeout: float = 60) -> dict:
    """Run `photosorter <args> --json <file>` in a child process (a GPU driver crash can't take the app down)."""
    fd, tmp = tempfile.mkstemp(prefix="photosorter-", suffix=".json")
    os.close(fd)
    try:
        kw = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
        subprocess.run(self_command(*args, "--json", tmp), cwd=str(install_dir()), timeout=timeout,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
        txt = Path(tmp).read_text(encoding="utf-8")
        return json.loads(txt) if txt.strip() else {"ok": False, "error": "no result (the check crashed)"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timed out after {int(timeout)} s"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        Path(tmp).unlink(missing_ok=True)


def gpu_probe() -> dict:
    """What the child process reports for `photosorter gpu-probe`."""
    res = {"adapters": [], "providers": [], "errors": []}
    try:
        from .models import available_providers

        res["providers"] = available_providers()
    except Exception as e:
        res["errors"].append(f"onnxruntime: {e}")
    if sys.platform == "win32":
        for fn in (_dxgi_adapters, _cim_adapters):
            try:
                res["adapters"] = fn()
                if res["adapters"]:
                    break
            except Exception as e:
                res["errors"].append(f"{fn.__name__}: {e}")
    return res


def is_software(adapter: dict) -> bool:
    n = (adapter.get("name") or "").lower()
    return bool(adapter.get("software")) or any(s in n for s in SOFTWARE_GPU_NAMES)


def recommend(probe: dict) -> dict:
    """GPU or CPU mode from the probe: GPU only when DirectML is present and a real graphics card is found."""
    dml = "DmlExecutionProvider" in probe.get("providers", [])
    real = [a for a in probe.get("adapters", []) if not is_software(a)]
    real.sort(key=lambda a: a.get("vram_mb", 0), reverse=True)
    best = real[0] if real else None
    if dml and best and best.get("vram_mb", 0) >= 1024:
        return {"use_gpu": True, "gpu": best["name"],
                "why": f"{best['name']} ({best['vram_mb'] // 1024} GB) supports DirectML: the image AI should run "
                       "several times faster on it."}
    if dml and best:
        return {"use_gpu": True, "gpu": best["name"],
                "why": f"{best['name']} looks usable through DirectML (shared or small memory). If it is slow or "
                       "fails, the app moves to the CPU on its own."}
    if not dml:
        return {"use_gpu": False, "gpu": best["name"] if best else None,
                "why": "GPU acceleration (DirectML) isn't available in this build or on this PC, so the CPU is used."}
    return {"use_gpu": False, "gpu": None, "why": "No graphics card that can run the AI was found, so the CPU is used."}


def detect_gpus(timeout: float = 30) -> dict:
    """Probe in a child process when possible (DXGI calls are native code), else in-process."""
    if sys.platform == "win32":
        res = run_child_json(["gpu-probe"], timeout=timeout)
        if "adapters" in res:
            return res
        res2 = gpu_probe()
        res2["errors"].append(f"child probe: {res.get('error')}")
        return res2
    return gpu_probe()


# ---- disk --------------------------------------------------------------------------------------------------
def _existing_parent(p: Path) -> Path:
    p = Path(p)
    while not p.exists() and p.parent != p:
        p = p.parent
    return p


def free_bytes(path) -> int:
    try:
        return shutil.disk_usage(_existing_parent(Path(path))).free
    except Exception:
        return -1


def folder_bytes(folders, limit_s: float = 4.0) -> tuple[int, bool]:
    """Rough size of the photo folders (stops after `limit_s`; the bool says whether it finished)."""
    from .config import IMAGE_EXTS

    tot, t0 = 0, time.monotonic()
    for f in folders:
        for root, _dirs, files in os.walk(f):
            for n in files:
                if os.path.splitext(n)[1].lower() in IMAGE_EXTS:
                    try:
                        tot += os.path.getsize(os.path.join(root, n))
                    except OSError:
                        pass
            if time.monotonic() - t0 > limit_s:
                return tot, False
    return tot, True


def disk_check(models_folder, output_folder, sources=(), need_models_mb: int = MODELS_NEED_MB) -> dict:
    """Free space for models and for the sorted copy. Same drive -> both needs add up."""
    mfree, ofree = free_bytes(models_folder), free_bytes(output_folder) if output_folder else -1
    lib, complete = folder_bytes(sources) if sources else (0, True)
    need_out = int(lib * LIBRARY_HINT_PCT)
    try:
        same = bool(output_folder) and os.stat(_existing_parent(Path(models_folder))).st_dev == \
            os.stat(_existing_parent(Path(output_folder))).st_dev
    except OSError:
        same = False
    need_m = need_models_mb << 20
    warn = []
    if mfree >= 0 and mfree < need_m + (need_out if same else 0):
        warn.append(f"The models folder's drive has {mfree / 2**30:.1f} GB free; about "
                    f"{(need_m + (need_out if same else 0)) / 2**30:.1f} GB is needed.")
    if output_folder and ofree >= 0 and need_out and ofree < need_out + (need_m if same else 0):
        warn.append(f"The output drive has {ofree / 2**30:.1f} GB free, but the sorted copy of your photos needs "
                    f"about {need_out / 2**30:.1f} GB{'' if complete else ' or more'}.")
    return {"models_free": mfree, "output_free": ofree, "library_bytes": lib, "library_complete": complete,
            "need_models": need_m, "need_output": need_out, "same_drive": same, "warnings": warn, "ok": not warn}


# ---- models ------------------------------------------------------------------------------------------------
def set_models_dir(folder) -> Path:
    """Remember a models folder outside the install folder (kept across updates)."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    if folder.resolve() == default_models_dir().resolve():
        save_app_config(models_dir="")
    else:
        save_app_config(models_dir=str(folder))
    return models_dir()


def find_existing_model_folders() -> list[Path]:
    """Model folders from an earlier source install (e.g. D:\\PhotoSorter\\PhotoSorter-v0.1-src\\models), so a
    friend upgrading from the zip doesn't download 1 GB again."""
    cands: list[Path] = []
    roots = []
    if sys.platform == "win32":  # pragma: no cover - Windows only
        import string

        for d in string.ascii_uppercase:
            r = Path(f"{d}:\\")
            try:
                if r.exists():
                    roots.append(r)
            except OSError:
                pass
        roots.append(Path.home())
    extra = os.environ.get("PHOTOSORTER_LEGACY_SEARCH")
    if extra:
        roots += [Path(x) for x in extra.split(os.pathsep) if x]
    for r in roots:
        for base in (r / "PhotoSorter", r):
            try:
                for sub in [base, *[p for p in base.iterdir() if p.is_dir()]] if base.is_dir() else []:
                    m = sub / "models"
                    if (m / "siglip2" / "vision_model.onnx").is_file() or \
                            (m / "faces" / "yunet_sface" / "face_recognition_sface_2021dec.onnx").is_file():
                        if m.resolve() != models_dir().resolve() and m not in cands:
                            cands.append(m)
            except OSError:
                continue
    return cands


def models_status() -> dict:
    from .embedders import embedder_available
    from .faces import faces_available
    from .models import missing_bytes

    return {"siglip2": embedder_available("siglip2"), "yunet_sface": faces_available("yunet_sface"),
            "missing_bytes": {"siglip2": missing_bytes("siglip2"), "yunet_sface": missing_bytes("yunet_sface")},
            "models_dir": str(models_dir())}


# ---- self-test ---------------------------------------------------------------------------------------------
def self_test(use_gpu: bool = True, use_ai: bool = True, use_faces: bool = True) -> dict:
    """Load the models and run them once on a made-up picture (no photos needed)."""
    import numpy as np
    from PIL import Image, ImageDraw

    res: dict = {"ok": True, "steps": [], "use_gpu": use_gpu}
    im = Image.new("RGB", (640, 480), (90, 140, 200))
    d = ImageDraw.Draw(im)
    d.ellipse([220, 120, 420, 360], fill=(230, 190, 160))
    for x in (270, 350):
        d.ellipse([x, 200, x + 30, 230], fill=(40, 40, 40))
    if use_ai:
        t0 = time.monotonic()
        try:
            from .embedders import get_embedder

            emb = get_embedder("siglip2", use_gpu=use_gpu)
            if emb is None:
                raise RuntimeError("image AI model is not installed")
            v = emb.encode_images([im])
            tv = emb.encode_texts(["a photo of a face", "a screenshot of a phone"])
            ok = bool(np.isfinite(v).all() and np.isfinite(tv).all() and v.shape[-1] == tv.shape[-1])
            res["steps"].append({"step": "image AI", "ok": ok, "gpu": bool(emb.on_gpu),
                                 "ms": int((time.monotonic() - t0) * 1000)})
            res["ok"] &= ok
        except Exception as e:
            res["steps"].append({"step": "image AI", "ok": False, "error": str(e)})
            res["ok"] = False
    if use_faces:
        t0 = time.monotonic()
        try:
            from .faces import FaceEngine

            eng = FaceEngine("yunet_sface", use_gpu=use_gpu)
            eng.faces(np.asarray(im))
            res["steps"].append({"step": "faces", "ok": True, "ms": int((time.monotonic() - t0) * 1000)})
        except Exception as e:
            res["steps"].append({"step": "faces", "ok": False, "error": str(e)})
            res["ok"] = False
    try:
        from .imaging import heic_support

        res["heic"] = heic_support()
    except Exception:
        res["heic"] = None
    return res


def run_self_test(use_gpu: bool, use_ai: bool = True, use_faces: bool = True, timeout: float = 300) -> dict:
    """Self-test in a child process; if the GPU run fails or crashes, try again on the CPU."""
    args = ["selftest"] + ([] if use_gpu else ["--cpu"]) + ([] if use_ai else ["--no-ai"]) + \
        ([] if use_faces else ["--no-faces"])
    r = run_child_json(args, timeout=timeout)
    if use_gpu and not r.get("ok"):
        cpu = run_child_json(["selftest", "--cpu"] + args[1:], timeout=timeout)
        cpu["gpu_failed"] = r.get("error") or next((s.get("error") for s in r.get("steps", []) if not s.get("ok")), "")
        return cpu
    return r


def first_run_defaults() -> dict:
    """Folders to suggest: an earlier install's choices (gui.json), else Pictures and a new output folder."""
    try:
        gui = json.loads((app_data_dir() / "gui.json").read_text(encoding="utf-8"))
    except Exception:
        gui = {}
    pics = Path.home() / "Pictures"
    return {"sources": gui.get("sources") or ([str(pics)] if pics.is_dir() else []),
            "output": gui.get("output") or str(Path.home() / "Pictures" / "Photo Sorter library"),
            "from_earlier_install": bool(gui.get("output")),
            "models_dir": str(models_dir()), "update_check": load_app_config().get("update_check", True)}
