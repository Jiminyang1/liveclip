"""口头禅混剪：从几场直播的字幕里找同一句话，每次截 0.8–2 秒，拼成 20–35 秒的竖屏混剪。

起因：同一个人在多场里反复说同一句口头禅，剪成 25–35 秒的混剪，是一类分享数很高的格式；pick 只挑连续片段，做不了。

用法:
    liveclip montage --suggest [--all-recent 6] [--min-sessions 3]
    liveclip montage --phrase 某句话 [--phrase 别的说法 …] [--sessions LIVE_20260101_2000_main …|--all-recent 3]
        [--speaker 主播] [--max 25] [--target 30] [--pad 0.25] [--min-hit 0.8] [--max-hit 2.0]
        [--caption words|phrase|none] [--counter] [--allow-duet] [--title 标题] [--alias 错字=正字 …] [--out deliver/混剪_<slug>/]

--suggest：扫最近几场字幕（只算主播说的句子；字幕没分人的整场都算），列出两类候选，帮着挑口头禅（需要 jieba）：
  ① 单独成句的短句（「这是实话」「有没有道理」这种整句就是它的）；② 高频特色词组（按 jieba 切词对齐，2–8 字，
  至少出现在 --min-sessions 场里；和 jieba 通用词频比明显偏高、或多词搭配远超偶然的才留；然后/就是/对对对/比如说 这类口水话去掉）。
  不要视频，旧场次视频归档到云盘了也能扫。

出混剪：
1. 每场找字幕：<ID>_bailian_spk_reviewed > _reviewed > _bailian_spk > _bailian_whole > _bailian > <ID>.json（校对过的优先，
   同音错字校对时已按上下文改过）；字幕没分人时找同场 _bailian_spk 按时间补说话人。
   匹配用字幕原文 + 别名表（config montage.aliases，百炼常见同音错字，--alias 可加）。
2. 字级时间：百炼原始结果里有逐词时间（data/asr/<ID>_bailian_spk_raw/ 等整场结果，没有或缺份时用跟播逐段的
   recordings/live/*/<场>/*.asr_raw/ + state.json 里的分段偏移），和字幕句子逐字对齐找到这句话的起止。都没有时按字数在句内估。
3. 每次截取：这句话所在的小句（两个标点之间）不超过 --max-hit 秒就整句用；否则以这句话为中心按整词往两边扩到 --min-hit 以上。
   不切半个词；再按声音定刀口（refine）：首词前、尾词后要各有一段 ≥40ms 的停顿，刀下在停顿里、离说话最多 --pad 秒，
   不越过相邻词；识别结果里前后词挨着（一口气说下来）、或停顿里夹进了别的词的，这次不要（百炼词时间常差 0.1 秒，只按词时间切会带字/吃尾音）。
4. 挑：主播说的、整句的、这句话占比高的、不带嗯呃和结巴碎字的优先；各场轮流拿（同场两次至少隔 30 秒），单人满屏画面优先
   （分屏的只在不够时补、而且要分过人确定是主播；--allow-duet 同等对待）；字面撞上的别的词整句排除（montage.exclude / --exclude），
   凑够 --max 条或总长到 --target 秒为止；成片按场次、时间先后排。
5. 出片：每段先按自己的画面（liveclip.layout.detect_layout 判单人整幅/连麦分屏，同成片）合成 1080×1920（模糊背景 + 原画，同 cut），
   声音每段先按说话段响度拉齐、两头各 40ms 淡入淡出；再整条交给 cut._render 加标题、字幕、尾卡、loudnorm -14 LUFS、同一套编码参数。
6. 交付：<out>/01_<标题>/{视频.mp4, 封面.jpg, 封面_B站.jpg, 封面_横4比3.jpg, 文案.txt}（发布工具按普通切片处理），
   <out>/混剪清单.json（每段的场次、时间、文字、版式、增益）、验收.json + 抽帧/（verify_clips 完整解码）。
标题默认「一场直播说了 N 次 X」/「3 场直播说了 N 次 X」（N = 这几场里主播说这句话的总次数，按字幕数，非人工核对）；--title 覆盖。
没做：音画复核（每段边界要人听一遍）；面板「出片墙」只列跟播和手动上传，混剪目录要用发布工具直接指路径。
"""
import argparse
import collections
import json
import logging
import math
import re
import subprocess
import sys
import tempfile
import time
from bisect import bisect_left
from datetime import date, datetime
from pathlib import Path

from . import cut as cc
from .caption import caption_text
from .config import C, py_mod
from .cut import slug as title_slug
from .layout import detect_layout, split_band
from .tight import raw_dirs

ROOT = C.ROOT
ASR = C.ASR
SUBS_ORDER = ["_bailian_spk_reviewed", "_bailian_whole_reviewed", "_bailian_reviewed", "_reviewed",
              "_bailian_spk", "_bailian_whole", "_bailian", ""]
