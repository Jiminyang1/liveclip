"""整场字幕一次校对：语音识别的同音错字在整场字幕上改一遍，下游选段/标题、烧录字幕、完整版 SRT 都用校对后的文字。
起因：一条讲「AI 剪辑」的切片，烧进画面的是「人减的还是AI减的」——识别没有上下文，剪/减分不出。

- 规则和 liveclip.review.captions（单条切片逐句校对）共用 RULES_BASE：只改识别错字，不润色口语；否定字不能删；同音热词按上下文，
  定不了标 check；拿不准的标 check 不硬猜；主播名字的各种识别写法统一（config streamer.name_variants → canonical_name，
  另有程序确定替换；「2 0个」这种拆开的数字也确定合并）；不加《》等原文没有的标点。不给模型看切片标题（这里本来就没有）。
- 按 ~150 行分块，每块带前面 --context 秒（默认 120 秒）和后面 60 秒的原文当上下文，最多 4 块同时问 Claude。
  模型只返回要改和拿不准的行，没返回的算 ok。不许合并/拆分行：输出行数、时间、spk 和输入完全一样，只换 content。
- 程序兜底：改动丢了否定字、或改得面目全非（和原文相似度 <0.5）的不采用，原文保留并标 check。
- 每行加 orig（识别原文）、status（ok / check）、note（改了或要确认时写原因）；没校对到的行（不在 --from/--to 里、或那块 Claude 失败）不带 status。
  顶层 review 记模型、块数、改了几句、待确认几句、用时、花费。
- 缓存：每块按「模型 + 规则版本 + 整段提示词」的哈希存在 data/asr/review_cache/，重跑不花钱、结果不变。
- 字幕校对模型：config models.review。
- 增量（跟播用）：--out 已存在时，时间和原文都没变的行沿用上次的校对结果，只校对 [--from, --to) 里还没校对过的行（--redo 强制重校）；
  输入里新增的行先原样并进来。所以输出总是和输入逐行对应，可以直接当 plan 的 subs。

用法:
    liveclip review-subs data/asr/X_bailian_spk.json [--alt data/asr/X.json]
    liveclip review-subs data/asr/LIVE_x.json --out data/asr/LIVE_x_reviewed.json --from 1200 --to 2400 --context 300
    ... --check-txt 字幕待确认.txt     # 另写一份 status=check 的行（时间、文字、原因）给人听
默认输出 <输入名>_reviewed.json（同目录）。有块失败时照样写输出（那几块的行不带 status），退出码 1。
"""
import argparse
import copy
import difflib
import hashlib
import json
import re
import sys
import time
from bisect import bisect_left
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..config import C
from ..pick import ask_cli
from .captions import RULES_NO_MARKS as RULES, SCHEMA, alt_text

ROOT = C.ROOT
MODEL = C.model("review")
VERSION = "subs-2"            # 规则或提示词改了就加版本号，旧缓存自然作废
CHUNK = 150
AFTER_SEC = 60
CACHE = C.ASR / "review_cache"
NEGATIONS = "不没别未无非"
# 主播名字的识别写法统一成 canonical_name：模型之外再做一遍确定替换。英文只换整词（前后不是英文字母）；
# 跟播素材都是主播自己的直播间，这几种写法实际只指他。没配置 name_variants 时不做
NAME_RE = C.name_variants_re()
DIGIT_GAP = re.compile(r"(?<=\d) (?=\d)")   # 百炼常把数字拆成「2 0个」「1 0 0 KG」，确定合并（模型改了一部分、漏了一部分）
SCHEMA_SUBS = copy.deepcopy(SCHEMA)
SCHEMA_SUBS["properties"]["lines"]["items"]["properties"]["status"]["enum"] = ["fixed", "check"]

