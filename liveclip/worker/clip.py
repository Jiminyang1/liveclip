"""Clip worker for dashboard uploads: drag a video onto the dashboard → it is queued → this worker runs
`liveclip clip` on it → the clips appear on the dashboard (and can be queued for publishing there).

The dashboard (liveclip.dashboard.server) stores uploads as videos/UP_<YYYYMMDD_HHMM>_<n>.<ext>, appends a job to
data/clip_queue.json and starts this worker if it isn't running. One job at a time (`nice -n 10`, so it doesn't fight a
running live-follow), output deliver/手动上传_<ID>/.

Defaults per orientation:
  vertical: --live --auto-layout (diarized ASR, per-clip single/split-screen detection) + streamer.live_layout_note
  horizontal: --layout talk_h
  tight edit (--tight --tight-separate) and burned subtitles on by default; delivery: 视频.mp4 = normal cut,
  视频_精剪.mp4 = tight cut; clip count when not given: ~1 per 10 minutes, 2–20.
.ts/.mkv/.flv/.webm are remuxed to .mp4 first (-c copy); when durations match the original goes to the trash.

Robustness: fcntl-locked queue + atomic writes; single instance (data/.clip_worker.pid);
stop flag data/.clip_worker.stop (finish the current job, exit when idle — use this to restart safely);
on start, a job left "running" by a crash is stopped and marked failed — retry continues from caches (ASR and picking
are cached, nothing is paid twice). Notifications on done/failed; log logs/clip_worker.log, per-job output
logs/clip_worker/<ID>.out.

  liveclip worker clip            run continuously, poll every 3 s (the dashboard starts it automatically)
  liveclip worker clip --once     process the queue and exit
"""
import argparse
import fcntl
import json
import math
import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from ..config import C, py_mod
from ..platform import notify as _notify, processes, trash  # noqa: F401  (trash used by the dashboard)

ROOT = C.ROOT
QUEUE = C.DATA / "clip_queue.json"
LOCK = C.DATA / ".clip_queue.lock"
PIDLOCK = C.DATA / ".clip_worker.pid"
STOPFLAG = C.DATA / ".clip_worker.stop"
LOG = C.LOGS / "clip_worker.log"
OUTS = C.LOGS / "clip_worker"
EXTS = {".mp4", ".mov", ".ts", ".mkv", ".flv", ".m4v", ".webm"}
REMUX = {".ts", ".mkv", ".flv", ".webm"}
# 同 follow.NOTE_BASE（竖屏直播回放给选段模型的说明）
NOTE_LIVE = C.get("streamer.live_layout_note") or ""
# 步骤顺序（liveclip.auto 实际执行顺序）。看 日志.txt 里最新一条「$ 命令」判断做到哪；只往前走（切脸返工回头剪不算倒退）
STEPS = ["remux", "asr", "review", "pick", "cut", "cover", "qa", "tight"]
STEP_OF = [("liveclip.tight", "tight"), ("验收_精剪", "tight"), ("--tight", "tight"), ("liveclip.asr", "asr"),
           ("liveclip.review.subs", "review"), ("liveclip.pick", "pick"), ("liveclip.hook", "pick"), ("liveclip.cut", "cut"),
           ("liveclip.cover", "cover"), ("liveclip.verify", "qa"), ("liveclip.qa", "qa")]


def log(msg):
    LOG.parent.mkdir(exist_ok=True)
    line = f"[{datetime.now():%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def notify(title, text):
    _notify(title, text)


@contextmanager
def locked():
    LOCK.parent.mkdir(exist_ok=True)
    with open(LOCK, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        q = json.loads(QUEUE.read_text()) if QUEUE.exists() else []
        yield q
        tmp = QUEUE.with_suffix(".tmp")
        tmp.write_text(json.dumps(q, ensure_ascii=False, indent=1) + "\n")
        tmp.replace(QUEUE)          # 原子替换


def update(job_id, **kw):
    with locked() as q:
        for j in q:
            if j["id"] == job_id:
                j.update(kw)
                return dict(j)
    return None


def get(job_id):
    with locked() as q:
        return next((dict(j) for j in q if j["id"] == job_id), None)


def probe(path):
    """ffprobe：时长、宽高、视频编码、有没有声音。不是视频返回 None。"""
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name,width,height",
                        "-of", "json", str(path)], capture_output=True, text=True, timeout=120)
    try:
        d = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return None
    vs = [s for s in d.get("streams", []) if s.get("codec_type") == "video" and s.get("width")]
    dur = float((d.get("format") or {}).get("duration") or 0)
    if not vs or dur <= 0:
        return None
    return {"dur": round(dur, 1), "w": vs[0]["width"], "h": vs[0]["height"], "vcodec": vs[0].get("codec_name"),
            "audio": any(s.get("codec_type") == "audio" for s in d.get("streams", []))}


