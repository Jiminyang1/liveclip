"""One command: video → candidate clip package (videos, covers, captions, candidate list, preview page).

Usage:
    liveclip clip <video.mp4> [--subs subs.srt|.json] [--asr-json transcript.json] [--title 标题] [--top 8] [--out deliver/候选_xxx]
                              [--layout talk_h|full_h|live_v|duet_v|full_v] [--live] [--auto-layout] [--note "给选段模型的说明"]
                              [--tags a,b] [--cover-frame] [--burn-subs|--no-burn-subs] [--tight [--tight-separate]]
                              [--cover-dir ...]
A side-car <video>.json next to the video (keys as above: title/top/layout/live/note/tags/cover_frame/burn_subs/subs)
and a side-car <video>.srt are picked up automatically.

Steps (every step writes to disk; log in <out>/日志.txt):
1. ASR: Bailian whole-file → data/asr/<id>_bailian.json (with --live: diarized → <id>_bailian_spk.json) as the main
   transcript; SenseVoice → data/asr/<id>.json only when Bailian fails. --asr-json skips ASR and uses that file.
1b. Subtitle review: liveclip.review.subs proofreads the whole transcript once → <name>_reviewed.json (picking, titles
   and burned-in subtitles use it); on failure the raw transcript is used. External SRTs are not reviewed.
2. External subtitles: an SRT is converted (Chinese lines only) and aligned to the video timeline (liveclip.align).
3. Picking: > 60 min is split into ~45-min chunks (5-min overlap) for liveclip.pick, merged by score;
   every clip's position is cross-checked against the ASR text.
4. Cutting: hook pass (liveclip.hook) → cut top N (liveclip.cut) → full decode check (liveclip.verify) → covers →
   QA (liveclip.qa; clips that still cut faces are re-cut full-frame) → optional tight edit (liveclip.tight).
5. Output: <out>/NN_标题/{视频.mp4, 封面.jpg, 封面_B站.jpg, 封面_横4比3.jpg, 文案.txt}, 候选清单.txt, 专辑.html
Not automated: human audio/video review.
"""
import argparse
import difflib
import json
import re
import shutil
import subprocess
import time
from datetime import date
from pathlib import Path

from .caption import caption_text, short_title  # noqa: F401  (short_title re-exported for old callers)
from .config import C, py_mod
from .layout import SOLO, detect_layout, split_band  # noqa: F401  (split_band re-exported)

norm = lambda s: re.sub(r"[\W_]", "", s).lower()
ROOT = C.ROOT


