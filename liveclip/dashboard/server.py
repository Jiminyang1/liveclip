"""Local dashboard: recorder → remote storage → live-follow (ASR / review / pick / hook / cut / QA / covers) → clips → publish.

  liveclip dashboard [--port 8771]

For the operator only: it shows tool names, raw logs, costs. It listens on 127.0.0.1 only — do not expose it.
Videos / covers are served by random tokens, never by path.

Read-mostly. Data sources:
  recorder: `ssh <recorder.ssh_host>` reads the recorder/uploader logs and systemd state every 20 s (optional)
  live-follow: the `liveclip follow` process and its current child (which step is running), recordings/live/**/state.json,
    logs/live_follow.log
  clips: deliver/直播跟播_<ID>/, clips/<ID>/follow.json, 质检.json
  publishing: data/publish_log.json, data/publish_queue.json; known issues: rows with ⚠️ in data/publish_issues.md (optional)
Writes only for: uploads (drag a video in → videos/UP_*.* → data/clip_queue.json → clip worker → deliver/手动上传_<ID>/),
the publish queue (only when publishing is enabled in config), and the "today's event" note for live picking.
Deleting an upload moves the source video to the trash.
"""
import argparse
import hashlib
import secrets
import http.server
import json
import mimetypes
import os
import shutil
import re
import subprocess
import threading
import time
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from ..config import C, py_mod
from ..platform import processes

ROOT = C.ROOT
HERE = Path(__file__).resolve().parent
SEG = re.compile(r"^(?P<prefix>.+_(?P<ts>\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}))_(?P<n>\d{3})\.ts$")
LOGLINE = re.compile(r"^\[(\d\d-\d\d \d\d:\d\d:\d\d)\] (.*)$")
BJ = C.audience_tz
VPS_TZ = C.recorder_tz   # 录制机文件名用的是它的本地时间
SALT = secrets.token_hex(8)   # 每次启动换一次，编号猜不出路径
WINDOW_MIN = round(C.get("live.window_sec", 1200) / 60)   # 和 follow 的 WINDOW 一致
FOLLOW_RE = re.compile(r"liveclip[ .]follow\b")
WHO = {a["match"]: a.get("key", "live") for a in (C.get("live.anchors") or []) if a.get("match")}

# 跟播子进程 → 步骤（按优先级从具体到笼统）
STEP_OF = [
    ("liveclip.asr", "recognize"), ("liveclip.review", "review"), ("liveclip.pick", "pick"), ("liveclip.hook", "hook"),
    ("liveclip.tight", "tight"), ("liveclip.cut", "cut"), ("liveclip.qa", "qa"), ("liveclip.cover", "cover"),
    ("rclone copy", "download"), ("rclone copyto", "download"), ("ffmpeg", "ffmpeg"),
    ("claude", "claude"), ("rclone lsf", "poll"),
]
STEPS = [  # 流水线顺序，前端按这个画
    ("record", "录制"), ("upload", "上传远端"), ("download", "下载分段"), ("recognize", "百炼识别"),
    ("review", "字幕校对"), ("pick", "Claude 选段"), ("hook", "挑预告"), ("cut", "剪辑"), ("tight", "精剪"),
    ("qa", "质检"), ("cover", "封面文案"), ("deliver", "出片"),
]


INTERNAL_NAMES = dict(STEPS, poll="查远端等新分段", ffmpeg="拼接整场视频", claude="Claude 处理中")


def pipeline(live, cur, ses):
    steps, key = STEPS, (lambda k: k)
    active = set()
    if live:
        active.add(key("record"))
    if cur:
        active.add({"claude": "review", "ffmpeg": "cut", "poll": "upload"}.get(cur["step"], cur["step"]))
    done = set()
    if ses and ses["segments"]:
        done |= {key(k) for k in ("record", "upload", "download", "recognize")}
    if ses and ses["rounds"]:
        done |= {key(k) for k in ("review", "pick", "hook", "cut", "tight", "qa", "cover", "deliver")}
    out = []
    for k, name in steps:
        t = cur["elapsed"] if cur and key(cur["step"]) == k else (live["elapsed"] if k == key("record") and live else "")
        out.append({"key": k, "name": name, "state": "active" if k in active else "done" if k in done else "", "t": t})
    return out


def sh(cmd, timeout=15):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


# ---------- 录制机（后台线程，ssh 慢，不阻塞请求；config recorder.ssh_host 留空就不查） ----------
RC = C.get("recorder") or {}
VPS = {"ok": None, "checked": None, "error": "还没查" if RC.get("ssh_host") else "没配置录制机（recorder.ssh_host）"}


