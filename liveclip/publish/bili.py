"""Bilibili upload through biliup (https://github.com/biliup/biliup, MIT) — API upload, no browser.

  liveclip bili <dir> [--tags a,b] [--title ...] [--at "YYYY-MM-DD HH:MM"]   print the biliup command (dry run)
  liveclip bili <dir> ... --go                                               upload, then record in the publish log
  liveclip bili login                                                        QR-code login (cookie in data/biliup/)

Disabled unless config publish.enabled and publish.bili.enabled are true. Only upload content you have the rights to.
Title / tags / description / repost source come from liveclip.publish.common.info (must-have + content tags).
--at is in the audience timezone (config timezones.audience); biliup requires ≥ 4 h in the future.
"""
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timedelta

from .. import tags as T
from ..config import C
from . import common as P

COOKIE = C.DATA / "biliup" / "cookies.json"


def biliup_bin():
    b = C.get("publish.bili.biliup") or "biliup"
    path = shutil.which(b) or (b if b and os.path.exists(b) else None)
    if not path:
        sys.exit("biliup not found: pip install 'liveclip[publish]' (or set publish.bili.biliup to its path)")
    return path


def build(d, extra_tags=(), title=None, at=None):
    m = P.info(d)
    b = m["bili"]
    tags = T.merge(b["tags"], extra_tags, limit=T.LIMIT["bili"])
    desc = T.hashtags(T.merge(T.MUST, [t for t in tags if t not in T.BILI_MUST], limit=T.LIMIT["douyin"]))
    copyright_ = int(C.get("publish.bili.copyright") or 2)
    cmd = [biliup_bin(), "-u", str(COOKIE), "upload", "--copyright", str(copyright_)]
    if copyright_ == 2:
        if not b["source"]:
            sys.exit("repost (copyright 2) needs a source: set branding.source in config.yaml")
        cmd += ["--source", b["source"]]
    cmd += ["--tid", str(C.get("publish.bili.tid") or 21), "--title", title or b["title"], "--desc", desc, "--tag", ",".join(tags)]
    if m["cover_bili"]:
        cmd += ["--cover", m["cover_bili"]]
    if at:
        ts = datetime.strptime(at, "%Y-%m-%d %H:%M").replace(tzinfo=C.audience_tz)
        if ts - datetime.now(C.audience_tz) < timedelta(hours=4, minutes=5):
            sys.exit("biliup scheduled publishing must be ≥ 4 hours from now")
        cmd += ["--dtime", str(int(ts.timestamp()))]
    return m, cmd + [m["video"]]


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["login"]:
        COOKIE.parent.mkdir(parents=True, exist_ok=True)
        sys.exit(subprocess.run([biliup_bin(), "-u", str(COOKIE), "login"]).returncode)
    ap = argparse.ArgumentParser(prog="liveclip bili", description="Bilibili upload via biliup")
    ap.add_argument("dir")
    ap.add_argument("--tags", default="", help="extra content tags, comma separated")
    ap.add_argument("--title")
    ap.add_argument("--at", help='scheduled publish, "YYYY-MM-DD HH:MM" in the audience timezone')
    ap.add_argument("--go", action="store_true", help="really upload; without it only print the command")
    a = ap.parse_args(argv)
    if "bili" in P.published(a.dir):
        sys.exit("already published on Bilibili (data/publish_log.json)")
    m, cmd = build(a.dir, [t for t in re.split(r"[,，]", a.tags) if t], a.title, a.at)
    if m["notes"]:
        print("注意：", *m["notes"], sep="\n  ")
    print(" ".join(shlex.quote(c) for c in cmd))
    if not a.go:
        return
    C.require_publish("bili")
    r = subprocess.run(cmd, capture_output=True, text=True)
    out = re.sub(r"\x1b\[[0-9;]*m", "", r.stdout + r.stderr)
    bv = re.search(r'bvid": String\("(BV\w+)"\)', out)
    if r.returncode or not bv:
        print(out[-3000:])
        sys.exit("upload failed")
    P.done(a.dir, "bili", cmd[cmd.index("--title") + 1])
    print(json.dumps({"bvid": bv.group(1), "title": cmd[cmd.index("--title") + 1]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
