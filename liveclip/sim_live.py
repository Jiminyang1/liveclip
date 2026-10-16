"""Simulate a live session for testing `liveclip follow`: split a recording into 5-minute segments named exactly
like the recorder's segment mode, upload them one by one to a test directory on an rclone remote, then ENDED.

Usage:
    liveclip sim-live videos/replay.mp4 --minutes 60 --every 45 --remote myremote:liveclip-live-sim
then in another terminal:  liveclip follow --remote myremote:liveclip-live-sim
(rclone also accepts a local path as "remote", e.g. --remote /tmp/livesim, to test without any cloud account.)
"""
import argparse
import subprocess
import tempfile
import time
from pathlib import Path


def main(argv=None):
    ap = argparse.ArgumentParser(prog="liveclip sim-live")
    ap.add_argument("video")
    ap.add_argument("--minutes", type=float, default=60)
    ap.add_argument("--start", type=float, default=0, help="从回放第几秒开始")
    ap.add_argument("--every", type=float, default=45, help="每隔几秒传一段（真实直播是 300）")
    ap.add_argument("--remote", required=True, help="rclone remote dir (or a local dir) used only for the simulation")
    ap.add_argument("--anchor", default="demo")
    ap.add_argument("--prefix-time", default=time.strftime("%Y-%m-%d_%H-%M-%S"))
    a = ap.parse_args(argv)
    prefix = f"{a.anchor}_{a.prefix_time}"
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(a.start), "-t", str(a.minutes * 60), "-i", a.video,
                        "-c:v", "copy", "-c:a", "copy", "-map", "0", "-f", "segment", "-segment_time", "300",
                        "-segment_format", "mpegts", "-reset_timestamps", "1", f"{d}/{prefix}_%03d.ts"], check=True)
        segs = sorted(Path(d).glob("*.ts"))
        print(f"{len(segs)} 段", flush=True)
        for p in segs:
            subprocess.run(["rclone", "copyto", str(p), f"{a.remote}/{a.anchor}/{prefix}/{p.name}"], check=True,
                           capture_output=True)
            print(f"[{time.strftime('%H:%M:%S')}] 传 {p.name}", flush=True)
            time.sleep(a.every)
        subprocess.run(["rclone", "touch", f"{a.remote}/{a.anchor}/{prefix}/ENDED"], check=True, capture_output=True)
        print(f"[{time.strftime('%H:%M:%S')}] 传 ENDED", flush=True)


if __name__ == "__main__":
    main()
