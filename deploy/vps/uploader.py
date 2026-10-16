#!/usr/bin/env python3
"""Runs on the recording server every minute (systemd timer): moves finished 5-minute live segments to an rclone
remote, deleting the local copy once uploaded; when a session has ended, uploads an ENDED marker.

DouyinLiveRecorder segment names: <anchor>_<YYYY-MM-DD_HH-MM-SS>_<NNN>.ts under <recordings>/<platform>/<anchor>/.
Same time prefix = same session (a reconnect after the stream drops starts a new prefix = a new session).
Remote layout: <REMOTE>/<anchor>/<prefix>/<segment>.ts and <REMOTE>/<anchor>/<prefix>/ENDED.
`liveclip follow` on the editing machine watches this remote and clips while the stream is still live.
Standard library only. Configure with environment variables (see liveclip-uploader.service):
  LIVECLIP_REC_ROOT   base dir (default /opt/liveclip) — recordings/ and state live here
  LIVECLIP_REMOTE     rclone remote dir, e.g. myremote:liveclip-live (required)
"""
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("LIVECLIP_REC_ROOT", "/opt/liveclip"))
REC = ROOT / "recordings"
STATE = ROOT / "state.json"
REMOTE = os.environ.get("LIVECLIP_REMOTE", "")
QUIET = 90           # 文件超过 90 秒没再写入，才算这一段录完
SEG = re.compile(r"^(?P<prefix>.+_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})_(?P<n>\d{3})\.ts$")


def log(msg):
    print(f"[{time.strftime('%m-%d %H:%M:%S')}] {msg}", flush=True)


def ffmpeg_running(prefix):
    out = subprocess.run(["pgrep", "-af", "ffmpeg"], capture_output=True, text=True).stdout
    return prefix in out


def rclone(*args):
    return subprocess.run(["rclone", *args], capture_output=True, text=True)


def main():
    if not REMOTE:
        sys.exit("set LIVECLIP_REMOTE (an rclone remote dir such as myremote:liveclip-live)")
    lock = open(ROOT / "uploader.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return                      # 上一轮还在传
    state = json.loads(STATE.read_text()) if STATE.exists() else {"active": {}}
    now = time.time()
    for f in sorted(REC.rglob("*.ts")):
        m = SEG.match(f.name)
        if not m or now - f.stat().st_mtime < QUIET:
            continue
        anchor, prefix = f.parent.name, m["prefix"]
        if f.stat().st_size < 100_000:      # 开播瞬间的空壳文件
            f.unlink()
            continue
        dest = f"{REMOTE}/{anchor}/{prefix}/{f.name}"
        r = rclone("moveto", str(f), dest, "--drive-chunk-size", "64M")
        if r.returncode == 0:
            log(f"上传 {anchor}/{f.name}")
            state["active"][prefix] = anchor
        else:
            log(f"上传失败 {f.name}：{r.stderr[-300:]}")
    # 一场结束：本地没有这个前缀的文件，也没有 ffmpeg 在录它
    for prefix, anchor in list(state["active"].items()):
        if any(REC.rglob(f"{prefix}_*.ts")) or ffmpeg_running(prefix):
            continue
        r = rclone("touch", f"{REMOTE}/{anchor}/{prefix}/ENDED")
        if r.returncode == 0:
            log(f"一场结束 {anchor}/{prefix}")
            del state["active"][prefix]
    STATE.write_text(json.dumps(state, ensure_ascii=False))


if __name__ == "__main__":
    main()
