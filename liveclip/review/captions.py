"""剪之前让 Claude 按上下文逐句校对一段切片要烧进画面的字幕（直播录屏这类原片没有字幕的才需要）。

- 只改语音识别的错字，不润色、不改口语、不改意思；改不准的标 "check" 留给人听/问。
- 同音专有名词（热词和普通词同音）模型听不出来，只能按上下文判断，两种都说得通的一律标 check；
  这类规则写在 config review.extra_rules 里。
- 不给模型看切片标题：标题是选段模型按同一份（有错的）字幕起的，拿它判断会循环论证（曾因此把同音词全改成了热词）。
- 结果写回 plan 的 clips[i]["captions"]（liveclip.cut 有这个字段就用它，不再套全局 typo_fixes），
  另写一份 <plan名>/<NN>_字幕校对.txt 方便人看：✓ 没改、✎ 改了、? 要确认。

用法:
    liveclip review-captions <ID> <plan.json> --only 2,4 [--alt-subs data/asr/<ID>.json]
--alt-subs 给一份不带热词的 SenseVoice 字幕做对照：两边写法不同的地方让模型重点看，
能发现热词带偏的字（例：「人生学」被热词带成「人生血」，SenseVoice 是「人参学」，两边都不对但一比就知道这里有问题）。
"""
import argparse
import json
import re

from .. import cut as cc
from ..cut import slug
from ..config import C
from ..pick import ask_cli

ROOT = C.ROOT

CONTEXT_LINES = 15
SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["lines"],
    "properties": {"lines": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["i", "text", "status", "note"],
        "properties": {"i": {"type": "integer"}, "text": {"type": "string"},
                       "status": {"type": "string", "enum": ["ok", "fixed", "check"]},
                       "note": {"type": "string"}}}}},
}

# 校对规则：这里和 liveclip.review.subs（整场字幕一次校对）共用；两边只是「返回哪些行」不同


def _intro():
    st = C.get("streamer")
    return (f"你在给{C.name}的直播切片校对字幕。字幕是语音识别出来的，会有同音错字。\n"
            f"{C.name}是{st['persona']}，常聊{st['topics']}。\n")


def _name_rule():
    variants = [v for v in C.get("streamer.name_variants") or [] if v]
    if not variants:
        return ""
    return (f"   主播本人一律写「{C.canonical_name}」：说的是他（他自称、观众或别人叫他）时，识别成"
            + "、".join(f'"{v}"' for v in variants) + f"之类的都改成「{C.canonical_name}」。\n")


def _extra_rules():
    rules = [r for r in C.get("review.extra_rules") or [] if r]
    if not rules:
        return ("2. 热词（专有名词）和普通词同音时，声音分不出，只能按上下文判断；两种都讲得通、上下文定不了的，status 填 check，"
                "note 说明两种可能。同一段里每次出现的同一个音指同一件事时，要么全部改、要么全部标 check，前后保持一致。\n")
    return "2. " + "\n   ".join(rules) + "\n   两种都讲得通、上下文定不了的，status 填 check，note 说明两种可能。\n"


INTRO = _intro()
RULES_BASE = ("""1. 只改明显的识别错字（同音字、近音字、专有名词），按上下文判断。不润色，不删口头禅，不改语序，不改意思，不加减内容。
   例外：识别出来的英文如果连成一串或大小写不对，按正确写法拆开单词、改大小写（"theslightedge"→"The Slight Edge"）{marks}。
""" + _name_rule() + """   否定字（不、没、别、未、无、非）一个都不能删：识别成"不"的就算觉得读不通，也只能标 check。
""" + _extra_rules() + """3. 不确定的改法也用 check，text 写你认为最可能的，note 写原因。
4. 他/它：指人用"他"，指身体、事物用"它"，这种改动算 fixed。
5. 有些行后面附了「另一个模型：…」，是同一时间段另一个识别模型（不带热词）的结果，只作对照：
   两边一致的字基本可信；不一致的地方是重点怀疑对象，按上下文选更合理的那个（可以采用对照里的写法），定不了就标 check。
   对照模型不认识热词，会写成同音的普通词，这一处不要跟它；主模型带了热词，可能把读音相近的字也带偏（例："人生学"被写成"人生血"）。
""")
# 单条切片校对给书名、作品名加《》；整场校对只改识别错字，不加这类标点
RULES = RULES_BASE.replace("{marks}", "；书名、电影名、节目名加《》；这两种算 fixed")
RULES_NO_MARKS = RULES_BASE.replace("{marks}", "，算 fixed；不加书名号《》、引号这类原文没有的标点，节目名、书名照识别的字写")
PROMPT = INTRO + """
规则：
""" + RULES + """6. 没问题的 status 填 ok，text 原样照抄。每个要校对的行都要返回，i 用行首的编号；"上文/下文"只用来理解，不要返回。

上文（只供参考）：
{before}

要校对的字幕（编号: 内容）：
{lines}

下文（只供参考）：
{after}
"""