def vps_loop():
    import shlex
    svcs = " ".join(shlex.quote(x) for x in (RC.get("services") or [])[:2])
    script = (f"systemctl is-active {svcs}; echo @@; "
              f"tail -c 3000 {shlex.quote(RC['recorder_log'])}; echo @@; tail -n 8 {shlex.quote(RC['uploader_log'])}; echo @@; "
              f"df -h {shlex.quote(RC.get('disk_path') or '/')} | tail -1; echo @@; date +%z")
    while True:
        try:
            r = subprocess.run(["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes", RC["ssh_host"], script],
                               capture_output=True, text=True, errors="replace", timeout=25)   # 录制日志偶尔有半个汉字（截断），别整块读挂
            parts = r.stdout.split("@@\n")
            if len(parts) < 5:
                raise RuntimeError((r.stderr or r.stdout)[-200:] or "ssh 没返回")
            svc = parts[0].split()
            rec = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", parts[1])
            # 录制器每几秒刷一屏，只看最后一屏（「共监测」开头）；注意「正在录制中」也含「正在录制」
            block = rec.split("共监测")[-1]
            live = [{"name": m.group(1), "elapsed": m.group(2)}
                    for m in re.finditer(r"序号\d+ (.+?)\[[^\]]*\] 正在录制中 (\d+:\d\d:\d\d)", block)]
            watching = re.findall(r"共监测(\d+)个直播", rec)
            # 录制机日志是它的本地时间；换成本机本地时间显示
            z = parts[4].strip()
            vps_off = (1 if z[:1] != "-" else -1) * (int(z[1:3]) * 3600 + int(z[3:5]) * 60) if len(z) >= 5 else 0
            shift = (datetime.now().astimezone().utcoffset().total_seconds() - vps_off)
            ups = []
            for ln in parts[2].strip().splitlines():
                m = LOGLINE.match(ln)
                if m:
                    t = datetime.strptime(f"{datetime.now().year}-{m.group(1)}", "%Y-%m-%d %H:%M:%S") + timedelta(seconds=shift)
                    ups.append({"t": t.strftime("%m-%d %H:%M:%S"), "msg": m.group(2).replace("上传 ", "")})
            df = parts[3].split()
            VPS.update(ok=all(s == "active" for s in svc[:2]), recorder=svc[0] if svc else "?",
                       uploader=svc[1] if len(svc) > 1 else "?", live=live,
                       watching=int(watching[-1]) if watching else None, uploads=ups[-6:],
                       disk=f"{df[2]}/{df[1]}" if len(df) > 3 else "", error=None,
                       checked=datetime.now().strftime("%H:%M:%S"))
        except Exception as e:
            VPS.update(ok=False, error=str(e)[-200:], checked=datetime.now().strftime("%H:%M:%S"))
        time.sleep(20)


# ---------- Mac 跟播 ----------
def proc_tree():
    """返回 follow 主进程信息和它当前的所有子孙进程命令行。"""
    rows = processes()
    main = next((r for r in rows if FOLLOW_RE.search(r[3]) and "grep" not in r[3]), None)
    if not main:
        return None, [], False
    kids, frontier = [], {main[0]}
    while frontier:
        nxt = {r[0] for r in rows if r[1] in frontier}
        kids += [r for r in rows if r[1] in frontier]
        frontier = nxt
    caf = any(("caffeinate" in r[3] or "systemd-inhibit" in r[3]) and str(main[0]) in r[3] for r in rows)
    return main, kids, caf


def current_step(kids):
    for key, step in STEP_OF:
        for k in kids:
            if key in k[3]:
                return {"step": step, "elapsed": k[2], "cmd": k[3][:160]}
    return None


def read_log(n=2000):
    p = C.LOGS / "live_follow.log"
    if not p.exists():
        return []
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    out = []
    for ln in lines:
        m = LOGLINE.match(ln)
        if m:
            out.append({"t": m.group(1), "msg": m.group(2)})
    return out


def kind_of(msg):
    q = re.search(r"质检：(\d+)/(\d+)", msg)
    if q:
        return "good" if q[1] == q[2] else "warn"
    if "出错" in msg or re.search(r"失败(?! ?0\b)", msg) or "✗" in msg:
        return "bad"
    if "出片" in msg or "整场结束" in msg or "完整版" in msg:
        return "good"
    if "留给下一轮" in msg or "跳过" in msg or "待确认" in msg:
        return "warn"
    return "info"


