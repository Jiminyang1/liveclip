"""Archive a finished live session's full-length version to cloud storage with rclone (optional).

After `liveclip follow` finishes a session it writes deliver/直播跟播_<ID>/完整版/ (录屏.mp4 + 字幕.srt) and, when
config live.archive_remote is set (e.g. myremote:liveclip-archive/full), starts this in the background.
Files are copied one by one, then verified with `rclone check` (MD5); the result goes to 完整版/.drive.json, which
the dashboard shows. Local files are kept. Already-archived sessions are skipped, so re-running is safe.

  liveclip archive LIVE_20260101_2000_main          # one session
  liveclip archive LIVE_20260101_2000_main --wait   # wait until the full version exists
  liveclip archive --all                            # every session not archived yet
"""
import argparse
import json
import subprocess
import sys
import time
from datetime import datetime

from .config import C
from .platform import notify

FILES = ["录屏.mp4", "字幕.srt", "候选清单.txt"]


def log(msg):
    line = f"[{datetime.now():%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    C.LOGS.mkdir(parents=True, exist_ok=True)
    with open(C.LOGS / "live_follow.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def upload(vid, remote):
    out = C.DELIVER / f"直播跟播_{vid}"
    full = out / "完整版"
    mark = full / ".drive.json"
    if mark.exists() and json.loads(mark.read_text()).get("ok"):
        print(f"{vid} already archived: {json.loads(mark.read_text())['remote']}")
        return True
    files = [p for p in [full / "录屏.mp4", full / "字幕.srt", out / "候选清单.txt"] if p.exists()]
    if not (full / "录屏.mp4").exists():
        print(f"{vid} has no full version yet: {full}", file=sys.stderr)
        return False
    dest = f"{remote.rstrip('/')}/{vid}"
    size = sum(p.stat().st_size for p in files) / 1e9
    log(f"{vid} 完整版上传开始：{', '.join(p.name for p in files)}（{size:.1f}GB）→ {dest}")
    t0 = time.time()
    try:
        for p in files:   # copyto 一个个传：候选清单不在 完整版/ 目录里
            r = subprocess.run(["rclone", "copyto", str(p), f"{dest}/{p.name}", "--drive-chunk-size", "64M"],
                               capture_output=True, text=True, timeout=4 * 3600)
            if r.returncode:
                raise RuntimeError(f"{p.name} 上传失败：{r.stderr[-400:]}")
        for p in files:   # 逐个 MD5 核对
            r = subprocess.run(["rclone", "check", str(p.parent), dest, "--one-way", "--include", p.name],
                               capture_output=True, text=True, timeout=3600)
            if r.returncode:
                raise RuntimeError(f"{p.name} 核对不一致：{r.stderr[-400:]}")
    except Exception as e:  # noqa: BLE001
        log(f"{vid} 完整版上传失败：{e}")
        mark.write_text(json.dumps({"ok": False, "remote": dest, "error": str(e)[-300:],
                                    "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False))
        notify("完整版上传失败", f"{vid}：再跑 liveclip archive {vid}")
        return False
    mark.write_text(json.dumps({"ok": True, "remote": dest, "files": [p.name for p in files], "gb": round(size, 2),
                                "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False))
    log(f"{vid} 完整版已上传并核对一致（{size:.1f}GB，{(time.time() - t0) / 60:.0f} 分钟）→ {dest}")
    notify("完整版已归档", f"{vid}（{size:.1f}GB）")
    return True


def wait_ready(vid):
    """等 follow 把完整版做完：字幕.srt 是 full_version() 最后写的。"""
    srt = C.DELIVER / f"直播跟播_{vid}" / "完整版" / "字幕.srt"
    while not srt.exists():
        time.sleep(60)
    time.sleep(120)   # 写完后它还要删分段、写状态，等一会儿


def main(argv=None):
    ap = argparse.ArgumentParser(prog="liveclip archive", description="archive full-length live versions with rclone")
    ap.add_argument("vid", nargs="?")
    ap.add_argument("--wait", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--remote", default=C.get("live.archive_remote") or None)
    a = ap.parse_args(argv)
    if not a.remote:
        ap.error("set live.archive_remote in config.yaml or pass --remote")
    if a.all:
        vids = [d.parent.name.removeprefix("直播跟播_") for d in sorted(C.DELIVER.glob("直播跟播_*/完整版"))]
        sys.exit(0 if all([upload(v, a.remote) for v in vids]) else 1)
    if not a.vid:
        ap.error("give a session ID, or --all")
    if a.wait:
        wait_ready(a.vid)
    sys.exit(0 if upload(a.vid, a.remote) else 1)


if __name__ == "__main__":
    main()
