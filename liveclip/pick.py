"""Ask Claude to read a long video's full transcript and pick clips worth posting on their own
(short 12–90 s / medium 1.5–3 min / long 3–5 min), each with a title, hook (teaser), category, tags and checks.

Usage:
    liveclip pick <ID> --subs data/asr/<ID>_bailian_reviewed.json --title "..." [--live] [--note "..."] --out clips.json
Output: clips/<ID>/<out>

Model access (liveclip.llm): ANTHROPIC_API_KEY → Anthropic SDK; otherwise the local `claude -p` CLI (your Claude login).
The prompt is Chinese and parameterised by config `streamer:` (name, persona, topics, event examples, hot points).
"""
import argparse
import json
import re
from pathlib import Path

from .config import C
from .llm import ask_json, lean_args

MODEL = C.model("pick")
LEAN = lean_args()      # kept for modules that build their own claude command line


def _s(key, default=""):
    return C.get(f"streamer.{key}") or default


def criteria():
    name, guest = C.name, C.guest_label
    ev = _s("event_examples", [])
    ev_txt = f"（例如{'、'.join(ev)}这类）" if ev else "（连麦对象、播客嘉宾、正在火的人物、理论或热点事件）"
    hot = _s("hot_points", [])
    hot_txt = ("\n热门观点（别的号反复切、反复火的，遇到就优先选，讲过多少遍都照样选——反复出现说明热门，不去重）：\n"
               + " / ".join(hot) + "。\n在 why 里写命中了哪一个（没有就写\"新观点\"）。\n") if hot else ""
    avoid = _s("avoid_topics") or "离观众生活远的概念"
    tagc = C.get("tags") or {}
    brand = [*(tagc.get("douyin_must") or []), *(tagc.get("soft") or [])]
    cond = (tagc.get("conditional") or {})
    no_brand = f"不要写 {'、'.join(brand)}（程序会加）；" if brand else ""
    cond_txt = (f"只有讲{'、'.join(cond.get('when_any') or [])}的段落才写 {cond['tag']}。" if cond.get("tag") else "")
    return f"""你是{name}的切片编辑。{name}是{_s("persona")}，常聊{_s("topics")}。
你的任务：从下面这条长视频的完整字幕里，挑出最值得单独发到短视频平台（{_s("audience")}）的竖屏切片。

观众反应最强的通常是这几类（按优先级）：
1. 事件和联动：和正在火的人、理论或事件有关的段落{ev_txt}，
   尤其是双方当场碰撞、互相认可或互相反驳的部分。这类段落时效最强，score 直接加 1。
2. 观众自己的问题：从"20 岁没资源怎么办""大学该干嘛"这种观众会代入的问题开始、
   他给出明确答案的段落。同一个观点，从观众的问题切入，比从他的结论切入好。
3. 开场论点：视频开头那句核心主张，连同紧跟的第一个论据
4. 他自创的比喻、类比和梗：把抽象概念说成一个好记、好笑的说法
5. 把观点推到极端的推演，或一长串排比/连环反问
6. 点名真人或用具体数字的例子（越离谱越好记：几千万、月薪 1000、一天只睡 4 小时）
7. 回应质疑：他正面回应一条具体的黑评或某个人的批评（念出原评论、再讲逻辑）
8. 他自曝的真实经历或弱点
9. 干货清单：观众会倒回去记笔记的具体做法（这类适合中档，收藏率最高）
10. 引战点：有争议的断言（照常选，争议大的标 risky=true；risky 只是等级标注，不是不选的理由）
{hot_txt}
少选：{avoid}和没有具体人/数字/场景的泛泛方法论，除非他讲出了一句很狠的话。

要求：
- 只挑最值得发的 3 到 5 段，宁缺毋滥。按推荐程度从高到低排序，score 是 1-10 分。
- 时长分三档（完整的论证、有深度的讨论值得更长）：
  短 12–90 秒：一个金句、一个表态、一个反常识观点（只要一句话就能独立成立，12–25 秒也可以，不要为了凑长度带上铺垫）；
  中 90–180 秒：一个完整的论证，有来有回；
  长 180–300 秒：连麦里有深度的讨论，双方都有实质内容。
  按内容需要选档：不要为了短把论证切断，也不要为了长塞闲聊。中、长档优先给"观众拿去就能用"的内容（步骤、方法、数字），
  这类收藏率是短片段的 2–3 倍。所有片段加起来不超过全片时长的 30%。
- 任意两段之间至少隔 60 秒。不要把一段连续的内容切成首尾相接的几块；同一个话题只留最好的一段。
- 不要选结尾的感谢关注、开头的打招呼、需要看画面才懂的段落。
- start / end 用字幕里的秒数，start 取某句字幕的开始时间，end 取某句字幕的结束时间。
- 独立性：片段第一句不能以"所以、但是、然后、这个、刚才、那、他、她、它"等依赖上文的词开头。只看这一段，陌生观众要能听懂论点，否则不要选。
- 结构：每段必须包含 ①一句论点或金句 ②至少一个理由、例子或梗 ③一个收尾（结论、反问或笑点）。缺 ① 或 ③ 的不要选。
- 中、长档的片段，中间每 20 秒都要有新信息（新论据、反驳、例子、转折），否则截短或降档。
- 开头预告（hook）：如果这段最狠的那一句不在开头，用 hook_start / hook_end 标出它的起止秒数，成片会把它复制到开头做预告，然后从头正常播放。预告必须满足：
  ① 是一个完整的句子或完整的论断，陌生观众只听这几秒就知道他在说什么，并且想知道"为什么"；
  ② 4 到 8 秒，从一句话的开头取到一句话的结尾，不能截半句；
  ③ 不包含"嗯、对对对、是是是"这类附和（以"所以""但是"开头没关系，只看抓不抓人）。连麦对方/{guest}/观众的话一般不用，
     例外：对方那句本身就是这段的金句或冲突点，或是念出来的黑评，这两种可以做预告；
  ④ 不能是只有数字或结论、缺了主语和对象的碎片（例如只说"一个月只花300"而不说这是他给自己定的规矩）。
  抓人的预告通常是：最离谱的具体数字、贴标签/暴论、反常识一句话、观众的问题加他的一句答案。
  这段最狠的话本来就在开头时 hook 填 null，不要重复；找不到满足这四条的句子也填 null，不要硬凑。不要重排句子，也不要把两处的话拼成新句子。
- 抓重点：片段必须完整包含这段的核心观点和它的结论/金句，结论不能落在片段之外；不要选只有铺垫、闲聊或半截论证的段落。在 why 里写出这段的核心观点是哪一句。
- 断章取义检查：在 context_check 里写一句"脱离上下文后，会不会被理解成比原意更极端或相反"。会的话，把起点往前扩到包含他的限定语（这是为了不歪曲他的原意，不是因为有争议就不选）。
- 群体标签：段里出现性别、地域、群体标签（如"女生都…""男人就该…"）且没有限定语的，标 risky=true（照常选，只作等级标注）。
- 标题：最多两行，每行不超过 10 个字。没看过视频的人只读标题也要明白在讲什么，不要用只有看完才懂的说法（例如"你的主线呢"）。可选公式：反常识、提问、数字、身份代入、结论前置。
  优先用片中最狠的那句原话（压缩到字数内）；有联动对象/事件时，把事件写进标题，人名规则见下条。
- 标题调性跟内容走：这段在一本正经地搞抽象、开玩笑、胡扯，标题就要抽象——顺着他们的离谱逻辑面不改色地再推一步，或者直接用最离谱的那句原话，像懂中文互联网的人起的；不要套「X 慌了」「X 本位制」「X 终结者竟然是」这类用烂的模板。这段在认真输出干货，标题就老实讲清楚干货。无论哪种都不能歪曲原意、不能编原话里没有的事。
- 标题主语：不要出现「{guest}」两个字，也不要拿连麦对象的名字当主语。也不要用「{name}：」这种冒号开头；直接写观点本身，需要点出是他时自然地写进句子里，拿不准是谁说的就不写主语。标题只能改写段内原话的意思，不能出现原话没有的立场，也不能用比原话更绝对的词（"所有""一定""全都"）。不要出现"切片"二字。
- point：用一句话（20 字以内）写这段的核心观点本身（不是标题，不加修饰），用来做观点库。
- 话题分两组：
  event_tags：这段涉及的人、事件、别人的理论，0–2 个，没有就空；
  tags：这段内容的话题 2–4 个，2–6 个字，要和这段讲的东西直接对得上、是观众会搜的词（如 择偶、理财、自媒体、睡眠、减脂、AI、大学生）。
  不要写只说明形式的词（直播、观点、金句、干货、切片、分享），{no_brand}{cond_txt}
- 字幕是机器识别的，有错字（专有名词常被识别成同音词）。在 typo_fixes 里列出你在所选片段里确定的错字和正确写法。
"""


SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["clips", "typo_fixes"],
    "properties": {
        "clips": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["start", "end", "score", "title", "category", "why", "risky",
                             "hook_start", "hook_end", "context_check", "tags", "event_tags", "point"],
                "properties": {
                    "hook_start": {"type": ["number", "null"]},
                    "hook_end": {"type": ["number", "null"]},
                    "context_check": {"type": "string"},
                    "start": {"type": "number"},
                    "score": {"type": "integer"},
                    "end": {"type": "number"},
                    "title": {"type": "string"},
                    "category": {"type": "string", "enum": [
                        "事件联动", "观众问题", "开场论点", "自创比喻", "极端推演", "真人或数字", "回应质疑",
                        "亲身经历", "干货清单", "引战点"]},
                    "why": {"type": "string"},
                    "risky": {"type": "boolean"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "event_tags": {"type": "array", "items": {"type": "string"}},
                    "point": {"type": "string"},
                },
            },
        },
        "typo_fixes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["wrong", "right"],
                "properties": {"wrong": {"type": "string"}, "right": {"type": "string"}},
            },
        },
    },
}


def live_note():
    return f"""
这是一场直播录屏，不是剪好的视频。直播里有大量不能切的东西：念观众名字和礼物、等人上麦、调试设备、口播广告、重复回答同一个问题、和弹幕的碎片化互动。
直播很长，可以挑 5 到 8 段（不受上面"3 到 5 段"的限制；给了配额时以配额为准）。
优先挑：他完整回答一个观众问题并给出观点的段落；连麦时双方观点冲突、他给出反驳或金句的段落；他讲一个完整的个人经历。
宁可少挑也不要凑数：只选和他的核心内容（{_s("topics")}）直接相关、而且他把观点讲完整了的段落。
片段从提问或引出话题的那一句开始（连麦对方的提问可以保留，帮助观众理解），到他说完结论为止；中间连麦对方的长篇闲聊会让片段变散，这样的段落不要选。
字幕是语音识别的，错字更多，也可能把连麦对方和观众的话混在一起，请根据上下文判断是谁在说。
"""


