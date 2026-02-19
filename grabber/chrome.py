"""Shared Chrome browser lifecycle helpers.

Provides functions for locating, launching, and managing Chrome instances
with remote debugging (CDP).  These are used by any provider that needs
a real browser session with the user's existing cookies/profile.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

#: Regex for characters that are unsafe in filenames on any major OS.
UNSAFE_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def elapsed(start: float) -> str:
    """Format elapsed time since *start* as a human-readable string."""
    secs = time.time() - start
    if secs < 60:
        return f"{secs:.1f}s"
    mins = int(secs // 60)
    remainder = secs % 60
    return f"{mins}m {remainder:.1f}s"


def find_chrome() -> str | None:
    """Locate the system Chrome binary."""
    system = platform.system()
    if system == "Darwin":
        path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if os.path.exists(path):
            return path
    elif system == "Linux":
        for name in ("google-chrome", "google-chrome-stable", "chromium-browser"):
            path = shutil.which(name)
            if path:
                return path
    elif system == "Windows":
        for base in (
            os.environ.get("PROGRAMFILES", ""),
            os.environ.get("PROGRAMFILES(X86)", ""),
            os.environ.get("LOCALAPPDATA", ""),
        ):
            path = os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")
            if os.path.exists(path):
                return path
    return None


def chrome_profile_dir() -> Path | None:
    """Return the path to the user's default Chrome profile directory."""
    system = platform.system()
    if system == "Darwin":
        p = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    elif system == "Linux":
        p = Path.home() / ".config" / "google-chrome"
    elif system == "Windows":
        local = os.environ.get("LOCALAPPDATA", "")
        p = Path(local) / "Google" / "Chrome" / "User Data" if local else None
    else:
        return None
    return p if p and p.exists() else None


def clone_profile(profile_dir: Path, tmp_dir: str) -> None:
    """Clone the user's Chrome profile into *tmp_dir*.

    Copies only the ``Default`` profile and ``Local State``, excluding
    large cache directories that aren't needed for session cookies.
    """
    src_default = profile_dir / "Default"
    dst_default = Path(tmp_dir) / "Default"
    if src_default.exists():
        shutil.copytree(
            src_default,
            dst_default,
            ignore=shutil.ignore_patterns(
                "Cache", "Code Cache", "GPUCache",
                "Service Worker", "Storage", "blob_storage",
                "File System", "IndexedDB", "Sessions",
            ),
        )
    else:
        dst_default.mkdir(parents=True)

    for name in ("Local State",):
        src = profile_dir / name
        if src.exists():
            shutil.copy2(src, Path(tmp_dir) / name)


def launch_chrome(
    chrome: str, tmp_dir: str, port: int,
) -> subprocess.Popen | None:
    """Launch Chrome with remote debugging.

    On macOS uses ``open -gjn`` to keep the window hidden.  Returns
    the ``Popen`` handle on Linux/Windows, or ``None`` on macOS
    (where Chrome is detached from our process tree).
    """
    if platform.system() == "Darwin":
        subprocess.Popen(
            [
                "open", "-gjn",
                "-a", "Google Chrome",
                "--args",
                f"--remote-debugging-port={port}",
                "--no-first-run",
                "--no-default-browser-check",
                f"--user-data-dir={tmp_dir}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return None

    return subprocess.Popen(
        [
            chrome,
            f"--remote-debugging-port={port}",
            "--no-first-run",
            "--no-default-browser-check",
            f"--user-data-dir={tmp_dir}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def kill_chrome(
    proc: subprocess.Popen | None, port: int,
) -> None:
    """Terminate a Chrome process launched by :func:`launch_chrome`."""
    if proc:
        proc.terminate()
        proc.wait(timeout=10)
    else:
        # macOS: ``open`` detached Chrome; find it by port.
        try:
            out = subprocess.check_output(
                ["lsof", "-ti", f":{port}"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
            for line in out.strip().splitlines():
                os.kill(int(line.strip()), signal.SIGTERM)
        except (subprocess.CalledProcessError, ValueError, OSError):
            pass


def minimize_window(context, page) -> None:
    """Minimize the Chrome window via CDP."""
    try:
        cdp = context.new_cdp_session(page)
        win = cdp.send("Browser.getWindowForTarget")
        cdp.send(
            "Browser.setWindowBounds",
            {
                "windowId": win["windowId"],
                "bounds": {"windowState": "minimized"},
            },
        )
    except Exception:
        pass  # Non-critical — window stays visible.
