"""成片自动质检：切脸、字幕/标题挡脸、起止切在句中、音频时间戳、音画同步。借鉴 AutoClip studio/qa。
用法: liveclip qa <ID> <plan.json> [--only 1,2] [--frames 12] [--tight] [--out 文件]
成片按 liveclip.cut 的命名在 clips/<ID>/<plan 名>/NN_*.mp4。结果写 clips/<ID>/<plan 名>/质检.json（--out 指定别的文件，复查时不动原结果），终端打印每段结论。
人脸相关的检查需要人脸检测后端（liveclip.vision：macOS Vision / OpenCV）；没有时这几项标「跳过」。
--tight：查精剪成片 clips/<ID>/<plan 名>_tight/NN_*.mp4。成片不是 [预告][正文] 两段连续，而是 tight_edit 存在同名 .json 里的
  「保留段」依次拼接：成片时间按保留段换算回原片；音画同步只在整段落在同一个保留段里的 1 秒窗口上测（不跨接缝）；
  切在句中查的是精剪取字的窗口边缘（同正常出片的起止），整句在窗口里的字都被删掉的不算。

检查项（成片 1080x1920 坐标）：
- 切脸：均匀抽帧找脸框；脸框伸出画面区（make_layout 的 fg_y/fg_h，铺满时是整幅）
  超过脸高 8%，或贴着左右边，算这一帧切脸。单人画面套分屏裁切切掉半张脸、人往前凑时下巴被切，都是这种。
- 字幕挡嘴（加 --mouth 才查）：这一帧正在显示字幕（按 liveclip.cut 的时间映射：先预告、再正文），字幕区盖住嘴到下巴（脸框下 45%）超过 35%；≥2 帧给 ⚠。
  默认不查：竖屏放不下标题 + 自拍大脸 + 两行字幕 + 底部 384px 安全区，字幕压嘴是可接受的取舍。
- 标题挡脸：按 overlay_png 重画一张标题层，取不透明的字（alpha>200），和脸框重叠超过脸高 10%。
- 没脸：一半以上的帧找不到脸，多半是取景错了或人离开了画面，提醒人看。
- 切在句中：实际剪的起止点（cut_clips.bounds，含前后余量）落在一句字幕中间（这句在起止点前后都超过 0.25 秒）。
- 音画同步：成片 15%/50%/80% 处 1 秒音频和原片对应位置互相关（±0.3s 内找，相关 <0.35 不算）得声音偏移；同样三处各 2 秒画面
  逐帧对原片原生帧得画面偏移（video_offset，按换帧时刻取中位数）；两者中位数之差 >45ms ✗。
- 开头有脸：成片 0.3s、1.2s 两帧都没脸给 ⚠（预告开头要有正脸）。
- 音频时间戳：成片 AAC 相邻包的 pts 差应全部是 1024（±1 是时间基舍入）；出现只占几个采样的包就是 10-09 剪映错位那个 bug。
同一问题 ≥2 帧算 ✗，1 帧算 ⚠。只报告，不改成片。
"""
import argparse
import json
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

from . import vision
from .config import C
from .cut import (CLIP_LAYOUT_KEYS, W, H, bounds, clean_caption, find_video, make_layout,
                  manifest_row, overlay_png)

ROOT = C.ROOT
EDGE = 8          # 脸框离画面区边缘多少像素以内算碰边
CAP_H = 160       # 字幕最多两行：62 号字，行距 80，加描边


def faces(pngs):
    rows = {}
    for r in vision.detect(pngs) or []:
        rows[r["file"]] = [(x * W, y * H, w * W, h * H) for x, y, w, h in r.get("boxes", [])]
    return [rows.get(str(p), []) for p in pngs]


def title_box(title, lay, tmp):
    """标题字的外框（不含铺满时那层暗渐变）。"""
    import numpy as np
    from PIL import Image
    p = Path(tmp) / "title.png"
    overlay_png(title, p, lay["title_y"], shade=lay["full"])
    a = np.array(Image.open(p))[:, :, 3]
    ys, xs = np.nonzero(a > 200)
    return (xs.min(), ys.min(), xs.max(), ys.max()) if len(xs) else None


def overlap_h(box, y0, y1, x0=0, x1=W):
    """脸框和区域 [x0,x1]×[y0,y1] 的重叠高度占脸高的比例（左右不重叠时为 0）。"""
    x, y, w, h = box
    if min(x + w, x1) <= max(x, x0):
        return 0.0
    return max(0.0, min(y + h, y1) - max(y, y0)) / h if h else 0.0