class Log:
    def __init__(self, path):
        self.path, self.t0 = path, time.time()

    def __call__(self, msg):
        line = f"[{time.time() - self.t0:6.0f}s] {msg}"
        print(line, flush=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


OVERLAP_SEC = 300


def run(cmd, log, **kw):
    log("$ " + " ".join(str(c) for c in cmd))
    r = subprocess.run([str(c) for c in cmd], cwd=ROOT, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        log(r.stderr[-1500:])
        name = next((str(c) for c in cmd if str(c).startswith("liveclip.")), str(cmd[0]))
        raise SystemExit(f"失败：{name}")
    return r.stdout


def srt_to_json(srt, out):
    raw = Path(srt).read_text(encoding="utf-8-sig")
    ts = lambda t: sum(float(x) * k for x, k in zip(t.replace(",", ".").split(":"), (3600, 60, 1)))
    body = []
    for b in re.split(r"\n\s*\n", raw.strip()):
        ls = b.strip().split("\n")
        m = re.match(r"(\S+) --> (\S+)", ls[1]) if len(ls) >= 3 else None
        if m:
            body.append({"from": round(ts(m.group(1)), 3), "to": round(ts(m.group(2)), 3), "content": " ".join(ls[2:]).strip()})
    zh = [c for c in body if re.search(r"[一-鿿]", c["content"])] or body   # 双语字幕只留中文行
    Path(out).write_text(json.dumps({"body": zh}, ensure_ascii=False), encoding="utf-8")
    return zh


def main(argv=None):
    ap = argparse.ArgumentParser(prog="liveclip clip", description="video → candidate clip package")
    ap.add_argument("video")
    ap.add_argument("--subs", help="external subtitles (.srt / subtitle JSON) made by a human; used for picking and burning")
    ap.add_argument("--asr-json", help="use this transcript JSON instead of running ASR")
    ap.add_argument("--title"); ap.add_argument("--out")
    ap.add_argument("--top", type=int); ap.add_argument("--layout"); ap.add_argument("--note")
    ap.add_argument("--tags"); ap.add_argument("--live", action="store_true")
    ap.add_argument("--cover-frame", action="store_true", help="封面用两人同框整幅画面样式（播客）")
    ap.add_argument("--burn-subs", action="store_true", help="烧识别字幕（原片没字幕的直播录屏）")
    ap.add_argument("--no-burn-subs", action="store_true", help="不烧字幕（--live 默认会烧）")
    ap.add_argument("--cover-dir", help="封面中间文件放哪（默认 deliver/_covers/<ID>/）")
    ap.add_argument("--source", default=None, help="B站转载来源（默认 config branding.source）")
    ap.add_argument("--fresh", action="store_true", help="不沿用上次的分块选段结果，全部重新调 Claude 挑")
    ap.add_argument("--tight", action="store_true",
                    help="精剪：正常出片后前 top 条再跑 liveclip.tight，交付的 视频.mp4 用精剪版，正常版留作 视频_原版.mp4")
    ap.add_argument("--tight-separate", action="store_true",
                    help="配合 --tight：交付照跟播的约定，视频.mp4 = 正常版，精剪版另存 视频_精剪.mp4（面板手动上传用）")
    ap.add_argument("--auto-layout", action="store_true",
                    help="竖屏直播回放：逐段抽 5 帧判断单人满屏 / 左右连麦分屏，分别设段内 layout（每条候选单独检测）")
    a = ap.parse_args(argv)
    C.ensure_dirs()
    video = Path(a.video).resolve()
    side = video.with_suffix(".json")
    cfg = json.loads(side.read_text(encoding="utf-8")) if side.exists() else {}
    get = lambda k, d=None: getattr(a, k.replace("-", "_"), None) or cfg.get(k, d)
    subs_in = get("subs") or next((str(p) for p in (video.with_suffix(".srt"),) if p.exists()), None)
    title = get("title", video.stem)
    top = int(get("top", 8)); tags = [t for t in str(get("tags", "")).split(",") if t]
    vid = re.sub(r"[^\w]+", "_", video.stem)[:40]
    out = Path(get("out") or C.DELIVER / f"候选_{vid}_{date.today():%Y%m%d}")
    out.mkdir(parents=True, exist_ok=True)
    log = Log(out / "日志.txt")
    log(f"开始：{video.name}  标题「{title}」  字幕 {subs_in or '无（用识别）'}  输出 {out}")

    # 1. 识别：主字幕用百炼整条直连（直播加 --diarize 分说话人，选段模型会看到 [主播]/[嘉宾]）。
    #    SenseVoice 只在百炼失败时跑、当主字幕
    sv_json = C.ASR / f"{vid}.json"
    asr_json = C.ASR / f"{vid}_bailian{'_spk' if get('live') else ''}.json"
    if a.asr_json:
        asr_json = Path(a.asr_json).resolve()
    if not asr_json.exists():
        try:
            run(py_mod("asr", video, "--engine", "bailian", "--out", asr_json, *(["--diarize"] if get("live") else [])),
                log, timeout=7200)
        except (SystemExit, subprocess.TimeoutExpired) as e:
            log(f"百炼识别失败（{e}），主字幕退回 SenseVoice")
    if not asr_json.exists():
        if not sv_json.exists():
            run(py_mod("asr", video, "--engine", "sensevoice", "--out", sv_json), log)
        asr_json = sv_json
    log(f"主字幕 {asr_json.name}（{json.loads(asr_json.read_text(encoding='utf-8')).get('source')}）")
    asr = json.loads(asr_json.read_text(encoding="utf-8"))["body"]
    total = asr[-1]["to"] if asr else 0
    log(f"识别 {len(asr)} 句，{total / 60:.0f} 分钟")

    # 1b. 整场字幕校对（liveclip.review.subs）：改同音错字，选段/标题、烧录字幕都用校对后的文字；
    #     不带对照字幕。结果按块缓存，重跑不花钱。失败就用没校对的识别字幕接着做（部分块失败时校对文件照样逐行完整，照用）
    main_subs = asr_json
    rev_json = asr_json.with_name(asr_json.stem + "_reviewed.json")
    try:
        out_rev = run(py_mod("review.subs", asr_json, "--out", rev_json), log, timeout=3600)
        log(out_rev.strip().splitlines()[-1] if out_rev.strip() else "字幕校对完成")
    except (SystemExit, subprocess.TimeoutExpired) as e:
        log(f"字幕校对没跑完（{e}），{'用已校对的部分' if rev_json.exists() else '用没校对的识别字幕'}")
    if rev_json.exists():
        main_subs = rev_json
        asr = json.loads(rev_json.read_text(encoding="utf-8"))["body"]

    # 2. 字幕：外来字幕要先对到视频时间轴。外来字幕（人做的 SRT）不再校对：它不是识别出来的，同音错字少，
    #    而且要按原文对齐；对齐用的是识别字幕原文（不影响结果）
    pick_subs, aligned = main_subs, None
    if subs_in:
        sj = C.ASR / f"{vid}_subs.json"
        if subs_in.endswith(".srt"):
            srt_to_json(subs_in, sj)
        else:
            sj = Path(subs_in)
        pick_subs = sj
        aligned = C.ASR / f"{vid}_map.json"
        out_align = run(py_mod("align", sj, asr_json, aligned), log)
        log(out_align.strip().split("\n")[0])

    # 3. 选段（长的分块）
    plan_dir = C.CLIPS / vid
    plan_dir.mkdir(parents=True, exist_ok=True)
    cues = json.loads(Path(pick_subs).read_text(encoding="utf-8"))["body"]
    n_chunks = max(1, round(cues[-1]["to"] / 2700)) if cues[-1]["to"] > 3600 else 1
    note = get("note", "")
    if n_chunks > 1:
        note += "本文件只是全片的一部分，时间是全片的绝对秒数。"
    clips = []
    for i in range(n_chunks):
        sub = pick_subs
        if n_chunks > 1:
            # 相邻分块重叠 5 分钟，跨在分界上的片段两边都有机会完整选到，下面再去重（借鉴 AutoClip 60 分钟窗口 + 4 分钟重叠）
            lo = max(0, i * cues[-1]["to"] / n_chunks - OVERLAP_SEC)
            hi = (i + 1) * cues[-1]["to"] / n_chunks
            sub = C.ASR / f"{vid}_p{i + 1}.json"
            body = json.dumps({"body": [c for c in cues if lo <= c["from"] < hi]}, ensure_ascii=False)
            if not sub.exists() or sub.read_text(encoding="utf-8") != body:   # 内容没变不重写，断点续跑靠修改时间判断
                sub.write_text(body, encoding="utf-8")
        part_f = plan_dir / f"auto_p{i + 1}.json"
        # 断点续跑（借鉴 AutoClip 把模型原始回复落盘重放）：这一块上次已挑过、且字幕没变过，就直接用，不再调 Claude；--fresh 强制重挑
        if not a.fresh and part_f.exists() and part_f.stat().st_mtime > Path(sub).stat().st_mtime:
            log(f"分块 {i + 1}/{n_chunks}：沿用上次挑的 {part_f.name}（要重挑加 --fresh）")
        else:
            cmd = py_mod("pick", vid, "--subs", sub, "--title", title, "--out", f"auto_p{i + 1}.json")
            if get("live"): cmd.append("--live")
            if note: cmd += ["--note", note]
            run(cmd, log, timeout=1800)
        part = json.loads(part_f.read_text(encoding="utf-8"))
        clips += part["clips"]
        log(f"分块 {i + 1}/{n_chunks}：{len(part['clips'])} 段")
    clips.sort(key=lambda c: (-c["score"], c["start"]))
    kept = []                     # 重叠区两边选到同一段：重叠超过较短那段一半算同一段，留分高的
    for c in clips:
        if all(min(c["end"], k["end"]) - max(c["start"], k["start"]) <= 0.5 * min(c["end"] - c["start"], k["end"] - k["start"])
               for k in kept):
            kept.append(c)
    if len(kept) < len(clips):
        log(f"分块重叠去重：{len(clips)} → {len(kept)} 段")
    clips = kept

    # 字幕时间 → 视频时间，并用识别文本核对每段
    if aligned:
        from .align import to_video
        anc = json.loads(aligned.read_text(encoding="utf-8"))["anchors"]
        for c in clips:
            c["srt_start"], c["srt_end"] = c["start"], c["end"]
            vs, _ = to_video(c["start"], anc)
            d = vs - c["start"]
            c["start"], c["end"] = round(vs, 3), round(c["end"] + d, 3)
            if c.get("hook_start") is not None:
                c["hook_start"], c["hook_end"] = round(c["hook_start"] + d, 3), round(c["hook_end"] + d, 3)
    for c in clips:
        heard = norm("".join(q["content"] for q in asr if c["start"] - 1 <= q["from"] <= c["end"] + 1))
        c["align_check"] = round(difflib.SequenceMatcher(None, norm(c.get("text", "")), heard).ratio(), 2)
    good = [c for c in clips if c["align_check"] >= 0.5]
    log(f"候选 {len(clips)} 段，位置核对通过 {len(good)} 段")

    if get("auto_layout"):
        for c in good:
            c.update(detect_layout(video, c))
        log("画面检测：" + "，".join(f"{i + 1}{'分屏' if c['layout'] == 'duet_v' else '单人'}{'⚠' if c.get('layout_note') else ''}"
                                  for i, c in enumerate(good)))
    from .cut import probe_size
    sw, sh = probe_size(video)
    layout = get("layout") or ("live_v" if get("live") else ("talk_h" if sw >= sh else "full_v"))
    plan = {"bv": vid, "title": title, "model": C.model("pick"), "video": str(video), "subs": C.rel(pick_subs),
            "layout": layout, "live": bool(get("live")), "burn_subs": bool((get("burn_subs") or get("live")) and not a.no_burn_subs), "zoom": False,
            "cover_probe": {"x": 0, "y": 100, "w": 600, "h": 600, "min_mean": 110}, "clips": good, "typo_fixes": [],
            "note": f"auto_clip {date.today()} 自动生成，未音画复核"}
    if get("cover_frame"):
        plan["cover_frame"] = {"series": title, "box": [40, 100, 1842, 760]}
    pf = plan_dir / "auto.json"
    pf.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    # 4. 剪辑 + 封面
    n = min(top, len(good))
    if n == 0:
        log("没有通过的候选，不出片（看 clips/<ID>/auto_p*.json 的 dropped 了解原因）")
        (out / "候选清单.txt").write_text(f"{title}：0 条候选（{date.today()}）\n", encoding="utf-8")
        print(str(out))
        return
    only = ",".join(str(i) for i in range(1, n + 1))
    run(py_mod("hook", vid, "auto.json", "--out", "auto.json", "--only", only), log, timeout=1800)
    good = json.loads(pf.read_text(encoding="utf-8"))["clips"]      # 预告专项重挑后的计划
    run(py_mod("cut", vid, "--plan", "auto.json", "--only", only, "--allow-check"), log, timeout=7200)
    mp4s = sorted((plan_dir / "auto").glob("*.mp4"))[:n]
    log(f"出片 {len(mp4s)} 条")
    cover_dir = Path(a.cover_dir).resolve() if a.cover_dir else C.DELIVER / "_covers" / vid
    run(py_mod("cover.make", *mp4s, "--out", cover_dir, "--bili"), log, timeout=3600)
    run(py_mod("verify", out / "验收.json", out / "抽帧", *mp4s), log, timeout=3600)
    bad = [r for r in json.loads((out / "验收.json").read_text(encoding="utf-8")) if r["full_decode"] != "ok"]
    log(f"完整解码：{len(mp4s) - len(bad)}/{len(mp4s)} 通过")
    qa = run(py_mod("qa", vid, "auto.json", "--only", only), log, timeout=3600)
    for line in qa.strip().splitlines():
        if line[:1] in "✓⚠✗" or line.startswith("质检"):
            log(line)
    # 质检还报切脸 ✗ 的段：改整幅重剪一次、重出封面、再质检（剪之前的预检漏掉的兜底）
    rep = json.loads((plan_dir / "auto" / "质检.json").read_text(encoding="utf-8"))
    redo = [r["clip"] for r in rep if r.get("切脸", {}).get("result") == "✗" and r.get("layout") != "full_v"]
    if redo:
        log(f"切脸返工：第 {','.join(map(str, redo))} 段改整幅重剪")
        for i in redo:
            c = good[i - 1]
            for k in ("layout", "v_crop", "v_y", "cap_y"):
                c.pop(k, None)
            c.update(SOLO)
            c["layout_note"] = (c.get("layout_note", "") + " 质检切脸，已改整幅重剪").strip()
        plan["clips"] = good
        pf.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        only_redo = ",".join(map(str, redo))
        run(py_mod("cut", vid, "--plan", "auto.json", "--only", only_redo, "--allow-check"), log, timeout=7200)
        redo_mp4 = [m for m in mp4s if int(m.name[:2]) in redo]
        run(py_mod("cover.make", *redo_mp4, "--out", cover_dir, "--bili"), log, timeout=3600)
        qa = run(py_mod("qa", vid, "auto.json"), log, timeout=3600)
        for line in qa.strip().splitlines():
            if line.startswith("质检"):
                log("返工后" + line)
    shutil.copy2(plan_dir / "auto" / "质检.json", out / "质检.json")

    # 4b. 精剪（--tight）：在正常版（含切脸返工）基础上，同样的 plan、版式和预告
    tight = {}
    if get("tight"):
        tdir = plan_dir / "auto_tight"
        for m in mp4s:          # 先删掉同名旧精剪片，免得这次失败时误用上次的
            (tdir / m.name).unlink(missing_ok=True)
        tout = run(py_mod("tight", vid, "auto.json", "--only", only, "--allow-check"), log, timeout=7200)
        for line in tout.strip().splitlines():
            if ".mp4" in line or line.startswith(("第 ", "问 Claude", "精剪总用时")):
                log("精剪 " + line)
        probe = lambda f: float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                                str(f)], capture_output=True, text=True).stdout)
        for m in mp4s:
            tm = tdir / m.name
            if tm.exists() and tm.with_suffix(".json").exists():
                info = json.loads(tm.with_suffix(".json").read_text(encoding="utf-8"))
                tight[m.stem] = {"mp4": tm, "before": probe(m), "after": probe(tm), "info": info}
        if tight:
            log("精剪前后时长：" + "，".join(f"{k[:2]} {v['before']:.0f}→{v['after']:.0f}s" for k, v in tight.items())
                + f"；合计 {sum(v['before'] for v in tight.values()):.0f}→{sum(v['after'] for v in tight.values()):.0f}s")
        missing = [m.stem[:2] for m in mp4s if m.stem not in tight]
        if missing:
            log(f"精剪没出片的段：{','.join(missing)}，交付里用正常版")
        if tight:
            tms = [v["mp4"] for v in tight.values()]
            run(py_mod("verify", out / "验收_精剪.json", out / "抽帧_精剪", *tms), log, timeout=3600)
            tbad = [r for r in json.loads((out / "验收_精剪.json").read_text(encoding="utf-8")) if r["full_decode"] != "ok"]
            log(f"精剪完整解码：{len(tms) - len(tbad)}/{len(tms)} 通过")
            for r in tbad:      # 解不完的精剪片不交付
                tight.pop(Path(r.get("file", "")).stem, None)
        if tight:
            tq = run(py_mod("qa", vid, "auto.json", "--only", ",".join(str(int(k[:2])) for k in tight), "--tight"),
                     log, timeout=3600)
            for line in tq.strip().splitlines():
                if line[:1] in "✓⚠✗" or line.startswith("质检"):
                    log("精剪" + line)
            shutil.copy2(tdir / "质检.json", out / "质检_精剪.json")

    # 5. 输出包
    for old in out.glob("[0-9][0-9]_*"):
        shutil.rmtree(old)
    for k, m in enumerate(mp4s, 1):
        c = good[k - 1]
        stem = m.stem.split("_", 1)[1]
        d = out / f"{k:02d}_{stem}"
        d.mkdir()
        if m.stem in tight and a.tight_separate:      # 跟播的约定：视频.mp4 正常版 + 视频_精剪.mp4
            shutil.copy2(m, d / "视频.mp4")
            shutil.copy2(tight[m.stem]["mp4"], d / "视频_精剪.mp4")
        elif m.stem in tight:
            shutil.copy2(tight[m.stem]["mp4"], d / "视频.mp4")
            shutil.copy2(m, d / "视频_原版.mp4")
        else:
            shutil.copy2(m, d / "视频.mp4")
        cov = cover_dir / f"{m.stem}.jpg"
        if cov.exists():
            shutil.copy2(cov, d / "封面.jpg")
        if (cover_dir / f"{m.stem}_B站.jpg").exists():
            shutil.copy2(cover_dir / f"{m.stem}_B站.jpg", d / "封面_B站.jpg")
        if (cover_dir / f"{m.stem}_4比3.jpg").exists():
            shutil.copy2(cover_dir / f"{m.stem}_4比3.jpg", d / "封面_横4比3.jpg")
        (d / "文案.txt").write_text(caption_text(c, tags + (["直播"] if get("live") else []), a.source), encoding="utf-8")
    hms = lambda s: f"{int(s) // 3600}:{int(s) % 3600 // 60:02d}:{int(s) % 60:02d}"
    lines = [f"{title}：{len(clips)} 条候选（{date.today()}，按分数排序；前 {n} 条已出片，均未音画复核；✗ = 位置核对不通过）", ""]
    for i, c in enumerate(clips, 1):
        lines.append(f"{i:2d}. {'✓' if c['align_check'] >= 0.5 else '✗'} [{c['score']}分]{'⚠' if c.get('risky') else ' '} "
                     f"{hms(c['start'])}  {int(c['end'] - c['start'])}s  {c['title'].replace(chr(10), ' ')}  —— {c.get('why', '')[:80]}")
    if tight:
        lines += ["", f"精剪（--tight，liveclip.tight）：{len(tight)} 条，交付文件夹里 "
                      + ("视频.mp4 是正常版，视频_精剪.mp4 是精剪版；" if a.tight_separate else "视频.mp4 是精剪版，视频_原版.mp4 是正常版；")
                      + "删改明细见 clips/" + vid + "/auto_tight/同名.json"]
        for k, v in tight.items():
            inf = v["info"]
            lines.append(f"  {k[:2]}. 原版 {v['before']:.0f}s → 精剪 {v['after']:.0f}s（-{(1 - v['after'] / v['before']) * 100:.0f}%），"
                         f"Claude 删 {len(inf['删除'])} 处，语气词 {inf['自动删除']['语气词']} 个，对方附和 {len(inf['自动删除']['附和'])} 行"
                         f"{'，没采用 ' + str(len(inf['没采用'])) + ' 处（否定字保护）' if inf['没采用'] else ''}  {k[3:]}")
    (out / "候选清单.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    run(py_mod("album", out, pf, f"{title} · 候选 {n} 条"), log)
    log(f"完成：{out}")
    print(str(out))


if __name__ == "__main__":
    main()