# 百炼常见同音错字（校对过的字幕多数已改正；没校对的场次靠这张表）：config montage.aliases，正字 → [错字]
ALIASES = {"对嘞": ["对咧", "对勒", "对哩"], **(C.get("montage.aliases") or {})}
# 字面含这句话、其实是别的词的（热词把别的词带偏），这些整句不要：config montage.exclude；--exclude 可加
EXCLUDE = dict(C.get("montage.exclude") or {})
# 口水话：--suggest 里不算口头禅
FILLER = set("""然后 就是 就是说 对对对 对对 对 对吧 对不对 对啊 对呀 对的 是的 是吧 好吧 好的 好了 嗯 啊 哦 呃 OK ok 那个 这个 其实 因为 所以
但是 不是 没有 真的 比如说 你比如说 就比如说 比如说你 比如说我 我觉得 你觉得 觉得 什么 为什么 怎么 怎么说呢 有没有 是不是 可以
然后呢 所以说 我会 嘛对 哎呀 有人说 的话 一个 这样 那么 还是 已经 自己 现在 时候 感谢 感谢感谢 谢谢 哈哈 哈哈哈 来来来 好好好 是是是 嘛对吧 的嘛 对吧对吧""".split())
FADE = 0.04          # 每段两头淡入淡出，防爆音
PUNCT_RE = re.compile(r"[，,。！？!?；;、：:…\s]")
log = print


# ---------- 场次和字幕 ----------

def base_id(stem):
    return re.sub(r"(_bailian.*|_reviewed|_sv|_p\d+|_v\d+|_map|_zh.*)$", "", stem)


def session_time(sid):
    """场次 ID → 开播时间（排「最近」用）；认不出的返回 None。"""
    m = re.match(r"(?:LIVE|UP)_(\d{8})_(\d{4})", sid)
    if m:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M")
    return None


def all_sessions():
    ids = {base_id(p.stem) for p in list(ASR.glob("LIVE_*.json")) + list(ASR.glob("UP_*.json"))}
    return sorted((s for s in ids if session_time(s)), key=session_time, reverse=True)


def subs_file(sid):
    for suf in SUBS_ORDER:
        p = ASR / f"{sid}{suf}.json"
        if p.exists():
            body = json.loads(p.read_text(encoding="utf-8")).get("body") or []
            if body:
                return p, body
    return None, []


def with_speakers(sid, path, body):
    """字幕没分人时，从同场分人字幕按时间补 spk（取时间重叠最多的那句）。"""
    if any("spk" in q for q in body[:300]):
        return body
    for suf in ("_bailian_spk_reviewed", "_bailian_spk", "_bailian_whole"):
        p = ASR / f"{sid}{suf}.json"
        if p.exists() and p != path:
            ref = json.loads(p.read_text(encoding="utf-8"))["body"]
            if not any("spk" in q for q in ref[:300]):
                continue
            froms = [q["from"] for q in ref]
            for q in body:
                k = bisect_left(froms, q["to"])
                near = [r for r in ref[max(0, k - 4):k + 1] if min(r["to"], q["to"]) - max(r["from"], q["from"]) > 0]
                if near:
                    q["spk"] = max(near, key=lambda r: min(r["to"], q["to"]) - max(r["from"], q["from"]))["spk"]
            return body
    return body


def session_video(sid):
    for p in (C.VIDEOS / f"{sid}.mp4", C.DELIVER / f"直播跟播_{sid}" / "完整版" / "录屏.mp4"):
        if p.exists():
            return p
    row = cc.manifest_row(sid)
    p = row.get("media_path")
    return Path(p) if p and Path(p).exists() else None


# ---------- 字级时间（百炼原始结果） ----------

def _parts(d):
    """原始结果目录 → [(偏移毫秒, 止毫秒, sentences)]；和 tight_edit.load_raw 一样认两种格式，但缺了的份跳过（不整个作废）。"""
    for meta_name, key, fname in (("meta.json", "parts", "part{}.json"), ("chunks.json", "chunks", "{:03d}.json")):
        mf = d / meta_name
        if not mf.exists():
            continue
        out = []
        for i, (b, e) in enumerate(json.loads(mf.read_text(encoding="utf-8")).get(key) or []):
            f = d / fname.format(i)
            if f.exists():
                out.append((b, e, json.loads(f.read_text(encoding="utf-8"))["transcripts"][0].get("sentences", [])))
        return out
    return []


def _words(parts, extra_off=0.0):
    ws = []
    for off, _, sents in parts:
        for s in sents:
            for w in s.get("words", []):
                t = w["text"].strip()
                if t:
                    ws.append({"t": t, "s": (w["begin_time"] + off) / 1000 + extra_off, "e": (w["end_time"] + off) / 1000 + extra_off,
                               "p": (w.get("punctuation") or "").strip()})
    return ws


class WordIndex:
    """一场的逐词时间。来源按顺序：整场百炼原始结果（字幕同名 _raw/、同场 _bailian_spk_raw/ 等），跟播逐段 .asr_raw/。"""

    def __init__(self, sid, subs_path):
        self.sources = []
        seen = set()
        for d in raw_dirs(C.rel(subs_path)):
            if d.exists() and d not in seen:
                seen.add(d)
                ps = _parts(d)
                if ps:
                    self.sources.append((d.name, sorted(_words(ps), key=lambda w: w["s"])))
        m = re.match(r"LIVE_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})", sid)
        if m:
            pat = f"*_{m[1]}-{m[2]}-{m[3]}_{m[4]}-{m[5]}-*/state.json"
            for st in (C.RECORDINGS / "live").glob(f"*/{pat}"):
                ws = []
                for seg in json.loads(st.read_text(encoding="utf-8")).get("segments", []):
                    d = st.parent / (Path(seg["name"]).stem + ".asr_raw")
                    if d.exists():
                        ws += _words(_parts(d), seg.get("offset", 0.0))
                if ws:
                    self.sources.append(("跟播逐段", sorted(ws, key=lambda w: w["s"])))
        self.starts = [[w["s"] for w in ws] for _, ws in self.sources]

    def window(self, k, a, b):
        ws = self.sources[k][1]
        i = bisect_left(self.starts[k], a)
        out = []
        while i < len(ws) and ws[i]["s"] <= b:
            out.append(ws[i]); i += 1
        return out


