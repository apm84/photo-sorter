"""v0.5: installer build helpers - model downloads (resume, checksum, mirrors), onboarding, in-app updates."""
import hashlib
import http.server
import json
import sys
import threading
from pathlib import Path

import pytest

from photosorter import __version__, config, models, onboarding, updater


class _RangeHandler(http.server.BaseHTTPRequestHandler):
    files: dict = {}
    hits: list = []
    fail_first: set = set()
    ignore_range = False

    def log_message(self, *a):
        pass

    def do_GET(self):
        self.hits.append((self.path, self.headers.get("Range")))
        if self.path in self.fail_first:
            self.fail_first.discard(self.path)
            data = self.files[self.path][:1000]           # drop the connection part-way
            self.send_response(200)
            self.send_header("Content-Length", str(len(self.files[self.path])))
            self.end_headers()
            self.wfile.write(data)
            self.wfile.flush()
            self.connection.close()
            return
        if self.path not in self.files:
            self.send_error(404); return
        data = self.files[self.path]
        rng = self.headers.get("Range")
        if rng and not self.ignore_range:
            start = int(rng.split("=")[1].split("-")[0])
            if start >= len(data):
                self.send_error(416); return
            body = data[start:]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        else:
            body = data
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def server():
    _RangeHandler.files, _RangeHandler.hits, _RangeHandler.fail_first = {}, [], set()
    _RangeHandler.ignore_range = False
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    yield _RangeHandler, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture(autouse=True)
def private_models_dir(tmp_path, monkeypatch):
    """Never write into a real models folder (PHOTOSORTER_TEST_MODELS); models_env re-points it when needed."""
    monkeypatch.setenv("PHOTOSORTER_MODELS", str(tmp_path / "models-under-test"))


def _blob(n=300_000, seed=1):
    return bytes((i * 31 + seed) % 251 for i in range(n))


def test_download_resumes_a_partial_file(server, tmp_path):
    h, base = server
    data = _blob()
    h.files["/m.onnx"] = data
    dest = tmp_path / "m.onnx"
    (tmp_path / "m.onnx.part").write_bytes(data[:120_000])          # left over from an interrupted run
    seen = []
    models.download_file(f"{base}/m.onnx", dest, lambda l, d, t: seen.append((d, t)), "m",
                         sha256=hashlib.sha256(data).hexdigest(), size=len(data), sleep=lambda s: None)
    assert dest.read_bytes() == data and not (tmp_path / "m.onnx.part").exists()
    assert h.hits[-1][1] == "bytes=120000-"
    assert seen[-1] == (len(data), len(data))


def test_download_restarts_when_range_is_ignored(server, tmp_path):
    h, base = server
    data = _blob(seed=2)
    h.files["/m.onnx"] = data
    h.ignore_range = True
    (tmp_path / "m.onnx.part").write_bytes(b"junk" * 10)
    models.download_file(f"{base}/m.onnx", tmp_path / "m.onnx", sha256=hashlib.sha256(data).hexdigest(),
                         sleep=lambda s: None)
    assert (tmp_path / "m.onnx").read_bytes() == data


def test_download_retries_after_a_drop_and_uses_mirrors(server, tmp_path):
    h, base = server
    data = _blob(seed=3)
    h.files["/mirror/m.onnx"] = data
    h.files["/m.onnx"] = data
    h.fail_first = {"/m.onnx"}
    dest = models.download_file([f"{base}/missing.onnx", f"{base}/m.onnx"], tmp_path / "m.onnx",
                                sha256=hashlib.sha256(data).hexdigest(), size=len(data), sleep=lambda s: None)
    assert dest.read_bytes() == data
    assert any(p == "/missing.onnx" for p, _ in h.hits)


def test_download_rejects_a_bad_checksum(server, tmp_path):
    h, base = server
    h.files["/m.onnx"] = _blob(seed=4)
    with pytest.raises(RuntimeError, match="checksum"):
        models.download_file(f"{base}/m.onnx", tmp_path / "m.onnx", sha256="0" * 64, retries=2,
                             sleep=lambda s: None)
    assert not (tmp_path / "m.onnx").exists() and not (tmp_path / "m.onnx.part").exists()


