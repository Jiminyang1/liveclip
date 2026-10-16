"""精剪：把一个已选好的片段里的口头禅、卡壳、说错重说、连麦对方的附和剪掉，长停顿压短，节奏更紧。
liveclip clip --tight 在正常出片、质检之后对前 N 条跑这个；也可以单独跑。

流程：
1. 字级时间：优先用百炼整条识别的原始结果（plan 字幕旁边的 <字幕名>_raw/，或同场的 <id>_bailian_spk_raw/ 等；
   每个词有起止毫秒，「呃」「嗯」也单独成词），取片段窗口里的词，词内按字均分时间。
   没有原始结果才退回旧做法：对这一段原片重跑 SenseVoice（认不出的「呃」删不掉）。
   说话人：plan 字幕本身带 spk 就用它，否则用同场的 _bailian_spk.json，按时间对到每个字。
2. 分行：按片段的字幕句（逐句校对过的 captions 优先，否则 plan 字幕）分行，换说话人再断开，交给 Claude 标出要删的原文（只删不改）。
   不靠模型的确定规则：整词是「呃/嗯/额」的删；连麦对方（嘉宾/其他）单独一行的附和（对对对、是是、好的、太对了…）删。
   安全：一条删除里有否定字（不/没/别…），而删完后前后 4 行里没有同样的说法（不是结巴或说错重说），这条不采用；
   字幕改正丢了否定字时用识别原文。
3. 剩下的字连成若干小段；停顿超过 0.45 秒的压到约 0.25 秒。
4. cut_clips.cut(segs=...) 拼接：版式（含单段 layout 覆盖）、标题、尾卡、响度、音频时间戳修正与正常出片同一套。
   字幕：一个字没删的句子用原句（校对过的用校对文本），删过的用剩下的字（Claude 的识别改正优先）。

确定性：Claude 的删除决定按「片段原文」（逐行文字 + 说话人）的哈希存在 <输出目录>/精剪决定.json，原文不变就复用，
重跑得到同样的保留段；--fresh 强制重新问。--reuse：只用存过的决定，没有就跳过这段（不调 Claude）。

用法:
    liveclip tight <ID> auto.json --only 1,2,3 [--fresh | --reuse] [--allow-check]
输出 clips/<ID>/<plan 名>_tight/NN_标题.mp4（与正常出片同名）；删改明细、保留段、逐字时间写同名 .json（liveclip qa --tight 读它）。
"""
import argparse
import difflib
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import cut as cc
from .cut import slug
from .config import C
from .llm import ask_json

ROOT = C.ROOT
MODEL = C.model("tight")
HOST, GUEST = C.host_label, C.guest_label

PUNCT = set("，,。！？!?；;、：:")
FILLER_WORD = re.compile(r"^[嗯呃额]+$")          # 百炼里「呃」「嗯」单独成词；「额」只认整词（「额度」「额外」不能删）
BACKCHANNEL = re.compile(r"^(对|是|嗯|哦|噢|啊|好|行|哈|嘿|呵|欸|诶|哎|ok|okay|对的|是的|好的|没错|可以|确实|真的|太对了|说得太好了|说得好)+$", re.I)
NEGATIONS = "不没别未无非"
TAG_QUESTION = re.compile(r"^(啊|呀|吧|嘛)?(对不对|是不是|好不好|行不行|对吧)+(啊|呀|吧|嘛)?$")   # 当口头禅的反问，删了不改意思
NEAR = 4                               # 否定字保护：删掉的否定说法要在前后几行里原样还在
PAD_BEFORE, PAD_AFTER = 0.08, 0.22     # 字的起点前留一点；字本身要再响 0.2 秒左右（字幕句末同理）
DEL_TAIL = 0.12                        # 被删的字之后切点要过它 0.12 秒，否则留个尾音
END_TAIL = 0.45                        # 片段最后一个字多留一点，不然最后一个字被吞
MAX_GAP, KEEP_GAP = 0.45, 0.25         # 停顿超过 0.45 秒压成约 0.25 秒
DECISIONS = "精剪决定.json"

EDIT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["cuts", "captions"],
    "properties": {
        "cuts": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["line", "text", "why"],
            "properties": {"line": {"type": "integer"}, "text": {"type": "string"}, "why": {"type": "string"}}}},
        "captions": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["line", "text"],
            "properties": {"line": {"type": "integer"}, "text": {"type": "string"}}}}},
}

