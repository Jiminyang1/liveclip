"""Face boxes + eye openness for cover-frame picking and QA, with backends:

1. macOS Vision via a tiny Swift helper (liveclip/assets/eyes_open.swift), compiled on first use with `swiftc`
   into ~/.cache/liveclip/eyes_open (or set LIVECLIP_EYES=/path/to/binary).
2. OpenCV Haar cascades (pip install 'liveclip[vision]'): face boxes; eye openness is approximated
   (0.3 when two eyes are found inside the face, 0.1 otherwise).
3. None: detect() returns None and callers skip face-based checks (covers take the middle frame).

detect(files) → list of {"file", "faces", "min_eye", "boxes": [[x, y, w, h] normalised, top-left origin]} or None.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / "assets" / "eyes_open.swift"
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "liveclip"
_BACKEND = None


def _eyes_bin():
    env = os.environ.get("LIVECLIP_EYES")
    if env and Path(env).exists():
        return Path(env)
    if sys.platform != "darwin":
        return None
    exe = CACHE / "eyes_open"
    if exe.exists() and exe.stat().st_mtime >= SRC.stat().st_mtime:
        return exe
    if not shutil.which("swiftc"):
        return None
    CACHE.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["swiftc", "-O", str(SRC), "-o", str(exe)], capture_output=True, text=True)
    return exe if r.returncode == 0 and exe.exists() else None


def backend():
    """'vision' | 'opencv' | None (cached)."""
    global _BACKEND
    if _BACKEND is None:
        if os.environ.get("LIVECLIP_VISION") == "none":
            _BACKEND = "none"
        elif _eyes_bin():
            _BACKEND = "vision"
        else:
            try:
                import cv2  # noqa: F401
                _BACKEND = "opencv"
            except ImportError:
                _BACKEND = "none"
    return None if _BACKEND == "none" else _BACKEND


def available():
    return backend() is not None


def _opencv(files):
    import cv2
    face = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    eye = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_eye.xml")
    out = []
    for f in files:
        img = cv2.imread(str(f))
        if img is None:
            out.append({"file": str(f), "faces": 0, "min_eye": 0, "boxes": []})
            continue
        h, w = img.shape[:2]
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        fs = face.detectMultiScale(g, 1.1, 5, minSize=(max(24, w // 30), max(24, w // 30)))
        boxes, eyes = [], []
        for x, y, fw, fh in fs:
            boxes.append([round(x / w, 4), round(y / h, 4), round(fw / w, 4), round(fh / h, 4)])
            es = eye.detectMultiScale(g[y:y + fh // 2 + fh // 6, x:x + fw], 1.1, 5)
            eyes.append(0.3 if len(es) >= 2 else 0.1)
        out.append({"file": str(f), "faces": len(boxes), "min_eye": min(eyes) if eyes else 0, "boxes": boxes})
    return out


def detect(files):
    files = [str(f) for f in files]
    if not files:
        return []
    b = backend()
    if b == "vision":
        out = subprocess.run([str(_eyes_bin()), *files], capture_output=True, text=True).stdout
        rows = []
        for line in out.splitlines():
            if line.startswith("{"):
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return rows
    if b == "opencv":
        return _opencv(files)
    return None