def test_download_can_be_cancelled(server, tmp_path):
    h, base = server
    h.files["/m.onnx"] = _blob(2_500_000, seed=5)
    with pytest.raises(models.DownloadCancelled):
        models.download_file(f"{base}/m.onnx", tmp_path / "m.onnx", cancel=lambda: True)


def test_ensure_models_uses_pinned_checksums(server, tmp_path, monkeypatch):
    h, base = server
    a, b = _blob(5000, 6), _blob(7000, 7)
    h.files["/a"], h.files["/b"] = a, b
    grp = [models.ModelFile("a", f"{base}/a", "g/a.onnx", 1, "MIT", sha256=hashlib.sha256(a).hexdigest(), size=len(a)),
           models.ModelFile("b", f"{base}/nope", "g/b.onnx", 1, "MIT", sha256=hashlib.sha256(b).hexdigest(),
                            size=len(b), mirrors=(f"{base}/b",))]
    monkeypatch.setitem(models.MODELS, "test", grp)
    monkeypatch.setattr(models.time, "sleep", lambda s: None)
    assert models.missing_bytes("test") == len(a) + len(b)
    models.ensure_models("test")
    assert models.is_installed("test") and models.missing_bytes("test") == 0
    assert models.verify_group("test") == []
    models.model_path("g/a.onnx").write_bytes(b"tampered")
    assert models.verify_group("test") == ["g/a.onnx"]


def test_registry_points_at_this_repos_release_and_pins_hashes():
    for g in ("siglip2", "yunet_sface"):
        for mf in models.MODELS[g]:
            assert len(mf.sha256) == 64 and mf.size > 0
    assert all(mf.url.startswith("https://github.com/apm84/photo-sorter/releases/download/models-v1/")
               for mf in models.MODELS["siglip2"]) or models.SIGLIP_BASE != models.MODELS_RELEASE_URL
    assert any("models-v1" in u for u in models.MODELS["yunet_sface"][0].mirrors)


def test_import_models_from_earlier_install(tmp_path, monkeypatch):
    old = tmp_path / "old" / "models"
    data = _blob(4000, 8)
    (old / "g").mkdir(parents=True)
    (old / "g" / "a.onnx").write_bytes(data)
    (old / "g" / "b.onnx").write_bytes(b"wrong bytes")
    grp = [models.ModelFile("a", "x", "g/a.onnx", 1, "MIT", sha256=hashlib.sha256(data).hexdigest(), size=len(data)),
           models.ModelFile("b", "x", "g/b.onnx", 1, "MIT", sha256="1" * 64, size=11)]
    monkeypatch.setitem(models.MODELS, "test", grp)
    assert models.import_models_from(old, ["test"]) == ["g/a.onnx"]
    assert models.model_path("g/a.onnx").read_bytes() == data and not models.model_path("g/b.onnx").exists()
    monkeypatch.setenv("PHOTOSORTER_LEGACY_SEARCH", str(tmp_path))
    (tmp_path / "PhotoSorter" / "PhotoSorter-v0.1-src" / "models" / "siglip2").mkdir(parents=True)
    (tmp_path / "PhotoSorter" / "PhotoSorter-v0.1-src" / "models" / "siglip2" / "vision_model.onnx").write_bytes(b"x")
    found = onboarding.find_existing_model_folders()
    assert tmp_path / "PhotoSorter" / "PhotoSorter-v0.1-src" / "models" in found


def test_models_dir_choice_is_remembered_outside_the_install(tmp_path, monkeypatch):
    monkeypatch.delenv("PHOTOSORTER_MODELS", raising=False)
    target = tmp_path / "big-drive" / "models"
    onboarding.set_models_dir(target)
    assert config.models_dir() == target
    assert json.loads((config.app_data_dir() / "app.json").read_text())["models_dir"] == str(target)
    onboarding.set_models_dir(config.default_models_dir())
    assert config.models_dir() == config.default_models_dir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "inst" / "PhotoSorter.exe"))
    (tmp_path / "inst" / "models").mkdir(parents=True)
    assert config.install_dir() == tmp_path / "inst"
    assert config.models_dir() == config.default_models_dir(), "installed app never keeps models in its folder"