EDIT_PROMPT = """下面是""" + C.name + """直播（单人直播或连麦）里一个片段的逐行字幕（机器识别，可能有错字；连麦时可能混有对方的话）。
这段要做成短视频切片，现在节奏太拖，请标出要删掉的文字，让它听起来紧凑、利落：
{speaker_note}
要删：
- 口头禅和无意义的语气词：嗯、呃、啊、哦、额，以及当口头禅用的"就是说""然后""对吧"
- 结巴和重复：如"我我我""那那那""为为什么"，只留一个
- 说错后重说：删掉说错的前半句，保留后面说对的那句（例如"我觉得这个我觉得这件事很简单"删掉"我觉得这个"）。
  相邻两行开头一样、后半不同的，基本都是说错重说，删前一行
- 连麦对方的附和和打断：对对对、是是是、嗯嗯、哦哦、说得太好了，以及和主题无关的插话
- 重复表达同一个观点：同一个意思换个说法又讲了一遍，第二遍没有新的理由、例子、数字或递进。保留说得最清楚完整的那一遍，删掉其余几遍。
  跨多行的，每行各给一条删除。删之前确认删完前后能接上，不能删出半句话；结尾为了强调金句的有意重复保留；拿不准是不是重复的不删
- 和主题无关的直播杂音：念无关弹幕、和观众打招呼、"等一下""我看一下"

不要删：
- 任何影响意思的内容，包括论点、数字、例子、结论和笑点
- 否定词（不、没、别、未、无、非）所在的话，除非它是结巴或说错重说、后面原样又说了一遍
- 连麦对方或弹幕提出的问题，以及他念出来的观众提问（观众需要它理解后面的回答）
- 不确定的就不删；数字前后被识别成怪字的单个字（例如"百分之 我"里的"我"其实是"五"）不要删

- 片段最后如果开始了下一个话题的半句话（说完结论之后又起了个头），删掉

每条删除给出行号 line 和要删的原文 text。text 必须是那一行里连续的一段原文，一字不差地照抄（同一行可以有多条{no_tag}）。why 用几个字说明原因。

另外，这些字会直接做成字幕。机器识别有错字和识别错的地方（例如同音字、"5%"被识别成"百分之"、数字被拆开）。
逐行检查删完之后还有内容的行，有错就在 captions 里给出这一行改正后的字幕文字：只改识别错误，按他实际说的话写，被删的部分不要出现，不要加标点。
数字被截断或识别错的（例如前后文都在说250，某一行却是"20"），按上下文改正。没错的行不用给。
""" + "".join(r + "\n" for r in (C.get("review.extra_rules") or [])) + """
{lines}
"""

SPEAKER_NOTE = f"""
行首方括号是说话人（机器按声音分的，偶尔会错）：[{HOST}] 是{C.name}本人，[{GUEST}] 是连麦对方，[其他] 是背景声音。
- [{GUEST}] 的附和、接话（对对对、是是、太对了、说得太好了）删；[{GUEST}] 提的问题、引出话题的话保留
- [{HOST}] 的论点、例子、数字、结论不删，只删他的口头禅、结巴、说错重说和重复
"""


def is_latin(u):
    return u[:1].isascii() and u[:1].isalnum()


# ---------- 字级时间 ----------

def raw_dirs(subs):
    """plan 字幕对应的百炼原始结果目录候选（liveclip.asr：<输出名>_raw/），同场其他百炼结果作后备。"""
    p = C.ws(subs)
    base = re.sub(r"(_bailian.*|_reviewed)$", "", p.stem)   # review_subs 校对过的 <名>_reviewed.json 也找回原名
    out = []
    for name in (p.stem + "_raw", f"{base}_bailian_spk_raw", f"{base}_bailian_raw", f"{base}_bailian_whole_raw"):
        d = p.with_name(name)
        if d not in out:
            out.append(d)
    return out


def load_raw(d):
    """原始结果目录 → [(偏移毫秒, 接口 sentences)]。直连整条是 meta.json + partN.json，bl 分段是 chunks.json + NNN.json。"""
    for meta_name, key, fname in (("meta.json", "parts", "part{}.json"), ("chunks.json", "chunks", "{:03d}.json")):
        mf = d / meta_name
        if not mf.exists():
            continue
        spans = json.loads(mf.read_text(encoding="utf-8")).get(key) or []
        parts = []
        for i, (b, _) in enumerate(spans):
            f = d / fname.format(i)
            if not f.exists():
                return None
            parts.append((b, json.loads(f.read_text(encoding="utf-8"))["transcripts"][0].get("sentences", [])))
        return parts or None
    return None


def raw_tokens(parts, a, b):
    """原片 [a, b] 秒里的字：[{w, s, e, filler, p}]。中文词按字均分时间，英文/数字串整串一个；标点单独一项（p=True，不占时间）。"""
    toks = []
    for pi, (off, sents) in enumerate(parts):
        for sent in sents:
            if (sent["end_time"] + off) / 1000 < a - 2 or (sent["begin_time"] + off) / 1000 > b + 2:
                continue
            for w in sent.get("words", []):
                t = w["text"].strip()
                ws, we = (w["begin_time"] + off) / 1000, (w["end_time"] + off) / 1000
                units = [u for u in re.findall(r"[A-Za-z0-9.%+']+|\S", t) if u not in PUNCT]
                filler = bool(FILLER_WORD.match(t))
                for k, u in enumerate(units):
                    step = (we - ws) / len(units)
                    toks.append({"w": u, "s": ws + k * step, "e": ws + (k + 1) * step, "filler": filler, "part": pi})
                for ch in (w.get("punctuation") or "").strip():
                    toks.append({"w": ch, "s": we, "e": we, "p": True, "part": pi})
    toks.sort(key=lambda t: t["s"])
    out = []
    for t in toks:   # bl 分段之间重叠 0.4 秒，同一个字两份都有
        if not t.get("p") and any(not o.get("p") and o["part"] != t["part"] and o["w"] == t["w"] and abs(o["s"] - t["s"]) < 0.2
                                  for o in out[-8:]):
            continue
        out.append(t)
    out = [t for t in out if a - 0.05 <= t["s"] <= b]
    while out and out[0].get("p"):
        out.pop(0)
    for t in out:
        t.pop("part", None)
    return out