def spk_note():
    h, g = C.host_label, C.guest_label
    return (f"每句字幕前方括号里的 [{h}] / [{g}] 是按声音自动区分的说话人（声纹聚类得到，大体可靠，很短的附和偶尔会标错；"
            "[其他] 是背景音乐、外放视频等，[未知] 是没法判断的）。"
            f"判断一句话是不是{C.name}说的，以这个标记为准，再结合上下文；标题主语按上面的规则：不用冒号开头，确定是他说的才可以把他自然写进句子，否则不写主语。\n")


def load_cues(bv, path=None, with_spk=False):
    """返回 [(开始秒, 结束秒, 文字)]。with_spk=True 时另返回和它对齐的说话人列表（liveclip.asr --diarize 的 spk 字段），没有就是 None。"""
    path = Path(path) if path else C.ASR / f"{bv}.json"
    body = json.loads(path.read_text(encoding="utf-8"))["body"]
    cues = [(c["from"], c["to"], c["content"]) for c in body]
    if not with_spk:
        return cues
    return cues, ([c.get("spk", "未知") for c in body] if any("spk" in c for c in body) else None)


def quota_note(max_n, min_score, avoid):
    """本轮配额和已经剪过的观点（剪得太多、同一个观点反复剪）。"""
    out = ""
    if max_n is not None:
        out += (f"\n本次最多选 {max_n} 段（覆盖上面所有关于段数的说法），只留最值得发的"
                + (f"，score 低于 {min_score} 的不要选" if min_score else "") + "；一段都不够好就一段都不选。\n")
    if avoid:
        rows = "\n".join(f"- {a['point']}（{a.get('date', '')}）" for a in avoid)
        out += ("\n下面这些观点已经剪过了。同一个观点（换个说法、换个例子也算）不要再选；只有这次讲得明显更好"
                "（更完整、有新论据或更抓人）才可以选，并在 why 开头写「比旧的好：…」说明好在哪。\n" + rows + "\n")
    # 跟播现在不传 --max / --avoid（不设上限、不去重，热门观点照样剪），留着给手动调用
    return out