UNIT = re.compile(r"[A-Za-z0-9.%+']+|[^\W_]")


def units(text, keep_case=False):
    return UNIT.findall(text if keep_case else text.lower())


def locate(widx, q, start_char, length):
    """句子 q 里第 start_char 个字（去掉标点后的序号）起 length 个字 → (词列表, 首词下标, 尾词下标, 来源) 或 None。
    逐字对齐：字幕字串和原始词拆成的字串 difflib 对齐（校对改过的字一对一 replace 也算对上）。"""
    import difflib
    cue_u = units(q["content"])
    best = None
    for k in range(len(widx.sources)):
        ws = widx.window(k, q["from"] - 1.0, q["to"] + 1.0)
        if not ws:
            continue
        cu, owner = [], []
        for wi, w in enumerate(ws):
            for u in units(w["t"]):
                cu.append(u); owner.append(wi)
        sm = difflib.SequenceMatcher(None, cue_u, cu, autojunk=False)
        mp, loose = {}, {}   # mp：一字对一字（找这句话用）；loose：长短不一的替换按比例摊（「Jhon」→「John」，取字幕文字用）
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal" or (tag == "replace" and i2 - i1 == j2 - j1):
                for d in range(i2 - i1):
                    mp[i1 + d] = j1 + d
            elif tag == "replace":
                for d in range(i2 - i1):   # 句首的替换整块对到最后一个词（窗口里还带着上一句的尾巴），句尾的对到第一个，其余按比例
                    off = min(j2 - j1 - 1, d * (j2 - j1) // (i2 - i1))
                    loose[i1 + d] = j2 - 1 if i1 == 0 else (j1 if i2 == len(cue_u) else j1 + off)
        want = range(start_char, start_char + length)
        if not all(i in mp for i in want):
            continue
        ratio = sm.ratio()
        if best is None or ratio > best[0]:
            best = (ratio, ws, owner[mp[want[0]]], owner[mp[want[-1]]], widx.sources[k][0],
                    {i: owner[j] for i, j in {**loose, **mp}.items()})
    if best and best[0] >= 0.5:
        return best[1:]
    return None


# ---------- 找候选 ----------

def find_hits(sid, phrases, aliases, speaker, exclude=()):
    path, body = subs_file(sid)
    if not body:
        return [], None, 0
    body = with_speakers(sid, path, body)
    widx = WordIndex(sid, path)
    variants = sorted({v for p in phrases for v in [p] + aliases.get(p, [])}, key=len, reverse=True)
    hits, total = [], 0
    for qi, q in enumerate(body):
        spk = q.get("spk")
        if speaker and spk not in (None, speaker):
            continue
        u = units(q["content"])
        text = "".join(u)
        if any(x in text for x in exclude):
            continue
        taken = set()
        for v in variants:
            vv = "".join(units(v))
            for m in re.finditer(re.escape(vv), text):
                # 字串位置 → 字序号（英文词一个单位多个字母）
                pos = [0]
                for x in u:
                    pos.append(pos[-1] + len(x))
                if m.start() not in pos or m.end() not in pos:
                    continue
                c0, c1 = pos.index(m.start()), pos.index(m.end())
                if any(c in taken for c in range(c0, c1)):
                    continue
                taken.update(range(c0, c1))
                total += 1
                hits.append({"sid": sid, "qi": qi, "q": q, "c0": c0, "n": c1 - c0, "variant": v, "spk": spk, "widx": widx})
    return hits, path, total


def shape(hit, a):
    """定一次截取的起止。返回 dict（start/end/text/natural/…）或 None（太长切不出来、找不到字级时间且句子太长）。"""
    q = hit["q"]
    loc = locate(hit["widx"], q, hit["c0"], hit["n"])
    if loc is None:   # 没有字级时间：按字数在句内估，句子短才用
        u = units(q["content"])
        per = (q["to"] - q["from"]) / max(1, len(u))
        s = q["from"] + per * hit["c0"]; e = s + per * hit["n"]
        if q["to"] - q["from"] <= a.max_hit:
            s, e = q["from"], q["to"]
        while e - s < a.min_hit and (s > q["from"] or e < q["to"]):
            s, e = max(q["from"], s - per), min(q["to"], e + per)
        if e - s > a.max_hit + 0.3:
            return None
        return {"start": s - 0.05, "end": e + 0.1, "core": (s, e), "text": q["content"], "natural": False, "timing": "估算"}
    ws, i, j, src, cue_word = loc
    core = (ws[i]["s"], ws[j]["e"])
    # 所在小句：往前到上一个带标点的词之后，往后到这个词带标点为止
    ci = i
    while ci > 0 and not ws[ci - 1]["p"] and ws[ci]["s"] - ws[ci - 1]["e"] < 0.6:
        ci -= 1
    cj = j
    while cj < len(ws) - 1 and not ws[cj]["p"] and ws[cj + 1]["s"] - ws[cj]["e"] < 0.6:
        cj += 1
    natural = ws[cj]["e"] - ws[ci]["s"] <= a.max_hit
    if natural:
        i, j = ci, cj

        def clause(k, step):   # 从 k 往 step 方向的下一个整小句的另一头
            while 0 <= k + step < len(ws):
                lo, hi = min(k, k + step), max(k, k + step)
                if ws[lo]["p"] or ws[hi]["s"] - ws[lo]["e"] >= 0.6:
                    break
                k += step
            return k
        if ws[j]["e"] - ws[i]["s"] < a.min_hit:   # 小句太短（「对。」）：接上前一个或后一个整小句，不超过上限
            opts = []
            if j + 1 < len(ws):
                opts.append((i, clause(j + 1, 1)))
            if i > 0:
                opts.append((clause(i - 1, -1), j))
            opts = [(x, y) for x, y in opts if ws[y]["e"] - ws[x]["s"] <= a.max_hit]
            if opts:
                i, j = min(opts, key=lambda o: ws[o[1]]["e"] - ws[o[0]]["s"])
    if ws[j]["e"] - ws[i]["s"] < a.min_hit or not natural:   # 整词往两边扩：先往后（同一小句里），再往前，交替
        turn = 1
        while ws[j]["e"] - ws[i]["s"] < a.min_hit:
            nxt = j + 1 if j + 1 < len(ws) and not ws[j]["p"] else None
            prv = i - 1 if i > 0 and not ws[i - 1]["p"] else None
            cand = (nxt, prv) if turn else (prv, nxt)
            k = next((x for x in cand if x is not None), None)
            if k is None:   # 小句到头了，跨标点再扩
                k = j + 1 if j + 1 < len(ws) else (i - 1 if i > 0 else None)
                if k is None:
                    break
            if (ws[max(j, k)]["e"] - ws[min(i, k)]["s"]) > a.max_hit:
                break
            i, j = min(i, k), max(j, k)
            turn ^= 1
        if ws[j]["e"] - ws[i]["s"] > a.max_hit + 0.3:
            return None
    prev_e = ws[i - 1]["e"] if i > 0 else ws[i]["s"] - 1
    next_s = ws[j + 1]["s"] if j + 1 < len(ws) else ws[j]["e"] + 1
    pad = max(0.0, min(a.pad, (a.max_hit + 0.3 - (ws[j]["e"] - ws[i]["s"])) / 2))   # 加上余量也不超过上限 0.3 秒以上
    s = min(ws[i]["s"], max(ws[i]["s"] - pad, prev_e + 0.02))
    e = max(ws[j]["e"], min(ws[j]["e"] + pad, next_s - 0.02))
    # 字幕文字用校对过的字幕句子里对应这几个词的那一段（名字的识别写法已统一）；对不上时用识别原文
    cu = units(q["content"], keep_case=True)
    idxs = sorted(c for c, o in cue_word.items() if i <= o <= j)
    if idxs:   # 左右边界取到相邻词对上的字为止，中间没对上的字（长短不一的改字）也算进来
        left = max([c for c, o in cue_word.items() if o < i], default=-1) + 1
        right = min([c for c, o in cue_word.items() if o > j], default=len(cu)) - 1
        seg = cu[min(left, idxs[0]):max(right, idxs[-1]) + 1]
        text = "".join(x + (" " if x.isascii() and k + 1 < len(seg) and seg[k + 1].isascii() else "") for k, x in enumerate(seg))
    else:
        text = "".join(w["t"] + (w["p"] if k < j else "") for k, w in enumerate(ws[i:j + 1], i))
    if hit["variant"] not in hit.get("phrases", [hit["variant"]]):
        text = text.replace(hit["variant"], hit["phrases"][0])
    return {"start": s, "end": e, "core": core, "text": text, "natural": natural, "timing": src,
            "w0": ws[i]["s"], "w1": ws[j]["e"],
            "prev_e": ws[i - 1]["e"] if i > 0 else None, "next_s": ws[j + 1]["s"] if j + 1 < len(ws) else None,
            # 识别结果里前后有没有停顿：词和词挨着（间隔 <60ms）又没标点，就是一口气说下来的，声音上找到的「停顿」多半是字中间的塞音
            "gap_before": i == 0 or bool(ws[i - 1]["p"]) or ws[i]["s"] - ws[i - 1]["e"] >= 0.06,
            "gap_after": j + 1 >= len(ws) or bool(ws[j]["p"]) or ws[j + 1]["s"] - ws[j]["e"] >= 0.06}


def rank(h):
    """越大越好：整句 > 主播（分过人）> 这句话在这段里的占比 > 长度接近 1.3 秒。"""
    dur = h["end"] - h["start"]
    share = h["n"] / max(1, len(units(h["text"])))
    filler = len(re.findall(r"[嗯呃额]", h["text"]))
    stub = sum(1 for c in re.split(r"[，。？！、,.?!]", h["text"]) if len(units(c)) == 1)   # 「…真理，真」这种结巴碎字
    return ((2 if h["natural"] else 0) + (1 if h["spk"] else 0) + 1.5 * share - abs(dur - 1.3) * 0.5
            - (1 if h["timing"] == "估算" else 0) - 1.5 * filler - stub)


# ---------- 声音 ----------

def pcm(video, a, b, sr=16000):
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(0, a):.3f}", "-t", f"{b - max(0, a):.3f}", "-i", str(video),
                          "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"], capture_output=True, stdin=subprocess.DEVNULL).stdout
    return np.frombuffer(raw, np.int16).astype(float) / 32768