def sensevoice_tokens(video, a, b):
    """后备：对原片 [a, b] 重跑 SenseVoice（旧做法）。"""
    import soundfile as sf
    from .asr import SR, AutoModel, pick_device
    with tempfile.NamedTemporaryFile(suffix=".wav") as tmp:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", str(video), "-vn",
                        "-af", "aresample=async=1:first_pts=0", "-ac", "1", "-ar", str(SR), tmp.name],
                       check=True, stdin=subprocess.DEVNULL)
        audio, _ = sf.read(tmp.name, dtype="float32")
    vad = AutoModel(model="fsmn-vad", max_single_segment_time=15000, disable_update=True, log_level="ERROR")
    asr = AutoModel(model="iic/SenseVoiceSmall", device=pick_device(C.get("asr.device")), disable_update=True, log_level="ERROR")
    toks = []
    for s, e in vad.generate(input=audio, fs=SR, disable_pbar=True)[0]["value"]:
        chunk = audio[int(s * SR / 1000):int(e * SR / 1000)]
        if len(chunk) < SR * 0.3:
            continue
        r = asr.generate(input=chunk, fs=SR, language="auto", use_itn=True, disable_pbar=True, output_timestamp=True)[0]
        for w, (ts, te) in zip(r.get("words", []), r.get("timestamp", [])):
            toks.append({"w": w, "s": a + s / 1000 + ts / 1000, "e": a + s / 1000 + te / 1000,
                         "filler": w in ("嗯", "呃"), **({"p": True} if w in PUNCT else {})})
    return toks


def spk_body(plan):
    """带说话人的整条字幕：plan 字幕本身有 spk 就用，否则找同场的 _bailian_spk / _bailian_whole。返回 (文件, body)。"""
    p = C.ws(plan["subs"])
    base = re.sub(r"(_bailian.*|_reviewed)$", "", p.stem)   # review_subs 校对过的 <名>_reviewed.json 也找回原名
    for f in (p, p.with_name(f"{base}_bailian_spk.json"), p.with_name(f"{base}_bailian_whole.json")):
        if f.exists():
            body = json.loads(f.read_text(encoding="utf-8"))["body"]
            if any("spk" in q for q in body[:200]):
                return f, body
    return None, None


def cue_at(cues, froms, t, tol=0.1):
    """t 秒落在哪一句（下标）；落在两句之间时取近的，离得超过 1 秒算没有。"""
    k = bisect_right(froms, t) - 1
    if k >= 0 and t <= cues[k]["to"] + tol:
        return k
    best = min((x for x in (k, k + 1) if 0 <= x < len(cues)),
               key=lambda x: min(abs(t - cues[x]["from"]), abs(t - cues[x]["to"])), default=None)
    if best is None or min(abs(t - cues[best]["from"]), abs(t - cues[best]["to"])) > 1.0:
        return None
    return best


def fix_tokens(toks, cues):
    """plan 字幕是 liveclip.review.subs 整场校对过的（每句带 orig）：把改过的字写回百炼的逐字结果，精剪给 Claude 看的原文、
    删过字的字幕行都用校对后的字（「人减的」→「人剪的」）。中文按等长替换逐字改；英文词整词换成校对后对应的那段（hirre→hire）；
    增删字、这一句的逐字结果和 orig 对不上的不动。返回 (改到的句数, 对不上的句数)。"""
    froms = [q["from"] for q in cues]
    by = {}
    for i, t in enumerate(toks):
        if not t.get("p"):
            k = cue_at(cues, froms, (t["s"] + t["e"]) / 2)
            if k is not None:
                by.setdefault(k, []).append(i)
    ok = miss = 0
    for k, q in enumerate(cues):
        if not q.get("orig") or q["orig"] == q["content"] or k not in by:
            continue
        a, b = norm_text(q["orig"]), norm_text(q["content"])
        if "".join(toks[i]["w"] for i in by[k]).lower() != a.lower():
            miss += 1
            continue
        ops = difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
        same = {x for op, a0, a1, _, _ in ops if op == "equal" for x in range(a0, a1)}
        sub = {a0 + d: b[b0 + d] for op, a0, a1, b0, b1 in ops if op == "replace" and a1 - a0 == b1 - b0 for d in range(a1 - a0)}

        def bpos(x, end):
            """a 里的位置 x 对到 b：落在相同段里按偏移，落在改动段里取改动段的头（end 时取尾）。"""
            for op, a0, a1, b0, b1 in ops:
                if (a0 < x <= a1) if end else (a0 <= x < a1):
                    return b0 + (x - a0) if op == "equal" else (b1 if end else b0)
            return len(b) if end else 0
        hit, x = False, 0
        for i in by[k]:
            w = toks[i]["w"]
            span = range(x, x + len(w))
            x += len(w)
            if is_latin(w):         # 英文词里插字（ags→agents）也算，所以不看是不是全在相同段里
                nw = b[bpos(span.start, False):bpos(span.stop, True)]
            elif all(j in same for j in span):
                continue
            elif all(j in same or j in sub for j in span):
                nw = "".join(sub.get(j, a[j]) for j in span)
            else:
                continue
            if nw and nw != w:
                toks[i]["w"] = nw
                hit = True
        ok += hit
    return ok, miss