PROMPT = f"""你在校对{C.name}一场直播的整场字幕，校对后的字幕要拿来挑切片、烧进切片画面、做完整版字幕。字幕是语音识别出来的，会有同音错字。
{C.name}是{C.get("streamer.persona")}，常聊{C.get("streamer.topics")}；直播里也会念弹幕、回答观众。
""" + """
规则：
{rules}6. 只返回要改的行（status 填 fixed）和拿不准的行（status 填 check）；没问题的行不要返回。i 用行首的编号。
7. 每个编号是独立的一行：不要把两行合成一行，也不要把一行的字挪到别的行；一句话被识别切成两行时，各行只改自己的字。
8. 行首方括号里是说话人（[主播]、[嘉宾] 等），只帮你理解，text 里不要带。
9. "上文/下文"只用来理解，不要返回。

上文（只供参考）：
{before}

要校对的字幕（编号: 内容）：
{lines}

下文（只供参考）：
{after}
"""

norm = lambda s: re.sub(r"[\W_]", "", s).lower()


def rel(p):
    p = Path(p).resolve()
    return str(p.relative_to(ROOT) if p.is_relative_to(ROOT) else p)


def key(q):
    return (round(q["from"], 2), round(q["to"], 2), q.get("orig", q["content"]))


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def merge(src, old):
    """输入字幕 + 上次的校对结果 → 逐行对应输入的新 body。时间和原文都对得上的行沿用校对结果，其他行原样（不带 status）。"""
    done = {key(q): q for q in (old or []) if "status" in q}
    body = []
    for q in src:
        q = {k: v for k, v in q.items() if k not in ("orig", "status", "note")}
        o = done.get(key(q))
        if o:
            q.update(content=o["content"], orig=o["orig"], status=o["status"])
            if o.get("note"):
                q["note"] = o["note"]
        body.append(q)
    return body


def tag(q, spk):
    return f"[{q['spk']}] " if spk and q.get("spk") else ""


def guard(orig, text):
    """不采用的改动返回原因：丢了否定字（意思会反）、改得面目全非（不是改错字，是重写）。"""
    lost = [ch for ch in NEGATIONS if text.count(ch) < orig.count(ch)]
    if lost:
        return f"改成「{text}」会丢掉否定字「{''.join(lost)}」，没采用"
    a, b = norm(NAME_RE.sub(C.canonical_name, orig) if NAME_RE else orig), norm(text)      # 主播名统一不算大改
    if a and difflib.SequenceMatcher(None, a, b).ratio() < 0.5:
        return f"改成「{text}」改动太大，没采用"
    return None


def make_chunks(idx, size):
    """要校对的行下标 → 若干块：先按连续段分开，再把每段平均切成不超过 size 行的块。"""
    runs, cur = [], []
    for i in idx:
        if cur and i != cur[-1] + 1:
            runs.append(cur); cur = []
        cur.append(i)
    if cur:
        runs.append(cur)
    out = []
    for r in runs:
        n = -(-len(r) // size)
        step = -(-len(r) // n)
        out += [r[k:k + step] for k in range(0, len(r), step)]
    return out


def chunk_prompt(body, ids, alt, alt_froms, context, spk):
    first, last = body[ids[0]], body[ids[-1]]
    before = [q for q in body[max(0, ids[0] - 80):ids[0]] if q["from"] >= first["from"] - context]
    after = [q for q in body[ids[-1] + 1:ids[-1] + 31] if q["from"] <= last["to"] + AFTER_SEC]

    def alt_for(q):
        if not alt:
            return ""
        return alt_text(alt[bisect_left(alt_froms, q["from"] - 30):bisect_left(alt_froms, q["to"])], q)
    show = lambda q: tag(q, spk) + q.get("orig", q["content"])
    return PROMPT.format(
        rules=RULES,
        before="\n".join(map(show, before)) or "（无）",
        lines="\n".join(f"{j}: {show(body[i])}{alt_for(body[i])}" for j, i in enumerate(ids)),
        after="\n".join(map(show, after)) or "（无）")


def ask(prompt):
    """问 Claude，按提示词哈希缓存。返回 (结果, 花费美元, 是否命中缓存)。"""
    h = hashlib.sha1(f"{MODEL}\n{VERSION}\n{prompt}".encode()).hexdigest()[:20]
    f = CACHE / f"{h}.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))["result"], 0.0, True
    meta = {}
    res = ask_cli(prompt, SCHEMA_SUBS, meta, tool="字幕校对", label=COST_LABEL[0], model=MODEL)
    CACHE.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"model": MODEL, "version": VERSION, "at": time.strftime("%Y-%m-%d %H:%M"),
                             "cost_usd": meta.get("cost_usd"), "result": res}, ensure_ascii=False), encoding="utf-8")
    return res, meta.get("cost_usd") or 0.0, False


