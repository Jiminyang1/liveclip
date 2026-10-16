"""字幕文件和视频不是同一个剪辑版本时，用视频的语音识别结果把「字幕时间」对到「视频时间」。

用法:
    liveclip align data/asr/X_subs.json data/asr/X_bailian.json data/asr/X_map.json [--apply clips/X/clips.json --out clips_video.json]

做法：字幕每句和识别每句做字符 4-gram 粗筛 + 相似度精算，得到一批「字幕时间 → 视频时间」锚点；
偏移（视频时间 - 字幕时间）在同一连续段内是常数，按锚点分段。--apply 时把计划里的 start/end/hook 按最近锚点的偏移换算，
片段跨了两个不同偏移的段就标 "align_warning"，剪之前要人看。
"""
import argparse
import difflib
import json
import re
from collections import defaultdict
from pathlib import Path

norm = lambda s: re.sub(r"[\W_]", "", s).lower()


def anchors(subs, asr):
    grams = defaultdict(set)
    for j, q in enumerate(asr):
        t = norm(q["content"])
        for k in range(len(t) - 3):
            grams[t[k:k + 4]].add(j)
    out = []
    for i, q in enumerate(subs):
        t = norm(q["content"])
        if len(t) < 6:
            continue
        hits = defaultdict(int)
        for k in range(len(t) - 3):
            for j in grams.get(t[k:k + 4], ()):
                hits[j] += 1
        cands = sorted(hits, key=lambda j: -hits[j])[:5]
        best, score = None, 0
        for j in cands:
            r = difflib.SequenceMatcher(None, t, norm(asr[j]["content"])).ratio()
            if r > score:
                best, score = j, r
        if best is not None and score >= 0.7:
            out.append({"sub": q["from"], "video": asr[best]["from"], "score": round(score, 2), "text": q["content"]})
    # 去掉和前后锚点偏移都不一致的孤立点（识别到相似但不是同一处的句子）
    offs = [a["video"] - a["sub"] for a in out]
    keep = []
    for k, a in enumerate(out):
        near = [offs[m] for m in range(max(0, k - 3), min(len(out), k + 4)) if m != k]
        if sum(abs(offs[k] - o) < 4 for o in near) >= len(near) // 2 + 1:
            keep.append(a)
    return keep


def segments(anc):
    """把锚点按偏移连续性切成段：[{sub_from, sub_to, video_from, offset, n}]"""
    segs = []
    for a in anc:
        off = a["video"] - a["sub"]
        if segs and abs(off - segs[-1]["offset"]) < 4 and a["sub"] - segs[-1]["sub_to"] < 180:
            s = segs[-1]
            s["sub_to"], s["n"] = a["sub"], s["n"] + 1
            s["offset"] = s["offset"] + (off - s["offset"]) / s["n"]
        else:
            segs.append({"sub_from": a["sub"], "sub_to": a["sub"], "video_from": a["video"], "offset": off, "n": 1})
    return segs


def to_video(t, anc):
    """字幕时间 → 视频时间：取最近锚点的偏移。返回 (视频时间, 锚点距离秒)。"""
    a = min(anc, key=lambda x: abs(x["sub"] - t))
    return t + (a["video"] - a["sub"]), abs(a["sub"] - t)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="liveclip align")
    ap.add_argument("subs")
    ap.add_argument("asr")
    ap.add_argument("map_out")
    ap.add_argument("--apply")
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    subs = json.loads(Path(args.subs).read_text(encoding="utf-8"))["body"]
    asr = json.loads(Path(args.asr).read_text(encoding="utf-8"))["body"]
    anc = anchors(subs, asr)
    segs = segments(anc)
    big = [s for s in segs if s["n"] >= 5]
    Path(args.map_out).write_text(json.dumps({"anchors": anc, "segments": segs}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"字幕 {len(subs)} 句，识别 {len(asr)} 句，锚点 {len(anc)} 个，≥5 锚点的连续段 {len(big)} 段：")
    for s in big:
        print(f"  字幕 {s['sub_from']:7.0f}–{s['sub_to']:7.0f}s → 视频偏移 {s['offset']:+7.1f}s（{s['n']} 个锚点）")
    if args.apply:
        plan = json.loads(Path(args.apply).read_text(encoding="utf-8"))
        for i, c in enumerate(plan["clips"], 1):
            c["srt_start"], c["srt_end"] = c["start"], c["end"]
            (vs, ds), (ve, de) = to_video(c["start"], anc), to_video(c["end"], anc)
            warn = []
            if abs((ve - c["end"]) - (vs - c["start"])) > 2:
                warn.append(f"起止落在不同偏移段：{vs - c['start']:+.1f} / {ve - c['end']:+.1f}")
            if max(ds, de) > 30:
                warn.append(f"最近锚点距离 {max(ds, de):.0f}s")
            c["start"], c["end"] = round(vs, 3), round(vs + (c["srt_end"] - c["srt_start"]), 3)
            if c.get("hook_start") is not None:
                c["hook_start"] = round(c["hook_start"] + (vs - c["srt_start"]), 3)
                c["hook_end"] = round(c["hook_end"] + (vs - c["srt_start"]), 3)
            c["align_offset"] = round(vs - c["srt_start"], 1)
            if warn:
                c["align_warning"] = "；".join(warn)
            print(f"{i:2d}. 字幕 {c['srt_start']:7.0f}s → 视频 {c['start']:7.0f}s（{c['align_offset']:+.1f}s）{'  ⚠ ' + c['align_warning'] if warn else ''}  {c['title'].replace(chr(10), ' ')}")
        plan["subs_aligned"] = args.asr
        out = Path(args.apply).parent / (args.out or "clips_video.json")
        out.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        print("写入", out)


if __name__ == "__main__":
    main()