# ---------- 分行、删除 ----------

def utext(toks, idx):
    out, prev = "", ""
    for i in idx:
        w = toks[i]["w"]
        if prev and is_latin(w) and is_latin(prev[-1:]) and not (w[:1].isdigit() and prev[-1:].isdigit()):
            out += " "          # 英文词之间加空格；百炼把数字拆成一位一个词（「2」「0」），数字之间不加
        out += w
        prev = w
    return out


def make_lines(toks, cues):
    """按字幕句分行（没有字幕句时按标点、0.3 秒停顿、25 字），换说话人再断开。返回 [{"idx", "cue", "spk"}]。"""
    froms = [q["from"] for q in cues] if cues else []
    lines, cur, cur_cue = [], None, None
    for i, t in enumerate(toks):
        if t.get("p"):
            if cur:
                cur["idx"].append(i)
                if not cues:
                    cur = None
            continue
        cue = cue_at(cues, froms, (t["s"] + t["e"]) / 2) if cues else None
        brk = cur is None
        if cur and cues and cue != cur_cue:
            brk = True
        if cur and t.get("spk") and cur["spk"] and t["spk"] != cur["spk"]:
            brk = True
        if cur and not cues:
            last = toks[[k for k in cur["idx"] if not toks[k].get("p")][-1]]
            brk = brk or t["s"] - last["e"] > 0.3 or len(cur["idx"]) >= 25
        if brk:
            cur = {"idx": [], "cue": cue, "spk": t.get("spk")}
            lines.append(cur)
            cur_cue = cue
        cur["idx"].append(i)
    for ln in lines:   # 行首行尾的标点不要（行尾留着给字幕；行首的去掉）
        while ln["idx"] and toks[ln["idx"][0]].get("p"):
            ln["idx"].pop(0)
    return [ln for ln in lines if ln["idx"]]


def line_chars(toks, ln):
    """一行去掉标点和空格后的逐字 [(字, token 下标)]，用来定位删除的原文。"""
    return [(ch, i) for i in ln["idx"] if not toks[i].get("p") for ch in toks[i]["w"] if not ch.isspace()]


def norm_text(s):
    return "".join(ch for ch in s if ch not in PUNCT and not ch.isspace() and ch not in "[]【】")


def neg_snippet(chars, j):
    """否定字和它后面第一个不同的字（「不不是」取「不是」），用来判断附近有没有原样重说。"""
    ch = chars[j][0]
    nxt = next((c for c, _ in chars[j + 1:] if c != ch), "")
    return ch + nxt


def apply_edits(lines, toks, cuts):
    """Claude 给的删除原文 → 要删的 token 下标。返回 (deleted, 没定位到, 没采用)。
    否定字保护：删掉的字里有否定字，而删完以后前后 NEAR 行（含本行）留下的字里没有同样的「否定字+下一个字」，这条不采用
    （「对不对」「是不是」这种口头禅反问除外）。"""
    spans, missed = [], []
    for c in cuts:
        if not 0 <= c["line"] < len(lines) or not norm_text(c["text"]):
            missed.append(c)
            continue
        chars = line_chars(toks, lines[c["line"]])
        s = "".join(ch for ch, _ in chars)
        pos = s.find(norm_text(c["text"]))
        if pos < 0:
            missed.append(c)
            continue
        js = range(pos, pos + len(norm_text(c["text"])))
        negs = [] if TAG_QUESTION.match(norm_text(c["text"])) else [neg_snippet(chars, j) for j in js if chars[j][0] in NEGATIONS]
        spans.append((c, {chars[j][1] for j in js}, negs))
    rejected = []
    while True:
        deleted = set().union(*(s for _, s, _ in spans)) if spans else set()
        bad = None
        for k, (c, s, negs) in enumerate(spans):
            if not negs:
                continue
            n = c["line"]
            near = "".join(ch for m in range(max(0, n - NEAR), min(len(lines), n + NEAR + 1))
                           for ch, i in line_chars(toks, lines[m]) if i not in deleted)
            if not all(x in near for x in negs):
                bad = k
                break
        if bad is None:
            return deleted, missed, rejected
        c = spans.pop(bad)[0]
        rejected.append({**c, "没采用": "删掉了否定字，附近没有同样的说法（不是结巴或重说）"})