def source_time(t, b):
    """成片时间 → 原片时间（cut_clips：有预告时先放预告 h0..h1，再放正文 start..end）。"""
    start, end, h0, h1 = b
    if h0 is not None:
        hl = h1 - h0
        return h0 + t if t < hl else start + (t - hl)
    return start + t


def seg_map(segs, dur=None):
    """精剪成片：保留段 [(原片起, 止)] 依次拼接。返回 成片时间 → 原片时间。
    tight_edit 存的保留段已对齐到剪辑的帧格（cut_clips.snap_segs），成片时长应等于各段之和；
    给了成片时长 dur 就把剩下的差额（编码尾帧等，几十毫秒）平摊到各段末尾。"""
    extra = (dur - sum(e - s for s, e in segs)) / len(segs) if dur else 0.0

    def f(t):
        acc = 0.0
        for s, e in segs:
            if t < acc + (e - s) + extra:
                return min(e, s + (t - acc))
            acc += e - s + extra
        return segs[-1][1]
    return f


def steady_times(segs, dur, need=1.4):
    """15%/50%/80% 附近、1 秒窗口整个落在同一个保留段里的取样时间（窗口跨接缝时互相关没意义）。"""
    extra = (dur - sum(e - s for s, e in segs)) / len(segs)     # 同 seg_map
    spans, acc = [], 0.0
    for s, e in segs:
        spans.append((acc, acc + e - s))
        acc += e - s + extra
    long_ = [(a, b) for a, b in spans if b - a >= need]
    out = []
    for frac in (0.15, 0.5, 0.8):
        if not long_:
            break
        t = dur * frac
        a_, b_ = min(long_, key=lambda x: 0 if x[0] + 0.15 <= t <= x[1] - 1.1 else min(abs(t - x[0]), abs(t - x[1] + 1.1)))
        t = round(min(max(t, a_ + 0.15), b_ - 1.1), 3)
        if t not in out:
            out.append(t)
    return out


def straddles(cues, edge):
    return [q for q in cues if q["from"] < edge - 0.25 and q["to"] > edge + 0.25]


def pcm(path, t, dur, sr=16000):
    import numpy as np
    out = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(0, t):.3f}", "-t", f"{dur:.3f}", "-i", str(path), "-vn",
                          "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"], capture_output=True).stdout
    return np.frombuffer(out, np.int16).astype(float)


def av_offset(mp4, video, b, dur, src_t=None, times=None):
    """成片 15%/50%/80% 处各取 1 秒音频，在原片对应位置 ±0.3 秒里做互相关找偏移（借鉴 AutoClip qa/avsync）。
    相关系数 <0.35 的窗口不算。返回各窗口的音频偏移毫秒。src_t/times：精剪成片的时间换算和取样时间。"""
    import numpy as np
    sr, lags = 16000, []
    src_t = src_t or (lambda t: source_time(t, b))
    for t in (times if times is not None else [dur * frac for frac in (0.15, 0.5, 0.8)]):
        a = pcm(mp4, t, 1.0, sr)
        src = pcm(video, src_t(t) - 0.3, 1.6, sr)
        if len(a) < sr // 2 or len(src) < len(a):
            continue
        a = a - a.mean()
        best, best_r = None, 0.0
        for k in range(0, len(src) - len(a) + 1, 4):            # 0.25ms 步长
            w = src[k:k + len(a)] - src[k:k + len(a)].mean()
            den = np.sqrt((a * a).sum() * (w * w).sum())
            r = float((a * w).sum() / den) if den else 0.0
            if r > best_r:
                best, best_r = k, r
        if best is not None and best_r >= 0.35:
            lags.append(round((best / sr - 0.3) * 1000))
    return lags