def apply(body, ids, res):
    got = {}
    for r in res.get("lines", []):
        if 0 <= r.get("i", -1) < len(ids) and r["i"] not in got:
            got[r["i"]] = r
    for j, i in enumerate(ids):
        q = body[i]
        orig = q.get("orig", q["content"])
        q.update(content=orig, orig=orig, status="ok")
        q.pop("note", None)
        r = got.get(j)
        if not r:
            continue
        text = re.sub(r"^\[[^\]]{1,6}\]\s*", "", r["text"].strip()) or orig
        note = r.get("note", "").strip()
        bad = guard(orig, text)
        if bad:
            q.update(status="check", note=bad + (f"（模型说明：{note}）" if note else ""))
        elif r["status"] == "check":
            q.update(content=text, status="check", note=note or "拿不准")
        elif text != orig:
            q.update(content=text, note=note)


COST_LABEL = [None]   # 记账用：这次校对的是哪场（review() 里按字幕路径设）


def review(subs, out=None, alt=None, lo=None, hi=None, context=120, redo=False, jobs=4, chunk=CHUNK, log=print):
    """校对 subs 里 [lo, hi) 秒开始的行（None 表示不限），结果并进 out。返回统计 dict；有块失败时 stats["failed"] > 0。"""
    from .. import cost as costlog
    COST_LABEL[0] = costlog.label_of(subs, out)
    t0 = time.time()
    subs = Path(subs)
    out = Path(out) if out else subs.with_name(subs.stem + "_reviewed.json")
    data = load(subs)
    old = load(out)["body"] if out.exists() else None
    body = merge(data["body"], old)
    spk = any(q.get("spk") for q in body[:300])
    alt_body = sorted(load(alt)["body"], key=lambda q: q["from"]) if alt else []
    alt_froms = [q["from"] for q in alt_body]
    lo_ = float("-inf") if lo is None else lo
    hi_ = float("inf") if hi is None else hi
    idx = []
    for i, q in enumerate(body):
        if not lo_ <= q["from"] < hi_ or ("status" in q and not redo):
            continue
        if not norm(q.get("orig", q["content"])):        # 纯标点/空行不用问
            q.update(orig=q.get("orig", q["content"]), status="ok")
            continue
        idx.append(i)
    chunks = make_chunks(idx, chunk)
    stats = {"chunks": len(chunks), "lines": len(idx), "cached": 0, "failed": 0, "cost_usd": 0.0}

    def one(ids):
        try:
            return ids, ask(chunk_prompt(body, ids, alt_body, alt_froms, context, spk)), None
        except BaseException as e:      # noqa: BLE001  一块失败不拖垮别的块
            return ids, None, e
    if chunks:
        log(f"校对 {len(idx)} 句，分 {len(chunks)} 块，{min(jobs, len(chunks))} 块并行（{MODEL}）")
        with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
            for ids, got, err in ex.map(one, chunks):
                if err is not None:
                    stats["failed"] += 1
                    log(f"  {body[ids[0]]['from']:.0f}–{body[ids[-1]]['to']:.0f}s 这块校对失败：{str(err)[-300:]}")
                    continue
                res, cost, hit = got
                stats["cached"] += hit
                stats["cost_usd"] += cost
                apply(body, ids, res)
    for q in body:                  # 确定替换：主播名统一、数字合并（已校对的行都做，包括没问 Claude 的；幂等）
        if "status" not in q:
            continue
        for pat, rep, why in ((NAME_RE, C.canonical_name, "主播名统一"), (DIGIT_GAP, "", "数字合并")):
            if pat is not None and pat.search(q["content"]):
                q["content"] = pat.sub(rep, q["content"])
                q["note"] = ((q.get("note") or "") + "；" + why).lstrip("；")
    done = [q for q in body if "status" in q]
    changed = [q for q in done if q["content"] != q["orig"]]
    checks = [q for q in done if q["status"] == "check"]
    stats.update(sec=round(time.time() - t0, 1), cost_usd=round(stats["cost_usd"], 4))
    prev = (load(out).get("review") or {}) if out.exists() else {}
    runs = (prev.get("runs") or [])[-19:] + [{
        "at": time.strftime("%Y-%m-%d %H:%M:%S"), "range": [lo, hi], "redo": redo, "context_sec": context, **stats}]
    meta = {k: v for k, v in data.items() if k not in ("body", "review")}
    meta["review"] = {
        "tool": "liveclip.review.subs", "model": MODEL, "version": VERSION,
        "input": rel(subs), "alt": rel(alt) if alt else None,
        "lines": len(body), "reviewed": len(done), "changed": len(changed), "check": len(checks),
        "unreviewed": len(body) - len(done), "cost_usd": round(sum(r.get("cost_usd") or 0 for r in runs), 4), "runs": runs}
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps({**meta, "body": body}, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(out)
    log(f"校对完：本次 {len(idx)} 句（{stats['chunks']} 块，缓存 {stats['cached']}，失败 {stats['failed']}），"
        f"全文已校对 {len(done)}/{len(body)} 句，改 {len(changed)}，待确认 {len(checks)}；"
        f"用时 {stats['sec']:.0f}s，花费 ${stats['cost_usd']:.2f} → {out}")
    stats.update(out=out, changed=len(changed), check=len(checks), reviewed=len(done), total=len(body))
    return stats


def sync(subs, out):
    """不问 Claude，只把输入的新行并进校对文件（校对失败时用，保证 out 和输入逐行对应、可以当 plan 的 subs）。"""
    subs, out = Path(subs), Path(out)
    data = load(subs)
    old = load(out) if out.exists() else {}
    body = merge(data["body"], old.get("body"))
    meta = {k: v for k, v in data.items() if k not in ("body", "review")}
    if old.get("review"):
        meta["review"] = old["review"]
    out.write_text(json.dumps({**meta, "body": body}, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def hms(t):
    return f"{int(t) // 3600}:{int(t) % 3600 // 60:02d}:{int(t) % 60:02d}"


def check_report(body, title=""):
    """status=check 的行：时间、校对后文字、原识别、原因。"""
    rows = [q for q in body if q.get("status") == "check"]
    head = (f"{title}\n" if title else "") + f"字幕待确认 {len(rows)} 句（听原音定；文字是校对模型认为最可能的写法）\n"
    lines = [f"{hms(q['from'])}  {q['content']}    ← "
             + "；".join(x for x in ((f"原识别：{q['orig']}" if q["orig"] != q["content"] else ""), q.get("note", "")) if x)
             for q in rows]
    return head + "\n" + "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="liveclip review-subs", description="整场字幕一次校对（只改识别错字，行数和时间不变）")
    ap.add_argument("subs")
    ap.add_argument("--alt", help="对照字幕（不带热词的 SenseVoice，data/asr/<id>.json 之类）")
    ap.add_argument("--out", help="默认 <输入名>_reviewed.json")
    ap.add_argument("--from", dest="lo", type=float, help="只校对从这一秒开始的行")
    ap.add_argument("--to", dest="hi", type=float, help="只校对这一秒之前开始的行")
    ap.add_argument("--context", type=float, default=120, help="每块前面带多少秒原文当上下文（默认 120）")
    ap.add_argument("--redo", action="store_true", help="范围里已经校对过的行也重校")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--chunk", type=int, default=CHUNK, help=f"每块多少行（默认 {CHUNK}）")
    ap.add_argument("--check-txt", help="另写一份待确认清单")
    a = ap.parse_args(argv)
    st = review(a.subs, a.out, a.alt, a.lo, a.hi, a.context, a.redo, a.jobs, a.chunk)
    if a.check_txt:
        Path(a.check_txt).write_text(check_report(load(st["out"])["body"]), encoding="utf-8")
    sys.exit(1 if st["failed"] else 0)


if __name__ == "__main__":
    main()