def clip_cues(cues, c, b):
    start, end, h0, h1 = b
    lo = min(start, h0) if h0 is not None else start
    hi = max(end, h1) if h1 is not None else end
    idx = [k for k, q in enumerate(cues) if min(q["to"], hi) - max(q["from"], lo) >= 0.3]
    return idx


def alt_text(alt, q):
    """对照字幕里和这一句时间重叠的部分；和主字幕一样就不附。"""
    t = "".join(a["content"] for a in alt if min(a["to"], q["to"]) - max(a["from"], q["from"]) > 0.15)
    norm = lambda x: re.sub(r"[\W_]", "", x)
    return f"    （另一个模型：{t}）" if t and norm(t) != norm(q["content"]) else ""


def main(argv=None):
    ap = argparse.ArgumentParser(prog="liveclip review-captions")
    ap.add_argument("bv")
    ap.add_argument("plan")
    ap.add_argument("--only", help="只校对这几段，如 2,4")
    ap.add_argument("--alt-subs", help="另一份字幕做对照（例：SenseVoice 不带热词的 data/asr/<id>.json）")
    args = ap.parse_args(argv)

    path = C.CLIPS / args.bv / args.plan
    plan = json.loads(path.read_text(encoding="utf-8"))
    cues = json.loads(C.ws(plan["subs"]).read_text(encoding="utf-8"))["body"]
    alt = json.loads(C.ws(args.alt_subs).read_text(encoding="utf-8"))["body"] if args.alt_subs else []
    video = cc.find_video(args.bv, plan)
    live = plan.get("live", cc.manifest_row(args.bv).get("role") == "livestream_recording")
    only = {int(x) for x in args.only.split(",")} if args.only else None
    outdir = C.CLIPS / args.bv / args.plan.removesuffix(".json")
    outdir.mkdir(parents=True, exist_ok=True)

    for n, c in enumerate(plan["clips"], 1):
        if only and n not in only:
            continue
        b = cc.bounds(video, c, snap=not live, cues=cc.plan_cues(plan))
        idx = clip_cues(cues, c, b)
        if not idx:
            continue
        before = cues[max(0, idx[0] - CONTEXT_LINES):idx[0]]
        after = cues[idx[-1] + 1:idx[-1] + 1 + CONTEXT_LINES]
        prompt = PROMPT.format(
            before="\n".join(q["content"] for q in before) or "（无）",
            lines="\n".join(f"{j}: {cues[k]['content']}" + alt_text(alt, cues[k]) for j, k in enumerate(idx)),
            after="\n".join(q["content"] for q in after) or "（无）")
        res = {r["i"]: r for r in ask_cli(prompt, SCHEMA, tool="单条校对", model=C.model("review"))["lines"]}
        caps, report = [], []
        for j, k in enumerate(idx):
            q = cues[k]
            r = res.get(j, {"text": q["content"], "status": "check", "note": "模型没返回这一行"})
            text = r["text"].strip() or q["content"]
            status = "ok" if text == q["content"] and r["status"] == "fixed" else r["status"]
            caps.append({"from": q["from"], "to": q["to"], "text": text, "orig": q["content"],
                         "status": status, "note": r.get("note", "")})
            mark = {"ok": "✓", "fixed": "✎", "check": "?"}[status]
            line = f"{mark} {q['from']:8.2f}  {text}"
            if status != "ok":
                line += f"    ← 原识别：{q['content']}" + (f"；{r['note']}" if r.get("note") else "")
            report.append(line)
        c["captions"] = caps
        name = f"{n:02d}_{slug(c['title'])}"
        n_fix = sum(x["status"] == "fixed" for x in caps)
        n_chk = sum(x["status"] == "check" for x in caps)
        (outdir / f"{name}_字幕校对.txt").write_text(
            f"{c['title']}\n✓ 没改  ✎ 改了  ? 要确认（共 {len(caps)} 句，改 {n_fix}，待确认 {n_chk}）\n\n"
            + "\n".join(report) + "\n", encoding="utf-8")
        print(f"{name}: {len(caps)} 句，改 {n_fix}，待确认 {n_chk}")
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