def auto_deletes(lines, toks):
    """不靠模型的删除：整词语气词；连麦对方单独一行的附和。"""
    fill = {i for i, t in enumerate(toks) if t.get("filler")}
    back = []
    for n, ln in enumerate(lines):
        if ln["spk"] and ln["spk"] != HOST:
            s = norm_text(utext(toks, [i for i in ln["idx"] if not toks[i].get("p")]))
            if s and len(s) <= 6 and BACKCHANNEL.match(s):
                back.append(n)
    return fill, back


def keep_segments(toks, kept, a, b):
    """保留的字 → 原片里的小段 [(开始, 结束)]，限定在 [a, b] 内。
    相邻两个保留的字之间：有被删的字就断开（切点不压到被删的字）；停顿超过 MAX_GAP 也断开，
    只留前字之后 PAD_AFTER + 后字之前 PAD_BEFORE ≈ 0.3 秒；否则连在一起。"""
    idx = sorted(i for i in kept if a - 0.05 <= toks[i]["s"] <= b)
    segs = []
    for j, i in enumerate(idx):
        s, e = max(a, toks[i]["s"] - PAD_BEFORE), min(b, toks[i]["e"] + PAD_AFTER)
        if not segs:
            segs.append([s, e])
            continue
        p = idx[j - 1]
        dels = [k for k in range(p + 1, i) if not toks[k].get("p")]
        if dels:
            segs[-1][1] = min(segs[-1][1], toks[dels[0]]["s"] - 0.02)
            segs.append([max(s, min(toks[dels[-1]]["e"] + DEL_TAIL, toks[i]["s"] - 0.03), segs[-1][1]), e])
        elif toks[i]["s"] - toks[p]["e"] > MAX_GAP:
            segs.append([max(s, segs[-1][1]), e])
        else:
            segs[-1][1] = max(segs[-1][1], e)
    if segs:
        segs[-1][1] = min(b, max(segs[-1][1], toks[idx[-1]]["e"] + END_TAIL))
    return [(round(s, 3), round(e, 3)) for s, e in segs if e - s > 0.05]


def ask_edits(lines, toks):
    has_spk = any(ln["spk"] for ln in lines)
    text = "\n".join(f"{n}: {('[' + ln['spk'] + '] ') if has_spk and ln['spk'] else ''}{utext(toks, ln['idx'])}"
                     for n, ln in enumerate(lines))
    prompt = (EDIT_PROMPT.replace("{lines}", text).replace("{speaker_note}", SPEAKER_NOTE if has_spk else "")
              .replace("{no_tag}", "；不要带行首的方括号" if has_spk else ""))
    return ask_json(prompt, EDIT_SCHEMA, model=MODEL, tool="精剪")


# ---------- 字幕 ----------

def tidy(s):
    s = re.sub(r"([，,。！？!?；;、：:])[，,。！？!?；;、：:]+", lambda m: m.group(0)[-1], s)
    return s.lstrip("，,。！？!?；;、：: ")


def caption_rows(lines, toks, kept, segs, offset, cue_text, corrected):
    """按字幕句出字幕，时间换算到拼接后的新时间轴。返回 [(文字, 开始, 结束)]。"""
    def new_t(t):
        acc = 0.0
        for s, e in segs:
            if t < s:
                return offset + acc
            if t <= e:
                return offset + acc + t - s
            acc += e - s
        return offset + acc
    lo, hi = segs[0][0], segs[-1][1]
    inwin = lambda i: lo - 0.05 <= toks[i]["s"] <= hi
    groups = []
    for n, ln in enumerate(lines):
        if groups and ln["cue"] is not None and groups[-1][0] == ln["cue"]:
            groups[-1][1].append(n)
        else:
            groups.append((ln["cue"], [n]))
    rows = []
    for cue, ns in groups:
        words = [i for n in ns for i in lines[n]["idx"] if not toks[i].get("p")]
        ks = [i for i in words if i in kept and inwin(i)]
        if not ks:
            continue
        if cue is not None and cue in cue_text and all(i in kept and inwin(i) for i in words):
            text = cue_text[cue]          # 一个字没删、整句都在：用逐句校对过的原句
        else:
            parts = []
            for n in ns:
                li = [i for i in lines[n]["idx"] if (toks[i].get("p") or i in kept) and inwin(i)]
                if not any(not toks[i].get("p") for i in li):
                    continue
                raw = utext(toks, li)
                fix = corrected.get(n) if all(inwin(i) for i in lines[n]["idx"]) else None
                if fix and not any(fix.count(ch) < raw.count(ch) for ch in NEGATIONS):
                    raw = fix     # 改字幕时丢了否定字，意思会反（「我从来不去做」→「我从来去做」）
                parts.append(raw)
            text = tidy("".join(parts))
        text = cc.clean_caption(text)
        if not text:
            continue
        rows.append([text, new_t(toks[ks[0]]["s"] - 0.05), new_t(toks[ks[-1]]["e"] + PAD_AFTER)])
    merged = []
    for r in rows:      # 不超过 2 个字的碎片并到下一行（"我""所以"单独闪一下很怪）
        if merged and len(merged[-1][0]) <= 2 and r[1] - merged[-1][2] < 0.6:
            merged[-1] = [merged[-1][0] + r[0], merged[-1][1], r[2]]
        else:
            merged.append(r)
    merged = [r for r in merged if r[2] - r[1] >= 0.2]
    for k in range(len(merged) - 1):   # 不重叠
        merged[k][2] = min(merged[k][2], merged[k + 1][1])
    return merged