def refine(video, h, pad):
    """按声音定起止：首词前、尾词后各找一段 ≥40ms 的停顿（比这段说话响度低 20dB 左右，按底噪放宽），刀下在停顿里、
    离说话最多 pad 秒。百炼的词时间常差 0.1 秒左右，只按词时间切会带进前后的字或吃掉尾音（实测 21 段里 6 段）。
    找不到停顿（话没停就接着说）就标 clean=False，挑的时候跳过。顺带量说话响度 level（dB），拼接前按它拉齐。"""
    import numpy as np
    sr, hop = 16000, 160
    w0, w1 = h.get("w0", h["start"]), h.get("w1", h["end"])
    a0 = max(0.0, w0 - 0.7)
    x = pcm(video, a0, w1 + 0.7, sr)
    h["clean"], h["level"] = False, None
    if len(x) < hop * 20:
        return h
    n = len(x) // hop
    db = 20 * np.log10(np.sqrt((x[:n * hop].reshape(n, hop) ** 2).mean(1)) + 1e-9)
    t = lambda k: a0 + k * hop / sr
    idx = lambda s: min(n - 1, max(0, int(round((s - a0) * sr / hop))))
    body = db[idx(w0):idx(w1) + 1]
    if len(body) < 5:
        return h
    level = float(np.sort(body)[len(body) // 2:].mean())       # 说话部分（响的一半）的平均
    floor = float(np.percentile(db, 10))
    thr = min(level - 12, max(level - 20, floor + 6))
    quiet = db < thr
    RUN = 4                                                        # 40ms

    # 起点：从首词开头往前（最多 0.35 秒）找第一段连续安静 ≥40ms，取它的尾 = 说话开始
    k = idx(w0 + 0.05)
    lo = idx(w0 - 0.35)
    on = None
    while k - RUN >= lo:
        if quiet[k - RUN:k].all():
            on = k
            break
        k -= 1
    # 终点：从尾词结尾往后（最多 0.35 秒）找第一段连续安静 ≥40ms，取它的头 = 说话结束
    k = idx(w1 - 0.05)
    hi = idx(w1 + 0.35)
    off = None
    while k + RUN <= hi:
        if quiet[k:k + RUN].all():
            off = k
            break
        k += 1
    if on is None or off is None or off <= on or not h.get("gap_before", True) or not h.get("gap_after", True):
        return h
    # 停顿找到了，但中间夹进了前后别的词也不要
    # （前一个词的尾巴伸进来超过 30ms 就算：截「抽烟的X」实际听到「不抽烟的X」）
    if (h.get("prev_e") is not None and h["prev_e"] > t(on) + 0.03) or (h.get("next_s") is not None and h["next_s"] < t(off) - 0.03):
        return h
    q0 = on - RUN
    lo = idx(h["prev_e"]) if h.get("prev_e") is not None else 0      # 余量不越过前一个词的结尾、后一个词的开头
    while q0 > lo and quiet[q0 - 1] and t(on) - t(q0 - 1) <= pad:
        q0 -= 1
    q1 = off + RUN
    hi = idx(h["next_s"]) if h.get("next_s") is not None else n - 1
    while q1 < hi and quiet[q1] and t(q1) - t(off) <= pad:
        q1 += 1
    h["start"] = t(q0) + 0.01                                      # 停顿里离说话最多 pad 秒
    h["end"] = t(q1) - 0.01
    h["speech"] = (t(on), t(off))
    h["level"], h["clean"] = level, True
    return h


# ---------- 出片 ----------

def render_hit(video, h, lay, out, gain):
    """一段 → 1080×1920 30 帧中间文件（画面同 cut._render 的模糊背景 + 原画；声音拉齐 + 淡入淡出），时长对齐到 1/30 秒。"""
    W, H = cc.W, cc.H
    n = max(1, round((h["end"] - h["start"]) * 30))
    D = n / 30
    sw, sh = lay["src"]
    left, top, cw, ch = lay["crop"]
    crop = f"crop={int(sw * cw)}:{int(sh * ch)}:{int(sw * left)}:{int(sh * top)}"
    graph = (f"[0:v]fps=30,trim=end_frame={n},setpts=PTS-STARTPTS,split[a][b];"
             f"[a]scale={W // 4}:{H // 4}:force_original_aspect_ratio=increase,crop={W // 4}:{H // 4},boxblur=6:2,"
             f"eq=brightness=-0.28,scale={W}:{H}[bg];"
             f"[b]{crop},scale={W}:{lay['fg_h']}[fg];[bg][fg]overlay=0:{lay['fg_y']},format=yuv420p,setsar=1[v];"
             f"[0:a]aresample=48000,aformat=channel_layouts=stereo,atrim=end={D:.6f},asetpts=PTS-STARTPTS,volume={gain:.2f}dB,"
             f"afade=t=in:d={FADE},afade=t=out:st={D - FADE:.4f}:d={FADE},apad=whole_dur={D:.6f}[aa]")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{h['start']:.3f}", "-t", f"{D + 0.5:.3f}", "-i", str(video),
                    "-filter_complex", graph, "-map", "[v]", "-map", "[aa]", "-c:v", "libx264", "-crf", "12", "-preset", "veryfast",
                    "-c:a", "pcm_s16le", str(out)], check=True, stdin=subprocess.DEVNULL)
    return D


