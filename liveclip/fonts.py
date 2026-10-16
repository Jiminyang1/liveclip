"""CJK font resolution for titles, captions and covers.

Order: config fonts.title / fonts.cover → <workspace>/assets/fonts/NotoSansSC*.ttf|otf → system fonts
(macOS Hiragino Sans GB / PingFang, Linux Noto Sans CJK) → `fc-match`.
Variable fonts (Noto Sans SC VF) get their weight set by name ("Bold" for titles, "Black" for covers).
"""
from __future__ import annotations

import functools
import shutil
import subprocess
from pathlib import Path

from PIL import ImageFont

from .config import C

# (path, ttc index for bold / SC face)
SYSTEM = [
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", 2),
    ("/System/Library/Fonts/PingFang.ttc", 0),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 2),
    ("/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc", 2),
    ("/usr/share/fonts/google-noto-cjk/NotoSansCJK-Bold.ttc", 2),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 2),
    ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", 0),
]


def _local():
    d = C.ASSETS / "fonts"
    for pat in ("NotoSansSC*.ttf", "NotoSansSC*.otf", "*.ttf", "*.otf", "*.ttc"):
        hit = sorted(d.glob(pat)) if d.exists() else []
        if hit:
            return str(hit[0]), 0
    return None


@functools.lru_cache(maxsize=None)
def resolve(kind="title"):
    cfg = C.get(f"fonts.{kind}") or (C.get("fonts.title") if kind == "cover" else None)
    if cfg:
        return str(Path(cfg).expanduser()), int(C.get("fonts.title_index") or 0)
    loc = _local()
    if loc:
        return loc
    for p, idx in SYSTEM:
        if Path(p).exists():
            return p, idx
    if shutil.which("fc-match"):
        out = subprocess.run(["fc-match", "-f", "%{file}", "sans:lang=zh-cn:weight=bold"], capture_output=True, text=True)
        if out.stdout.strip() and Path(out.stdout.strip()).exists():
            return out.stdout.strip(), 0
    raise SystemExit("No Chinese font found. Put NotoSansSC (SIL OFL) into <workspace>/assets/fonts/ "
                     "or set fonts.title in config.yaml — see README › Fonts")


@functools.lru_cache(maxsize=256)
def font(size, kind="title", weight="Bold"):
    path, idx = resolve(kind)
    f = ImageFont.truetype(path, size, index=idx if path.endswith(".ttc") else 0)
    try:
        f.set_variation_by_name(weight)
    except Exception:  # noqa: BLE001 - not a variable font / no such named instance
        pass
    return f
