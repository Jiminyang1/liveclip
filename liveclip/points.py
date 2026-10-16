"""观点库：每条剪出的片子一行——讲的什么观点、属于哪个主题、片子本身的特征、在各平台发了没有、用的什么标题和标签、数据怎样。
观点库不是拿来去重的（反复讲的观点说明热门），是用来看「什么观点 × 什么平台 × 怎么包装」火。

数据都是从现成文件拼出来的，随时重建，不手改：
  片子    clips/<ID>/follow.json（跟播计划：观点 point、分类、分数、起止、预告、标签…）、deliver/**/NN_标题/
  发布    data/publish_log.json + 各平台实际用的标题/标签（交付目录 文案.txt 的【抖音】【B站】段）
  数据    data/perf/<最新日期>.json（liveclip perf 拉的各平台数据，有就并进来）
  主题    data/themes.json：观点 → 主题，新观点用 Claude（config models.themes）归类，归过的不再问

  liveclip points              重建 data/points.json，打印按主题汇总
  liveclip points --no-llm     不调 Claude 归主题（新观点主题留空）
"""
import argparse
import json
import re
import subprocess
from collections import defaultdict

from .config import C
from .llm import ask_json

ROOT = C.ROOT
OUT, THEMES = C.DATA / "points.json", C.DATA / "themes.json"
PLAT = {"douyin": "抖音", "bili": "B站"}
norm = lambda s: re.sub(r"[\W_]", "", s or "").lower()


def tier(d):
    return None if d is None else "短" if d < 90 else "中" if d < 180 else "长"


def dur(p):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        return round(float(out), 1) if out else None
    except Exception:  # noqa: BLE001
        return None


def caption(d):
    """文案.txt 里各平台实际用的标题和标签。"""
    f = d / "文案.txt"
    if not f.exists():
        return {}
    txt, out = f.read_text(encoding="utf-8"), {}
    for key, name in (("douyin", "抖音"), ("bili", "B站")):
        m = re.search(rf"【{name}】(.*?)(?=\n【|\Z)", txt, re.S)
        if not m:
            continue
        seg = m.group(1)
        t = re.search(r"标题：\s*\n(.+)", seg)
        if key == "bili":
            tg = re.search(r"标签：\s*\n(.+)", seg)
            tags = [x.strip() for x in re.split(r"[，,]", tg.group(1))] if tg else []
        else:
            tags = re.findall(r"#(\S+)", seg)
        out[key] = {"title": t.group(1).strip() if t else None, "tags": [x for x in tags if x]}
    return out