# ---------- 每段 ----------

class Ctx:
    def __init__(self, bv, planf, allow_check):
        self.bv, self.planf = bv, planf
        self.plan = json.loads((C.CLIPS / bv / planf).read_text(encoding="utf-8"))
        self.video = cc.find_video(bv, self.plan)
        self.role = cc.manifest_row(bv).get("role")
        self.lay = cc.make_layout(self.video, self.plan, None, self.role)
        self.live = self.plan.get("live", self.role == "livestream_recording")
        self.burn = self.plan.get("burn_subs", self.lay["name"] in ("live_v", "duet_v"))
        self.timing = cc.plan_cues(self.plan)
        self.subs_reviewed = any("orig" in q for q in (self.timing or []))     # liveclip.review.subs 的输出
        self.allow_check = allow_check
        self.outdir = C.CLIPS / bv / (Path(planf).stem + "_tight")
        self.raw, self.raw_dir = None, None
        if self.plan.get("subs"):
            for d in raw_dirs(self.plan["subs"]):
                parts = load_raw(d)
                if parts:
                    self.raw, self.raw_dir = parts, d
                    break
        self.spk_file, body = spk_body(self.plan) if self.plan.get("subs") else (None, None)
        self.spk_cues = body
        self.spk_froms = [q["from"] for q in body] if body else None


def prepare(ctx, i):
    c = ctx.plan["clips"][i - 1]
    over = {k: c[k] for k in cc.CLIP_LAYOUT_KEYS if k in c}
    lay_c = cc.make_layout(ctx.video, {**ctx.plan, **over}, None, ctx.role) if over else ctx.lay
    b = cc.bounds(ctx.video, c, snap=not ctx.live, cues=ctx.timing)
    start, end, h0, h1 = b
    a_ = min(start, h0) if h0 is not None else start
    b_ = max(end, h1) if h1 is not None else end
    if ctx.raw:
        toks, how = raw_tokens(ctx.raw, a_, b_), f"百炼原始字级时间 {C.rel(ctx.raw_dir)}"
    else:
        toks, how = sensevoice_tokens(ctx.video, a_, b_), "没有百炼原始结果，SenseVoice 重新识别这一段"
    if ctx.spk_cues:
        for t in toks:
            if not t.get("p"):
                k = cue_at(ctx.spk_cues, ctx.spk_froms, (t["s"] + t["e"]) / 2, tol=0.05)
                t["spk"] = ctx.spk_cues[k].get("spk") if k is not None else None
    reviewed = ctx.burn and bool(c.get("captions"))
    if reviewed and not ctx.allow_check:
        pending = [x for x in c["captions"] if x.get("status") == "check"]
        if pending:
            raise RuntimeError(f"还有 {len(pending)} 句字幕待人确认（status=check），加 --allow-check 照剪")
    cues = ([{"from": x["from"], "to": x["to"], "content": x["text"]} for x in c["captions"]] if reviewed
            else (ctx.timing or None))
    if cues:
        cues = [q for q in cues if q["to"] >= a_ - 2 and q["from"] <= b_ + 2]
    fixed = None
    if cues and not reviewed and ctx.subs_reviewed:       # 整场校对过的字幕：校对后的字写回逐字结果，整句没删的行用校对后的原句
        fixed = fix_tokens(toks, cues)
    lines = make_lines(toks, cues)
    cue_text = {k: q["content"] for k, q in enumerate(cues)} if reviewed or ctx.subs_reviewed else {}
    src = [[ln["spk"] or "", utext(toks, ln["idx"])] for ln in lines]
    key = hashlib.sha1(json.dumps(src, ensure_ascii=False).encode()).hexdigest()[:16]
    name = f"{i:02d}_{slug(c['title'])}"
    if fixed:
        how += f"；整场校对的改字写回 {fixed[0]} 句" + (f"（{fixed[1]} 句逐字结果和原文对不上，没写回）" if fixed[1] else "")
    return {"i": i, "c": c, "lay": lay_c, "b": b, "toks": toks, "how": how, "lines": lines, "cue_text": cue_text,
            "src": src, "key": key, "name": name}


