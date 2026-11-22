"""Update button: checks GitHub Releases and swaps in the new .exe."""
import json
import os
import subprocess
import sys
import tempfile
import urllib.request

from version import __version__

GITHUB_USER = "adibuddin905-web"
GITHUB_REPO = "sheet-printer"
ASSET_NAME = "SheetPrinter.exe"
API = "https://api.github.com/repos/%s/%s/releases/latest" % (GITHUB_USER, GITHUB_REPO)
RELEASES_PAGE = "https://github.com/%s/%s/releases" % (GITHUB_USER, GITHUB_REPO)


def _ver(text):
    nums = []
    for p in text.strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        nums.append(int(digits) if digits else 0)
    return tuple(nums)


def check_latest():
    """Returns (latest_version_text, download_url or None, is_newer)."""
    req = urllib.request.Request(API, headers={"User-Agent": "SheetPrinter"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode("utf-8"))
    tag = data.get("tag_name", "")
    url = None
    for a in data.get("assets", []):
        if a.get("name") == ASSET_NAME:
            url = a.get("browser_download_url")
    return tag, url, _ver(tag) > _ver(__version__)


def download_and_replace(url):
    """Downloads the new exe and restarts the app with it."""
    if not getattr(sys, "frozen", False):
        raise RuntimeError("Auto-update only works in the installed .exe version.")
    exe = sys.executable
    tmp_dir = tempfile.mkdtemp(prefix="sheetprinter_")
    new_exe = os.path.join(tmp_dir, "SheetPrinter_new.exe")
    req = urllib.request.Request(url, headers={"User-Agent": "SheetPrinter"})
    with urllib.request.urlopen(req, timeout=120) as r, open(new_exe, "wb") as f:
        while True:
            chunk = r.read(1024 * 256)
            if not chunk:
                break
            f.write(chunk)
    bat = os.path.join(tmp_dir, "update.bat")
    with open(bat, "w") as f:
        f.write(
            "@echo off\r\n"
            "ping 127.0.0.1 -n 3 >nul\r\n"
            ":retry\r\n"
            'move /Y "%s" "%s" >nul 2>&1\r\n'
            "if errorlevel 1 (ping 127.0.0.1 -n 2 >nul & goto retry)\r\n"
            'start "" "%s"\r\n'
            % (new_exe, exe, exe)
        )
    subprocess.Popen(["cmd", "/c", bat], creationflags=0x08000000)  # no window
