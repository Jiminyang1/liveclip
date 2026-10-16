"""Per-clip framing detection for vertical livestream recordings: single person full-screen vs. split-screen co-stream.

- split_band(): finds the video band of a split-screen co-stream (Douyin-style live rooms put a large flat-colour
  area under the band). Returns (top, bottom) as fractions of the frame height, None for single person, False when the
  frame could not be read.
- detect_layout(): 5 frames per clip, majority vote → {"layout": "duet_v", "v_crop": [...]} or full-frame single.
- face_cut_frames(): pre-flight before cutting — would this crop cut a face? (needs a face detector, see vision.py)

Single-person clips always use the full frame (full_v): zoomed-in crops kept cutting chins whenever the streamer
leaned in or sat lower.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from . import vision

SOLO = {"layout": "full_v"}
SOLO_PHONE = SOLO


def _runs(mask):
    """布尔序列里连续为真的段：[(起, 止), ...]，止不含。"""
    out, s = [], None
    for i, m in enumerate(list(mask) + [False]):
        if m and s is None:
            s = i
        if not m and s is not None:
            out.append((s, i)); s = None
    return out


def split_band(video, t, size):
    """分屏时视频带下面是一大块直播间纯色底（每行左右灰度标准差 <2.5），单人画面那里是衣服或房间（标准差 4 以上）。
    先在画面 0.45–0.8 找起点、高度 ≥12% 的纯色块，它上面最长的一段非纯色行就是视频带（高度要在 25%–75%）；
    PK 时视频带上面还贴着一条比分条，上沿取开头 8% 里最低的一条横贯全宽的分界线（60% 以上的列上下差 >10）。
    返回 (上沿, 下沿)，按原画高度比例；单人返回 None，抽帧失败返回 False。
    实测：直播间录屏 0.192/0.598（人工 0.190/0.595），手机录屏连线 0.242/0.647、PK 0.261/0.647；
    旧的「顶部灰度」判断会漏掉暗色衣服贴底的分屏，也不认手机录屏。"""
    import numpy as np
    sw, sh = size
    w, h = 360, round(sh * 360 / sw / 2) * 2
    out = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
                          "-vf", f"scale={w}:{h},format=gray", "-f", "rawvideo", "-"],
                         capture_output=True, stdin=subprocess.DEVNULL).stdout
    if len(out) != w * h:
        return False
    g = np.frombuffer(out, np.uint8).reshape(h, w).astype(float)
    flat = g.std(1) < 2.5
    floor = [r for r in _runs(flat) if 0.45 * h <= r[0] <= 0.8 * h and r[1] - r[0] >= 0.12 * h]
    if not floor:
        return None
    cand = [r for r in _runs(~flat) if r[0] >= 0.08 * h and r[1] <= floor[0][0]]
    if not cand:
        return None
    a, b = max(cand, key=lambda r: r[1] - r[0])
    if not 0.25 <= (b - a) / h <= 0.75:
        return None
    edge = (np.abs(g[2:] - g[:-2]) > 10).mean(1)      # 第 i 项是第 i 行和第 i+2 行之间
    tops = [i + 2 for i in range(a, min(a + int(0.08 * h), len(edge))) if edge[i] > 0.6]
    return (max(tops) if tops else a) / h, b / h


def detect_layout(video, c):
    """一段候选里均匀抽 5 帧找分屏视频带：多数是分屏 → duet_v（v_crop 取这几帧的中位数）；多数是单人 → 单人取景；
    混着的按多数，并在 layout_note 里提醒人看。"""
    from .cut import probe_size
    size = probe_size(video)
    ts = [c["start"] + (c["end"] - c["start"]) * k / 6 for k in range(1, 6)]
    bands = [b for b in (split_band(video, t, size) for t in ts) if b is not False]
    duets = [b for b in bands if b]
    if len(duets) * 2 > len(bands):
        top = sorted(b[0] for b in duets)[len(duets) // 2]
        bot = sorted(b[1] for b in duets)[len(duets) // 2]
        pick = {"layout": "duet_v", "v_crop": [round(float(top), 4), round(float(bot - top), 4)]}
    else:
        pick = dict(SOLO_PHONE if size[1] / size[0] > 1.9 else SOLO)
    if 0 < len(duets) < len(bands):
        pick["layout_note"] = f"画面中途切换（{len(bands)} 帧里 {len(duets)} 帧是分屏），按多数用 {pick['layout']}，要人看一下"
    bad = face_cut_frames(video, ts, pick, size)
    if pick["layout"] != "full_v" and bad >= 1:   # 剪之前就知道会切脸：直接退回整幅，不用等质检
        pick = {**SOLO, "layout_note": (pick.get("layout_note", "") + f" 预检：{pick['layout']} 取景 {bad}/5 帧会切脸，改整幅").strip()}
    return pick


def face_cut_frames(video, ts, pick, size):
    """剪之前的取景预检：在原片这几帧上找脸，按 pick 的裁切框算会不会切脸
    （脸框伸出裁切框上下超过脸高 8%，或裁过左右时脸贴边）。返回会切脸的帧数；没有人脸检测后端时返回 0。"""
    if not vision.available():
        return 0
    from .cut import make_layout
    lay = make_layout(video, {"live": True, **pick})
    cx, cy, cw, ch = lay["crop"]
    with tempfile.TemporaryDirectory() as tmp:
        files = []
        for k, t in enumerate(ts):
            f = Path(tmp) / f"p{k}.png"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
                            "-vf", "scale=540:-2", str(f)], stdin=subprocess.DEVNULL)
            if f.exists():
                files.append(f)
        rows = vision.detect(files) or []
    bad = 0
    for r in rows:
        for x, y, w, h in r.get("boxes", []):
            over = max(cy - y, (y + h) - (cy + ch)) / h if h else 0
            side = cw < 0.98 and (x < cx + 0.005 or x + w > cx + cw - 0.005)
            if over > 0.08 or side:
                bad += 1
                break
    return bad
