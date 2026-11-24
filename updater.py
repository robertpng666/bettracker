"""Get updates: compare with the latest GitHub release, download POTDTracker.exe, swap, restart."""
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

from version import REPO, __version__

ASSET = "POTDTracker.exe"
FROZEN = getattr(sys, "frozen", False)
EXE = Path(sys.executable) if FROZEN else None


def _ver(v):
    return tuple(int(x) for x in v.lstrip("v").split(".") if x.isdigit())


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "POTDTracker", "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=15)


def check():
    out = {"current": __version__, "latest": None, "available": False, "canApply": FROZEN and sys.platform == "win32", "error": None}
    if REPO.startswith("OWNER/"):
        out["error"] = "Updates are not set up yet (no GitHub repo configured)."
        return out
    try:
        rel = json.load(_get(f"https://api.github.com/repos/{REPO}/releases/latest"))
        out["latest"] = rel["tag_name"].lstrip("v")
        out["available"] = _ver(out["latest"]) > _ver(__version__)
        out["url"] = next((a["browser_download_url"] for a in rel.get("assets", []) if a["name"] == ASSET), None)
        if out["available"] and not out["url"]:
            out["error"] = f"Release {rel['tag_name']} has no {ASSET} file."
            out["available"] = False
    except Exception as ex:
        out["error"] = f"Could not check for updates: {ex}"
    return out


def apply(shutdown):
    info = check()
    if not info["available"]:
        return info
    if not info["canApply"]:
        info["error"] = "Automatic update only works in the packaged Windows app."
        return info
    new = EXE.with_suffix(".new")
    try:
        with _get(info["url"]) as r, open(new, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        old = EXE.with_suffix(".old")
        if old.exists():
            old.unlink()
        os.replace(EXE, old)     # a running exe can be renamed, not overwritten
        os.replace(new, EXE)
    except Exception as ex:
        info["error"] = f"Update failed: {ex}"
        return info
    # start the new version after this one has released the port
    subprocess.Popen(f'ping -n 4 127.0.0.1 >nul & start "" "{EXE}"', shell=True, creationflags=0x08000000)
    import threading
    threading.Thread(target=shutdown, daemon=True).start()
    info["restarting"] = True
    return info


def cleanup():
    if FROZEN:
        try:
            EXE.with_suffix(".old").unlink(missing_ok=True)
        except Exception:
            pass