def test_onboarding_state_and_defaults(tmp_path, monkeypatch):
    assert onboarding.needs_onboarding()
    (config.app_data_dir() / "gui.json").write_text(json.dumps({"sources": ["D:/Pics"], "output": "D:/Sorted"}))
    d = onboarding.first_run_defaults()
    assert d["sources"] == ["D:/Pics"] and d["output"] == "D:/Sorted" and d["from_earlier_install"]
    onboarding.mark_done(use_ai=False, use_faces=False)
    assert not onboarding.needs_onboarding()
    onboarding.mark_done(use_ai=True, use_faces=False)
    assert onboarding.needs_onboarding(), "model missing -> setup again"
    monkeypatch.setenv("PHOTOSORTER_SKIP_ONBOARDING", "1")
    assert not onboarding.needs_onboarding()


@pytest.mark.parametrize("probe,gpu", [
    ({"providers": ["DmlExecutionProvider", "CPUExecutionProvider"],
      "adapters": [{"name": "Radeon RX 570 Series", "vram_mb": 8192}, {"name": "Microsoft Basic Render Driver",
                                                                         "vram_mb": 0, "software": True}]}, True),
    ({"providers": ["CPUExecutionProvider"], "adapters": [{"name": "Radeon RX 570 Series", "vram_mb": 8192}]}, False),
    ({"providers": ["DmlExecutionProvider"], "adapters": [{"name": "Microsoft Basic Render Driver", "vram_mb": 0}]}, False),
    ({"providers": ["DmlExecutionProvider"], "adapters": [{"name": "Intel(R) UHD Graphics 620", "vram_mb": 128}]}, True),
])
def test_gpu_recommendation(probe, gpu):
    r = onboarding.recommend(probe)
    assert r["use_gpu"] is gpu and r["why"]


def test_disk_check(tmp_path):
    src = tmp_path / "src"; src.mkdir()
    (src / "a.jpg").write_bytes(b"x" * 5000)
    r = onboarding.disk_check(tmp_path / "models", tmp_path / "out", [src], need_models_mb=1)
    assert r["library_bytes"] == 5000 and r["library_complete"] and r["ok"] and r["same_drive"]
    r = onboarding.disk_check(tmp_path / "models", tmp_path / "out", [src], need_models_mb=1 << 40)
    assert not r["ok"] and r["warnings"]


def test_cli_gpu_probe_and_selftest_write_json(tmp_path):
    from photosorter.cli import main

    out = tmp_path / "probe.json"
    assert main(["gpu-probe", "--json", str(out)]) == 0
    res = json.loads(out.read_text())
    assert "CPUExecutionProvider" in res["providers"] and "recommendation" in res
    out2 = tmp_path / "st.json"
    main(["selftest", "--cpu", "--no-ai", "--no-faces", "--json", str(out2)])
    assert json.loads(out2.read_text())["ok"]


def test_self_test_runs_real_models(models_env):
    r = onboarding.self_test(use_gpu=False)
    assert r["ok"], r
    assert {s["step"] for s in r["steps"]} == {"image AI", "faces"}


def test_child_process_self_test_and_probe():
    r = onboarding.run_child_json(["selftest", "--cpu", "--no-ai", "--no-faces"], timeout=120)
    assert r.get("ok"), r
    p = onboarding.detect_gpus()
    assert "providers" in p


def test_frozen_sync_worker_uses_the_exe(tmp_path, monkeypatch):
    from photosorter import sync

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "PhotoSorter.exe"))
    cmd = sync.worker_command(tmp_path / "out")
    assert cmd[0].endswith("PhotoSorter.exe") and cmd[1:3] == ["sync", "--out"] and "-m" not in cmd
    assert onboarding.self_command("gpu-probe") == [sys.executable, "gpu-probe"]