def sessions(log):
    out = []
    for st in sorted((C.RECORDINGS / "live").glob("*/*/state.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:4]:
        s = json.loads(st.read_text())
        prefix = st.parent.name
        m = re.search(r"_(\d{4})-(\d\d)-(\d\d)_(\d\d)-(\d\d)-\d\d$", prefix)
        who = next((v for k, v in WHO.items() if k in st.parent.parent.name), "live")
        vid = f"LIVE_{m.group(1)}{m.group(2)}{m.group(3)}_{m.group(4)}{m.group(5)}_{who}" if m else prefix
        segs = s.get("segments", [])
        total = sum(x["dur"] for x in segs)
        ev = [dict(e, kind=kind_of(e["msg"]), msg=e["msg"].replace(vid + " ", "", 1)) for e in log if vid in e["msg"]]
        out.append({
            "id": vid, "anchor": st.parent.parent.name, "started": m and f"{m.group(2)}-{m.group(3)} {m.group(4)}:{m.group(5)}",
            "started_bj": started_bj(st.parent),
            "segments": [{"n": i, "dur": round(x["dur"]), "asr": x.get("asr"), "fallback": x.get("asr") not in (None, "百炼")}
                         for i, x in enumerate(segs)],
            "dir_hint": f"deliver/直播跟播_{vid}/",
            "total_min": round(total / 60, 1), "rounds": s.get("rounds", 0),
            "picked_until_min": round(s.get("picked_until", 0) / 60, 1),
            "next_round_min": round(s.get("picked_until", 0) / 60 + WINDOW_MIN, 1),
            "ended": s.get("ended"), "done": s.get("done"),
            "deferred": [d["title"].replace("\n", " ") for d in s.get("deferred", [])],
            "clips": clips_of(vid), "events": ev[-40:], "full": full_of(vid),
            "decisions": decisions(vid, ev, s),
            "mtime": datetime.fromtimestamp(st.stat().st_mtime).strftime("%m-%d %H:%M"),
        })
    return out


NORM = lambda t: re.sub(r"\s+", "", t or "")
DEFER_WHY = "结尾落在最近 5 分钟里，话可能还没说完，留到下一轮再看"


def hook_info(c):
    hp = c.get("hook_pass") or {}
    cands = sorted(c.get("hook_candidates") or [], key=lambda h: -h.get("score", 0))
    if not cands and c.get("hook_start") is None:
        return None
    return {"type": hp.get("type"), "why": hp.get("why"),
            "cands": [{"text": h.get("text"), "score": h.get("score"), "reason": h.get("reason")} for h in cands[:3]]}


def item(c, decision, reason, n=None):
    return {"title": (c.get("title") or "").replace("\n", " / "), "score": c.get("score"), "category": c.get("category"),
            "start_min": round(c["start"] / 60, 1) if c.get("start") is not None else None,
            "end_min": round(c["end"] / 60, 1) if c.get("end") is not None else None,
            "dur": round(c["end"] - c["start"]) if c.get("end") else None, "risky": c.get("risky"),
            "why": c.get("why"), "context_check": c.get("context_check"), "tags": c.get("tags") or [],
            "decision": decision, "reason": reason, "n": n, "hook": hook_info(c) if decision == "picked" else None}


def decisions(vid, ev, state):
    """每一轮：模型挑出哪些、最后选中/留到下一轮/重叠跳过/规则筛掉，各自的理由；以及现在正在剪什么。"""
    plan_p = C.CLIPS / vid / "follow.json"
    plan = json.loads(plan_p.read_text())["clips"] if plan_p.exists() else []
    by_title = {NORM(c["title"]): (i + 1, c) for i, c in enumerate(plan)}
    logd = {}
    for e in ev:
        m = re.match(r"^第(\d+)轮 (留给下一轮|和已剪片段重叠，跳过|收回上一轮留下的).*?：(.+)$", e["msg"])
        if m:
            logd.setdefault(int(m[1]), {}).setdefault(m[2], set()).add(NORM(m[3]))
    delivered = {int(p.name[:2]) for p in (C.DELIVER / f"直播跟播_{vid}").glob("[0-9][0-9]_*")}
    rounds = []
    for r in range(1, state.get("rounds", 0) + 1):
        f = C.CLIPS / vid / f"follow_r{r}.json"
        cand = json.loads(f.read_text()) if f.exists() else {}
        lg = logd.get(r, {})
        items = []
        for c in cand.get("clips", []):
            t = NORM(c["title"])
            n, pc = by_title.get(t, (None, None))
            if t in lg.get("留给下一轮", set()):
                later = pc and pc.get("round") and pc["round"] > r
                items.append(item(c, "deferred", DEFER_WHY + (f"（第 {pc['round']} 轮收回，出片 {n:02d}）" if later else "")))
            elif t in lg.get("和已剪片段重叠，跳过", set()):
                items.append(item(c, "overlap", "和已经剪好的片段时间重叠"))
            elif pc and pc.get("round") == r:
                items.append(item(pc, "picked", f"选中，第 {r} 轮出片 {n:02d}", n))
            else:
                items.append(item(c, "unknown", "没有采用"))
        for t in lg.get("收回上一轮留下的", set()):
            n, pc = by_title.get(t, (None, None))
            if pc:
                items.append(item(pc, "picked", f"上一轮留下的，这轮收回，出片 {n:02d}", n))
        for c in cand.get("dropped", []):
            items.append(item(c, "dropped", c.get("drop_reason") or "规则筛掉"))
        order = {"picked": 0, "deferred": 1, "overlap": 2, "dropped": 3, "unknown": 4}
        items.sort(key=lambda x: (order[x["decision"]], -(x["score"] or 0)))
        rounds.append({"r": r, "items": items,
                       "count": {k: sum(1 for x in items if x["decision"] == k) for k in order}})
    # 正在剪：最新一轮选中、还没出片的
    last = max((c.get("round") or 0 for c in plan), default=0)
    cutting = [{"n": i + 1, "title": c["title"].replace("\n", " / ")} for i, c in enumerate(plan)
               if c.get("round") == last and (i + 1) not in delivered]
    return {"rounds": rounds[::-1], "cutting": cutting, "pending_deferred": [d["title"].replace("\n", " / ") for d in state.get("deferred", [])]}


def full_of(vid):
    """完整版（下播后 live_follow 生成）：录屏.mp4 + 字幕.srt。"""
    d = C.DELIVER / f"直播跟播_{vid}" / "完整版"
    v, srt = d / "录屏.mp4", d / "字幕.srt"
    if not v.exists():
        return {"ready": False}
    out = {"ready": True, "video": file_url(C.rel(v)), "size_gb": round(v.stat().st_size / 1e9, 2)}
    if srt.exists():
        out["srt"] = file_url(C.rel(srt))
    mark = d / ".drive.json"   # liveclip.archive 写的
    if not C.get("live.archive_remote") and not mark.exists():
        out["cloud"] = "off"       # 没配置归档：不显示云盘状态
    elif mark.exists():
        m = json.loads(mark.read_text(encoding="utf-8"))
        out["cloud"] = "ok" if m.get("ok") else "fail"
    return out


def started_bj(rec_dir):
    """开播时间换成观众时区。分段目录名里的时间是录制机的本地时间（config timezones.recorder）。"""
    m = re.search(r"_(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})$", rec_dir.name)
    if not m:
        return None
    t = datetime.strptime(m.group(1), "%Y-%m-%d_%H-%M-%S").replace(tzinfo=VPS_TZ)
    return t.astimezone(BJ).strftime("%m-%d %H:%M")


QA_TEXT = {"切脸": "人脸被画面边缘切掉", "字幕挡嘴": "字幕挡住嘴", "标题挡脸": "标题挡住脸", "没脸": "画面里经常找不到人脸",
           "切在句中": "开头或结尾切在半句话", "音频时间戳": "音频时间戳不连续，上传后可能声画错开",
           "音画同步": "声音和画面没对齐", "开头有脸": "开头一秒多没有人脸"}


def qa_reasons(r):
    """质检.json 一条 → [(级别, 人话原因)]，✗ 在前。"""
    out = []
    for k, v in r.items():
        if not isinstance(v, dict) or v.get("result") not in ("✗", "⚠"):
            continue
        text = QA_TEXT.get(k, k)
        if k == "音画同步" and v.get("detail"):
            m = re.match(r"声音比画面(早|晚) (\d+)ms", v["detail"])
            text = f"声音比画面{m[1]} {m[2]} 毫秒" if m else text
        elif k == "切在句中" and v.get("detail"):
            text = "；".join(v["detail"][:2])
        elif v.get("frames"):
            text += f"（抽查 {len(v['frames'])} 帧有问题）"
        out.append((v["result"], text))
    return sorted(out, key=lambda x: x[0] != "✗")


DUR = {}   # 视频时长缓存：(路径, mtime) → 秒；88 条每 5 秒跑 ffprobe 太浪费


def vdur(p):
    try:
        k = (str(p), p.stat().st_mtime)
    except FileNotFoundError:
        return None
    if k not in DUR:
        out = sh(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)], timeout=30)
        try:
            DUR[k] = round(float(out.strip()), 1)
        except ValueError:
            return None
    return DUR[k]