def build(a):
    phrases = a.phrase
    aliases = {k: list(v) for k, v in ALIASES.items()}
    for x in a.alias or []:
        wrong, right = x.split("=", 1)
        aliases.setdefault(right, []).append(wrong)
    sids = a.sessions or [s for s in all_sessions() if session_video(s)][:a.all_recent]
    if not sids:
        sys.exit("没有可用的场次（要有字幕和视频）；用 --sessions 指定")
    cands, totals, videos = [], {}, {}
    for sid in sids:
        v = session_video(sid)
        if not v:
            log(f"{sid}: 找不到视频，跳过（归档到云盘的旧场次先 rclone copy 回 videos/）")
            continue
        exclude = [x for p in phrases for x in EXCLUDE.get(p, [])] + (a.exclude or [])
        hs, path, total = find_hits(sid, phrases, aliases, a.speaker, exclude)
        videos[sid], totals[sid] = v, total
        nsrc = len(hs[0]["widx"].sources) if hs else 0
        log(f"{sid}: 字幕 {path.name if path else '无'}，{total} 次（字级时间来源 {nsrc} 个）")
        for h in hs:
            h["phrases"] = phrases
            r = shape(h, a)
            if r:
                h.update(r)
                cands.append(h)
    if not cands:
        sys.exit("一次都没找到")
    cands.sort(key=rank, reverse=True)
    sizes = {sid: cc.probe_size(v) for sid, v in videos.items()}

    def ok(h, strict):
        """按需检查（贵）：起止要落在停顿里；画面单人满屏优先。分屏的只在第二轮、而且分过人确定是主播时才要（--allow-duet 不限）。"""
        if "clean" not in h:
            refine(videos[h["sid"]], h, a.pad)
        if not h["clean"] or not a.min_hit - 0.1 <= h["end"] - h["start"] <= a.max_hit + 0.3:
            return False
        if "solo" not in h:
            h["solo"] = split_band(videos[h["sid"]], (h["start"] + h["end"]) / 2, sizes[h["sid"]]) is None
        return h["solo"] or a.allow_duet or (not strict and bool(h["spk"]))

    # 各场轮流拿自己排最前的一个（场次摊开）；同场两次至少隔 30 秒，不够再放宽到 3 秒；先只要单人满屏
    picked, total_d, checked = [], 0.0, 0
    for strict, gap in ((True, 30), (True, 3), (False, 30), (False, 3)):
        queues = collections.OrderedDict((sid, [h for h in cands if h["sid"] == sid and h not in picked]) for sid in videos)
        progress = True
        while progress and len(picked) < a.max and total_d < a.target:
            progress = False
            for sid, q in queues.items():
                if len(picked) >= a.max or total_d >= a.target:
                    break
                while q:
                    h = q.pop(0)
                    if any(p["sid"] == sid and abs(p["start"] - h["start"]) < gap for p in picked):
                        continue
                    checked += 1
                    if ok(h, strict):
                        picked.append(h); total_d += h["end"] - h["start"]; progress = True
                        break
    if not picked:
        sys.exit("一段都没选上：候选的前后都找不到清楚的停顿，或者都是分屏画面（可试 --allow-duet、调大 --max-hit）")
    picked.sort(key=lambda h: (session_time(h["sid"]) or datetime.min, h["start"]))
    log(f"候选 {len(cands)} 个，查了 {checked} 个，选 {len(picked)} 段，约 {total_d:.1f} 秒")
    levels = [h["level"] for h in picked if h.get("level") is not None]
    ref = sorted(levels)[len(levels) // 2] if levels else 0
    total_n = sum(totals.values())
    n_s = len([s for s in totals if totals[s]])
    title = a.title or (f"一场直播\n说了{total_n}次{phrases[0]}" if n_s == 1 else f"{n_s}场直播\n说了{total_n}次{phrases[0]}")
    title = title.replace("\\n", "\n")     # 命令行里写的 \n 也当换行
    slug = re.sub(r"[^\w]+", "", "_".join(phrases))
    out_root = Path(a.out) if a.out else C.DELIVER / f"混剪_{slug}_{date.today():%Y%m%d}"
    folder = out_root / f"01_{title_slug(title)}"
    folder.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        files, rows, t = [], [], 0.0
        for k, h in enumerate(picked, 1):
            v = videos[h["sid"]]
            pick = detect_layout(v, {"start": h["start"] - 0.5, "end": h["end"] + 0.5})
            plan = {"live": True, **{x: pick[x] for x in cc.CLIP_LAYOUT_KEYS if x in pick}}
            if pick["layout"] == "duet_v":
                plan["v_y"] = 560
            lay = cc.make_layout(v, plan)
            gain = max(-10.0, min(12.0, ref - h["level"])) if h.get("level") is not None else 0.0
            f = tmp / f"h{k:02d}.mkv"
            D = render_hit(v, h, lay, f, gain)
            files.append(f)
            rows.append({"no": k, "sid": h["sid"], "start": round(h["start"], 3), "end": round(h["start"] + D, 3),
                         "at": round(t, 3), "dur": round(D, 3), "text": h["text"], "spk": h["spk"], "natural": h["natural"],
                         "timing": h["timing"], "layout": pick["layout"], "layout_note": pick.get("layout_note"),
                         "gain_db": round(gain, 1)})
            log(f"  {k:02d} {h['sid']} {h['start']:9.2f} +{D:.2f}s  {pick['layout']:7s} {gain:+5.1f}dB  {h['text']}")
            t += D
        total = t
        # 标题、尾卡、字幕（成片时间轴），交给 cut._render
        png_title, png_tag, png_end = tmp / "title.png", tmp / "tag.png", tmp / "end.png"
        cc.overlay_png(title, png_title, cc.VIDEO_Y, shade=True)
        from PIL import Image
        Image.new("RGBA", (cc.W, cc.H), (0, 0, 0, 0)).save(png_tag)
        if cc.end_card_text(True):
            cc.label_png(cc.end_card_text(True), cc.VIDEO_Y + 30, png_end, fg=(255, 255, 255), bg=(0, 0, 0, 200))
        else:
            Image.new("RGBA", (cc.W, cc.H), (0, 0, 0, 0)).save(png_end)
        caps = []
        for r in rows:
            text = cc.clean_caption(r["text"]) if a.caption == "words" else ""
            if text:
                p = tmp / f"cap{r['no']}.png"
                cc.caption_png(text, cc.CAP_Y_V, p)
                caps.append((p, r["at"], r["at"] + r["dur"]))
            elif a.caption == "phrase":   # 大号黄底黑字只写这句话
                p = tmp / f"cap{r['no']}.png"
                cc.label_png(phrases[0], cc.CAP_Y_V - 20, p, size=110)
                caps.append((p, r["at"], r["at"] + r["dur"]))
            if a.counter:
                p = tmp / f"cnt{r['no']}.png"
                cc.label_png(f"第 {r['no']} 次", cc.CAP_Y_V + 190, p, size=52)
                caps.append((p, r["at"], r["at"] + r["dur"]))
        inputs = sum((["-i", str(f)] for f in files), [])
        for p in [png_title, png_tag, png_end] + [c[0] for c in caps]:
            inputs += ["-i", str(p)]
        n = len(files)
        src = "".join(f"[{i}:v][{i}:a]" for i in range(n)) + f"concat=n={n}:v=1:a=1[sv][sa];"
        lay_id = {"src": (cc.W, cc.H), "crop": (0, 0, 1, 1), "fg_h": cc.H, "fg_y": 0, "zoom": False}
        video_out = folder / "视频.mp4"
        cc._render(inputs, n, src, 0, total, total, video_out, lay_id, caps, False)

        # 封面：挑一段单人整幅的（中间那段优先），前后各 8 秒里找睁眼帧
        solo = [(h, r) for h, r in zip(picked, rows) if r["layout"] == "full_v"] or list(zip(picked, rows))
        h, r = solo[len(solo) // 2]
        try:
            from .cover.v2 import covers_all
            cv = covers_all(videos[h["sid"]], {"live": True, "layout": r["layout"]},
                            {"start": max(0, h["start"] - 8), "end": h["end"] + 8, "title": title, "layout": r["layout"]})
            cv["34"].save(folder / "封面.jpg", quality=92)
            cv["43"].save(folder / "封面_横4比3.jpg", quality=92)
            cv["bili"].save(folder / "封面_B站.jpg", quality=92)
        except Exception as ex:   # 封面失败不挡出片
            log(f"封面失败：{ex}")

    tags = a.tags.split(",") if a.tags else [phrases[0], "口头禅"]
    (folder / "文案.txt").write_text(caption_text({"title": title, "tags": tags}, []), encoding="utf-8")
    (out_root / "混剪清单.json").write_text(json.dumps(
        {"title": title, "phrases": phrases, "sessions": sids, "counts": totals, "duration": round(total, 2),
         "hits": rows, "made": datetime.now().isoformat(timespec="seconds")}, ensure_ascii=False, indent=1), encoding="utf-8")
    subprocess.run(py_mod("verify", out_root / "验收.json", out_root / "抽帧", video_out), cwd=ROOT, check=False)
    log(f"\n{folder}  {total:.1f} 秒，{len(rows)} 段")


# ---------- --suggest ----------

def suggest(a):
    try:
        import jieba
    except ImportError:
        sys.exit("--suggest needs jieba: pip install 'liveclip[montage]'")
    jieba.setLogLevel(logging.ERROR)
    for w in ["自媒体", "连麦", *(C.get("montage.jieba_words") or [])]:
        jieba.add_word(w, 2000)
    gen, tot_g = {}, 0
    for line in open(Path(jieba.__file__).parent / "dict.txt", encoding="utf-8"):
        w, f = line.split()[:2]
        gen[w] = int(f); tot_g += int(f)
    singles = sorted((w for w in gen if len(w) == 1), key=lambda w: -gen[w])
    func = set(singles[:500])                                                     # 最常用的 500 个单字（的了是我你…）
    common = set(sorted((w for w in gen if len(w) >= 2), key=lambda w: -gen[w])[:3000])   # 最常用的 3000 个词
    sids = a.sessions or [s for s in all_sessions() if subs_file(s)[1]][:a.all_recent]
    grams, sess, alone, alone_s, wc = (collections.Counter(), collections.defaultdict(set), collections.Counter(),
                                       collections.defaultdict(set), collections.Counter())
    used = []
    for sid in sids:
        path, body = subs_file(sid)
        body = with_speakers(sid, path, body)
        has_spk = any("spk" in q for q in body)
        used.append(f"{sid}（{path.name}{'' if has_spk else '，没分人，整场都算'}）")
        for q in body:
            if a.speaker and q.get("spk") not in (None, a.speaker):
                continue
            for cl in PUNCT_RE.split(q["content"]):
                cl = re.sub(r"[^\w]", "", cl)
                if not cl or re.search(r"[A-Za-z0-9]", cl):
                    continue
                if 2 <= len(cl) <= 8:
                    alone[cl] += 1; alone_s[cl].add(sid)
                ts = jieba.lcut(cl, HMM=False)
                wc.update(ts)
                for i in range(len(ts)):
                    for j in range(i + 1, min(len(ts), i + 4) + 1):
                        g = "".join(ts[i:j])
                        if 2 <= len(g) <= 8:
                            grams[g] += 1; sess[g].add(sid)
    N = sum(wc.values()) or 1
    key = lambda w: math.log2((wc[w] / N) / ((gen.get(w, 0) + 1) / tot_g)) if wc[w] else 20.0

    def filler(g, ws):
        return g in FILLER or all(w in FILLER or w in func for w in ws)

    rows = []
    for g, c in grams.items():
        if c < a.min_count or len(sess[g]) < a.min_sessions:
            continue
        ws = jieba.lcut(g, HMM=False)
        if filler(g, ws):
            continue
        content = [w for w in ws if (len(w) >= 2 and w not in common and w not in FILLER) or (len(w) == 1 and w not in func)]
        kk = max((key(w) for w in content), default=0)
        pmi = 0.0
        if len(ws) >= 2 and all(wc[w] for w in ws):   # 多词搭配：实际次数 / 各词独立出现时的期望
            pmi = math.log2(c * N ** (len(ws) - 1) / math.prod(wc[w] for w in ws))
        if kk >= 4 or (pmi >= 9 and c >= 15):
            rows.append((g, c, len(sess[g]), round(kk, 1), round(pmi, 1)))
    cnt = {g: c for g, c, *_ in rows}
    rows = [r for r in rows if not any(r[0] != h and r[0] in h and cnt[h] >= 0.75 * r[1] for h in cnt)]   # 被更长的词组包住的不单列
    rows.sort(key=lambda r: -r[1])
    std = [(g, c, len(alone_s[g])) for g, c in alone.most_common()
           if c >= max(5, a.min_count // 2) and len(alone_s[g]) >= a.min_sessions and not filler(g, jieba.lcut(g, HMM=False))]
    print("扫了：\n  " + "\n  ".join(used))
    print("\n① 单独成句的短句（次数 / 场数）：")
    for g, c, s in std[:a.top]:
        print(f"  {g:<10} {c:5d} 次  {s} 场")
    print("\n② 高频特色词组（次数 / 场数 / 比通用词频高多少（2 的幂）/ 搭配紧密度）：")
    for g, c, s, kk, pmi in rows[:a.top]:
        print(f"  {g:<10} {c:5d} 次  {s} 场  特色 {kk if kk < 20 else '词典没有'}  搭配 {pmi or '-'}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="liveclip montage", description="口头禅混剪（见文件开头说明）")
    ap.add_argument("--suggest", action="store_true", help="列出常说的口头禅/词组，帮着挑 --phrase")
    ap.add_argument("--phrase", action="append", help="要混剪的那句话，可给多个说法（标题用第一个）")
    ap.add_argument("--alias", action="append", help="同音错字，写成 错字=正字")
    ap.add_argument("--exclude", action="append", help="句子里含这个词就不要（同音的别的词），可给多个")
    ap.add_argument("--sessions", nargs="+", help="场次 ID（data/asr/<ID>*.json），如 LIVE_20260101_2000_main")
    ap.add_argument("--all-recent", type=int, default=3, help="不给 --sessions 时取最近几场（出片只算有视频的；--suggest 默认 6）")
    ap.add_argument("--speaker", default=C.get("montage.speaker") or C.host_label,
                    help="只要这个说话人（字幕没分人的场次不过滤）；给空串不过滤")
    ap.add_argument("--max", type=int, default=25, help="最多几段")
    ap.add_argument("--target", type=float, default=30, help="总长到这么多秒就停")
    ap.add_argument("--pad", type=float, default=0.25, help="每段起止往外留的秒数（不越过相邻的词）")
    ap.add_argument("--min-hit", type=float, default=0.8)
    ap.add_argument("--max-hit", type=float, default=2.0)
    ap.add_argument("--caption", choices=["words", "phrase", "none"], default="words", help="烧字幕：这段的原话 / 只写这句话 / 不烧")
    ap.add_argument("--counter", action="store_true", help="字幕下面显示「第 N 次」")
    ap.add_argument("--allow-duet", action="store_true", help="连麦分屏画面和单人满屏同等挑（默认单人优先）")
    ap.add_argument("--title", help="成片标题（默认「一场直播说了 N 次…」）")
    ap.add_argument("--tags", help="内容话题，逗号分隔（默认 这句话,口头禅；按 liveclip.tags 规则分到各平台）")
    ap.add_argument("--out", help="输出目录（默认 deliver/混剪_<这句话>_<日期>/）")
    ap.add_argument("--min-sessions", type=int, default=3, help="--suggest：至少出现在几场里")
    ap.add_argument("--min-count", type=int, default=10, help="--suggest：至少出现几次")
    ap.add_argument("--top", type=int, default=30, help="--suggest：每类列几个")
    a = ap.parse_args(argv)
    a.speaker = a.speaker or None
    t0 = time.time()
    if a.suggest:
        if "--all-recent" not in sys.argv:
            a.all_recent = 6
        suggest(a)
    elif a.phrase:
        build(a)
    else:
        ap.error("要 --phrase 或 --suggest")
    print(f"用时 {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