def auto_top(dur):
    return max(2, min(20, math.ceil(dur / 600)))


def build_cmd(job):
    o = job.get("opts") or {}
    vertical = job["h"] > job["w"]
    top = o.get("top") or auto_top(job["dur"])
    cmd = ["nice", "-n", "10", *py_mod("auto", C.ws(job["file"]), "--title", job["title"],
                                       "--top", str(top), "--out", C.ws(job["out"]))]
    note = (o.get("note") or "").strip()
    if vertical:
        cmd += ["--live", "--auto-layout"]
        note = NOTE_LIVE + (note and "\n" + note)
    else:
        cmd += ["--layout", "talk_h"]
    cmd.append("--burn-subs" if o.get("burn", True) else "--no-burn-subs")
    if o.get("tight", True):
        cmd += ["--tight", "--tight-separate"]     # 和跟播一样：视频.mp4 正常版 + 视频_精剪.mp4
    cmd += ["--cover-dir", str(C.DELIVER / "_covers" / job["id"])]
    if note:
        cmd += ["--note", note]
    return cmd, top


def remux(job):
    """.ts/.mkv/.flv/.webm → 同名 .mp4（-c copy，同 after_record）；时长对得上原文件进废纸篓。失败就用原文件跑。"""
    src = C.ws(job["file"])
    mp4 = src.with_suffix(".mp4")
    cmd = ["nice", "-n", "10", "ffmpeg", "-v", "error", "-y", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0", "-c", "copy"]
    if job.get("vcodec") == "hevc":
        cmd += ["-tag:v", "hvc1"]
    r = subprocess.run(cmd + ["-movflags", "+faststart", str(mp4)], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    info = probe(mp4) if r.returncode == 0 and mp4.exists() else None
    if not info or abs(info["dur"] - job["dur"]) > 2:
        log(f"  {job['id']} 转 mp4 没成功，直接用 {src.suffix}：{(r.stderr or '')[-200:]}")
        mp4.unlink(missing_ok=True)       # 自己刚生成的半成品
        return job["file"]
    t = trash(src)
    log(f"  {job['id']} 转成 {mp4.name}，原文件进废纸篓 {t}")
    return C.rel(mp4)


def step_of(line):
    for key, step in STEP_OF:
        if key in line:
            return step
    return None


def tail_error(logf, offset, outf):
    """失败时给面板看的几行：日志.txt 这次新写的最后几行 + auto_clip 自己的输出最后几行。"""
    lines = []
    if logf.exists():
        with open(logf, encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            lines += [ln.rstrip() for ln in f.read().splitlines() if ln.strip()][-6:]
    if outf.exists():
        out = [ln.rstrip() for ln in outf.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
        lines += [ln for ln in out[-6:] if ln not in lines]
    root = str(ROOT) + "/"
    short = [(ln.replace(root, "")[:160] + "…") if len(ln.replace(root, "")) > 160 else ln.replace(root, "") for ln in lines]
    return "\n".join(short)[-1500:] or "clip 没有输出"


def process(job):
    jid = job["id"]
    out = C.ws(job["out"])
    out.mkdir(parents=True, exist_ok=True)
    job = update(jid, status="running", started_ts=time.time(), finished_ts=None, step=None, step_ts=time.time(),
                 error=None, pid=None, cancel_req=False) or job
    if Path(job["file"]).suffix.lower() in REMUX:
        update(jid, step="remux", step_ts=time.time())
        job = update(jid, file=remux(job)) or job
    cmd, top = build_cmd(job)
    log(f"开始 {jid}「{job['title']}」{job['w']}x{job['h']} {job['dur'] / 60:.0f} 分钟，出 {top} 条：{' '.join(map(str, cmd[3:]))}")
    logf = out / "日志.txt"
    offset = logf.stat().st_size if logf.exists() else 0
    OUTS.mkdir(parents=True, exist_ok=True)
    outf = OUTS / f"{jid}.out"
    with open(outf, "a", encoding="utf-8") as fo:
        fo.write(f"\n==== {datetime.now():%m-%d %H:%M:%S} {' '.join(cmd)}\n")
        fo.flush()
        p = subprocess.Popen([str(c) for c in cmd], cwd=ROOT, stdout=fo, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             start_new_session=True)
    update(jid, pid=p.pid, top_used=top, step="asr", step_ts=time.time())
    step, pos, cancelled = "asr", offset, False
    while p.poll() is None:
        time.sleep(2)
        if logf.exists():
            with open(logf, encoding="utf-8", errors="replace") as f:
                f.seek(pos)
                new = f.read()
                pos = f.tell()
            for ln in new.splitlines():
                s = step_of(ln) if "] $ " in ln else None
                if s and STEPS.index(s) > STEPS.index(step):
                    step = s
                    update(jid, step=step, step_ts=time.time())
        cur = get(jid)
        if cur and cur.get("cancel_req") and not cancelled:
            cancelled = True
            log(f"  {jid} 面板点了停止，结束剪辑进程（pid {p.pid}）")
            kill_group(p.pid, p)
    rc = p.returncode
    n = len([d for d in out.glob("[0-9][0-9]_*") if (d / "视频.mp4").exists()])
    if rc == 0 and not cancelled:
        update(jid, status="done", finished_ts=time.time(), n_clips=n, pid=None, step=None)
        log(f"完成 {jid}「{job['title']}」{n} 条，用时 {(time.time() - job['started_ts']) / 60:.1f} 分钟")
        notify("手动上传剪好了", f"{job['title']}：{n} 条，在面板里看")
    elif cancelled:
        update(jid, status="cancelled", finished_ts=time.time(), pid=None, cancel_req=False, error=None)
        log(f"已停止 {jid}")
    else:
        err = tail_error(logf, offset, outf)
        if rc < 0:
            err = f"剪辑进程被终止（信号 {-rc}）\n" + err
        update(jid, status="failed", finished_ts=time.time(), pid=None, error=err)
        log(f"失败 {jid}（返回码 {rc}）：{err.splitlines()[-1] if err else ''}")
        notify("手动上传剪辑失败", f"{job['title']}：看面板里的错误，点重试接着做")


def kill_group(pid, proc=None):
    """剪辑进程和它的子进程是一个进程组（start_new_session）：先 TERM，10 秒还在就 KILL。
    macOS 上组里只剩没回收的僵尸组长时 killpg 报 EPERM，当作已经没了。"""
    gone = (ProcessLookupError, PermissionError)
    try:
        os.killpg(pid, signal.SIGTERM)
    except gone:
        return
    for _ in range(20):
        time.sleep(0.5)
        if proc:
            proc.poll()          # 回收组长，免得它当僵尸
        try:
            os.killpg(pid, 0)
        except gone:
            return
    try:
        os.killpg(pid, signal.SIGKILL)
    except gone:
        pass


def recover():
    """上次中断留下的 running：剪辑进程还活着就停掉（没人盯它的进度了），标失败，等人点重试（有缓存，接着做）。"""
    with locked() as q:
        stale = [j for j in q if j["status"] == "running"]
    for j in stale:
        pid = j.get("pid")
        if pid:
            cmd = next((r[3] for r in processes() if r[0] == pid), "")
            if "liveclip.auto" in cmd:
                kill_group(pid)
        update(j["id"], status="failed", pid=None, finished_ts=time.time(), cancel_req=False,
               error="小工人中断（上次没做完）。点重试接着做，识别和选段有缓存，不重复花钱。")
        log(f"恢复：{j['id']} 上次没做完，标为失败")


def next_job():
    with locked() as q:
        for j in q:
            if j["status"] == "pending":
                return dict(j)
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(prog="liveclip worker clip")
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args(argv)
    C.ensure_dirs()
    PIDLOCK.parent.mkdir(exist_ok=True)
    pid_fh = open(PIDLOCK, "a+")
    try:
        fcntl.flock(pid_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)      # 同一时间只能有一个
    except BlockingIOError:
        print("已经有一个剪辑小工人在跑，退出", flush=True)
        return 0
    pid_fh.seek(0), pid_fh.truncate(), pid_fh.write(str(os.getpid())), pid_fh.flush()
    log(f"剪辑小工人启动（pid {os.getpid()}）")
    recover()
    while True:
        try:
            job = next_job()
            if job:
                process(job)
                continue
        except Exception as e:          # 单条出错不让整个小工人退出
            log(f"出错：{e}")
            try:
                if job:
                    update(job["id"], status="failed", pid=None, finished_ts=time.time(), error=f"小工人出错：{e}")
            except Exception:  # noqa: BLE001
                pass
            time.sleep(5)
        if STOPFLAG.exists():           # 想重启：touch 这个文件，手上这条做完、队列空了就退出
            STOPFLAG.unlink()
            log("收到停止标记，队列空了，退出")
            return 0
        if a.once:
            return 0
        time.sleep(3)


if __name__ == "__main__":
    sys.exit(main())
