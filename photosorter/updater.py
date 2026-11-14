"""In-app updates (v0.5) from this repo's GitHub Releases. No third-party scripts: the app reads the public
Releases API, downloads the installer asset, checks its SHA-256 against the SHA256SUMS.txt published with the
release (and GitHub's own asset digest when present), then runs the installer silently after the app closes.
Your photos, index, settings and models live outside the install folder, so an update never touches them."""
from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Callable

from . import __version__
from .config import app_data_dir, is_frozen, load_app_config, save_app_config

log = logging.getLogger("photosorter")

REPO = "apm84/photo-sorter"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
INSTALLER_RE = re.compile(r"^PhotoSorterSetup.*\.exe$", re.I)
SUMS_NAME = "SHA256SUMS.txt"
CHECK_EVERY_S = 24 * 3600


def parse_version(v: str) -> tuple:
    m = re.match(r"^v?(\d+)\.(\d+)(?:\.(\d+))?(?:[-.]?(a|b|rc|beta|alpha)\.?(\d*))?", (v or "").strip(), re.I)
    if not m:
        return (0, 0, 0, 0, 0)
    pre = m.group(4)
    rank = {"alpha": 0, "a": 0, "beta": 1, "b": 1, "rc": 2}.get((pre or "").lower(), 3)   # final > rc > beta
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0), rank, int(m.group(5) or 0))


def is_newer(remote: str, local: str = __version__) -> bool:
    return parse_version(remote) > parse_version(local)


def _get_json(url: str, timeout: float = 20) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": f"PhotoSorter/{__version__}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _get_text(url: str, timeout: float = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": f"PhotoSorter/{__version__}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(1 << 20).decode("utf-8", "replace")


def parse_sums(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        m = re.match(r"^([0-9a-fA-F]{64})\s+\*?(\S.*)$", line.strip())
        if m:
            out[m.group(2).strip()] = m.group(1).lower()
    return out


def check(fetch_json: Callable[[str], dict] = _get_json, fetch_text: Callable[[str], str] = _get_text,
          local: str = __version__) -> dict:
    """Latest release info: {available, version, notes, url, asset_url, asset_name, size, sha256}."""
    rel = fetch_json(API_LATEST)
    tag = rel.get("tag_name") or ""
    info = {"available": False, "version": tag.lstrip("v"), "current": local, "notes": rel.get("body") or "",
            "url": rel.get("html_url") or RELEASES_PAGE, "name": rel.get("name") or tag,
            "published": rel.get("published_at")}
    if rel.get("draft") or rel.get("prerelease") or not tag.startswith("v"):
        return info
    assets = rel.get("assets") or []
    inst = next((a for a in assets if INSTALLER_RE.match(a.get("name", ""))), None)
    sums = next((a for a in assets if a.get("name") == SUMS_NAME), None)
    if not inst:
        info["error"] = "This release has no Windows installer yet."
        return info
    sha = ""
    if sums:
        sha = parse_sums(fetch_text(sums["browser_download_url"])).get(inst["name"], "")
    digest = (inst.get("digest") or "")
    if digest.startswith("sha256:"):
        d = digest.split(":", 1)[1].lower()
        if sha and d != sha:
            info["error"] = "The release's checksums don't agree; not offering this update."
            return info
        sha = sha or d
    if not sha:
        info["error"] = "The release has no checksum for the installer; not offering this update."
        return info
    info.update(available=is_newer(tag, local), asset_url=inst["browser_download_url"], asset_name=inst["name"],
                size=int(inst.get("size") or 0), sha256=sha)
    return info


# ---- automatic daily check -----------------------------------------------------------------------------------
def auto_check_enabled() -> bool:
    return bool(load_app_config().get("update_check", True))


def set_auto_check(on: bool) -> None:
    save_app_config(update_check=bool(on))


def due(now: float | None = None) -> bool:
    cfg = load_app_config()
    now = time.time() if now is None else now
    return auto_check_enabled() and now - float(cfg.get("update_last_check") or 0) >= CHECK_EVERY_S


def record_check(info: dict | None = None) -> None:
    save_app_config(update_last_check=time.time(),
                    update_last_seen=(info or {}).get("version") if (info or {}).get("available") else None)


# ---- download + install ------------------------------------------------------------------------------------
def updates_dir() -> Path:
    d = app_data_dir() / "updates"
    d.mkdir(parents=True, exist_ok=True)
    return d


def download(info: dict, progress: Callable[[str, int, int], None] | None = None,
             cancel: Callable[[], bool] | None = None) -> Path:
    from .models import download_file

    if not info.get("asset_url") or not info.get("sha256"):
        raise ValueError("No installer to download")
    name = Path(info["asset_name"]).name
    dest = updates_dir() / f"{Path(name).stem}-{info['version']}.exe"
    if dest.exists():
        from .models import sha256_file

        if sha256_file(dest) == info["sha256"]:
            return dest
        dest.unlink()
    for old in updates_dir().glob("PhotoSorterSetup*.exe"):   # keep only the newest download
        if old != dest:
            old.unlink(missing_ok=True)
    return download_file(info["asset_url"], dest, progress, f"Photo Sorter {info['version']}",
                         sha256=info["sha256"], size=info.get("size") or 0, cancel=cancel)


def install_command(installer: Path, relaunch: bool = True) -> list[str]:
    args = [str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS",
            "/SP-", f"/LOG={updates_dir() / 'install.log'}"]
    if relaunch:
        args.append("/RELAUNCH=1")
    return args


def can_self_install() -> bool:
    return sys.platform == "win32" and is_frozen()


def launch_installer(installer: Path, relaunch: bool = True) -> None:  # pragma: no cover - Windows only
    """Start the verified installer detached; the caller then closes the app. Inno Setup waits for (and if
    needed closes) running Photo Sorter windows, upgrades in place and starts the app again."""
    from .models import sha256_file

    info_sha = load_app_config().get("update_pending_sha256")
    if info_sha and sha256_file(installer) != info_sha:
        raise RuntimeError("The downloaded installer changed after it was checked; not running it.")
    flags = 0x00000008 | 0x00000200        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(install_command(installer, relaunch), close_fds=True, creationflags=flags,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