# ---- updates -----------------------------------------------------------------------------------------------
def test_version_compare():
    assert updater.is_newer("v0.5.1", "0.5.0") and updater.is_newer("v1.0.0", "0.9.9")
    assert not updater.is_newer("v0.5.0", "0.5.0") and not updater.is_newer("v0.4.9", "0.5.0")
    assert updater.is_newer("v0.6.0", "0.6.0rc1") and not updater.is_newer("v0.6.0rc1", "0.6.0")
    assert updater.parse_version("garbage") == (0, 0, 0, 0, 0)
    assert __version__ == "0.6.1"


def _release(tag="v0.6.0", sha="a" * 64, digest=None, sums=True, draft=False):
    assets = [{"name": "PhotoSorterSetup.exe", "size": 123, "browser_download_url": "https://x/PhotoSorterSetup.exe",
               **({"digest": digest} if digest else {})}]
    if sums:
        assets.append({"name": "SHA256SUMS.txt", "browser_download_url": "https://x/SHA256SUMS.txt"})
    rel = {"tag_name": tag, "name": f"Photo Sorter {tag}", "body": "notes", "html_url": "https://x/rel",
           "draft": draft, "prerelease": False, "assets": assets}
    return rel, f"{sha}  PhotoSorterSetup.exe\n{'b' * 64}  other.zip\n"


def test_update_check_finds_and_verifies_installer():
    rel, sums = _release()
    info = updater.check(lambda u: rel, lambda u: sums, local="0.5.0")
    assert info["available"] and info["sha256"] == "a" * 64 and info["asset_url"].endswith("PhotoSorterSetup.exe")
    assert info["notes"] == "notes" and info["version"] == "0.6.0"
    assert not updater.check(lambda u: rel, lambda u: sums, local="0.6.0")["available"]


def test_update_check_refuses_without_or_with_conflicting_checksums():
    rel, sums = _release(sums=False)
    info = updater.check(lambda u: rel, lambda u: "", local="0.5.0")
    assert not info["available"] and "checksum" in info["error"]
    rel, sums = _release(sums=False, digest="sha256:" + "c" * 64)
    assert updater.check(lambda u: rel, lambda u: "", local="0.5.0")["sha256"] == "c" * 64
    rel, sums = _release(digest="sha256:" + "c" * 64)
    info = updater.check(lambda u: rel, lambda u: sums, local="0.5.0")
    assert not info["available"] and "agree" in info["error"]


def test_update_download_verifies_and_installer_command(server, tmp_path):
    h, base = server
    data = _blob(50_000, 9)
    h.files["/PhotoSorterSetup.exe"] = data
    info = {"asset_url": f"{base}/PhotoSorterSetup.exe", "asset_name": "PhotoSorterSetup.exe", "version": "0.6.0",
            "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    p = updater.download(info)
    assert p.read_bytes() == data and p.parent == updater.updates_dir()
    assert updater.download(info) == p                                   # already there, re-verified, no refetch
    cmd = updater.install_command(p)
    assert cmd[0] == str(p) and "/VERYSILENT" in cmd and "/RELAUNCH=1" in cmd
    bad = dict(info, sha256="0" * 64, version="0.6.1")
    with pytest.raises(RuntimeError):
        updater.download(bad)


def test_daily_check_schedule():
    assert updater.due(now=10**10)
    updater.record_check({"available": False})
    assert not updater.due()
    updater.set_auto_check(False)
    assert not updater.due(now=10**12)


def test_autostart_moves_to_the_installed_exe(tmp_path, monkeypatch):
    from photosorter import sync

    f = tmp_path / "Startup" / "PhotoSorter-FamilySync.cmd"
    f.parent.mkdir()
    f.write_text('@echo off\r\ncd /d "D:\\PhotoSorter\\src"\r\nstart "" /low /b "D:\\PhotoSorter\\src\\.venv\\Scripts\\'
                 'pythonw.exe" "-m" "photosorter" "sync" "--out" "D:\\Sorted"\r\n')
    monkeypatch.setattr(sync, "startup_file", lambda: f)
    assert not sync.refresh_autostart(), "source install: left alone"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "PhotoSorter.exe"))
    assert sync.refresh_autostart()
    txt = f.read_text()
    assert "PhotoSorter.exe" in txt and '"--out" "D:\\Sorted"' in txt and "pythonw" not in txt
    assert not sync.refresh_autostart(), "already up to date"