def plans():
    """所有计划文件里的片子，按 (交付目录名里的序号+标题) 和 标题 两种方式索引。"""
    by_title = {}
    for f in C.CLIPS.glob("*/*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(d, dict) or not isinstance(d.get("clips"), list):
            continue
        for i, c in enumerate(d["clips"], 1):
            if isinstance(c, dict) and c.get("title"):
                by_title.setdefault(norm(c["title"]), (f, i, c))
    return by_title


def latest_perf():
    snaps = sorted((C.DATA / "perf").glob("*.json"))
    if not snaps:
        return None, {}
    rows = json.loads(snaps[-1].read_text(encoding="utf-8"))
    rows = rows.get("rows", rows) if isinstance(rows, dict) else rows
    idx = {}
    for r in rows:
        d = r.get("deliver_dir") or r.get("dir")
        if d and r.get("platform"):
            idx[(d.removeprefix(str(ROOT) + "/"), r["platform"])] = r
    return snaps[-1].stem, idx


# 数据快照里和平台表现有关的字段（计划特征、留存曲线明细不重复放）
PERF_KEYS = ["views", "completion", "completion_5s", "bounce_2s", "avg_view_sec", "avg_view_proportion", "ret_3s", "ret_5s",
             "ret_10s", "ret_30s", "ret_half", "likes", "comments", "shares", "favorites", "danmaku", "new_follows",
             "unfollows", "homepage_visits", "fan_view_proportion", "source_reco", "source_search", "pub_bj",
             "pub_hour_bj", "age_h", "via", "captured_at"]


def build():
    by_title = plans()
    pub = defaultdict(dict)
    publog = C.DATA / "publish_log.json"
    for x in (json.loads(publog.read_text(encoding="utf-8")) if publog.exists() else []):
        pub[x["dir"]][x["platform"]] = x
    perf_date, perf = latest_perf()
    rows = []
    for d in sorted(C.DELIVER.glob("*/[0-9][0-9]_*")):
        if not (d / "视频.mp4").exists():
            continue
        rel = C.rel(d)
        name = d.name[3:]
        hit = by_title.get(norm(name))
        if not hit:      # 交付目录名是标题去标点后的样子，计划里的标题可能两行
            hit = next((v for k, v in by_title.items() if k.startswith(norm(name)[:12]) and norm(name)[:12]), None)
        f, n, c = hit or (None, None, {})
        v_dur = dur(d / "视频.mp4")
        t_dur = dur(d / "视频_精剪.mp4") if (d / "视频_精剪.mp4").exists() else None
        cap = caption(d)
        hook = None      # 片头预告那句：hook_pass 选中的候选（分数最高的那个），没有预告就空
        if c.get("hook_start") is not None and c.get("hook_candidates"):
            hook = max(c["hook_candidates"], key=lambda h: h.get("score", 0)).get("text")
        session = d.parent.name.removeprefix("直播跟播_")
        r = {
            "id": rel, "session": session, "n": int(d.name[:2]),
            "date": (re.search(r"(\d{4})(\d\d)(\d\d)", session) and "-".join(re.search(r"(\d{4})(\d\d)(\d\d)", session).groups())) or None,
            "point": c.get("point") or c.get("title", name).replace("\n", " "), "point_from_title": not c.get("point"),
            "theme": None, "category": c.get("category"), "score": c.get("score"), "risky": c.get("risky"),
            "title": (c.get("title") or name).replace("\n", " / "), "why": c.get("why"),
            "layout": {"duet_v": "连麦", "full_v": "单人"}.get(c.get("layout"), c.get("layout")),
            "src_start": c.get("start"), "src_end": c.get("end"), "round": c.get("round"),
            "dur": v_dur, "tier": tier(v_dur), "dur_tight": t_dur, "tier_tight": tier(t_dur),
            "hook": hook, "content_tags": c.get("tags") or [], "plan": C.rel(f) if f else None,
            "platforms": {},
        }
        # 发过的平台：发布记录 + 数据快照（手动发的不在发布记录里，快照里标 via=manual）
        for p in sorted(set(pub.get(rel, {})) | {pl for (dd, pl) in perf if dd == rel}):
            x, m = pub.get(rel, {}).get(p, {}), perf.get((rel, p), {})
            r["platforms"][p] = {
                "published": m.get("pub_bj") or x.get("time"), "title": m.get("title") or (cap.get(p) or {}).get("title") or x.get("title"),
                "tags": m.get("tags") or (cap.get(p) or {}).get("tags"), "version": m.get("version") or "原版",
                "perf": {k: m[k] for k in PERF_KEYS if m.get(k) is not None} or None,
            }
        rows.append(r)
    return rows, perf_date


def assign_themes(rows, use_llm=True):
    """观点归主题：已有的沿用；新的让 Claude 归到现有主题或开新主题（同一主题 = 同一个核心主张，换说法也算）。"""
    known = json.loads(THEMES.read_text(encoding="utf-8")) if THEMES.exists() else {}
    for r in rows:
        r["theme"] = known.get(r["point"])
    todo = sorted({r["point"] for r in rows if not r["theme"]})
    if todo and use_llm:
        names = sorted(set(known.values()))
        prompt = (f"下面是{C.name}直播切片里的一条条核心观点。请把每条归到一个「主题」：主题是一个反复出现的核心主张或话题，"
                  "6–12 个字，例如「财富自由之后的意义」「AI改变一切」「睡眠与精力」。尽量用已有主题；"
                  "确实是新话题才开新主题，主题总数控制在 15–30 个。\n已有主题：" + ("、".join(names) or "（无）")
                  + "\n\n观点：\n" + "\n".join(f"{i}. {p}" for i, p in enumerate(todo)))
        schema = {"type": "object", "required": ["items"], "additionalProperties": False, "properties": {"items": {
            "type": "array", "items": {"type": "object", "required": ["i", "theme"], "additionalProperties": False,
                                       "properties": {"i": {"type": "integer"}, "theme": {"type": "string"}}}}}}
        res = ask_json(prompt, schema, model=C.model("themes"), tool="观点归类")
        for it in (res or {}).get("items", []):
            if 0 <= it["i"] < len(todo):
                known[todo[it["i"]]] = it["theme"].strip()
        THEMES.write_text(json.dumps(known, ensure_ascii=False, indent=1), encoding="utf-8")
        for r in rows:
            r["theme"] = known.get(r["point"])
    return rows


def summary(rows):
    by = defaultdict(list)
    for r in rows:
        by[r["theme"] or "（未归类）"].append(r)
    for th, rs in sorted(by.items(), key=lambda kv: -len(kv[1])):
        npub = sum(1 for r in rs if r["platforms"])
        print(f"{th:16s} {len(rs):3d} 条  已发 {npub}  " + " / ".join(r["title"][:18] for r in rs[:3]))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="liveclip points")
    ap.add_argument("--no-llm", action="store_true")
    a = ap.parse_args(argv)
    rows, perf_date = build()
    rows = assign_themes(rows, use_llm=not a.no_llm)
    OUT.write_text(json.dumps({"built_from_perf": perf_date, "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"观点库 {len(rows)} 条（已发 {sum(1 for r in rows if r['platforms'])} 条，数据快照 {perf_date or '还没有'}）→ {OUT}")
    summary(rows)


if __name__ == "__main__":
    main()