def clips_of(vid, deliver=None, plan_name="follow"):
    """出片卡片。跟播：deliver/直播跟播_<ID>/ + clips/<ID>/follow.json；
    手动上传：deliver/手动上传_<ID>/ + clips/<ID>/auto.json（liveclip clip 的计划名），质检在 auto/、auto_tight/。
    两边交付约定一样：视频.mp4 正常版，视频_精剪.mp4 精剪版（有才给 video_tight / dur_tight，面板播放器能切换）。"""
    d = C.DELIVER / (deliver or f"直播跟播_{vid}")
    if not d.exists():
        return []
    plan_p = C.CLIPS / vid / f"{plan_name}.json"
    plan = json.loads(plan_p.read_text())["clips"] if plan_p.exists() else []
    qa = {}
    for name in (plan_name, plan_name + "_tight"):
        q = C.CLIPS / vid / name / "质检.json"
        if q.exists():
            for r in json.loads(q.read_text()):
                rs = qa_reasons(r)
                qa.setdefault(r["clip"], {})["tight" if name.endswith("_tight") else "plain"] = {"ok": r.get("结论") == "✓", "level": r.get("结论"),
                                                      "bad": [t for _, t in rs], "levels": [lv for lv, _ in rs]}
    pub = publish_by_dir()
    out = []
    for cd in sorted(p for p in d.iterdir() if p.is_dir() and re.match(r"\d\d_", p.name)):
        n = int(cd.name[:2])
        c = plan[n - 1] if 0 < n <= len(plan) else {}
        rel = Path(C.rel(cd))
        tv = cd / "视频_精剪.mp4"
        out.append({
            "n": n, "title": (c.get("title") or cd.name[3:]).replace("\n", " / "), "dir": str(rel), "key": clip_key(rel),
            "dur": round(c["end"] - c["start"]) if c.get("end") else None, "score": c.get("score"),
            "start_min": round(c["start"] / 60, 2) if c.get("start") is not None else None,
            "end_min": round(c["end"] / 60, 2) if c.get("end") is not None else None,
            "risky": c.get("risky"), "layout": c.get("layout"), "round": c.get("round"), "tags": c.get("tags", []),
            "cover": file_url(rel / "封面.jpg") if (cd / "封面.jpg").exists() else None,
            "video": file_url(rel / "视频.mp4") if (cd / "视频.mp4").exists() else None,
            "tight": tv.exists(), "qa_all": qa.get(n, {}), "published": pub.get(str(rel), {}),
            "check": qa.get(n, {}).get("plain"),
            "video_tight": file_url(rel / "视频_精剪.mp4") if tv.exists() else None,
            "dur_v": vdur(cd / "视频.mp4"), "dur_tight": vdur(tv) if tv.exists() else None,
            "chips": [],
            "mtime": datetime.fromtimestamp(cd.stat().st_mtime).strftime("%H:%M"),
        })
    return out


FILES = {}   # 随机编号 → 路径（网页上只出现编号，不出现目录名）


def file_url(rel):
    tok = hashlib.sha1((SALT + str(rel)).encode()).hexdigest()[:16]
    FILES[tok] = str(rel)
    return f"/file?id={tok}"


CLIPS = {}   # 片子随机编号 → 交付目录（发布按钮用编号，不暴露路径）


def clip_key(rel):
    k = hashlib.sha1((SALT + "clip" + str(rel)).encode()).hexdigest()[:12]
    CLIPS[k] = str(rel)
    return k


def publish_by_dir():
    p = C.DATA / "publish_log.json"
    out = {}
    if p.exists():
        for r in json.loads(p.read_text()):
            out.setdefault(r["dir"], {})[r["platform"]] = r["time"][5:16]
    return out


def publish_recent():
    p = C.DATA / "publish_log.json"
    rows = json.loads(p.read_text()) if p.exists() else []
    return [dict(r, time=r["time"][5:16]) for r in rows[-12:]][::-1]


def costs(cur_label, n_clips):
    """花费模块：Claude（claude 命令行）记的是按 API 价折算的参考值（订阅制不实扣）；百炼按 token 实扣。"""
    from .. import cost as costlog
    rows = costlog.load()
    now = datetime.now()
    def win(pred):
        by, usd, cny = {}, 0.0, 0.0
        for r in rows:
            if not pred(r):
                continue
            a = by.setdefault(r["tool"], {"n": 0, "usd": 0.0, "cny": 0.0, "kind": r["kind"]})
            a["n"] += 1
            a["usd"] += r.get("usd") or 0
            a["cny"] += r.get("cny") or 0
            usd += r.get("usd") or 0
            cny += r.get("cny") or 0
        tools = sorted(({"tool": k, **{kk: round(vv, 4) if isinstance(vv, float) else vv for kk, vv in v.items()}}
                        for k, v in by.items()), key=lambda x: -(x["usd"] + x["cny"] / 7))
        return {"usd": round(usd, 2), "cny": round(cny, 3), "tools": tools}
    ts = lambda r: datetime.strptime(r["t"][:19], "%Y-%m-%d %H:%M:%S")
    today = now.strftime("%Y-%m-%d")
    out = {
        "session": dict(win(lambda r: r.get("label") == cur_label), label=cur_label),
        "today": win(lambda r: r["t"].startswith(today)),
        "week": win(lambda r: (now - ts(r)).days < 7),
        "all": win(lambda r: True),
        "recent": [{"t": r["t"][5:16], "tool": r["tool"], "usd": r.get("usd"), "cny": r.get("cny"), "label": r.get("label")}
                   for r in rows[-12:]][::-1],
    }
    out["per_clip"] = round(out["session"]["usd"] / n_clips, 2) if n_clips else None
    out["meta"] = "$ 折算 · ¥ 实扣"
    out["note"] = ("用 claude 命令行（订阅）时，$ 是按 API 单价折算的参考值，用来看哪一步贵、优化有没有用；"
                   "百炼语音识别按 token 实际扣费。")
    return out


