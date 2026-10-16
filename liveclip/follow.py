"""Live-follow clipping: watch an rclone remote where a recording server uploads 5-minute live segments
(deploy/vps/), transcribe each segment as it arrives, pick + cut a round of clips every 20 minutes of transcript,
and wrap up (full-length version, candidate list, album page, notification) when the server uploads ENDED.

Remote layout: <remote>/<anchor>/<anchor>_<YYYY-MM-DD_HH-MM-SS>/<anchor>_<YYYY-MM-DD_HH-MM-SS>_<NNN>.ts (+ ENDED)
(the file naming of DouyinLiveRecorder's segment mode; the time prefix identifies one live session).

用法：
    liveclip follow                         # 常驻，每 live.poll_sec 秒查一次（remote 取 config live.remote）
    liveclip follow --once                  # 只跑一轮（调试）
    liveclip follow --remote myremote:live-sim   # 模拟测试用另一个目录（配合 liveclip sim-live）
    liveclip follow --keep-awake            # macOS caffeinate / Linux systemd-inhibit，防止睡眠

窗口边界（一段对话正好跨两个 20 分钟怎么办）：
- 每轮看 [已定到的位置 - 5 分钟, 现在]，但只收结尾在「现在 - 5 分钟」之前的片段；最后 5 分钟的话可能没说完，留给下一轮。
- 和已剪片段时间上有重叠的不再剪（窗口重叠会重复选中）。
- 给模型的说明里写明这是进行中的直播，结尾没说完的不要选。
- 收到 ENDED 那一轮不留 5 分钟，全部处理完。
字幕校对：每轮选段前用 liveclip.review.subs 把新到的字幕校对一遍（带前 5 分钟上下文；SenseVoice 只在百炼失败时当主字幕），
选段、标题、烧录都用校对后的 data/asr/<ID>_reviewed.json；校对失败就用识别原文，记日志。结束时整场百炼重识别（带分人）后再整场校对一次，
完整版 字幕.srt 用校对后的文字。
每场的状态在 recordings/live/<主播>/<时间前缀>/state.json，片段计划 clips/<ID>/follow.json，成片 clips/<ID>/follow/，
交付 deliver/直播跟播_<ID>/，日志 logs/live_follow.log。
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from .caption import caption_text
from .config import C, py_mod
from .layout import detect_layout
from .platform import keep_awake, notify as _notify, open_path
from .review import subs as review_subs

ROOT = C.ROOT
LV = C.get("live")
WINDOW, TAIL, LOOKBACK, POLL = LV["window_sec"], LV["tail_sec"], LV["lookback_sec"], LV["poll_sec"]
# 观点库 data/points.json 由 liveclip.points 从计划文件、发布记录、数据快照重建（不在这里写）；
# 不拿来去重、不设条数上限（反复讲的观点说明热门，照样剪）
LOCAL = C.RECORDINGS / "live"
COVERS = C.DELIVER / "_covers"
SEG = re.compile(r"^(?P<prefix>.+_(?P<ts>\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}))_(?P<n>\d{3})\.ts$")
# 录制器里的主播名（目录名）→ 场次 ID 后缀；空 = 所有主播都处理，后缀用 live
WHO = {a["match"]: a.get("key", "live") for a in (LV.get("anchors") or []) if a.get("match")}
NOTE_LIVE = ("这是一场还在进行中的直播的一部分，时间是全场的绝对秒数。字幕最后几分钟的话题可能还没说完，"
             "结尾没说完、结论不在字幕里的段落不要选，下一轮会再看。")
NOTE_BASE = C.get("streamer.live_layout_note") or ""


def log(msg):
    line = f"[{datetime.now():%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    C.LOGS.mkdir(parents=True, exist_ok=True)
    with open(C.LOGS / "live_follow.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def sh(cmd, timeout=None):
    r = subprocess.run([str(c) for c in cmd], cwd=ROOT, capture_output=True, text=True, timeout=timeout,
                       stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        name = next((str(c) for c in cmd if str(c).startswith("liveclip.")), str(cmd[0]))
        raise RuntimeError(f"{name} 失败：{r.stderr[-800:]}")
    return r.stdout


def notify(title, text):
    _notify(title, text, sound=True)


EVENT_NOTE = C.DATA / (LV.get("event_note_file") or "live_note.txt")


def event_note():
    """当天的事件/联动（事件联动类切片时效最强，拼速度）。往 data/live_note.txt 写一行
    （如「今天和某某连麦」，面板上也能填），下一轮选段就优先挑相关段落；不用时清空。每轮现读，不用重启。"""
    try:
        t = EVENT_NOTE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return f"\n今天的事件/联动：{t}。和它有关的段落优先选（score 加 1），event_tags 写上相关的人和事件。" if t else ""


def write_srt(body, path):
    def ts(t):
        ms = int(round(t * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
    lines = []
    for k, q in enumerate((q for q in body if q["content"].strip()), 1):
        lines += [str(k), f"{ts(q['from'])} --> {ts(q['to'])}", q["content"].strip(), ""]
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout.strip()
    return float(out or 0)


class Session:
    def __init__(self, remote, anchor, prefix):
        self.remote, self.anchor, self.prefix = remote, anchor, prefix
        self.dir = LOCAL / anchor / prefix
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.dir / "state.json"
        self.s = json.loads(self.state_file.read_text()) if self.state_file.exists() else {
            "segments": [], "committed": 0.0, "picked_until": 0.0, "rounds": 0, "ended": False, "done": False,
            "deferred": []}
        self.s.setdefault("deferred", [])
        ts = datetime.strptime(SEG.match(prefix + "_000.ts")["ts"], "%Y-%m-%d_%H-%M-%S")
        who = next((v for k, v in WHO.items() if k in anchor), "live")
        self.vid = f"LIVE_{ts:%Y%m%d_%H%M}_{who}"
        self.title = (LV.get("title") or "{name}直播（{date:%Y年%m月%d日}）").format(name=C.name, date=ts, key=who, anchor=anchor)
        self.subs = C.ASR / f"{self.vid}.json"
        self.reviewed = C.ASR / f"{self.vid}_reviewed.json"   # 校对后的跟播字幕，选段和烧录用它
        self.video = C.VIDEOS / f"{self.vid}.mp4"
        self.plan_dir = C.CLIPS / self.vid
        self.plan_file = self.plan_dir / "follow.json"
        self.out = C.DELIVER / f"直播跟播_{self.vid}"

    def save(self):
        self.state_file.write_text(json.dumps(self.s, ensure_ascii=False, indent=1))

    @property
    def total(self):
        return sum(x["dur"] for x in self.s["segments"])

    def cues(self):
        return json.loads(self.subs.read_text(encoding="utf-8"))["body"] if self.subs.exists() else []

    # 1. 下载新分段 → 逐段识别，时间加上前面分段的总长
    def ingest(self, remote_files):
        sh(["rclone", "copy", f"{self.remote}/{self.anchor}/{self.prefix}", self.dir, "--include", "*.ts"], timeout=1800)
        self.s["ended"] = "ENDED" in remote_files
        done = {x["name"] for x in self.s["segments"]}
        new = sorted((p for p in self.dir.glob("*.ts") if p.name not in done), key=lambda p: int(SEG.match(p.name)["n"]))
        for p in new:
            n = int(SEG.match(p.name)["n"])
            if self.s["segments"] and n != int(SEG.match(self.s["segments"][-1]["name"])["n"]) + 1:
                log(f"{self.vid} 分段不连续：{p.name}，等前面的传完")
                break
            d = duration(p)
            offset = self.total
            tmp = self.dir / f"{p.stem}.asr.json"
            # 主字幕用百炼直连（每段 5 分钟约 20 秒），失败退回本地 SenseVoice。逐段识别不分说话人：
            # 各段编号对不上，整场结束后 full_version() 对整条视频重新识别并分人
            engine = "百炼"
            try:
                sh(py_mod("asr", p, "--engine", "bailian", "--out", tmp), timeout=1800)
            except (RuntimeError, subprocess.TimeoutExpired) as e:
                log(f"{self.vid} {p.name} 百炼识别失败，退回 SenseVoice：{str(e)[-200:]}")
                engine = "SenseVoice"
                sh(py_mod("asr", p, "--engine", "sensevoice", "--out", tmp), timeout=1800)
            body = json.loads(tmp.read_text(encoding="utf-8"))["body"]
            for q in body:
                q["from"], q["to"] = round(q["from"] + offset, 2), round(q["to"] + offset, 2)
            all_cues = self.cues() + body
            engines = {x.get("asr", "SenseVoice") for x in self.s["segments"]} | {engine}
            self.subs.write_text(json.dumps({"source": f"{'+'.join(sorted(engines))} 跟播逐段", "body": all_cues},
                                            ensure_ascii=False, indent=1), encoding="utf-8")
            self.s["segments"].append({"name": p.name, "dur": round(d, 3), "offset": round(offset, 3), "asr": engine})
            self.save()
            log(f"{self.vid} 识别 {p.name}（{engine}）：{d:.0f}s，{len(body)} 句，累计 {self.total / 60:.1f} 分钟")

    def review(self, hi):
        """每轮选段前：把 hi 秒之前还没校对的字幕校对掉（前面带 5 分钟上下文；上一轮失败的这轮会重试）。
        返回这轮选段和烧录用的字幕文件：校对文件总是和识别字幕逐行对应，校对失败的行就是识别原文；连这个都写不出来时用识别字幕。"""
        try:
            out = sh(py_mod("review.subs", self.subs, "--out", self.reviewed, "--to", f"{hi:.2f}", "--context", "300"),
                     timeout=1800)
            log(f"{self.vid} 字幕" + ((out or "").strip().splitlines() or ["校对完"])[-1].split(" → ")[0])
            return self.reviewed
        except Exception as e:
            log(f"{self.vid} 字幕校对失败，没校对到的句子用识别原文：{str(e)[-300:]}")
        try:
            review_subs.sync(self.subs, self.reviewed)
            return self.reviewed
        except Exception as e:
            log(f"{self.vid} 校对文件写不出来，这轮用识别字幕：{e}")
            return self.subs

    # 2. 拼成一个 mp4（concat 按每段时长接上时间轴，和字幕的偏移一致）
    def build_video(self):
        lst = self.dir / "concat.txt"
        lst.write_text("".join(f"file '{(self.dir / x['name']).as_posix()}'\n" for x in self.s["segments"]), encoding="utf-8")
        tmp = self.video.with_suffix(".tmp.mp4")
        # 画面直接拷；声音重编码并用静音补齐接缝空档。VPS 每段 TS 里声音比画面早约 0.1 秒开始（第一段反而晚 0.13 秒），
        # 无损拼接后每个接缝的声音轨留下 91–113ms 的空档；
        # liveclip.cut 为修剪映错位把声音按采样重新编号（asetpts=N/SR/TB），
        # 空档被挤掉，片子一跨接缝声音就提前 65–132ms（2026-10-12 跟播 qa_clips 查出）。async 补静音后声音连续、和画面对齐
        sh(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-map", "0:v:0", "-map", "0:a:0",
            "-c:v", "copy", "-af", "aresample=async=1:min_hard_comp=0.01", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", tmp], timeout=1800)
        tmp.replace(self.video)

    def plan(self):
        if self.plan_file.exists():
            return json.loads(self.plan_file.read_text(encoding="utf-8"))
        return {"bv": self.vid, "title": self.title, "model": C.model("pick"), "video": str(self.video),
                "subs": C.rel(self.reviewed), "layout": "live_v", "live": True, "burn_subs": True,
                "zoom": False, "cover_probe": {"x": 0, "y": 100, "w": 600, "h": 600, "min_mean": 110},
                "clips": [], "typo_fixes": [], "note": "live_follow 跟播自动生成，未音画复核"}

    # 3. 挑一轮：看 [committed - 5 分钟, 现在]，只收结尾在 现在 - 5 分钟 之前的（结束那轮不留）
    def pick_round(self):
        final = self.s["ended"]
        hi = self.total
        lo = max(0.0, self.s["committed"] - LOOKBACK)
        guard = hi if final else hi - TAIL
        subs = self.review(hi)
        cues = [q for q in json.loads(subs.read_text(encoding="utf-8"))["body"] if lo <= q["from"] < hi]
        r = self.s["rounds"] + 1
        win = self.dir / f"window_r{r}.json"
        win.write_text(json.dumps({"body": cues}, ensure_ascii=False), encoding="utf-8")
        plan = self.plan()
        accepted = []
        picked = []
        if len(cues) >= 20:
            note = NOTE_BASE + ("" if final else NOTE_LIVE) + event_note()
            self.plan_dir.mkdir(parents=True, exist_ok=True)
            sh(py_mod("pick", self.vid, "--subs", win, "--title", self.title, "--live", "--note", note,
                      "--out", f"follow_r{r}.json"), timeout=1800)
            picked = json.loads((self.plan_dir / f"follow_r{r}.json").read_text(encoding="utf-8"))["clips"]
        if picked or self.s["deferred"]:
            taken = [(c["start"], c["end"]) for c in plan["clips"]]
            # 上一轮因为落在最后 5 分钟被留下的片段：模型这轮不一定再选中它（模拟测试里就丢过一条），
            # 所以记下来，这轮先按分数和新选的放在一起比
            carry = [dict(c, carried=True) for c in self.s["deferred"]]
            self.s["deferred"] = []
            for c in sorted(picked + carry, key=lambda c: (-c["score"], c["start"])):
                if c["end"] > guard + 0.01:
                    self.s["deferred"].append({k: v for k, v in c.items() if k != "carried"})
                    log(f"{self.vid} 第{r}轮 留给下一轮（结尾 {c['end']:.0f}s > {guard:.0f}s）：{c['title'].replace(chr(10), ' ')}")
                    continue
                hit = [k for k in plan["clips"] + accepted if c["start"] < k["end"] and k["start"] < c["end"]]
                if any(c["start"] < e and s < c["end"] for s, e in taken):
                    # 更完整的长版：比重叠的那条长 1.5 倍以上、分数不低，照剪，两版都留，标明是谁的完整版
                    short = max(hit, key=lambda k: k["end"] - k["start"]) if hit else None
                    if short and (c["end"] - c["start"]) >= 1.5 * (short["end"] - short["start"]) \
                            and c.get("score", 0) >= short.get("score", 0):
                        c["full_of"] = short["title"].replace("\n", " ")
                        log(f"{self.vid} 第{r}轮 和已剪的「{c['full_of']}」重叠，但这条是更完整的长版"
                            f"（{c['end'] - c['start']:.0f}s 对 {short['end'] - short['start']:.0f}s），两版都留：{c['title'].replace(chr(10), ' ')}")
                    else:
                        log(f"{self.vid} 第{r}轮 和已剪片段重叠，跳过：{c['title'].replace(chr(10), ' ')}")
                        continue
                if c.pop("carried", False):
                    log(f"{self.vid} 第{r}轮 收回上一轮留下的：{c['title'].replace(chr(10), ' ')}")
                c["round"] = r
                accepted.append(c)
                taken.append((c["start"], c["end"]))
        self.s["committed"], self.s["picked_until"], self.s["rounds"] = guard, hi, r
        if accepted:
            self.build_video()
            for c in accepted:
                c.update(detect_layout(self.video, c))
            first = len(plan["clips"]) + 1
            plan["clips"] += accepted
            plan["typo_fixes"] = []
            plan["subs"] = C.rel(subs)       # 烧录用校对后的字幕（这轮校对失败时是识别原文）
            self.plan_file.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
            only = ",".join(str(i) for i in range(first, len(plan["clips"]) + 1))
            try:        # 预告专项重挑；失败就用选段时带的预告
                sh(py_mod("hook", self.vid, "follow.json", "--out", "follow.json", "--only", only), timeout=1200)
                plan = json.loads(self.plan_file.read_text(encoding="utf-8"))
            except Exception as e:
                log(f"{self.vid} 第{r}轮 预告重挑失败，用原预告：{e}")
            sh(py_mod("cut", self.vid, "--plan", "follow.json", "--only", only, "--allow-check"), timeout=7200)
            self.package(plan, range(first, len(plan["clips"]) + 1))
            names = "、".join(c["title"].replace("\n", "") for c in accepted)
            log(f"{self.vid} 第{r}轮出片 {len(accepted)} 条：{names}")
            notify(f"直播切片 +{len(accepted)} 条", names[:80])
        else:
            log(f"{self.vid} 第{r}轮没有新片段（窗口 {lo / 60:.0f}–{hi / 60:.0f} 分钟）")
        self.save()

    # 4. 封面 + 文案，放进 deliver/直播跟播_<ID>/NN_标题/
    def package(self, plan, idxs):
        mp4s = []
        for i in idxs:
            hit = sorted((self.plan_dir / "follow").glob(f"{i:02d}_*.mp4"))
            if hit:
                mp4s.append((i, hit[0]))
        if not mp4s:
            return
        covers = COVERS / self.vid          # 每场一个子目录，免得不同场的「01_标题」封面互相覆盖
        covers.mkdir(parents=True, exist_ok=True)
        sh(py_mod("cover.make", *[m for _, m in mp4s], "--out", covers, "--bili"), timeout=3600)
        self.out.mkdir(parents=True, exist_ok=True)
        for i, m in mp4s:
            c = plan["clips"][i - 1]
            d = self.out / m.stem
            d.mkdir(exist_ok=True)
            shutil.copy2(m, d / "视频.mp4")
            for suffix, name in (("", "封面.jpg"), ("_4比3", "封面_横4比3.jpg"), ("_B站", "封面_B站.jpg")):
                if (covers / f"{m.stem}{suffix}.jpg").exists():
                    shutil.copy2(covers / f"{m.stem}{suffix}.jpg", d / name)
            (d / "文案.txt").write_text(caption_text(c, ["直播"]), encoding="utf-8")
        only = ",".join(str(i) for i, _ in mp4s)
        self.qa(only, tight=False)
        # 精剪版另出一份：交付 视频_精剪.mp4，失败不影响普通版
        try:
            sh(py_mod("tight", self.vid, "follow.json", "--only", only, "--allow-check"), timeout=3600)
            for i, m in mp4s:
                hit = sorted((self.plan_dir / "follow_tight").glob(f"{i:02d}_*.mp4"))
                if hit:
                    shutil.copy2(hit[0], self.out / m.stem / "视频_精剪.mp4")
            self.qa(only, tight=True)
        except Exception as e:
            log(f"{self.vid} 精剪失败，只交付普通版：{e}")

    def qa(self, only, tight):
        """出片后自动质检（切脸、标题挡脸、音画同步、开头有脸、音频时间戳），结论写进日志；只报告不改片。"""
        try:
            out = sh(py_mod("qa", self.vid, "follow.json", "--only", only, *(["--tight"] if tight else [])), timeout=1800)
            for line in (out or "").splitlines():
                if line[:1] in "⚠✗" or line.startswith("质检"):
                    log(f"{self.vid} {'精剪' if tight else ''}{line}")
        except Exception as e:
            log(f"{self.vid} 质检没跑成：{e}")

    # 5. 结束：候选清单 + 专辑网页 + 完整版（可选传云盘）+ 通知；本地分段删掉（远端有原件，videos/ 里有拼好的整场）
    def finish(self):
        plan = self.plan()
        n = len(plan["clips"])
        if n:
            hms = lambda s: f"{int(s) // 3600}:{int(s) % 3600 // 60:02d}:{int(s) % 60:02d}"
            lines = [f"{self.title}：跟播出片 {n} 条（均未音画复核；字幕经 Claude 校对同音错字，没人工听过）", ""]
            for i, c in enumerate(plan["clips"], 1):
                lines.append(f"{i:2d}. [{c['score']}分]{'⚠' if c.get('risky') else ' '} 第{c.get('round')}轮 {hms(c['start'])} "
                             f"{int(c['end'] - c['start'])}s {c['title'].replace(chr(10), ' ')}"
                             f"{'  【' + c['layout_note'] + '】' if c.get('layout_note') else ''}")
            (self.out / "候选清单.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
            sh(py_mod("album", self.out, self.plan_file, f"{self.title} · 跟播 {n} 条"), timeout=600)
            open_path(self.out / "专辑.html")
        self.build_video()
        self.full_version()
        # 完整版传云盘（config live.archive_remote，MD5 核对）：单独进程跑，不挡下一场
        if LV.get("archive_remote"):
            with open(C.LOGS / "upload_full.out", "a") as f:
                subprocess.Popen(py_mod("archive", self.vid), cwd=ROOT, stdout=f, stderr=f,
                                 stdin=subprocess.DEVNULL, start_new_session=True)
        for x in self.s["segments"]:
            (self.dir / x["name"]).unlink(missing_ok=True)
        self.s["done"] = True
        self.save()
        log(f"{self.vid} 整场结束：{self.total / 60:.0f} 分钟，{self.s['rounds']} 轮，{n} 条 → {self.out}")
        notify("直播切片全部出齐", f"{self.vid}：{n} 条候选")

    # 6. 完整版：整场录屏 + SRT 字幕（外挂，不烧录）。字幕整场重新用百炼识别一次（带分人），
    #    比跟播逐段拼的统一（前面可能是 SenseVoice）；失败就用逐段字幕
    def full_version(self):
        full = self.out / "完整版"
        full.mkdir(parents=True, exist_ok=True)
        rec = full / "录屏.mp4"
        rec.unlink(missing_ok=True)
        try:
            os.link(self.video, rec)          # 同一块盘，硬链接不占空间
        except OSError:
            shutil.copy2(self.video, rec)
        whole = C.ASR / f"{self.vid}_bailian_spk.json"
        try:
            # 百炼按份串行转写，约 0.42 倍时长（7.4 小时的一场，一份 3.7 小时就转了 94 分钟），超时按时长放宽
            sh(py_mod("asr", self.video, "--engine", "bailian", "--diarize", "--out", whole),
               timeout=max(7200, int(self.total * 0.8)))
            body, src = json.loads(whole.read_text(encoding="utf-8"))["body"], "百炼整场识别"
        except Exception as e:
            log(f"{self.vid} 整场重识别失败，完整版字幕用跟播逐段字幕：{e}")
            whole, src = (self.reviewed if self.reviewed.exists() else self.subs), "跟播逐段字幕"
            body = json.loads(whole.read_text(encoding="utf-8"))["body"]
        # 整场校对（liveclip.review.subs），不带对照；失败就用没校对的字幕，记日志
        if src == "百炼整场识别":
            rev = whole.with_name(whole.stem + "_reviewed.json")
            try:
                out = sh(py_mod("review.subs", whole, "--out", rev), timeout=3600)
                log(f"{self.vid} 整场字幕" + ((out or "").strip().splitlines() or ["校对完"])[-1].split(" → ")[0])
            except Exception as e:
                log(f"{self.vid} 整场字幕校对没全做完，{'没校对到的句子用识别原文' if rev.exists() else '完整版用没校对的字幕'}：{str(e)[-300:]}")
            if rev.exists():
                body, src = json.loads(rev.read_text(encoding="utf-8"))["body"], "百炼整场识别 + 整场校对"
        write_srt(body, full / "字幕.srt")   # 待确认的句子照烧模型认为最可能的写法；明细在 _reviewed.json 的 status/note
        n_chk = sum(q.get("status") == "check" for q in body)
        log(f"{self.vid} 完整版：{rec.name} + 字幕.srt（{src}，{len(body)} 句{f'，待确认 {n_chk} 句' if n_chk else ''}）→ {full}")

    def step(self, remote_files):
        if self.s["done"]:
            return
        self.ingest(remote_files)
        total = self.total
        if total - self.s["picked_until"] >= WINDOW or (self.s["ended"] and total > self.s["picked_until"] + 1):
            self.pick_round()
        if self.s["ended"] and self.s["picked_until"] >= total - 1 and len(self.s["segments"]) == \
                sum(1 for f in remote_files if f.endswith(".ts")):
            self.finish()


def poll(remote):
    out = sh(["rclone", "lsf", "-R", "--files-only", remote], timeout=300)
    by = {}
    for line in out.splitlines():
        parts = line.split("/")
        if len(parts) == 3:
            by.setdefault((parts[0], parts[1]), []).append(parts[2])
    for (anchor, prefix), files in sorted(by.items(), key=lambda kv: kv[0][1]):
        if not SEG.match(prefix + "_000.ts") or (WHO and not any(k in anchor for k in WHO)):
            continue                # 配了 live.anchors 就只处理这些主播（测试录的别的直播间不剪）
        s = Session(remote, anchor, prefix)
        if s.s["done"]:
            continue
        try:
            s.step(files)
        except Exception as e:      # 一场出错不影响别的场，下一分钟再试
            log(f"{s.vid} 出错：{e}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="liveclip follow", description="follow live segments on an rclone remote and clip them")
    ap.add_argument("--remote", default=LV.get("remote") or None, help="rclone remote dir (default: config live.remote)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--keep-awake", action="store_true", help="prevent system sleep while following")
    a = ap.parse_args(argv)
    if not a.remote:
        ap.error("set live.remote in config.yaml or pass --remote (an rclone path such as myremote:liveclip-live)")
    C.ensure_dirs()
    if a.keep_awake:
        keep_awake(os.getpid())
    log(f"跟播启动，盯 {a.remote}，每 {POLL} 秒查一次；窗口 {WINDOW // 60} 分钟，留尾 {TAIL // 60} 分钟")
    while True:
        try:
            poll(a.remote)
        except Exception as e:
            log(f"查远端出错：{e}")
        if a.once:
            break
        time.sleep(POLL)


if __name__ == "__main__":
    main()