def frames_pts(path, ss, dur, vf, w_, h_):
    """[ss, ss+dur) 里每一帧（原生帧，不补帧）的灰度小图和真实时间戳（-copyts，和文件时间轴一致）。"""
    import re
    import numpy as np
    r = subprocess.run(["ffmpeg", "-v", "info", "-ss", f"{max(0, ss):.3f}", "-copyts", "-t", f"{dur:.3f}", "-i", str(path), "-an",
                        "-vf", f"{vf},scale={w_}:{h_},format=gray,showinfo", "-fps_mode", "passthrough",
                        "-f", "rawvideo", "-"], capture_output=True)
    fr = np.frombuffer(r.stdout, np.uint8)
    fr = fr[:len(fr) // (w_ * h_) * w_ * h_].reshape(-1, w_ * h_).astype(float)
    pts = [float(x) for x in re.findall(rb"pts_time:\s*([-0-9.]+)", r.stderr)][:len(fr)]
    return np.array(pts), fr[:len(pts)]


def video_offset(mp4, video, b, lay, t, src_t=None, win=2.0):
    """成片 [t, t+win] 的画面中段（避开标题、字幕）逐帧和原片对应位置的原生帧比对，返回画面偏移毫秒（算不出返回 None）。
    做法：成片每帧在原片期望位置 ±0.5 秒里找最像的原片帧；每个原片帧第一次出现在成片里的时刻，
    换算回原片时间和这一帧自己的时间戳比，取中位数。
    2026-10-14 改：旧做法只拿成片 1 帧去对 fps=100 补帧后的原片，连麦分屏段原片只有约 15 帧（间隔 67ms、最长 180ms，
    远端画面还常重复帧），人小、动作少，单帧最像的那帧能差出 ±100–200ms（同一段 3 个点测出 [30, -150, 40]），
    一晚 20 条 duet_v 误报 5 条 ✗；补帧后取最像的又总落在重复帧的第一帧，系统性偏早半帧。
    新做法每个点用几十个换帧时刻求中位数，一场连麦直播 25 条和音频偏移差都在 ±11ms 内。"""
    import numpy as np
    sw, sh = lay["src"]
    cx, cy, cw, ch = lay["crop"]
    y0 = 0 if lay["full"] else lay["fg_y"]
    fh = H if lay["full"] else lay["fg_h"]
    r0, r1 = 0.30, 0.62                                              # 画面区里这一段高度没有叠字
    w_, h_ = 128, 64
    st_of = src_t or (lambda x: source_time(x, b))
    cpts, cfr = frames_pts(mp4, t, win, f"crop={W}:{int(fh * (r1 - r0))}:0:{int(y0 + fh * r0)}", w_, h_)
    if len(cfr) < 5:
        return None
    s0 = st_of(t) - 0.6
    crop = f"crop={int(sw * cw)}:{int(sh * ch * (r1 - r0))}:{int(sw * cx)}:{int(sh * (cy + ch * r0))}"
    spts, sfr = frames_pts(video, s0, win + 1.2, crop, w_, h_)
    if len(sfr) < 5:
        return None
    first = {}
    for tp, f in zip(cpts, cfr):
        exp = st_of(tp)
        idx = np.nonzero(np.abs(spts - exp) < 0.5)[0]
        if len(idx) < 3:
            continue
        err = ((sfr[idx] - f) ** 2).mean(1)
        o = np.argsort(err)
        # 太不像（取景/时间对不上），或和第二像的帧几乎一样（画面不动、原片重复帧），这帧不算
        if err[o[0]] > 400 or err[o[1]] - err[o[0]] < 3:
            continue
        sp = float(spts[idx[o[0]]])
        first.setdefault(sp, exp)
    if len(first) < 5:                                               # 这段画面几乎不动，算不出来
        return None
    # 原片帧 sp 第一次出现时对应的原片时间 exp：同步时 exp≈sp（fps=30 取整，±17ms 内）；exp 比 sp 大 = 画面落后
    return round(float(np.median([sp - exp for sp, exp in first.items()])) * 1000)


def audio_ok(mp4):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "packet=pts",
                          "-of", "csv=p=0", str(mp4)], capture_output=True, text=True).stdout.split()
    pts = [int(x.strip(",")) for x in out if x.strip(",").lstrip("-").isdigit()]
    d = [b - a for a, b in zip(pts, pts[1:])]
    if not d:
        return True, "没有音频包"
    common = Counter(d).most_common(1)[0][0]
    bad = sum(abs(x - common) > 2 for x in d)       # 时间基换算有 ±1 的舍入（1023/1025 成对出现），不算错
    return bad == 0, f"包间隔 {common}" + (f"，{bad} 处不同" if bad else "")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="liveclip qa", description="automatic QA of rendered clips")
    ap.add_argument("bv"); ap.add_argument("plan")
    ap.add_argument("--only"); ap.add_argument("--frames", type=int, default=12)
    ap.add_argument("--mouth", action="store_true", help="也查字幕挡嘴（默认不查：接受字幕压嘴）")
    ap.add_argument("--tight", action="store_true", help="查精剪成片（<plan 名>_tight/，按 tight_edit 存的保留段换算时间）")
    ap.add_argument("--out", help="结果写到这个文件（默认 <成片目录>/质检.json，同样按片段编号合并）")
    a = ap.parse_args(argv)
    has_faces = vision.available()
    plan = json.loads((C.CLIPS / a.bv / a.plan).read_text(encoding="utf-8"))
    outdir = C.CLIPS / a.bv / (Path(a.plan).stem + ("_tight" if a.tight else ""))
    video = find_video(a.bv, plan)
    role = manifest_row(a.bv).get("role")
    live = plan.get("live", role == "livestream_recording")
    burn = plan.get("burn_subs", True)
    cues_all = json.loads(C.ws(plan["subs"]).read_text(encoding="utf-8"))["body"] if plan.get("subs") else []
    only = {int(x) for x in a.only.split(",")} if a.only else None
    report = []
    for i, c in enumerate(plan["clips"], 1):
        if only and i not in only:
            continue
        mp4s = sorted(outdir.glob(f"{i:02d}_*.mp4"))
        if not mp4s:
            continue
        mp4 = mp4s[0]
        over = {k: c[k] for k in CLIP_LAYOUT_KEYS if k in c}
        lay = make_layout(video, {**plan, **over}, None, role)
        b = bounds(video, c, snap=not live, cues=cues_all or None)
        cues = ([{"from": x["from"], "to": x["to"], "content": x["text"]} for x in c["captions"]]
                if c.get("captions") else cues_all)
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(mp4)],
                                   capture_output=True, text=True).stdout)
        src_t = times = words = None
        if a.tight:
            side = mp4.with_suffix(".json")
            if not side.exists():
                print(f"? {mp4.name}  没有精剪明细 {side.name}，跳过", flush=True)
                continue
            tinfo = json.loads(side.read_text(encoding="utf-8"))
            segs = tinfo["保留段"]["预告"] + tinfo["保留段"]["正片"]
            src_t, times, words = seg_map(segs, dur), steady_times(segs, dur), tinfo.get("词", [])
            b = tuple(tinfo.get("起止") or b)
        y0, y1 = (0, H) if lay["full"] else (lay["fg_y"], lay["fg_y"] + lay["fg_h"])
        issues = {"切脸": [], "字幕挡嘴": [], "标题挡脸": [], "没脸": []}
        h_cropped = lay["crop"][2] < 0.98
        with tempfile.TemporaryDirectory() as tmp:
            ts = [0.5 + (dur - 2.5) * k / (a.frames - 1) for k in range(a.frames)]   # 避开最后的尾卡
            pngs = []
            for k, t in enumerate(ts):
                p = Path(tmp) / f"f{k:02d}.png"
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(mp4), "-frames:v", "1", str(p)])
                pngs.append(p)
            heads = []
            for k, t in enumerate((0.3, 1.2)):
                p = Path(tmp) / f"h{k}.png"
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(mp4), "-frames:v", "1", str(p)])
                heads.append(p)
            first_face = any(f for f in faces(heads))
            tb = title_box(c["title"], lay, tmp)
            for t, fs in zip(ts, faces(pngs)):
                fs = [f for f in fs if f[1] + f[3] > y0 and f[1] < y1]       # 画面区外的不算（标题区的图形）
                if not fs:
                    issues["没脸"].append(round(t, 1)); continue
                st = src_t(t) if src_t else source_time(t, b)
                showing = burn and any(q["from"] <= st <= q["to"] and clean_caption(q["content"]) for q in cues)
                for x, y, w, h in fs:
                    # 脸框伸出画面区上下边的部分占脸高（Vision 的框比下巴略大，8% 以内不算）。左右贴边只在我们左右裁过时算：
                    # 手机自拍、连麦分屏里脸本来就顶着原片左右边，不是裁切造成的（10/6 上 实测）
                    out_ = max(y0 - y, y + h - y1) / h
                    side = h_cropped and (x < EDGE or x + w > W - EDGE)
                    if out_ > 0.08 or side:
                        issues["切脸"].append(round(t, 1)); break
                for x, y, w, h in fs:
                    # 字幕盖住嘴到下巴（脸框下 45%）超过 35%
                    if a.mouth and showing and overlap_h((x, y + 0.55 * h, w, 0.45 * h), lay["cap_y"] - 8, lay["cap_y"] + CAP_H, 70, W - 70) > 0.35:
                        issues["字幕挡嘴"].append(round(t, 1)); break
                for f in fs:
                    if tb and overlap_h(f, tb[1], tb[3], tb[0], tb[2]) > 0.1:
                        issues["标题挡脸"].append(round(t, 1)); break
        res = {"clip": i, "file": mp4.name, "layout": lay["name"]}
        for k, v in issues.items():
            if not has_faces:
                res[k] = {"result": "✓", "frames": [], "detail": "跳过（没有人脸检测后端）"}
            elif k == "没脸":
                res[k] = {"result": "⚠" if len(v) * 2 > a.frames else "✓", "frames": v}
            elif k == "字幕挡嘴":   # 构图取舍，只提醒
                res[k] = {"result": "⚠" if len(v) >= 2 else "✓", "frames": v}
            else:
                res[k] = {"result": "✗" if len(v) >= 2 else ("⚠" if v else "✓"), "frames": v}
        cuts = []
        for name, edge in (("正文开头", b[0]), ("正文结尾", b[1]), ("预告开头", b[2]), ("预告结尾", b[3])):   # 实际剪的起止（含前后余量）
            if edge is not None:
                qs = straddles(cues, edge)
                if words is not None:   # 精剪：这句在窗口里的字全删掉了，成片里根本没有这句，不算
                    inside = (lambda q, e=edge: (e, q["to"])) if name.endswith("开头") else (lambda q, e=edge: (q["from"], e))
                    qs = [q for q in qs if any(k and inside(q)[0] <= s <= inside(q)[1] for _, s, _, k in words)]
                cuts += [f"{name}切在「{q['content']}」中间" for q in qs]
        res["切在句中"] = {"result": "⚠" if cuts else "✓", "detail": cuts}
        ok, msg = audio_ok(mp4)
        res["音频时间戳"] = {"result": "✓" if ok else "✗", "detail": msg}
        # 音画同步 = 音频偏移 - 画面偏移（只比音频的话，整段起点差几十毫秒也会误报，AutoClip 的检查有这个盲点）。
        # 偏移为负 = 成片里这一刻放的是原片更早的内容 = 落后；差为负即声音比画面晚。人为把声音推后 150ms 测得 -162ms
        if a.tight:
            a_lags = av_offset(mp4, video, b, dur, src_t, times)
            v_lags = [v for v in (video_offset(mp4, video, b, lay, t, src_t, win=1.0) for t in times) if v is not None]
        else:
            a_lags = av_offset(mp4, video, b, dur)
            v_lags = [v for v in (video_offset(mp4, video, b, lay, dur * f) for f in (0.15, 0.5, 0.8)) if v is not None]
        if a.tight and not times:
            res["音画同步"] = {"result": "✓", "detail": "精剪成片没有 1.4 秒以上的整段，测不了，跳过"}
        elif a_lags and v_lags:
            import statistics
            gap = statistics.median(a_lags) - statistics.median(v_lags)
            res["音画同步"] = {"result": "✗" if abs(gap) > 45 else "✓",
                            "detail": f"声音比画面{'晚' if gap < 0 else '早'} {abs(gap):.0f}ms（音频 {a_lags}，画面 {v_lags}）"}
        else:
            res["音画同步"] = {"result": "✓", "detail": "测不出（画面不动或音频太静），跳过"}
        res["开头有脸"] = {"result": "✓" if (first_face or not has_faces) else "⚠",
                        "detail": "跳过（没有人脸检测后端）" if not has_faces else
                        ("" if first_face else "开头 0.3s、1.2s 两帧都没找到脸（高播片 1.5 秒内基本都有正脸）")}
        res["结论"] = "✗" if any(v.get("result") == "✗" for v in res.values() if isinstance(v, dict)) else \
            ("⚠" if any(v.get("result") == "⚠" for v in res.values() if isinstance(v, dict)) else "✓")
        report.append(res)
        bad = [f"{k}{v['result']}{(' ' + str(v.get('frames') or v.get('detail'))) if v['result'] != '✓' else ''}"
               for k, v in res.items() if isinstance(v, dict) and v.get("result") != "✓"]
        print(f"{res['结论']} {mp4.name}  {lay['name']}  " + ("；".join(bad) if bad else "全部通过"), flush=True)
    # 和已有结果合并（按片段编号）：跟播每轮只质检新片段，以前直接覆盖会把前几轮的结果丢掉
    old = Path(a.out) if a.out else outdir / "质检.json"
    merged = {r["clip"]: r for r in (json.loads(old.read_text(encoding="utf-8")) if old.exists() else [])}
    merged.update({r["clip"]: r for r in report})
    old.write_text(json.dumps(sorted(merged.values(), key=lambda r: r["clip"]), ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"质检：{sum(r['结论'] == '✓' for r in report)}/{len(report)} 全部通过，"
          f"{sum(r['结论'] == '✗' for r in report)} 条有 ✗ → {old}")


if __name__ == "__main__":
    main()