def build_prompt(title, cues, hint="", live=False, spks=None):
    if spks:
        lines = "\n".join(f"[{s:.1f}-{e:.1f}] [{p}] {t}" for (s, e, t), p in zip(cues, spks))
        fmt, hint = "[开始秒-结束秒] [说话人] 内容", spk_note() + hint
    else:
        lines = "\n".join(f"[{s:.1f}-{e:.1f}] {t}" for s, e, t in cues)
        fmt = "[开始秒-结束秒] 内容"
    return (f"{criteria()}{live_note() if live else ''}{hint}\n视频标题：{title}\n\n完整字幕（{fmt}）：\n{lines}\n\n"
            f"只输出符合下面 JSON Schema 的 JSON：\n{json.dumps(SCHEMA, ensure_ascii=False)}")


def ask_cli(prompt, schema=SCHEMA, meta=None, tool=None, label=None, model=None):
    """Compatibility wrapper used by the review modules: one prompt → one JSON object."""
    return ask_json(prompt, schema, model=model or MODEL, tool=tool or "选段", label=label, meta=meta)


def enforce(clips, total, dropped=None, max_n=None, min_score=0):
    """按排序依次保留：时长 12-310 秒、与已选片段间隔 >= 50 秒、总长 <= 全片 35%（给模型一点余量）、
    分数不低于 min_score、最多 max_n 条。
    dropped 给一个 list 时，筛掉的候选连同原因记进去（面板「选段决策」要看）。"""
    kept = []
    for c in clips:
        dur = c["end"] - c["start"]
        why = None
        if c.get("score", 0) < min_score:
            why = f"{c.get('score')} 分，低于 {min_score} 分"
        elif max_n is not None and len(kept) >= max_n:
            why = f"本轮配额 {max_n} 条已满，留更高分的"
        elif not 12 <= dur <= 310:
            why = f"时长 {dur:.0f} 秒，不在 12 秒–5 分钟"
        elif any(c["start"] < k["end"] + 50 and k["start"] < c["end"] + 50 for k in kept):
            near = next(k for k in kept if c["start"] < k["end"] + 50 and k["start"] < c["end"] + 50)
            why = f"和更高分的「{near['title'].replace(chr(10), ' ')}」离得太近（间隔不到 50 秒）"
        elif sum(k["end"] - k["start"] for k in kept) + dur > 0.35 * total:
            why = "加上它总时长会超过这段字幕的 35%"
        if why:
            if dropped is not None:
                dropped.append(dict(c, drop_reason=why))
            continue
        kept.append(c)
    return kept


def clean_title(title):
    """标题兜底：去掉「嘉宾：」「嘉宾称」这类主语（拿不准的不写主语），以及「主播名：」冒号前缀。"""
    g = re.escape(C.guest_label)
    names = "|".join(re.escape(x) for x in {C.name, C.host_label, C.canonical_name} if x)
    pat = rf"^(连麦)?{g}(\S{{0,6}}?)?[：:]|^(连麦)?{g}(称|说|问|觉得)" + (rf"|^({names})\s*[：:]" if names else "")
    lines = [re.sub(pat, "", ln).strip() for ln in title.split("\n")]
    return "\n".join(ln for ln in lines if ln) or title