def queue():
    p = C.DATA / "publish_queue.json"
    rows = json.loads(p.read_text()) if p.exists() else []
    return [dict(q, key=clip_key(q["dir"])) for q in rows[-15:][::-1]]


def problems():
    p = C.DATA / "publish_issues.md"
    if not p.exists():
        return []
    out = []
    for ln in p.read_text().splitlines():
        if ln.startswith("|") and "⚠️" in ln:
            cells = [c.strip() for c in ln.strip("|").split("|")]
            if len(cells) >= 5:
                out.append({"what": cells[1], "fix": cells[3], "state": cells[4]})
    return out


LIVE_NOTE = C.DATA / (C.get("live.event_note_file") or "live_note.txt")   # 当天的事件/联动，跟播每轮选段现读（follow.event_note）


def status():
    main, kids, caf = proc_tree()
    log = read_log()
    last = log[-1]["t"] if log else None
    cur = current_step(kids) if main else None
    ses = sessions(log)
    if ses:   # 选段决策顶上那句「正在…」
        dd, step = ses[0]["decisions"], cur and cur["step"]
        if step in ("review", "pick") or (step == "claude" and not dd["cutting"]):
            dd["now"] = {"kind": "picking", "round": ses[0]["rounds"] + 1}
        elif dd["cutting"] and step:
            dd["now"] = {"kind": "cutting", "round": max((c.get("round") or 0) for c in ses[0]["clips"]) if ses[0]["clips"] else 1}
        else:
            dd["now"] = {"kind": "done" if ses[0]["done"] else "waiting", "next_min": ses[0]["next_round_min"]}
    live = (VPS.get("live") or [None])[0]
    st = {
        "now": datetime.now().strftime("%m-%d %H:%M:%S"),
        "pipeline": pipeline(live, cur, ses[0] if ses else None),
        "step_name": INTERNAL_NAMES.get(cur["step"], cur["step"]) if cur else None,
        "editor": {"running": bool(main), "pid": main and main[0], "uptime": main and main[2], "caffeinate": caf,
                   "current": cur, "last_log": last, "warn": bool(main) and not caf,
                   "note": "防睡眠开" if caf else "⚠ 没开防睡眠"},
        "rec": dict(VPS, sync_ok=VPS.get("uploader") == "active", note=f"磁盘 {VPS.get('disk', '?')}"),
        "sessions": ses, "publish": publish_recent(), "problems": problems(), "queue": queue(),
        "live_note": LIVE_NOTE.read_text(encoding="utf-8").strip() if LIVE_NOTE.exists() else "",
        "publish_enabled": {p: C.publish_enabled(p) for p in ("douyin", "bili")},
        "tz": {"audience": C.get("timezones.audience"), "label": C.audience_tz_label},
        "recorder_configured": bool(RC.get("ssh_host")),
    }
    try:
        st["uploads"] = uploads()
    except Exception as e:  # noqa: BLE001
        st["uploads"] = {"error": str(e), "jobs": []}
    try:
        st["costs"] = costs(ses[0]["id"] if ses else None, len(ses[0]["clips"]) if ses else 0)
    except Exception as e:  # noqa: BLE001
        st["costs"] = {"error": str(e)}
    return st


def preview(rel):
    from ..publish import common as P
    m = P.info(C.ws(rel))
    return {"douyin": m["douyin"], "bili": m["bili"], "notes": m["notes"], "published": m["published"]}


def enqueue(body):
    rel = CLIPS.get(body.get("key", ""), "")
    d = C.ws(rel).resolve()
    if not str(d).startswith(str(C.DELIVER.resolve()) + "/") or not (d / "视频.mp4").exists():
        raise ValueError("找不到这条片子")
    plats = [x for x in body.get("platforms", []) if x in ("douyin", "bili")]
    if not plats:
        raise ValueError("至少选一个平台")
    off = [x for x in plats if not C.publish_enabled(x)]
    if off:
        raise ValueError("发布没打开：config 里 publish.enabled 和 publish.<平台>.enabled 都要是 true（只发布你有权发布的内容）")
    at = (body.get("at") or "").strip() or None
    if at:
        t = datetime.strptime(at, "%Y-%m-%d %H:%M").replace(tzinfo=BJ)
        mins = (t - datetime.now(BJ)).total_seconds() / 60
        if "bili" in plats and mins < 245:
            raise ValueError("B站定时要在 4 小时以后")
        if mins < 10:
            raise ValueError("定时时间太近")
    rel = C.rel(d)
    done = publish_by_dir().get(rel, {})
    dup = [x for x in plats if x in done]
    if dup:
        raise ValueError(f"已经发过：{'、'.join({'douyin': '抖音', 'bili': 'B站'}[x] for x in dup)}")
    from ..worker import publish as W
    with W.locked() as q:
        if any(j["dir"] == rel and j["status"] in ("pending", "running") and set(j["platforms"]) & set(plats) for j in q):
            raise ValueError("队列里已经有这条")
        job = {"id": datetime.now().strftime("%m%d%H%M%S"), "dir": rel, "title": preview(rel)["douyin"]["title"],
               "platforms": plats, "at": at, "status": "pending", "results": {},
               "created": datetime.now().strftime("%m-%d %H:%M:%S")}
        q.append(job)
    ensure_worker()
    return job


