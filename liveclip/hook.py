"""预告专项：给计划里每段重新挑开头预告（hook）。

选段时顺带定的预告常见三个问题：一半没预告、开头是"就是/我觉得/有没有"之类的口水话；
有预告的多是讲道理的总结句（"底层逻辑是创造价值"）；句子长、带结巴。
这里单独让 Claude 只看一段的逐句字幕，按抖音开头的标准找 3 个候选、打分、选最好的。

用法:
    liveclip hook <ID> <plan.json> --out <新plan.json> [--only 1,3,5]
输出新计划（旧计划不动），并在 clips/<ID>/<新plan名>_预告对比.txt 写旧/新预告对比。
"""
import argparse
import json
from pathlib import Path

from .config import C
from .llm import ask_json

ROOT = C.ROOT
MODEL = C.model("hook")


SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["candidates", "best", "why"],
    "properties": {
        "candidates": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["first_line", "last_line", "text", "type", "score", "reason"],
            "properties": {
                "first_line": {"type": "integer"}, "last_line": {"type": "integer"},
                "text": {"type": "string"}, "type": {"type": "string"},
                "score": {"type": "integer"}, "reason": {"type": "string"}}}},
        "best": {"type": "integer"},
        "why": {"type": "string"}},
}

PROMPT = f"""你在给{C.name}的短视频切片做"开头预告"：从这段里挑一句话，复制到视频最开头先放，再从头正常播放。
观众刷到视频的第一秒就决定划不划走，预告是唯一的机会。

标题：{{title}}
下面是这一段的逐句字幕（行号: [秒] 内容），机器识别，可能有错字；直播连麦时可能混着连麦对方的话。

{{lines}}

找 3 个最抓人的候选，每个是连续的一行或几行（first_line 到 last_line），按下面的标准给 1–10 分：

好预告（越往前越好）：
1. 反常识、颠覆认知的断言："学历是这个时代最不值钱的东西"
2. 具体数字或结果，一听就想问为什么："我一个月只允许自己花300块"
3. 直接戳观众、对"你"说的狠话："你每天刷的短视频，正在偷走你的专注力"
4. 冲突、点名、对立："我妈跟我说，别回老家了"
5. 只说结论不说原因，留悬念
6. 情绪峰值：他提高音量、全场笑、语气最冲的那一句
7. 观众的问题加他的一句答案："年轻人该 all in 什么？——先把身体练好"

差预告（打低分）：
- 讲道理的总结句："赚钱的底层逻辑是创造价值"——对，但没人会因为这句停下来
- 铺垫、背景介绍、"我觉得""有没有一种可能"开头的句子
- 连麦对方的附和、铺垫式提问（例外：对方那句本身就是金句或冲突点、念出来的黑评、观众会代入的炸问题——这些可以用）
- 带明显结巴、口误、重复的句子
- 离开这段上下文就看不懂的半句话

硬性要求：
- 总时长 3.5–7 秒，越短越好；不能截半句（以"所以""但是"开头没关系，只看抓不抓人）
- 只能用他原话，不能拼接两处、不能改字
- 预告的意思要和这段的核心观点一致，不能断章取义成相反或更极端的意思

text 抄写候选原文，type 写属于上面哪一类，reason 一句话说为什么抓人或为什么不行。
best 是你最推荐的候选序号（从 0 开始）。why 一句话说明为什么选它。
"""


def ask(prompt):
    return ask_json(prompt, SCHEMA, model=MODEL, tool="挑预告", timeout=600)


def clip_lines(c, cues):
    caps = c.get("captions")      # 校对过的字幕优先
    rows = [(q["from"], q["to"], q["text"]) for q in caps] if caps and isinstance(caps[0], dict) and "text" in caps[0] \
        else [(q["from"], q["to"], q["content"]) for q in cues if c["start"] - 0.05 <= q["from"] and q["to"] <= c["end"] + 0.05]
    return rows


def one(i, c, cues):
    rows = clip_lines(c, cues)
    if len(rows) < 4:
        return None
    old = "（无）" if c.get("hook_start") is None else " ".join(t for s, e, t in rows if s >= c["hook_start"] - 0.05 and e <= c["hook_end"] + 0.05)
    lines = "\n".join(f"{k}: [{s - c['start']:.1f}] {t}" for k, (s, e, t) in enumerate(rows))
    try:
        res = ask(PROMPT.format(title=c["title"].replace("\n", " "), lines=lines))
    except Exception as e:
        return f"{i:02d} {c['title']}：失败 {e}"
    cands = sorted(range(len(res["candidates"])), key=lambda k: (k != res["best"], -res["candidates"][k]["score"]))
    chosen = None
    for k in cands:
        h = res["candidates"][k]
        f, l = max(0, h["first_line"]), min(len(rows) - 1, h["last_line"])
        hs, he = rows[f][0], rows[l][1]
        text = "".join(t for _, _, t in rows[f:l + 1])
        if not (3.0 <= he - hs <= 9.0) or hs < c["start"] + 2:
            continue
        chosen = (hs, he, text, h)
        break
    c["hook_candidates"] = res["candidates"]
    if chosen:
        hs, he, text, h = chosen
        c["hook_start"], c["hook_end"], c["hook_pass"] = round(hs, 3), round(he, 3), {"score": h["score"], "type": h["type"], "why": res["why"]}
        new = f"{text}（{h['score']}分，{h['type']}，{he - hs:.1f}秒）"
    else:
        new = "（候选都不满足硬性要求，保留原预告）"
    return f"{i:02d} {c['title'].replace(chr(10), ' ')}\n   旧：{old}\n   新：{new}\n   理由：{res['why']}"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="liveclip hook", description="re-pick the opening teaser (hook) of each clip")
    ap.add_argument("vid"); ap.add_argument("plan"); ap.add_argument("--out", required=True)
    ap.add_argument("--only")
    a = ap.parse_args(argv)
    pdir = C.CLIPS / a.vid
    plan = json.loads((pdir / a.plan).read_text(encoding="utf-8"))
    cues = json.loads(C.ws(plan["subs"]).read_text(encoding="utf-8"))["body"]
    only = {int(x) for x in a.only.split(",")} if a.only else None
    todo = [(i, c) for i, c in enumerate(plan["clips"], 1) if not only or i in only]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as ex:       # 每段一次模型调用，并行省时间（跟播每轮要快）
        report = [r for r in ex.map(lambda ic: one(ic[0], ic[1], cues), todo) if r]
    for r in report:
        print(r, flush=True)
    (pdir / a.out).write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    (pdir / f"{Path(a.out).stem}_预告对比.txt").write_text("\n\n".join(report) + "\n", encoding="utf-8")
    print(f"写入 {pdir / a.out}")


if __name__ == "__main__":
    main()
