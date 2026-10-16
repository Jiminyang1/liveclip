"""Post-recording hook for a recorder running on the *same* machine (alternative to live-follow):
recording file → remux to mp4 → `liveclip clip --live --auto-layout` → notification.

Point DouyinLiveRecorder's "run a custom script after recording" setting at deploy/local/on_recorded.sh
(it backgrounds this so the recorder goes straight back to watching the room), or run it by hand:
    liveclip after-record recordings/<platform>/<anchor>/xxx.ts
Output deliver/直播自动_<ID>/, log logs/after_record.log. Recordings shorter than 10 minutes are ignored.
"""
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from .config import C, py_mod
from .platform import notify, open_path

MIN_SECS = 600            # 不到 10 分钟的（断流重连的碎片、试播）不剪


def log(msg):
    print(f"[{datetime.now():%m-%d %H:%M:%S}] {msg}", flush=True)


def wait_stable(path, secs=30):
    """录制器结束后文件可能还在落盘，大小连续 secs 秒不变再动。"""
    last = -1
    while True:
        size = path.stat().st_size
        if size == last:
            return
        last = size
        time.sleep(secs)


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name",
                          "-of", "json", str(path)], capture_output=True, text=True).stdout
    d = json.loads(out)
    dur = float(d.get("format", {}).get("duration", 0))
    vcodec = next((s["codec_name"] for s in d.get("streams", []) if s["codec_type"] == "video"), None)
    return dur, vcodec


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        sys.exit("usage: liveclip after-record <recording file>")
    src = Path(argv[0]).resolve()
    C.ensure_dirs()
    log(f"录制完成：{src}")
    wait_stable(src)
    dur, vcodec = probe(src)
    if dur < MIN_SECS:
        log(f"只有 {dur / 60:.1f} 分钟，不剪")
        return
    anchors = {a["match"]: a.get("key", "live") for a in (C.get("live.anchors") or []) if a.get("match")}
    who = next((v for k, v in anchors.items() if k in str(src)), "live")
    started = datetime.fromtimestamp(src.stat().st_mtime - dur)
    vid = f"LIVE_{started:%Y%m%d_%H%M}_{who}"
    mp4 = C.VIDEOS / f"{vid}.mp4"
    if not mp4.exists():
        cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0", "-c", "copy"]
        if vcodec == "hevc":
            cmd += ["-tag:v", "hvc1"]
        subprocess.run(cmd + ["-movflags", "+faststart", str(mp4)], check=True, stdin=subprocess.DEVNULL)
    # mp4 是原样转封装、内容一样；时长对得上（差不到 2 秒）就删掉 .ts，省一半硬盘
    mp4_dur, _ = probe(mp4)
    if abs(mp4_dur - dur) < 2:
        src.unlink()
        log(f"已删 {src.suffix}（mp4 时长 {mp4_dur:.0f}s 与原文件一致）")
    else:
        log(f"mp4 时长 {mp4_dur:.0f}s 和原文件 {dur:.0f}s 对不上，保留原文件")
    log(f"{vid}：{dur / 60:.0f} 分钟，{vcodec}，开始自动切片")
    notify("开始自动切片", f"{vid}，{dur / 60:.0f} 分钟")

    out = C.DELIVER / f"直播自动_{vid}"
    title = (C.get("live.title") or "{name}直播（{date:%Y年%m月%d日}）").format(name=C.name, date=started, key=who, anchor="")
    r = subprocess.run(py_mod("auto", str(mp4), "--live", "--auto-layout", "--top", "10", "--title", title, "--out", str(out),
                              "--note", C.get("streamer.live_layout_note") or ""),
                       cwd=C.ROOT, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        notify("自动切片失败", f"{vid}，看 {out}/日志.txt")
        log(f"clip 失败，返回码 {r.returncode}")
        return
    n = len(list(out.glob("[0-9][0-9]_*")))
    notify("直播切片出好了", f"{vid}：{n} 条候选，待复核")
    log(f"完成：{n} 条 → {out}")
    if (out / "专辑.html").exists():
        open_path(out / "专辑.html")


if __name__ == "__main__":
    main()