def queue_action(body, action):
    """重试：失败的条目，只把没成功的平台重新排队（已发的保留）。取消：只能取消还在排队的。"""
    from ..worker import publish as W
    with W.locked() as q:
        j = next((x for x in q if x["id"] == body.get("id")), None)
        if not j:
            raise ValueError("队列里找不到这条")
        if action == "retry":
            if j["status"] != "failed":
                raise ValueError("只有失败的才能重试")
            j["results"] = {k: v for k, v in (j.get("results") or {}).items() if k != "_" and v.get("state") == "done"}
            j["status"], j["retried"] = "pending", j.get("retried", 0) + 1
        else:
            if j["status"] != "pending":
                raise ValueError("只能取消还在排队的（已经开始的不能中途停）")
            j["status"] = "cancelled"
    if action == "retry":
        ensure_worker()
    return j


def _spawn(module, logname):
    C.LOGS.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(py_mod(module), cwd=ROOT, stdout=open(C.LOGS / logname, "a"), stderr=subprocess.STDOUT,
                     stdin=subprocess.DEVNULL, start_new_session=True)


def ensure_worker():
    if not any("liveclip.worker.publish" in r[3] for r in processes()):
        _spawn("worker.publish", "publish_worker.out")


# ---------- 手动上传剪辑：拖视频进来 → 自动剪 → 出片卡片照样能发布 ----------
UP_MIN_FREE = float(C.get("dashboard.min_free_gb", 20)) * 1e9   # 传完硬盘至少还要剩这么多（直播录制、剪辑都要地方）
CTRL = re.compile(r"[\x00-\x1f\x7f]")


def clean(t, n, lines=False):
    t = (t or "").replace("\r", "")
    t = "\n".join(CTRL.sub("", x) for x in t.split("\n")) if lines else CTRL.sub(" ", t)
    return t.strip()[:n]


def upload_check(name, size):
    """传之前先问一下（类型、大小、硬盘），免得传了几个 GB 才被拒。返回 (原文件名, 扩展名)。"""
    from ..worker import clip as W
    name = clean(os.path.basename(str(name or "").replace("\\", "/")), 120)
    ext = Path(name).suffix.lower()
    if ext not in W.EXTS:
        raise ValueError(f"不支持的文件类型「{ext or '没有扩展名'}」，只收 {' '.join(sorted(e[1:] for e in W.EXTS))}")
    if not size or int(size) <= 0:
        raise ValueError("文件是空的（0 字节）")
    free = shutil.disk_usage(ROOT).free
    if free - int(size) < UP_MIN_FREE:
        raise ValueError(f"硬盘只剩 {free / 1e9:.0f} GB，传完会不到 {UP_MIN_FREE / 1e9:.0f} GB，先清理再传")
    return name, ext


