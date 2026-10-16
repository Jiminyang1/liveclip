"""OS integration with graceful fallbacks: notifications, opening files, trash, keeping the machine awake.

macOS: osascript / open / ~/.Trash / caffeinate. Linux: notify-send / xdg-open / gio trash / systemd-inhibit.
Anything missing is silently skipped (notifications) or falls back to a workspace-local trash folder.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from .config import C

MAC = sys.platform == "darwin"


def _backend():
    b = C.get("notify.backend", "auto")
    if b != "auto":
        return b
    if MAC and shutil.which("osascript"):
        return "macos"
    if shutil.which("notify-send"):
        return "linux"
    return "none"


def notify(title, text, sound=False):
    text = str(text).replace('"', "'").replace("\\", "")[:160]
    title = str(title).replace('"', "'")
    b = _backend()
    try:
        if b == "macos":
            snd = ' sound name "Glass"' if sound else ""
            subprocess.run(["osascript", "-e", f'display notification "{text}" with title "{title}"{snd}'],
                           capture_output=True, timeout=10)
        elif b == "linux":
            subprocess.run(["notify-send", title, text], capture_output=True, timeout=10)
    except Exception:  # noqa: BLE001
        pass


def open_path(p):
    """Open a file in the default app (album page after a run). No-op on headless machines or with notify.backend: none."""
    if _backend() == "none":
        return
    opener = "open" if MAC else ("xdg-open" if shutil.which("xdg-open") else None)
    if opener:
        try:
            subprocess.Popen([opener, str(p)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:  # noqa: BLE001
            pass


def trash(p):
    """Move to the OS trash instead of deleting. Returns the new path (or None if p did not exist)."""
    p = Path(p)
    if not p.exists():
        return None
    if MAC:
        dest_dir = Path.home() / ".Trash"
    elif shutil.which("gio") and subprocess.run(["gio", "trash", str(p)], capture_output=True).returncode == 0:
        return Path.home() / ".local/share/Trash/files" / p.name
    else:
        dest_dir = C.ROOT / ".trash"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / p.name
    if dest.exists():
        dest = dest.with_name(f"{p.stem} {datetime.now():%H%M%S}{p.suffix}")
    shutil.move(str(p), str(dest))
    return dest


def keep_awake(pid):
    """Keep the machine from sleeping while process `pid` runs (macOS caffeinate / Linux systemd-inhibit)."""
    try:
        if MAC and shutil.which("caffeinate"):
            return subprocess.Popen(["caffeinate", "-i", "-s", "-w", str(pid)], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True)
        if shutil.which("systemd-inhibit"):
            return subprocess.Popen(["systemd-inhibit", "--what=sleep:idle", "--why=liveclip", "tail", f"--pid={pid}",
                                     "-f", "/dev/null"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    start_new_session=True)
    except Exception:  # noqa: BLE001
        pass
    return None


def processes():
    """[(pid, ppid, etime, command)] for every process (works with BSD and procps `ps`)."""
    try:
        out = subprocess.run(["ps", "-eo", "pid=,ppid=,etime=,args="], capture_output=True, text=True, timeout=15).stdout
    except Exception:  # noqa: BLE001
        return []
    rows = []
    for ln in out.splitlines():
        p = ln.split(None, 3)
        if len(p) == 4 and p[0].isdigit() and p[1].isdigit():
            rows.append((int(p[0]), int(p[1]), p[2], p[3]))
    return rows


def running(fragment):
    return any(fragment in r[3] for r in processes())