def render(ctx, job, edits):
    toks, lines, c, lay = job["toks"], job["lines"], job["c"], job["lay"]
    start, end, h0, h1 = job["b"]
    deleted, missed, rejected = apply_edits(lines, toks, edits["cuts"])
    fill, back = auto_deletes(lines, toks)
    deleted |= fill
    for n in back:
        deleted |= set(lines[n]["idx"])
    kept = {i for i, t in enumerate(toks) if i not in deleted and not t.get("p")}
    hook_segs = keep_segments(toks, kept, h0, h1) if h0 is not None else []
    main_segs = keep_segments(toks, kept, start, end)
    if not main_segs:
        raise RuntimeError("删完没剩下内容")
    hook_segs, main_segs = cc.snap_segs((hook_segs, main_segs))    # 对齐到剪辑用的帧格，字幕时间和存下的保留段与成片一致

    outdir, name = ctx.outdir, job["name"]
    outdir.mkdir(exist_ok=True)
    png, tag, endc = outdir / f"_{name}_title.png", outdir / f"_{name}_tag.png", outdir / f"_{name}_end.png"
    cc.overlay_png(c["title"], png, lay["title_y"], shade=lay["full"])
    from PIL import Image
    Image.new("RGBA", (cc.W, cc.H), (0, 0, 0, 0)).save(tag)     # 「高能预告」标签默认不显示（同 cut_clips）
    if cc.end_card_text(ctx.live):
        cc.label_png(cc.end_card_text(ctx.live), lay["title_y"] + 30, endc, fg=(255, 255, 255), bg=(0, 0, 0, 200))
    else:
        Image.new("RGBA", (cc.W, cc.H), (0, 0, 0, 0)).save(endc)
    hook_len = sum(e - s for s, e in hook_segs)
    corrected = {x["line"]: x["text"] for x in edits.get("captions", [])}
    rows = []
    if ctx.burn and (ctx.plan.get("subs") or c.get("captions")):
        rows = (caption_rows(lines, toks, kept, hook_segs, 0.0, job["cue_text"], corrected) if hook_segs else []) + \
            caption_rows(lines, toks, kept, main_segs, hook_len, job["cue_text"], corrected)
        for k in range(len(rows) - 1):
            rows[k][2] = min(rows[k][2], rows[k + 1][1])
    caps = []
    for k, (text, t0, t1) in enumerate(rows):
        p = outdir / f"_{name}_cap{k}.png"
        cc.caption_png(text, lay["cap_y"], p)
        caps.append((p, t0, t1))
    probe = c.get("duo_detect", ctx.plan.get("duo_detect"))
    if probe:   # 横屏播客单人/双人切换：每个保留段各查一次，偏移按拼接后时间轴
        spans, acc = [], 0.0
        for s, e in list(hook_segs) + list(main_segs):
            spans.append((s, e, acc))
            acc += e - s
        ivs = cc.detect_duo(ctx.video, spans, probe)
        if ivs:
            sw_, sh_ = lay["src"]
            alt_h = int(round(sh_ * cc.W / sw_ / 2)) * 2
            lay = {**lay, "alt": {"h": alt_h, "y": lay["fg_y"] + (lay["fg_h"] - alt_h) // 2,
                                  "expr": "+".join(f"between(t,{a:.2f},{bb:.2f})" for a, bb in ivs)}}
    try:
        cc.cut(ctx.video, None, [png, tag, endc], outdir / f"{name}.mp4", lay, caps, segs=(hook_segs, main_segs))
    finally:
        for p in [png, tag, endc] + [x[0] for x in caps]:
            p.unlink(missing_ok=True)

    old = end - start + ((h1 - h0) if h0 is not None else 0)
    new = hook_len + sum(e - s for s, e in main_segs)
    left = "".join(toks[i]["w"] for i in sorted(kept))
    info = {
        "原长": round(old, 2), "精剪后": round(new, 2), "决定": job["key"], "字级时间": job["how"],
        "说话人": C.rel(ctx.spk_file) if ctx.spk_file else None,
        "版式": lay["name"], "起止": [round(x, 3) if x is not None else None for x in job["b"]],
        "删除": edits["cuts"], "没定位到": missed, "没采用": rejected,
        "自动删除": {"语气词": len(fill), "附和": [job["src"][n][1] for n in back]},
        "字幕改正": edits.get("captions", []),
        "剩余呃": left.count("呃"), "剩余嗯": left.count("嗯"),
        "字幕": [[round(t0, 2), round(t1, 2), text] for text, t0, t1 in rows],
        "保留段": {"预告": hook_segs, "正片": main_segs},
        "原文": [f"[{s}] {t}" if s else t for s, t in job["src"]],
        "精剪后文字": [utext(toks, [i for i in ln["idx"] if i in kept]) for ln in lines],
        "词": [[t["w"], round(t["s"], 3), round(t["e"], 3), int(i in kept)] for i, t in enumerate(toks) if not t.get("p")],
    }
    (outdir / f"{name}.json").write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    return info


