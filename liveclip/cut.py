"""
按 clips/<ID>/<plan>.json 把长视频剪成 9:16 竖屏切片。

版式：同一画面放大模糊压暗做背景；中间放原画（按画面布局裁切，见 LAYOUTS）；上方黄色大标题；
最后 2 秒显示"完整版"尾卡；响度统一到 -14 LUFS。
如果片段有 hook_start/hook_end（AI 标出的段内金句，建议 4–8 秒，硬性不超过 12 秒），先把这句复制到开头做预告，再从头正常播放。

用法:
    liveclip cut <ID> [--plan clips.json] [--only 1,3] [--layout duet_v]
需要: 原视频、ffmpeg、Pillow、一个中文字体（见 liveclip.fonts）。原视频按 --video、plan 的 "video"、
      data/manifest.json 的 media_path（可选）、videos/<ID>.mp4、videos/**/<ID>.mp4 的顺序找。
输出: clips/<ID>/01_标题.mp4 …（--plan 不是 clips.json 时放在 clips/<ID>/<plan 名>/ 下）
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw

from .config import C
from .fonts import font as _font

W, H = 1080, 1920
VIDEO_H = W * 3 // 4                # 横屏口播裁出的 4:3 放大到 1080x810（裁切位置见 make_layout）
VIDEO_Y = 560                       # 上方留给标题（安全区：离顶 >= 200px）
# 竖屏原片（直播录屏）：去掉顶部直播间信息栏和底部评论区，只留中间 58% 的高度
V_CROP_TOP, V_CROP_H, V_Y = 0.17, 0.47, 380
CAP_Y_V, CAP_Y_H = 1360, VIDEO_Y + VIDEO_H + 40   # 直播字幕两行时底边也要离底 >= 384px（2026-10-09 从 1440 上调）
YELLOW, GREY = (255, 212, 0), (170, 170, 170)
HOOK_ZOOM = 0.08                     # 开头推近 8%：预告期间推近；没有预告时正片前 OPEN_ZOOM_SECS 秒推近后保持
OPEN_ZOOM_SECS = 3.5
PAD_BEFORE, PAD_AFTER = 0.3, 0.6   # 字幕时间点前后多留一点，避免吞字
HOOK_MAX = 12                      # 预告硬性上限（秒）：超过直接报错，不静默照剪；短的不限（pick_clips 提示词里建议 4–8 秒是软的）
MANIFEST = C.DATA / "manifest.json"   # 可选：{"materials": [{"id", "media_path", "role"}]}

# 画面布局。plan 里写 "layout" 或命令行 --layout 指定；都没写时横屏用 talk_h，竖屏直播录屏用 live_v，其他竖屏用 full_v。
LAYOUTS = {
    "talk_h": "横屏口播：裁成 4:3 放大（人在偏左、原片字幕居中），旧版 1920x1080 就是这个",
    "full_h": "横屏全画面：整幅 16:9 不裁，放在中间（reaction 左边贴的截图、需要看全画面的内容）",
    "live_v": "竖屏直播界面：去掉顶部直播间信息栏和底部评论区",
    "duet_v": "竖屏双人连麦：只取中间分屏那一条，两个人都留着，画面放在中间",
    "full_v": "竖拍全屏（vlog、抖音直播回放）：不裁，铺满 9:16，标题叠在画面上",
}
# 双人连麦分屏的默认位置（一条 390x854 录屏实测：分屏在第 250–553 行）。别的录屏位置不同，在 plan 里写 "v_crop" 覆盖
# （liveclip.layout.detect_layout 会按画面自动算）
DUET_CROP = (0.293, 0.356)
CLIP_LAYOUT_KEYS = ("layout", "v_crop", "v_y", "cap_y", "h_crop_x", "h_crop_w")


WRAP_AFTER = set("的了着过地得们吗呢吧啊嘛")         # 在这些字后面断行自然（借鉴 AutoClip caption_layout）
WRAP_BEFORE = set("把被而但又还也都让对从向为所因如我你他她它这那")   # 在这些字前面断行自然（含人称、指代；「和与及在就」常和前后连成词，不算）
WRAP_PUNCT = set("，。？！、；：,.?!;:")


def wrap(text, font, max_w, draw):
    """标题自带换行就照用；一行放得下就一行；否则断成两行，不拆开英文、数字，不让标点打头。
    两行都放得下的断点里挑：标点后最好，其次「的了吗呢…」之后或「和在但就…」之前，最后才是两个字中间；
    同档里挑两行最均衡的（2026-10-11 前只看均衡，会断成「被 / 骂钱」）。都放不下时退回最均衡。"""
    if "\n" in text:
        return [line.strip() for line in text.split("\n") if line.strip()][:2]
    if draw.textlength(text, font=font) <= max_w:
        return [text]
    tokens = re.findall(r"[A-Za-z0-9.%+]+(?: [A-Za-z0-9.%+]+)*|.", text)   # 识别出的「1 0 0」整串不拆
    # 不在标点前断行
    cuts = [i for i in range(1, len(tokens)) if not re.match(r"[，。？！、》）%：；,.?!;:]", tokens[i])]
    if not cuts:   # 整句是一串英文/数字：退回按词切
        tokens = re.findall(r"[A-Za-z0-9.%+]+|.", text)
        cuts = [i for i in range(1, len(tokens)) if not re.match(r"[，。？！、》）%：；,.?!;:]", tokens[i])] or [len(tokens) // 2 or 1]
    widths = {i: (draw.textlength("".join(tokens[:i]), font=font), draw.textlength("".join(tokens[i:]), font=font)) for i in cuts}

    def rank(i):
        prev, nxt = tokens[i - 1], tokens[i]
        if prev in WRAP_PUNCT or prev == " ":
            return 0
        if prev in WRAP_AFTER or nxt in WRAP_BEFORE:
            return 1
        return 3
    fit = [i for i in cuts if max(widths[i]) <= max_w and min(widths[i]) >= 0.3 * max(widths[i])]   # 不留一两个字的孤行
    if fit:   # 断点档次差一档 ≈ 两行宽度差半行
        best = min(fit, key=lambda i: rank(i) + 2 * abs(widths[i][0] - widths[i][1]) / max_w)
    else:
        best = min(cuts, key=lambda i: abs(widths[i][0] - widths[i][1]))
    return ["".join(tokens[:best]), "".join(tokens[best:])]


def slug(title):
    """标题 → 文件名用的字串（去掉标点和换行）。"""
    return re.sub(r"[^\w]+", "", title)


def probe_size(video):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                          "-of", "csv=p=0", str(video)], capture_output=True, text=True, check=True).stdout
    w, h = map(int, out.strip().split(",")[:2])
    return w, h


def manifest_row(bv):
    if not MANIFEST.exists():
        return {}
    rows = json.loads(MANIFEST.read_text(encoding="utf-8")).get("materials", [])
    return next((r for r in rows if r.get("id") == bv), {})


def find_video(bv, plan, explicit=None):
    """原视频路径：--video > plan 的 "video" > manifest media_path > videos/<ID>.mp4 > videos/**/<ID>.mp4（唯一一个时）。"""
    tried = []
    for p in (explicit, plan.get("video"), manifest_row(bv).get("media_path"), C.VIDEOS / f"{bv}.mp4"):
        if p:
            tried.append(str(p))
            if C.ws(p).exists():
                return C.ws(p)
    found = sorted(C.VIDEOS.glob(f"**/{bv}.mp4"))
    if len(found) == 1:
        return found[0]
    sys.exit(f"找不到 {bv} 的原视频（试过 {tried}，videos/** 里找到 {len(found)} 个）；用 --video 指定")


def make_layout(video, plan, name=None, role=None):
    """算出这条视频的裁切和摆放。返回 dict：
    name、crop（左, 上, 宽, 高，按原画比例）、fg_y/fg_h（原画放到 1080 宽后的位置和高度）、
    title_y（标题和标签按它摆）、cap_y（自加字幕的位置）、full（原画铺满整个竖屏）。"""
    sw, sh = probe_size(video)
    if not name:
        name = plan.get("layout")
    if not name:
        name = "talk_h" if sw >= sh else ("live_v" if (role == "livestream_recording" or plan.get("live")) else "full_v")
    if name not in LAYOUTS:
        sys.exit(f"不认识的 layout: {name}（可选 {', '.join(LAYOUTS)}）")
    fg_y = cap_y = None
    if name == "talk_h":
        cw = min(1.0, sh * 4 / 3 / sw)
        # 左边留出可裁余量的 62.5%：1920x1080 时正好是旧版的 x=300（人在偏左、右侧配图会被裁掉一点）
        cw = plan.get("h_crop_w", cw)   # 双人横屏播客只取一侧时写窄一点，画面会比 4:3 更接近正方形
        crop = (plan.get("h_crop_x", (1 - cw) * 0.625), 0, cw, 1)
        fg_y = VIDEO_Y
    elif name == "full_h":
        crop = (0, 0, 1, 1)
    elif name in ("live_v", "duet_v"):
        top, h, left, w = (tuple(plan.get("v_crop", (V_CROP_TOP, V_CROP_H) if name == "live_v" else DUET_CROP))
                           + (0, 1))[:4]   # 左右不写就取整个宽度；连麦左右分屏只要一个人那半时写左右两项
        crop = (left, top, w, h)
        if name == "live_v":
            fg_y, cap_y = V_Y, CAP_Y_V
    else:  # full_v：比 9:16 宽就左右居中裁，比 9:16 窄就上下居中裁
        if sw * H > sh * W:
            cw = sh * W / H / sw
            crop = ((1 - cw) / 2, 0, cw, 1)
        else:
            ch = sw * H / W / sh
            crop = (0, (1 - ch) / 2, 1, ch)
        fg_y = 0
    fg_h = int(round(int(sh * crop[3]) * W / int(sw * crop[2]) / 2)) * 2
    if fg_y is None:   # 画面上下居中，但顶边不高于 380，上面给标题留地方
        fg_y = max(380, (H - fg_h) // 2 // 2 * 2)
    fg_y = plan.get("v_y", fg_y)
    full = name == "full_v"
    title_y = VIDEO_Y if full else fg_y   # 铺满时标题照横屏的位置放
    if cap_y is None:
        cap_y = CAP_Y_V if full else min(fg_y + fg_h + 40, 1600)
    cap_y = plan.get("cap_y", cap_y)
    return {"name": name, "src": (sw, sh), "crop": crop, "fg_y": fg_y, "fg_h": fg_h,
            "title_y": title_y, "cap_y": cap_y, "full": full, "zoom": plan.get("zoom", True)}


def detect_duo(video, spans, probe):
    """横屏播客在“单人满屏”和“双人并排”之间切换时，按画面里的某个固定标记（例如连线窗口的绿色边框）
    找出双人并排的时间段，返回输出时间轴上的 [(开始, 结束)]。probe = {"x","y","w","h"}：原片里取样的矩形，
    里面偏绿的像素占比超过 0.3 就算双人画面。spans: [(原片起, 原片止, 输出偏移)]。"""
    import numpy as np
    fps, out = 4, []
    for s0, s1, off in spans:
        raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{s0:.3f}", "-t", f"{s1 - s0:.3f}", "-i", str(video), "-vf",
                              f"fps={fps},crop={probe['w']}:{probe['h']}:{probe['x']}:{probe['y']},format=rgb24",
                              "-f", "rawvideo", "-"], capture_output=True, stdin=subprocess.DEVNULL).stdout
        fr = np.frombuffer(raw, np.uint8).reshape(-1, probe["h"], probe["w"], 3).astype(int)
        duo = ((fr[..., 1] > fr[..., 0] + 30) & (fr[..., 1] > fr[..., 2] + 20)).mean(axis=(1, 2)) > 0.3
        k = 0
        while k < len(duo):
            if duo[k]:
                j = k
                while j < len(duo) and duo[j]:
                    j += 1
                out.append([off + k / fps, off + min(j / fps, s1 - s0)])
                k = j
            k += 1
    merged = []   # 补上 0.75 秒以内的小缝、丢掉 0.75 秒以内的闪烁
    for a, b in out:
        if merged and a - merged[-1][1] < 0.75:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged if b - a >= 0.75]


def overlay_png(title, path, vy, shade=False):
    """标题层。标题底边在 vy 上方 50px（vy 一般是画面顶边）。shade：画面铺满全屏时，在标题后面加一层上深下浅的暗渐变，
    否则黄字压在亮背景（健身房、白墙）上看不清。"""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    if shade:
        g = Image.linear_gradient("L").resize((W, vy + 120)).point(lambda v: int((255 - v) * 0.6))
        img.paste((0, 0, 0, 255), (0, 0, W, vy + 120), g)
    d = ImageDraw.Draw(img)
    size = 104
    while True:
        font = _font(size)
        lines = wrap(title, font, W - 120, d)
        if all(d.textlength(line, font=font) <= W - 120 for line in lines) or size <= 70:
            break
        size -= 10
    line_h = int(size * 1.3)
    y = vy - 50 - line_h * len(lines)
    for line in lines:
        d.text((W // 2, y), line, font=font, fill=YELLOW, anchor="ma",
               stroke_width=4, stroke_fill=(0, 0, 0))
        y += line_h
    # 画面下方不再写来源行（不好看）；来源靠结尾"完整版"尾卡和发布文案里的 @。
    img.save(path)


def caption_png(text, y, path):
    """一句字幕：白字黑描边，最多两行，居中。"""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    font = _font(62)
    lines = wrap(text, font, W - 140, d)
    for i, line in enumerate(lines[:2]):
        d.text((W // 2, y + i * 80), line, font=font, fill=(255, 255, 255), anchor="ma",
               stroke_width=6, stroke_fill=(0, 0, 0))
    img.save(path)


FILLER_RE = re.compile(r"^(嗯|呃|额|啊|哦|哈|欸|哎|诶|对吧|对对对|是是是|对对|okay|ok)+$", re.I)


def clean_caption(text):
    """烧字幕前的清理：去掉识别文本的句末句号/逗号，纯语气词的句子返回空串（调用方跳过）。"""
    t = text.strip().strip("。．，,、")
    return "" if not t or FILLER_RE.match(t) else t


def fix_typos(text, fixes):
    for f in fixes:
        text = text.replace(f["wrong"], f["right"])
    return text


def label_png(text, y, path, fg=(0, 0, 0), bg=YELLOW, size=44):
    """一个圆角色块小标签，用于"高能预告"和尾卡。"""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    font = _font(size)
    tw = d.textlength(text, font=font)
    x0 = (W - tw) / 2 - 28
    d.rounded_rectangle((x0, y, x0 + tw + 56, y + size + 32), radius=16, fill=bg)
    d.text((W // 2, y + 14), text, font=font, fill=fg, anchor="ma")
    img.save(path)


# 导出参数照剪映（1080P/30帧/码率"更高"）来：实测同一条片子直接传抖音开头卡，
# 过一遍剪映导出再传就不卡。差别是码率（1.3M vs 7.5M）、关键帧间隔（8 秒 vs 1 秒），
# 以及文件里带着原片的 "Packed by Bilibili XCoder" 标记。三样都照剪映改。
# 但码率直传抖音仍卡（抖音可能只认剪映导出标记），抖音那份还是过一遍剪映导出。
# 码率取 5.5M（留余量，5.8M 时有一段平均到 6009k）：B站 1080P 平均码率超过 6000kbps 会被二压，抖音经验值也是 6000 以上重压；
# 小红书没有公开上限。三个平台共用这一份。
ENC = ["-c:v", "libx264", "-preset", "medium", "-profile:v", "high", "-pix_fmt", "yuv420p",
       "-b:v", "5500k", "-maxrate", "9000k", "-bufsize", "12000k",
       "-g", "30", "-keyint_min", "30", "-sc_threshold", "0", "-r", "30",
       "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
       "-c:a", "aac", "-b:a", "160k", "-ar", "44100",
       "-map_metadata", "-1", "-movflags", "+faststart"]
# Mac 硬件编码（VideoToolbox，借鉴 AutoClip video_encoder）：环境变量 CUT_ENCODER=vt 时用，码率上限同上
if os.environ.get("CUT_ENCODER") == "vt":
    ENC = ["-c:v", "h264_videotoolbox", "-profile:v", "high", "-pix_fmt", "yuv420p",
           "-b:v", "5500k", "-maxrate", "9000k", "-bufsize", "12000k",
           "-g", "30", "-r", "30", *ENC[ENC.index("-color_primaries"):]]


def find_cut(video, a, b):
    """在原片 [a, b] 秒内找剪辑点（跳剪的那一帧）。口播常是同一机位跳剪，场景分数不高，
    所以取窗口内的最大值，且要明显高于窗口里其他帧，才算剪辑点。返回剪辑点时间或 None。"""
    import re as _re
    a = max(0.0, a)
    out = subprocess.run(["ffmpeg", "-v", "info", "-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", str(video),
                          "-vf", "scale=320:-2,select='gte(scene,0)',metadata=print:key=lavfi.scene_score",
                          "-an", "-f", "null", "-"], capture_output=True, text=True, stdin=subprocess.DEVNULL).stderr
    ts = [float(x) for x in _re.findall(r"pts_time:([0-9.]+)", out)]
    sc = [float(x) for x in _re.findall(r"scene_score=([0-9.]+)", out)]
    if len(sc) < 3:
        return None
    i = max(range(len(sc)), key=lambda k: sc[k])
    rest = sorted(sc[:i] + sc[i + 1:])
    median = rest[len(rest) // 2]
    if sc[i] >= 0.03 and sc[i] >= 3 * max(median, 0.005):
        return a + ts[i] + 0.02  # 往后挪半帧：-ss 正好落在剪辑点时会取到前一帧
    return None


def plan_cues(plan):
    """plan 的整条字幕（逐句起止），bounds 用它限制前后余量；没有字幕返回 None。"""
    return json.loads(C.ws(plan["subs"]).read_text(encoding="utf-8"))["body"] if plan.get("subs") else None


def bounds(video, clip, snap=True, cues=None):
    """片段和预告的起止秒数。起点往前、终点往后留一点防吞字；但附近如果有剪辑点，就对齐到剪辑点，
    避免把上一个/下一个镜头的几帧带进来（开头闪一下，看起来像卡）。
    snap=False：直播录屏没有剪辑点，找到的"剪辑点"其实是手势、转头，不对齐。
    cues：整条字幕。没对齐到剪辑点时，往前留的余量不越过上一句的结尾，往后留的余量不越过下一句的开头
    （2026-10-11 liveclip.qa 查出：直播句子挨得紧，固定 +0.6 秒常把下一句的第一个字和字幕带进结尾，-0.3 秒把上一句的尾字带进开头）。"""
    if not snap:
        find = lambda *_: None
    else:
        find = find_cut
    # find_cut 返回的是剪辑点后半帧，当起点正好；当终点要退回到剪辑点之前，否则 -t 会带上下一个镜头/下一句字幕的第一帧
    # （曾出现预告结尾闪出下一句字幕，看着像字幕和声音没对齐）
    END_BACK = 0.04
    s0 = find(video, clip["start"] - 0.35, clip["start"] + 0.15)
    start = s0 if s0 is not None else max(0, clip["start"] - PAD_BEFORE)
    e0 = find(video, clip["end"] - 0.05, clip["end"] + 0.7)
    end = e0 - END_BACK if e0 is not None else clip["end"] + PAD_AFTER
    if cues and e0 is None:
        # 结尾落在一句话中间：说了一大半就延到句末，只说了个开头就退到这句之前（最多挪 3 秒；借鉴 AutoClip boundaries 吸附整句）。
        # 开头不动：模型常故意跳过开头的「就是呃」。
        mid = [q for q in cues if q["from"] + 0.25 < clip["end"] < q["to"] - 0.25]
        if mid:
            q = mid[0]
            if clip["end"] - q["from"] >= q["to"] - clip["end"] and q["to"] - clip["end"] <= 3:
                clip = {**clip, "end": q["to"]}
                end = q["to"] + PAD_AFTER
            elif clip["end"] - q["from"] < q["to"] - clip["end"] and clip["end"] - q["from"] <= 3 and q["from"] - clip["start"] > 10:
                clip = {**clip, "end": q["from"] - 0.03}
                end = clip["end"]
    if cues:
        prev = [q["to"] for q in cues if q["to"] <= clip["start"] + 0.3]   # 起点离上一句结尾不到 0.3 秒也算上一句
        if s0 is None and prev:
            start = min(clip["start"], max(start, max(prev) + 0.02))
        nxt = [q["from"] for q in cues if q["from"] >= clip["end"] - 0.3]
        if e0 is None and nxt:
            end = max(clip["end"], min(end, min(nxt) - 0.03))
    hook = clip.get("hook_start") is not None and clip.get("hook_end") is not None
    if hook and not 0 < clip["hook_end"] - clip["hook_start"] <= HOOK_MAX:
        raise ValueError(f"预告 {clip['hook_end'] - clip['hook_start']:.2f} 秒，超过 {HOOK_MAX} 秒或起止颠倒："
                         f"{clip.get('title', '').replace(chr(10), '')}，改 plan 的 hook_start/hook_end 或设为 null")
    h0 = h1 = None
    if hook and clip.get("hook_exact"):
        # 人工逐帧核过的预告起止（原片帧时间），不再吸附：字幕淡入淡出时 find_cut 找不到或找偏
        h0, h1 = clip["hook_start"], clip["hook_end"]
    elif hook:
        c = find(video, clip["hook_start"] - 0.25, clip["hook_start"] + 0.15)
        h0 = c if c is not None else clip["hook_start"] - 0.05
        c = find(video, clip["hook_end"] - 0.05, clip["hook_end"] + 0.25)
        h1 = c - END_BACK if c is not None else clip["hook_end"] - 0.02  # 原片烧录字幕比 AI 字幕早约一帧换句
    return start, end, h0, h1


def cut(video, b, pngs, out, lay, captions=(), segs=None):
    """b: bounds() 的结果。pngs: 标题层、预告标签、尾卡。lay: make_layout() 的结果。
    captions: [(png, 片段内开始秒, 结束秒)]，原片没有字幕时用。
    segs: 精剪用，(预告保留段列表, 正片保留段列表)，每段是原片里的 (开始秒, 结束秒)；给了就忽略 b，
    把这些小段按顺序拼起来（去掉口头禅、卡壳、重复），每个接缝做 12 毫秒的声音淡入淡出防止爆音。"""
    if segs:
        return _cut_segs(video, segs, pngs, out, lay, captions)
    start, end, h0, h1 = b
    dur = end - start
    hook = h0 is not None
    inputs = []
    if hook:
        inputs += ["-ss", f"{h0:.3f}", "-t", f"{h1 - h0:.3f}", "-i", str(video)]
    inputs += ["-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(video)]
    k = len(inputs) // 6                              # 视频输入个数（1 或 2）
    for p in list(pngs) + [c[0] for c in captions]:
        inputs += ["-i", str(p)]
    hook_len = (h1 - h0) if hook else 0
    total = hook_len + dur
    if hook:
        src = ("[0:v]null[hv];[0:a]afade=t=in:d=0.1,afade=t=out:st=%.2f:d=0.15[ha];[1:a]afade=t=in:d=0.15[ma];"
               "[hv][ha][1:v][ma]concat=n=2:v=1:a=1[sv][sa];" % (hook_len - 0.15))
    else:
        src = "[0:v]null[sv];[0:a]afade=t=in:d=0.15[sa];"
    _render(inputs, k, src, hook_len, dur, total, out, lay, captions, hook)


def _render(inputs, k, src, hook_len, dur, total, out, lay, captions, hook):
    title, tag, end = k, k + 1, k + 2
    # 开头画面缓慢推近：抖音给新号的码率很低，画面动得少的开头在手机上会一顿一顿（2026-10-07 实测：
    # 不处理的版本开头卡、带推近的版本不卡）。有预告就在预告期间推近，进正片时随剪辑点复位；
    # 没有预告就在正片前 OPEN_ZOOM_SECS 秒推近，之后保持放大。先放大一倍再 zoompan，避免取景按整数像素抖动。
    sw, sh = lay["src"]
    left, top, cw, ch = lay["crop"]
    fg_h, fg_y = lay["fg_h"], lay["fg_y"]
    crop = f"crop={int(sw * cw)}:{int(sh * ch)}:{int(sw * left)}:{int(sh * top)}"
    zoom = ""
    alt = lay.get("alt")   # 双人画面时段：整幅原片缩放后放在同一位置（见 detect_duo）
    if HOOK_ZOOM and lay.get("zoom", True) and not alt:
        if hook:
            zl = f"{hook_len:.3f}"
            z = f"if(lt(it,{zl}),1+{HOOK_ZOOM}*it/{zl},1)"
        else:
            zl = f"{min(OPEN_ZOOM_SECS, dur):.3f}"
            z = f"1+{HOOK_ZOOM}*min(it,{zl})/{zl}"
        zoom = (f",scale={2 * W}:{2 * fg_h},"
                f"zoompan=z='{z}':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d=1:s={W}x{fg_h}:fps=30")
    graph = (src +
             # 先统一成 30 帧恒定帧率：zoompan 按"一帧进一帧出"并标成 30 帧，原片若是 27 帧（直播录屏），
             # 人像层会比声音快 11%，越到后面嘴型差得越多（音画对不上）
             f"[sv]fps=30,split={3 if alt else 2}[a][b]{'[c]' if alt else ''};"
             # 模糊背景在 1/4 分辨率上做再放大：全分辨率 boxblur 一条 80 秒的片要 14 秒，占剪辑四成（2026-10-11 实测），看起来一样
             f"[a]scale={W // 4}:{H // 4}:force_original_aspect_ratio=increase,crop={W // 4}:{H // 4},boxblur=6:2,"
             f"eq=brightness=-0.28,scale={W}:{H}[bg];"
             f"[b]{crop},scale={W}:{fg_h}{zoom}[fg];"
             + (f"[c]scale={W}:{alt['h']}[fg2];[bg][fg]overlay=0:{fg_y}:enable='not({alt['expr']})'[v0];"
                f"[v0][fg2]overlay=0:{alt['y']}:enable='{alt['expr']}'[v1];" if alt else f"[bg][fg]overlay=0:{fg_y}[v1];")
             + f"[v1][{title}:v]overlay[v2];"
             f"[v2][{tag}:v]overlay=enable='lt(t,{hook_len:.2f})'[v3];"
             f"[v3][{end}:v]overlay=enable='gte(t,{total - 2:.2f})'[c0];"
             + "".join(f"[c{i}][{end + 1 + i}:v]overlay=enable='between(t,{a:.2f},{b:.2f})'[c{i + 1}];"
                       for i, (_, a, b) in enumerate(captions))
             + f"[c{len(captions)}]format=yuv420p[out];"
             f"[sa]afade=t=out:st={total - 0.3:.2f}:d=0.3,loudnorm=I=-14:TP=-1.5:LRA=11,aresample=44100,asetpts=N/SR/TB[aout]")
    # 音频时间戳按采样数重新连续编号：预告+正片拼接后经 loudnorm 和重采样，时间戳会撞车，mp4 里留下一串只占 1 个采样的包
    # （ffmpeg/QuickTime 顺序播放听不出来，剪映和抖音按时间戳摆放，声音整体提前约 2 秒）。
    # 重采样直接到 ENC 的 44100，免得编码器再插一次
    subprocess.run(["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", graph,
                    "-map", "[out]", "-map", "[aout]", *ENC, str(out)], check=True,
                   stdin=subprocess.DEVNULL)



SEG_FPS = 30


def snap_segs(segs):
    """精剪保留段 (预告段列表, 正片段列表) 的切点对齐到 _cut_segs 的 1/30 秒帧格（以拼接输入的起点为原点），
    去掉对齐后为空的段。tight_edit 先对齐再算字幕时间、存保留段，成片时间和原片时间一一对得上。"""
    hook_segs, main_segs = segs
    base = max(0.0, min(a for a, _ in list(hook_segs) + list(main_segs)) - 0.5)
    snap = lambda ss: [(round(base + ka / SEG_FPS, 6), round(base + kb / SEG_FPS, 6))
                       for ka, kb in ((round((a - base) * SEG_FPS), round((b - base) * SEG_FPS)) for a, b in ss) if kb > ka]
    return snap(hook_segs), snap(main_segs)


def _cut_segs(video, segs, pngs, out, lay, captions):
    hook_segs, main_segs = snap_segs(segs)
    all_segs = list(hook_segs) + list(main_segs)
    base = max(0.0, min(a for a, _ in all_segs) - 0.5)
    span = max(b for _, b in all_segs) - base + 0.5
    inputs = ["-ss", f"{base:.3f}", "-t", f"{span:.3f}", "-i", str(video)]
    k = 1
    for p in list(pngs) + [c[0] for c in captions]:
        inputs += ["-i", str(p)]
    n = len(all_segs)
    # 先转 30 帧，切点对齐到 1/30 秒的帧格（snap_segs），画面、声音按同样的时间切（fps 输出的帧正好落在格点上），
    # 每段声音画面一样长、起点一致。直播录屏是可变帧率（10/9 平均 19 帧、10/11 约 16 帧），以前直接按秒切 + setpts=PTS-STARTPTS，
    # 每段第一帧比切点晚最多 60ms 却被挪到段首，concat 还把短的声音补静音补齐，十几个接缝攒下来
    # （2026-10-11 qa_clips --tight 测出声音比画面晚 40–70ms）。
    src = f"[0:v]fps={SEG_FPS},split={n}" + "".join(f"[vi{i}]" for i in range(n)) + f";[0:a]asplit={n}" + "".join(f"[ai{i}]" for i in range(n)) + ";"
    for i, (a, b) in enumerate(all_segs):
        ra, rb = round((a - base) * SEG_FPS) / SEG_FPS, round((b - base) * SEG_FPS) / SEG_FPS
        src += (f"[vi{i}]trim=start={ra - 0.001:.6f}:end={rb - 0.001:.6f},setpts=PTS-STARTPTS[v{i}];"
                f"[ai{i}]atrim=start={ra:.6f}:end={rb:.6f},asetpts=PTS-STARTPTS,"
                f"afade=t=in:d=0.012,afade=t=out:st={max(0, rb - ra - 0.012):.3f}:d=0.012[a{i}];")
    src += "".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[sv][sa];"
    hook_len = sum(b - a for a, b in hook_segs)
    dur = sum(b - a for a, b in main_segs)
    _render(inputs, k, src, hook_len, dur, hook_len + dur, out, lay, captions, bool(hook_segs))


def end_card_text(live):
    return C.get("branding.end_card_live" if live else "branding.end_card") or ""


def main(argv=None):
    ap = argparse.ArgumentParser(prog="liveclip cut", description="cut 9:16 clips from a plan")
    ap.add_argument("bv")
    ap.add_argument("--plan", default="clips.json")
    ap.add_argument("--only", help="只剪这几段，如 3,4,5")
    ap.add_argument("--tag", action="store_true", help="预告期间显示「高能预告」标签（默认不显示：这几个字太 low）")
    ap.add_argument("--no-tag", action="store_true", help=argparse.SUPPRESS)   # 旧命令兼容：现在默认就不显示
    ap.add_argument("--video", help="原视频路径（默认按 plan、工作清单、videos/ 依次找）")
    ap.add_argument("--layout", choices=list(LAYOUTS), help="画面布局，覆盖 plan 里的 \"layout\"")
    ap.add_argument("--allow-check", action="store_true",
                    help="校对结果里还有 status=check（待人确认）的字幕也照剪；默认遇到就停下来列出这些句子")
    args = ap.parse_args(argv)
    t_all = time.time()

    plan = json.loads((C.CLIPS / args.bv / args.plan).read_text(encoding="utf-8"))
    outdir = C.CLIPS / args.bv
    if args.plan != "clips.json":
        outdir = outdir / Path(args.plan).stem
        outdir.mkdir(exist_ok=True)
    video = find_video(args.bv, plan, args.video)
    role = manifest_row(args.bv).get("role")
    # 取景和位置都可以在 plan 里覆盖：
    #   "v_crop": [从上往下起点比例, 取的高度比例(, 从左起点比例, 取的宽度比例)]  竖屏直播/连麦的取景
    #   "h_crop_x": 横屏口播 4:3 裁切框的左边（比例）
    #   "v_y": 画面顶边位置（标题和标签跟着走）；"cap_y": 自加字幕的位置
    lay = make_layout(video, plan, args.layout, role)
    live = plan.get("live", role == "livestream_recording")
    # 自加字幕只给没有烧录字幕的直播录屏；口播、vlog、reaction 原片自带字幕，再加一层会重复。plan 写 "burn_subs" 覆盖
    burn = plan.get("burn_subs", lay["name"] in ("live_v", "duet_v"))
    print(f"{video}  {lay['src'][0]}x{lay['src'][1]}  layout={lay['name']}  "
          f"crop={tuple(round(x, 3) for x in lay['crop'])}  画面 y={lay['fg_y']} 高={lay['fg_h']}"
          f"{'  加字幕' if burn and plan.get('subs') else ''}")
    labels = {}

    def labels_at(vy):
        """预告标签和尾卡按标题位置画；同一位置只画一次（每段布局不同时位置可能不同）。"""
        if vy not in labels:
            tag, end = outdir / f"_tag{vy}.png", outdir / f"_end{vy}.png"
            if args.tag:
                label_png("高能预告", vy + 30, tag)
            else:
                Image.new("RGBA", (W, H), (0, 0, 0, 0)).save(tag)
            # 尾卡不写平台名：抖音、小红书画面里出现"B站"算站外导流，会被限流（2026-10-08）
            if end_card_text(live):
                label_png(end_card_text(live), vy + 30, end, fg=(255, 255, 255), bg=(0, 0, 0, 200))
            else:
                Image.new("RGBA", (W, H), (0, 0, 0, 0)).save(end)
            labels[vy] = (tag, end)
        return labels[vy]
    timing = plan_cues(plan)   # 限制前后余量用（不管烧不烧字幕）
    cues = []
    if burn and plan.get("subs"):  # 原片没有烧录字幕（直播录屏），用识别出的字幕自己加
        cues = json.loads(C.ws(plan["subs"]).read_text(encoding="utf-8"))["body"]
    only = {int(x) for x in args.only.split(",")} if args.only else None
    failed = []
    for i, c in enumerate(plan["clips"], 1):
        if only and i not in only:
            continue
        t0 = time.time()
        # 单段可以覆盖布局（同一场直播前半单人全屏、后半连麦分屏；reaction 有的段有贴图）：段里写 layout/v_crop/v_y/cap_y/h_crop_x
        over = {k: c[k] for k in CLIP_LAYOUT_KEYS if k in c}
        lay_c = make_layout(video, {**plan, **over}, None if "layout" in over else args.layout, role) if over else lay
        if over:
            print(f"  第 {i} 段 layout={lay_c['name']} crop={tuple(round(x, 3) for x in lay_c['crop'])}")
        tag, end = labels_at(lay_c["title_y"])
        name = f"{i:02d}_{slug(c['title'])}"
        png = outdir / f"{name}.png"
        overlay_png(c["title"], png, lay_c["title_y"], shade=lay_c["full"])
        b = bounds(video, c, snap=not live, cues=timing)
        probe = c.get("duo_detect", plan.get("duo_detect"))
        if probe:
            start_, end__, h0_, h1_ = b
            spans_ = ([(h0_, h1_, 0.0)] if h0_ is not None else []) + [(start_, end__, (h1_ - h0_) if h0_ is not None else 0.0)]
            ivs = detect_duo(video, spans_, probe)
            if ivs:
                sw_, sh_ = lay_c["src"]
                alt_h = int(round(sh_ * W / sw_ / 2)) * 2
                lay_c = {**lay_c, "alt": {"h": alt_h, "y": lay_c["fg_y"] + (lay_c["fg_h"] - alt_h) // 2,
                                          "expr": "+".join(f"between(t,{a:.2f},{bb:.2f})" for a, bb in ivs)}}
                print(f"  第 {i} 段双人画面 {len(ivs)} 段：" + " ".join(f"{a:.1f}-{bb:.1f}" for a, bb in ivs))
        caps = []
        # liveclip.review.captions 逐句校对过的字幕优先；没校对的直接用识别原文。
        # 以前没校对时套 plan 的全局 typo_fixes，会把同音的普通词一律改成热词，已弃用
        reviewed = burn and bool(c.get("captions"))
        if reviewed and not args.allow_check:
            pending = [x for x in c["captions"] if x.get("status") == "check"]
            if pending:
                sys.exit(f"第 {i} 段还有 {len(pending)} 句字幕待人确认（status=check），先听原音定下来，或加 --allow-check 照剪：\n"
                         + "\n".join(f"  {x['from']:8.2f}  {x['text']}    ← {x.get('note', '')}" for x in pending))
        clip_cues = [{"from": x["from"], "to": x["to"], "content": x["text"]} for x in c["captions"]] if reviewed else cues
        if clip_cues:
            start, end_, h0, h1 = b
            spans = [(start, end_, 0.0)]
            if h0 is not None:
                spans = [(h0, h1, 0.0), (start, end_, h1 - h0)]
            for s0, s1, offset in spans:
                for q in clip_cues:
                    # 只露出不到 0.3 秒的半句不加（会在开头/结尾闪一下）
                    if min(q["to"], s1) - max(q["from"], s0) >= 0.3:
                        text = clean_caption(q["content"])
                        if not text:   # 纯语气词（"对吧。""嗯"）单独闪一下很怪，不烧
                            continue
                        p = outdir / f"_cap{len(caps)}.png"
                        caption_png(text, lay_c["cap_y"], p)
                        caps.append((p, offset + max(q["from"], s0) - s0, offset + min(q["to"], s1) - s0))
            for k in range(len(caps) - 1):   # 识别时间偶尔前后交叠，后一句开始时前一句必须结束
                if caps[k][2] > caps[k + 1][1]:
                    caps[k] = (caps[k][0], caps[k][1], caps[k + 1][1])
        try:
            cut(video, b, [png, tag, end], outdir / f"{name}.mp4", lay_c, caps)
            print(f"{name}.mp4  {b[1] - b[0]:.0f}s{'  +预告' if b[2] is not None else ''}  用时 {time.time() - t0:.0f}s")
        except subprocess.CalledProcessError:
            failed.append(i)
            (outdir / f"{name}.mp4").unlink(missing_ok=True)
            print(f"{name}.mp4  失败（原视频这一段可能损坏）")
        finally:
            png.unlink()
            for p, _, _ in caps:
                p.unlink()
    for tag, end in labels.values():
        tag.unlink()
        end.unlink()
    print(f"总用时 {time.time() - t_all:.0f}s")
    if failed:
        print(f"失败的段: {','.join(map(str, failed))}，修好视频后用 --only 重剪")


if __name__ == "__main__":
    main()
