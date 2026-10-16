"""Shared publishing helpers: read a delivery folder's 文案.txt, compute per-platform title/tags, keep the publish log.

A delivery folder is deliver/<batch>/NN_<title>/ with 视频.mp4 (+ 视频_精剪.mp4), 封面.jpg (3:4),
封面_横4比3.jpg (4:3), 封面_B站.jpg (16:9) and 文案.txt (see liveclip.caption).
The publish log data/publish_log.json is the single source of truth for "was this already published" — every
uploader checks it before posting and appends to it right after a confirmed post.

  liveclip publish info <dir>                print what would be published (JSON)
  liveclip publish check <platform> <dir>    exit 1 if already published there
  liveclip publish mark <platform> <dir>     record a manual publish in the log
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

from .. import tags as T
from ..config import C

LOG = C.DATA / "publish_log.json"
PLATFORMS = {"douyin": "抖音", "xhs": "小红书", "bili": "B站", "shipinhao": "视频号"}


def parse_caption(path):
    """文案.txt → {'抖音': {'标题': ..., '作品描述': ...}, ...}；首行「？」「⚠」提示另存 notes。"""
    out, notes, cur, key = {}, [], None, None
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        m = re.match(r"^【(.+?)】(.*)$", line)
        if m:
            cur, key = m.group(1), None
            out[cur] = {"说明": m.group(2).strip()}
            continue
        if cur is None:
            if line.strip():
                notes.append(line.strip())
            continue
        if re.match(r"^[^\s#：]{1,8}：$", line):
            key = line[:-1]
            out[cur][key] = ""
        elif key and line.strip():
            out[cur][key] = (out[cur][key] + "\n" + line.strip()).strip()
    return out, notes


def _strip_prefix(t):
    """标题开头的「主播名：」前缀去掉（这类冒号前缀显得土），两行标题之间的多余逗号去掉。"""
    names = "|".join(re.escape(x) for x in {C.name, C.host_label, C.canonical_name} if x)
    if names:
        t = re.sub(rf"^({names})\s*[：:]\s*", "", t)
    return re.sub(r"([？！。，、])，", r"\1", t)


def info(d, video=None):
    d = Path(d).resolve()
    cap, notes = parse_caption(d / "文案.txt")
    v = d / (video or "视频.mp4")
    dy, bl = cap.get("抖音", {}), cap.get("B站", {})
    # 话题：必带 + 文案里各平台出现过的内容话题（旧文案没有必带的也补上）
    content = T.merge(*[re.findall(r"#(\S+)", x) for sec in cap.values() for x in sec.values()],
                      re.split(r"[，,\s]+", bl.get("标签", "")))
    src = re.search(r"转载来源填：(\S+)", bl.get("说明", ""))
    return {
        "dir": str(d),
        "video": str(v),
        "video_mb": round(v.stat().st_size / 1e6, 1) if v.exists() else None,
        "cover_3x4": str(d / "封面.jpg") if (d / "封面.jpg").exists() else None,
        "cover_4x3": str(d / "封面_横4比3.jpg") if (d / "封面_横4比3.jpg").exists() else None,
        "cover_bili": str(d / "封面_B站.jpg") if (d / "封面_B站.jpg").exists() else None,
        "douyin": {"title": _strip_prefix(dy.get("标题", "")), "tags": T.for_platform("douyin", content)},
        "bili": {"title": _strip_prefix(bl.get("标题", "")), "tags": T.for_platform("bili", content),
                 "desc": T.hashtags(T.for_platform("douyin", content)),
                 "source": src.group(1) if src else (C.get("branding.source") or "")},
        "captions": cap,
        "notes": notes,
        "published": published(d),
    }


def load_log():
    return json.loads(LOG.read_text(encoding="utf-8")) if LOG.exists() else []


def _rel(d):
    return C.rel(Path(d).resolve())


def published(d):
    d = _rel(d)
    return sorted({r["platform"] for r in load_log() if r["dir"] == d})


def done(d, platform, title):
    rows = load_log()
    rows.append({"dir": _rel(d), "platform": platform, "title": title, "time": time.strftime("%Y-%m-%d %H:%M:%S %z")})
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text(json.dumps(rows, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="liveclip publish", description="publishing helpers")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("info"); p.add_argument("dir"); p.add_argument("--video")
    p = sub.add_parser("check"); p.add_argument("platform", choices=PLATFORMS); p.add_argument("dir")
    p = sub.add_parser("mark", help="record a publish you did by hand"); p.add_argument("platform", choices=PLATFORMS); p.add_argument("dir")
    a = ap.parse_args(argv)
    if a.cmd == "check":
        hit = a.platform in published(a.dir)
        print("already published" if hit else "not published")
        sys.exit(1 if hit else 0)
    meta = info(a.dir, getattr(a, "video", None))
    if a.cmd == "mark":
        done(a.dir, a.platform, meta["douyin"]["title"])
        print(f"recorded: {PLATFORMS[a.platform]} {meta['douyin']['title']}")
        return
    print(json.dumps(meta, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