def upload(h, qs):
    """PUT /api/upload?name=&title=&top=&tight=&burn=&note=，请求体就是视频原始字节；边收边写盘（几个 GB 也不占内存）。"""
    from ..worker import clip as W
    g = lambda k, d="": (qs.get(k) or [d])[0]
    size = int(h.headers.get("Content-Length") or 0)
    name, ext = upload_check(g("name"), size)
    try:
        top = int(g("top") or 0)
    except ValueError:
        top = 0
    vdir = C.VIDEOS
    vdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    with W.locked() as q:              # 排号：同一分钟传多个按 _1 _2 …；.part 用 O_EXCL 占位，并发上传也不会撞
        ids, k = {j["id"] for j in q}, 1
        while True:
            jid = f"UP_{stamp}_{k}"
            part = vdir / f".{jid}{ext}.part"
            if jid not in ids and not any(vdir.glob(f"{jid}.*")):
                try:
                    fd = os.open(part, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                    break
                except FileExistsError:
                    pass
            k += 1
    final = vdir / f"{jid}{ext}"
    left = size
    try:
        with os.fdopen(fd, "wb") as f:
            while left > 0:
                chunk = h.rfile.read(min(left, 4 << 20))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
        if left:
            raise ValueError(f"上传中断（还差 {left / 1e6:.0f} MB）")
        os.replace(part, final)
    except BaseException:
        part.unlink(missing_ok=True)   # 面板自己写的半截临时文件
        raise
    info = W.probe(final)
    bad = "不是能读的视频（读不出画面）" if not info else None if info["audio"] else "没有声音轨，没法识别字幕"
    if bad:
        final.unlink(missing_ok=True)  # 刚收下的副本，原文件还在上传者电脑上
        raise ValueError(bad)
    job = {"id": jid, "name": name, "title": clean(g("title"), 60) or Path(name).stem[:60], "file": C.rel(final),
           "out": C.rel(C.DELIVER / f"手动上传_{jid}"), "size": size, **info,
           "opts": {"top": top if 1 <= top <= 30 else None, "tight": g("tight", "1") != "0", "burn": g("burn", "1") != "0",
                    "note": clean(g("note"), 500, lines=True)},
           "status": "pending", "created": datetime.now().strftime("%m-%d %H:%M:%S"), "created_ts": time.time()}
    with W.locked() as q:
        q.append(job)
    ensure_clip_worker()
    return job


def clip_job_action(body, action):
    """重试（失败/已停止的）、停止（排队的直接取消；在跑的让小工人停掉 auto_clip）、删除（不能删在跑的；原视频挪进废纸篓，成片保留）。"""
    from ..worker import clip as W
    with W.locked() as q:
        j = next((x for x in q if x["id"] == body.get("id")), None)
        if not j:
            raise ValueError("找不到这个任务")
        if action == "retry":
            if j["status"] not in ("failed", "cancelled"):
                raise ValueError("只有失败或已停止的才能重试")
            if not C.ws(j["file"]).exists():
                raise ValueError("上传的视频已经不在了")
            j.update(status="pending", error=None, cancel_req=False, retried=j.get("retried", 0) + 1)
        elif action == "cancel":
            if j["status"] == "pending":
                j.update(status="cancelled", error=None)
            elif j["status"] == "running":
                j["cancel_req"] = True
            else:
                raise ValueError("这个任务已经结束了")
        else:
            if j["status"] == "running":
                raise ValueError("正在剪，先停止再删除")
            q.remove(j)
    if action == "delete":
        W.trash(C.ws(j["file"]))
        return {"id": j["id"], "status": "deleted"}
    if action == "retry":
        ensure_clip_worker()
    return j


def ensure_clip_worker():
    if not any("liveclip.worker.clip" in r[3] for r in processes()):
        _spawn("worker.clip", "clip_worker.out")


def uploads():
    from ..worker import clip as W
    from .. import cost as costlog
    q = json.loads(W.QUEUE.read_text()) if W.QUEUE.exists() else []
    spent = {}
    labels = {j["id"] for j in q}
    for r in costlog.load():
        if r.get("label") in labels:
            a = spent.setdefault(r["label"], [0.0, 0.0])
            a[0] += r.get("usd") or 0
            a[1] += r.get("cny") or 0
    jobs = []
    for j in q[::-1]:
        x = {k: j.get(k) for k in ("id", "name", "title", "size", "dur", "w", "h", "status", "step", "step_ts", "created",
                                   "created_ts", "started_ts", "finished_ts", "error", "n_clips", "top_used", "retried", "cancel_req")}
        x["opts"] = j.get("opts") or {}
        x["vertical"] = (j.get("h") or 0) > (j.get("w") or 0)
        x["top_auto"] = W.auto_top(j.get("dur") or 0)
        x["cost"] = {"usd": round(spent[j["id"]][0], 2), "cny": round(spent[j["id"]][1], 3)} if j["id"] in spent else None
        x["dir_hint"] = j.get("out", "") + "/"
        x["clips"] = clips_of(j["id"], deliver=Path(j["out"]).name, plan_name="auto") if j["status"] == "done" else []
        jobs.append(x)
    running = any("liveclip.worker.clip" in r[3] for r in processes())
    return {"jobs": jobs, "worker": running, "free_gb": round(shutil.disk_usage(ROOT).free / 1e9), "min_free_gb": round(UP_MIN_FREE / 1e9),
            "exts": sorted(W.EXTS), "now_ts": time.time()}


# ---------- 英文（?lang=en）：只翻面板自己生成的文字；切片标题、模型写的理由是中文内容，保持原文 ----------
EN_STEPS = {"质检": "QA", "出片": "Delivered", "录制": "Recording", "上传远端": "Upload to remote",
            "下载分段": "Download segments", "百炼识别": "ASR (Bailian)", "字幕校对": "Subtitle review",
            "Claude 选段": "Clip picking (Claude)", "挑预告": "Hook picking", "剪辑": "Cutting", "精剪": "Tight edit",
            "封面文案": "Covers & captions", "查远端等新分段": "Polling for segments", "拼接整场视频": "Concatenating video",
            "Claude 处理中": "Claude working"}
EN_TOOLS = {"字幕校对": "Subtitle review", "选段": "Clip picking", "单条校对": "Clip subtitles", "挑预告": "Hook picking",
            "精剪": "Tight edit", "发布抖音": "Douyin publish", "语音识别": "ASR", "测试": "Test"}
EN_QA = {"人脸被画面边缘切掉": "Face cut off at frame edge", "字幕挡住嘴": "Subtitles cover mouth", "标题挡住脸": "Title covers face",
         "画面里经常找不到人脸": "Face often missing", "开头或结尾切在半句话": "Starts/ends mid-sentence",
         "音频时间戳不连续，上传后可能声画错开": "Audio timestamps not continuous", "声音和画面没对齐": "Audio/video out of sync",
         "开头一秒多没有人脸": "No face in first second"}
EN_RULES = [  # (中文正则, 英文模板)
    # 选段决策理由
    (r"^结尾落在最近 5 分钟里，话可能还没说完，留到下一轮再看（第 (\d+) 轮收回，出片 (\d+)）$",
     r"Ends in the last 5 min, may be unfinished — deferred (picked up in round \1 as #\2)"),
    (r"^结尾落在最近 5 分钟里，话可能还没说完，留到下一轮再看$", "Ends in the last 5 min, may be unfinished — deferred to next round"),
    (r"^选中，第 (\d+) 轮出片 (\d+)$", r"Picked — round \1, clip #\2"),
    (r"^上一轮留下的，这轮收回，出片 (\d+)$", r"Deferred last round, picked up now — clip #\1"),
    (r"^和已经剪好的片段时间重叠$", "Overlaps a clip already cut"),
    (r"^没有采用$", "Not used"),
    (r"^时长 (\d+) 秒，不在 12 秒–5 分钟$", r"Duration \1s, outside 12 s–5 min"),
    (r"^和更高分的「(.+)」离得太近（间隔不到 50 秒）$", r"Too close (<50s) to higher-scored “\1”"),
    (r"^加上它总时长会超过这段字幕的 35%$", "Would push total length over 35% of the window"),
    (r"^规则筛掉$", "Filtered by rules"),
    # 质检原因
    (r"^声音比画面(早|晚) (\d+) 毫秒$", lambda m: f"Audio {'ahead of' if m[1] == '早' else 'behind'} video by {m[2]} ms"),
    (r"^(正文|预告)(开头|结尾)切在「(.+)」中间$", lambda m: f"{'Clip' if m[1] == '正文' else 'Hook'} {'start' if m[2] == '开头' else 'end'} cuts “{m[3]}” mid-sentence"),
]


def en(text):
    if not isinstance(text, str) or not text:
        return text
    for pat, rpl in EN_RULES:
        m = re.match(pat, text)
        if m:
            return rpl(m) if callable(rpl) else m.expand(rpl)
    base = re.sub(r"（抽查 (\d+) 帧有问题）$", "", text)
    if base in EN_QA:
        n = re.search(r"（抽查 (\d+) 帧有问题）$", text)
        return EN_QA[base] + (f" ({n[1]} sampled frames)" if n else "")
    return EN_STEPS.get(text, EN_TOOLS.get(text, text))


def to_en(st):
    """把面板生成的中文字段换成英文（就地改）。"""
    for x in st.get("pipeline") or []:
        x["name"] = en(x["name"])
    st["step_name"] = en(st.get("step_name"))
    rec = st.get("rec") or {}
    rec["error"] = en(rec.get("error"))
    if rec.get("note"):
        rec["note"] = rec["note"].replace("磁盘", "disk")
    ed = st.get("editor") or {}
    if ed.get("note"):
        ed["note"] = {"防睡眠开": "sleep blocked", "⚠ 没开防睡眠": "⚠ may sleep"}.get(ed["note"], ed["note"])
    for u in rec.get("uploads") or []:
        u["msg"] = en(u["msg"])
    for ses in st.get("sessions") or []:
        for e in ses.get("events") or []:
            e["msg"] = en(e["msg"])
        for c in ses.get("clips") or []:
            if c.get("check"):
                c["check"] = dict(c["check"], bad=[en(b) for b in c["check"].get("bad") or []])
            c["chips"] = ["Tight edit" if x == "有精剪" else x for x in c.get("chips") or []]
        for r in (ses.get("decisions") or {}).get("rounds") or []:
            for it in r["items"]:
                it["reason"] = en(it["reason"])
    c = st.get("costs")
    if c and not c.get("error"):
        for w in ("session", "today", "week", "all"):
            for t in c[w]["tools"]:
                t["tool"] = en(t["tool"])
        c["meta"] = "$ = Claude at API price (subscription, not billed) · ¥ = Bailian billed"
        c["note"] = ("With the claude CLI (subscription), $ is the API-price equivalent, for comparing steps and checking "
                     "optimizations. Bailian ASR is billed per token.")
    return st


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype, extra=None):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):   # 浏览器刷新/关页时还没收完，不用管，也别刷一屏报错
            self.close_connection = True

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self.send(200, (HERE / "static" / "index.html").read_bytes(), "text/html; charset=utf-8")
        if u.path == "/api/status":
            try:
                st = status()
                if urllib.parse.parse_qs(u.query).get("lang", ["zh"])[0] == "en":
                    to_en(st)
                body = json.dumps(st, ensure_ascii=False).encode()
                return self.send(200, body, "application/json; charset=utf-8")
            except Exception as e:
                return self.send(500, json.dumps({"error": str(e)}).encode(), "application/json")
        if u.path == "/api/preview":
            try:
                rel = CLIPS.get(urllib.parse.parse_qs(u.query).get("key", [""])[0], "")
                if not rel:
                    raise ValueError("找不到这条片子")
                return self.send(200, json.dumps(preview(rel), ensure_ascii=False).encode(), "application/json; charset=utf-8")
            except Exception as e:
                return self.send(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode(), "application/json; charset=utf-8")
        if u.path == "/file":
            return self.file(FILES.get(urllib.parse.parse_qs(u.query).get("id", [""])[0], ""))
        self.send(404, b"not found", "text/plain")

    def do_PUT(self):
        u = urllib.parse.urlparse(self.path)
        if u.path != "/api/upload":
            return self.send(404, b"not found", "text/plain")
        try:
            job = upload(self, urllib.parse.parse_qs(u.query))
            return self.send(200, json.dumps({"id": job["id"], "status": job["status"]}).encode(), "application/json; charset=utf-8")
        except Exception as e:
            self.close_connection = True      # 请求体可能没读完，别复用这条连接
            return self.send(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path not in ("/api/publish", "/api/retry", "/api/cancel", "/api/upload/check", "/api/livenote",
                        "/api/clipjob/retry", "/api/clipjob/cancel", "/api/clipjob/delete"):
            return self.send(404, b"not found", "text/plain")
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if path == "/api/livenote":
                LIVE_NOTE.parent.mkdir(parents=True, exist_ok=True)
                LIVE_NOTE.write_text(str(body.get("text") or "").strip()[:200] + "\n", encoding="utf-8")
                return self.send(200, b'{"ok": true}', "application/json; charset=utf-8")
            if path == "/api/upload/check":
                upload_check(body.get("name"), body.get("size"))
                return self.send(200, b'{"ok": true}', "application/json; charset=utf-8")
            if path.startswith("/api/clipjob/"):
                j = clip_job_action(body, path.rsplit("/", 1)[1])
                return self.send(200, json.dumps({"id": j["id"], "status": j["status"]}).encode(), "application/json; charset=utf-8")
            job = enqueue(body) if path == "/api/publish" else queue_action(body, path.rsplit("/", 1)[1])
            out = {"id": job["id"], "status": job["status"]}      # 只回编号和状态，不带目录
            return self.send(200, json.dumps(out, ensure_ascii=False).encode(), "application/json; charset=utf-8")
        except Exception as e:
            return self.send(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def file(self, rel):
        """只给 deliver/ 下的封面和视频；视频支持 Range（浏览器拖进度条要用）。"""
        if not rel:
            return self.send(404, b"not found", "text/plain")
        p = C.ws(rel).resolve()
        if not str(p).startswith(str(C.DELIVER.resolve()) + "/") or not p.is_file():
            return self.send(404, b"not found", "text/plain")
        ctype = "text/plain; charset=utf-8" if p.suffix == ".srt" else (mimetypes.guess_type(p.name)[0] or "application/octet-stream")
        size = p.stat().st_size
        rng = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        start, end = 0, size - 1
        if rng and (rng.group(1) or rng.group(2)):
            if rng.group(1):
                start = int(rng.group(1))
                end = int(rng.group(2)) if rng.group(2) else size - 1
            else:
                start = size - int(rng.group(2))
            end = min(end, size - 1)
        n = end - start + 1
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(n))
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with open(p, "rb") as f:
                f.seek(start)
                while n > 0:
                    chunk = f.read(min(n, 1 << 20))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    n -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(prog="liveclip dashboard")
    ap.add_argument("--port", type=int, default=int(C.get("dashboard.port", 8771)))
    a = ap.parse_args(argv)
    C.ensure_dirs()
    if RC.get("ssh_host"):
        threading.Thread(target=vps_loop, daemon=True).start()
    print(f"dashboard: http://127.0.0.1:{a.port}  (workspace {ROOT})", flush=True)
    http.server.ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()


if __name__ == "__main__":
    main()
