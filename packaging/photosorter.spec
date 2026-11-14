# PyInstaller spec: pyinstaller --noconfirm packaging/photosorter.spec   (Build-Exe.bat, .github/workflows/release.yml)
import os

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
datas = collect_data_files("reverse_geocoder") + [(os.path.join(ROOT, "photosorter", "web", "static"),
                                                   os.path.join("photosorter", "web", "static"))]
for f in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
    if os.path.exists(os.path.join(ROOT, f)):
        datas.append((os.path.join(ROOT, f), "."))
binaries = collect_dynamic_libs("onnxruntime")   # includes DirectML.dll with onnxruntime-directml
hidden = collect_submodules("uvicorn") + collect_submodules("send2trash") + ["sklearn.cluster", "photosorter.cli", "photosorter.gui",
                                          "photosorter.web.app", "photosorter.embedders", "photosorter.faces",
                                          "photosorter.cleanup", "photosorter.albums", "photosorter.facegroup",
                                          "photosorter.thumbs", "photosorter.onboarding", "photosorter.updater",
                                          "photosorter.gui_setup", "photosorter.sync", "photosorter.verify",
                                          "photosorter.prefs", "photosorter.background"]

# HEIC: the installer bundles pi-heif, the decode-only edition of pillow-heif (libheif + libde265, LGPL-3.0,
# dynamically linked DLLs). pillow-heif itself is excluded because its wheels also carry the GPL x265 encoder.
try:
    import pi_heif  # noqa: F401
    hidden += collect_submodules("pi_heif")
    binaries += collect_dynamic_libs("pi_heif")
    datas += collect_data_files("pi_heif")
except ImportError:
    pass

a = Analysis([os.path.join(SPECPATH, "launcher.py")], pathex=[ROOT], binaries=binaries, datas=datas,
             hiddenimports=hidden, module_collection_mode={"reverse_geocoder": "py"},   # LGPL: shipped as replaceable .py files
             excludes=["pillow_heif", "torch", "transformers", "matplotlib", "pytest", "IPython"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="PhotoSorter", console=False,
          version=os.path.join(SPECPATH, "version_info.txt") if os.path.exists(os.path.join(SPECPATH, "version_info.txt")) else None)
coll = COLLECT(exe, a.binaries, a.datas, name="PhotoSorter")