def main(argv=None):
    ap = argparse.ArgumentParser(prog="liveclip tight", description="精剪：去口头禅、卡壳、重说、对方附和，压长停顿")
    ap.add_argument("bv")
    ap.add_argument("plan")
    ap.add_argument("n", nargs="?", help="第几段（旧用法）；多段用 --only")
    ap.add_argument("--only", help="只做这几段，如 1,2,3；不给就做 plan 里全部")
    ap.add_argument("--fresh", action="store_true", help="不用存过的删除决定，重新问 Claude")
    ap.add_argument("--reuse", action="store_true", help="只用存过的删除决定，没有的段跳过（不调 Claude）")
    ap.add_argument("--allow-check", action="store_true", help="校对结果里还有 status=check 的字幕也照剪")
    ap.add_argument("--jobs", type=int, default=4, help="同时问 Claude 的段数")
    a = ap.parse_args(argv)
    os.chdir(ROOT)      # plan 里的字幕、视频路径都相对工作区
    t_all = time.time()
    ctx = Ctx(a.bv, a.plan, a.allow_check)
    if a.only:
        todo = [int(x) for x in a.only.split(",") if x]
    elif a.n:
        todo = [int(a.n)]
    else:
        todo = list(range(1, len(ctx.plan["clips"]) + 1))
    print(f"{ctx.video}  layout={ctx.lay['name']}  字级时间：{C.rel(ctx.raw_dir) if ctx.raw_dir else 'SenseVoice 重识别'}  "
          f"说话人：{C.rel(ctx.spk_file) if ctx.spk_file else '无'}", flush=True)
    ctx.outdir.mkdir(exist_ok=True)
    dec_f = ctx.outdir / DECISIONS
    decisions = json.loads(dec_f.read_text(encoding="utf-8")) if dec_f.exists() else {}

    jobs, failed = [], []
    for i in todo:
        try:
            jobs.append(prepare(ctx, i))
        except (RuntimeError, ValueError) as e:
            failed.append(i)
            print(f"第 {i} 段跳过：{e}", flush=True)
    ask = [j for j in jobs if a.fresh or j["key"] not in decisions]
    if a.reuse and ask:
        for j in ask:
            failed.append(j["i"])
            print(f"第 {j['i']} 段没有存过的删除决定（原文变了或没做过），--reuse 下跳过", flush=True)
        jobs = [j for j in jobs if j not in ask]
        ask = []
    if ask:
        print(f"问 Claude（{MODEL}）{len(ask)} 段：{','.join(str(j['i']) for j in ask)}；复用 {len(jobs) - len(ask)} 段", flush=True)

        def one(j):
            try:
                return j, ask_edits(j["lines"], j["toks"])
            except Exception as e:      # noqa: BLE001  单段失败不拖垮其他段
                return j, e
        with ThreadPoolExecutor(max_workers=max(1, a.jobs)) as ex:
            for j, res in ex.map(one, ask):
                if isinstance(res, Exception):
                    failed.append(j["i"])
                    print(f"第 {j['i']} 段问 Claude 失败：{res}", flush=True)
                    continue
                decisions[j["key"]] = {"clip": j["i"], "title": j["c"]["title"].replace("\n", " "), "model": MODEL,
                                       "at": time.strftime("%Y-%m-%d %H:%M"), "cuts": res["cuts"],
                                       "captions": res.get("captions", [])}
        dec_f.write_text(json.dumps(decisions, ensure_ascii=False, indent=1), encoding="utf-8")
    for j in jobs:
        if j["key"] not in decisions:
            continue
        t0 = time.time()
        try:
            info = render(ctx, j, decisions[j["key"]])
        except (subprocess.CalledProcessError, RuntimeError) as e:
            failed.append(j["i"])
            (ctx.outdir / f"{j['name']}.mp4").unlink(missing_ok=True)
            print(f"{j['name']}.mp4  失败：{e}", flush=True)
            continue
        main_n = len(info["保留段"]["正片"])
        print(f"{j['name']}.mp4  {info['原长']:.1f}s → {info['精剪后']:.1f}s（-{(1 - info['精剪后'] / info['原长']) * 100:.0f}%），"
              f"Claude 删 {len(info['删除'])} 处（没定位到 {len(info['没定位到'])}，没采用 {len(info['没采用'])}），"
              f"语气词 {info['自动删除']['语气词']} 个、对方附和 {len(info['自动删除']['附和'])} 行，{main_n} 个小段，"
              f"剩余呃 {info['剩余呃']}，用时 {time.time() - t0:.0f}s", flush=True)
    print(f"精剪总用时 {time.time() - t_all:.0f}s" + (f"；失败/跳过的段: {','.join(map(str, sorted(set(failed))))}" if failed else ""))


if __name__ == "__main__":
    main()