def snap(clips, cues):
    """把起止时间对齐到最近的字幕句子边界，避免从半句话开始或结束。"""
    starts = [s for s, _, _ in cues]
    ends = [e for _, e, _ in cues]
    for c in clips:
        c["start"] = min(starts, key=lambda s: abs(s - c["start"]))
        c["end"] = min(ends, key=lambda e: abs(e - c["end"]))
        c["text"] = " / ".join(t for s, e, t in cues if s >= c["start"] - 0.01 and e <= c["end"] + 0.01)
        if c.get("hook_start") is not None and c.get("hook_end") is not None:
            hs = min(starts, key=lambda s: abs(s - c["hook_start"]))
            he = min(ends, key=lambda e: abs(e - c["hook_end"]))
            text = "".join(t for s_, e_, t in cues if hs - 0.01 <= s_ < he).strip()
            reason = None
            if not (c["start"] <= hs < he <= c["end"] and he - hs <= 12 and hs > c["start"] + 2):
                reason = "起止不合法"
            elif he - hs < 3.5:
                reason = f"只有 {he - hs:.1f} 秒"
            elif len(re.sub(r"[\W_]", "", text)) < 8:
                reason = f"太碎「{text}」"
            if reason:
                c["hook_rejected"] = reason     # 留在计划里，人工想补预告时能看到为什么被拒
            c["hook_start"], c["hook_end"] = (None, None) if reason else (hs, he)
    return clips


def main(argv=None):
    ap = argparse.ArgumentParser(prog="liveclip pick", description="LLM clip picking from a transcript")
    ap.add_argument("bv", help="video / session ID (output goes to clips/<ID>/)")
    ap.add_argument("--out", default="clips.json", help="输出文件名（放在 clips/<ID>/ 下）")
    ap.add_argument("--subs", help="字幕文件路径（默认 data/asr/<ID>.json）")
    ap.add_argument("--title", help="视频标题（默认用 ID）")
    ap.add_argument("--live", action="store_true", help="直播录屏模式：提示模型跳过念礼物、调设备等")
    ap.add_argument("--note", default="", help="附加给模型的说明（例如播客有嘉宾、字幕没标说话人）")
    ap.add_argument("--max", type=int, help="最多选几段")
    ap.add_argument("--min-score", type=int, default=0, help="低于这个分的不要")
    ap.add_argument("--avoid", help="已经剪过的观点 JSON 列表 [{point, date}]，同观点不再选")
    args = ap.parse_args(argv)

    title = args.title or args.bv
    cues, spks = load_cues(args.bv, args.subs, with_spk=True)
    avoid = json.loads(Path(args.avoid).read_text(encoding="utf-8")) if args.avoid else []
    prompt = build_prompt(title, cues, (args.note + "\n" if args.note else "")
                          + quota_note(args.max, args.min_score, avoid), live=args.live, spks=spks)

    from .llm import backend
    print(f"让 {MODEL} 挑片段（{'Anthropic API' if backend() == 'api' else 'claude 命令行'}），字幕 {len(cues)} 句…")
    result = ask_json(prompt, SCHEMA, model=MODEL, tool="选段")

    dropped = []
    clips = enforce(snap(result["clips"], cues), cues[-1][1], dropped, args.max, args.min_score)
    for c in clips + dropped:
        c["title"] = clean_title(c["title"])
    outdir = C.CLIPS / args.bv
    outdir.mkdir(parents=True, exist_ok=True)
    subs = C.rel(args.subs) if args.subs else None
    out = {"bv": args.bv, "title": title, "model": MODEL, "subs": subs, "live": args.live,
           "clips": clips, "dropped": dropped, "typo_fixes": result.get("typo_fixes", [])}
    (outdir / args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    for i, c in enumerate(clips, 1):
        print(f"{i:2d}. ({c['score']}分) {c['start']:7.1f}-{c['end']:7.1f} ({c['end'] - c['start']:4.0f}s) "
              f"[{c['category']}]{' ⚠' if c['risky'] else ''} {c['title']}")
    print(f"写入 {outdir}/{args.out}")


if __name__ == "__main__":
    main()
