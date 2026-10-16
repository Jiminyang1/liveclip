"""
给切片做封面：人脸 + 两行粗描边大字，3:4（1080x1440），抖音主页格子和小红书都按 3:4 显示。
直播切片（plan live=true）默认用 liveclip.cover.v2 的新版（顶部深色标题区 + 原片干净帧、睁眼、避贴纸，不加名字标签）；
--style old 用下面的旧版，--style v2 让其他素材也用新版。

用法:
    liveclip covers <切片 mp4>... --out deliver/_covers [--bili]
切片路径要是 cut 的输出（clips/<ID>/<plan 名>/NN_标题.mp4），按它找到计划和第几段。
封面用的画面取自原视频：片段开始后几秒的一帧。标题取自计划（AI 给的两行标题）。
"""
import argparse
import json
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance

from .. import vision
from ..config import C
from ..fonts import font as _font

W, H = 1080, 1440
WHITE, YELLOW = (255, 255, 255), (255, 212, 0)
# 横屏原片里人大多在画面偏左（书房口播）；3:4 裁切框的中心放在宽度的这个比例处
FACE_X = {"default": 0.42}


def find_plan(mp4):
    """由切片路径找到它的 clips json 和第几段。"""
    folder = mp4.parent
    bv = folder.parent.name
    plan = folder.parent / f"{folder.name}.json"
    idx = int(mp4.name[:2])
    return bv, json.loads(plan.read_text(encoding="utf-8")), idx


def grab(video, t):
    with tempfile.NamedTemporaryFile(suffix=".png") as tmp:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1", tmp.name],
                       check=True)
        return Image.open(tmp.name).convert("RGB")


def crop_34(img, vertical, face_x=None):
    w, h = img.size
    if vertical:  # 直播录屏：取中间他本人那块（和 cut_clips 的裁切一致）
        top, ch = int(h * 0.17), int(h * 0.47)
    else:  # 横屏原片：只取上面 70%，避开底部烧录的字幕
        top, ch = 0, int(h * 0.70)
    img = img.crop((0, top, w, top + ch))
    w, h = img.size
    tw = min(w, int(h * 3 / 4))
    cx = int(w * (0.5 if vertical else (face_x or FACE_X["default"])))
    x0 = max(0, min(w - tw, cx - tw // 2))
    th = int(tw * 4 / 3)
    return img.crop((x0, 0, x0 + tw, min(h, th))).resize((W, H))


def draw_title(img, lines):
    d = ImageDraw.Draw(img)
    size = 150
    font = _font(size)
    while max(d.textlength(l, font=font) for l in lines) > W - 80 and size > 80:
        size -= 8
        font = _font(size)
    y = H - 140 - int(size * 1.25) * len(lines)
    for i, line in enumerate(lines):
        d.text((W // 2, y), line, font=font, fill=YELLOW if i == len(lines) - 1 else WHITE, anchor="ma",
               stroke_width=max(8, size // 14), stroke_fill=(0, 0, 0))
        y += int(size * 1.25)


def panel(img, box, face_x, size):
    """从 img 的 box=(x,y,w,h) 区域里按 size=(宽,高) 的比例裁一块，水平中心放在 box 宽度的 face_x 处。"""
    x, y, w, h = box
    pw, ph = size
    cw = min(w, int(h * pw / ph))
    cx = x + int(w * face_x)
    x0 = max(x, min(x + w - cw, cx - cw // 2))
    return img.crop((x0, y, x0 + cw, y + h)).resize(size, Image.LANCZOS)


def cover_duo_split(video, c, plan, probe_single):
    """对话系列封面：上面文字带（系列名 + 两行标题），下面左右两格，左边主播、右边嘉宾，各带名字标签。
    plan["cover_split"] = {"series": "某某 对话 某某", "left": [x,y,w,h], "left_face_x": 0.35, "left_name": "主播",
                           "right": [x,y,w,h], "right_face_x": 0.5, "right_name": "嘉宾", "top": 520}"""
    cs = plan["cover_split"]
    TOP = cs.get("top", 520)
    size = (W // 2, H - TOP)
    # 左边的人只在双人画面里有：找一帧左侧不是浅灰墙的；右边的人优先用单人满屏的高清帧，没有就用双人画面里他那格
    t_duo = c["start"] + 3
    while probe_single(t_duo) and t_duo < c["end"] - 4:
        t_duo += 1.5
    duo = grab(video, t_duo)
    t_single = c["start"] + 3
    while t_single < c["end"] - 4 and not (probe_single(t_single - 1) and probe_single(t_single) and probe_single(t_single + 1)):
        t_single += 1.5
    if t_single < c["end"] - 4:
        # 单人满屏帧只取上面 80%，避开底部烧录的字幕
        right = panel(grab(video, t_single), (0, 0, duo.size[0], int(duo.size[1] * 0.80)), cs.get("right_single_face_x", 0.5), size)
    else:
        right = panel(duo, cs["right"], cs.get("right_face_x", 0.5), size)
    left = panel(duo, cs["left"], cs.get("left_face_x", 0.35), size)
    img = Image.new("RGB", (W, H), (16, 16, 16))
    img.paste(left, (0, TOP))
    img.paste(right, (W // 2, TOP))
    d = ImageDraw.Draw(img)
    d.rectangle((W // 2 - 3, TOP, W // 2 + 3, H), fill=(16, 16, 16))   # 中缝
    # 名字标签：两格底部各一个黄底黑字小块
    tag_font = _font(44)
    for name, cx in ((cs.get("left_name", ""), W // 4), (cs.get("right_name", ""), W * 3 // 4)):
        if not name:
            continue
        tw = d.textlength(name, font=tag_font)
        d.rounded_rectangle((cx - tw / 2 - 24, H - 110, cx + tw / 2 + 24, H - 40), radius=14, fill=YELLOW)
        d.text((cx, H - 98), name, font=tag_font, fill=(0, 0, 0), anchor="ma")
    # 文字带：系列名小字 + 两行标题（白 / 黄）
    sf = _font(54)
    d.text((W // 2, 44), cs.get("series", ""), font=sf, fill=(200, 200, 200), anchor="ma")
    lines = [l.strip() for l in c["title"].split("\n") if l.strip()][:2]
    size_ = 132
    font = _font(size_)
    while max(d.textlength(l, font=font) for l in lines) > W - 80 and size_ > 80:
        size_ -= 8
        font = _font(size_)
    lh = int(size_ * 1.25)
    y = TOP // 2 + 30 - lh * len(lines) // 2
    for i, line in enumerate(lines):
        d.text((W // 2, y), line, font=font, fill=YELLOW if i == len(lines) - 1 else WHITE, anchor="ma",
               stroke_width=max(6, size_ // 16), stroke_fill=(0, 0, 0))
        y += lh
    print(f"  对话封面：左取 t={t_duo:.1f}，右取 {'单人帧 t=%.1f' % t_single if t_single < c['end'] - 4 else '双人帧'}")
    return img


def cover_duo_frame(video, c, plan, probe_single):
    """对话系列封面：取两人都在画面里的一帧，裁掉顶部空白和底部字幕后整幅放在 3:4 画布中间，
    背景是同一帧放大模糊压暗，上面系列名 + 两行标题。plan["cover_frame"] = {"series": ..., "box": [x, y, w, h]}"""
    cf = c.get("cover_frame") or plan["cover_frame"]   # 段内可覆盖（同一场直播有的段是单人满屏）
    # 每秒抽一帧，只要两人都在的画面，再用人脸检测（liveclip.vision）看两张脸的眼睛
    # 都睁着，挑睁眼度最高的一帧；闭眼约 <0.12，正常 0.2–0.35
    t0, dur = c["start"] + 1, max(2.0, min(c["end"] - c["start"] - 2, 90))
    tmpd = Path(tempfile.mkdtemp())
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t0:.2f}", "-t", f"{dur:.2f}", "-i", str(video),
                    "-vf", "fps=1", str(tmpd / "f_%03d.png")], check=True)
    files = sorted(tmpd.glob("f_*.png"))
    cand = [f for f in files if not probe_single(t0 + files.index(f))]
    avoid = cf.get("avoid")   # 直播间贴纸会飘在这个区域：彩色像素多的帧不要（发到别的平台算他站界面元素）
    if avoid:
        def sticker(f):
            im = Image.open(f).convert("RGB").crop((avoid["x"], avoid["y"], avoid["x"] + avoid["w"], avoid["y"] + avoid["h"]))
            px = list(im.resize((40, 40)).getdata())
            return sum(1 for r, g, b in px if max(r, g, b) - min(r, g, b) > 90) / len(px) > 0.006
        cand2 = [f for f in cand if not sticker(f)]
        cand = cand2 or cand
    best, best_eye, t = None, -1.0, t0
    if cand:
        for r in vision.detect(cand) or []:
            if r["faces"] >= 2 and r["min_eye"] > best_eye:
                best, best_eye = Path(r["file"]), r["min_eye"]
    if best is None:
        best = cand[0] if cand else files[0]
    t = t0 + files.index(best)
    frame = Image.open(best).convert("RGB")
    for f in files:
        f.unlink()
    tmpd.rmdir()
    print(f"  对话封面：候选 {len(cand)} 帧，取 t={t:.1f}（片段起点 +{t - c['start']:.1f}s，睁眼度 {best_eye:.2f}）")
    x, y, w, h = cf.get("box", (0, 0, frame.size[0], frame.size[1]))
    d0 = ImageDraw.Draw(Image.new("RGB", (W, H)))
    lines = [l.strip() for l in c["title"].split("\n") if l.strip()][:2]
    size_ = 136
    font = _font(size_)
    while max(d0.textlength(l, font=font) for l in lines) > W - 80 and size_ > 80:
        size_ -= 8
        font = _font(size_)
    lh = int(size_ * 1.25)
    text_h = lh * len(lines) + (90 if cf.get("series") else 0) + 60   # 文字带需要的高度（含系列名、底部留白）
    pic = frame.crop((x, y, x + w, y + h))
    ph = int(round(h * W / w / 2)) * 2
    pw = W
    if ph > H - text_h - 60:   # 画面太高（单人满屏那种），缩小让标题放得下
        ph = (H - text_h - 60) // 2 * 2
        pw = int(round(ph * w / h / 2)) * 2
    pic = pic.resize((pw, ph), Image.LANCZOS)
    from PIL import ImageFilter
    bg = frame.resize((int(frame.size[0] * H / frame.size[1]), H))
    bg = bg.crop(((bg.size[0] - W) // 2, 0, (bg.size[0] - W) // 2 + W, H)).filter(ImageFilter.GaussianBlur(28))
    img = ImageEnhance.Brightness(bg).enhance(0.45)
    py = max(text_h, (H - ph) // 2 + 90)
    if py + ph > H - 40:
        py = H - 40 - ph
    img.paste(pic, ((W - pw) // 2, py))
    d = ImageDraw.Draw(img)
    sf = _font(52)
    ty = py - 60 - lh * len(lines)
    if cf.get("series"):
        d.text((W // 2, ty - 80), cf["series"], font=sf, fill=(220, 220, 220), anchor="ma", stroke_width=3, stroke_fill=(0, 0, 0))
    for i, line in enumerate(lines):
        d.text((W // 2, ty), line, font=font, fill=YELLOW if i == len(lines) - 1 else WHITE, anchor="ma",
               stroke_width=max(6, size_ // 16), stroke_fill=(0, 0, 0))
        ty += lh
    return img


LAST_FRAME = None


def cover_from_video(mp4, plan, c):
    """封面直接取成片里的一帧裁成 3:4（和视频里看到的一样：标题层 + 画面）。每秒抽一帧，避开贴纸区域有彩色贴纸的帧，
    两张脸都睁眼的里挑睁眼度最高的。plan["cover_from_video"] = {"top": 150, "avoid": {"x","y","w","h"}}（坐标按 1080x1920 成片）"""
    cfg = plan["cover_from_video"]
    tmpd = Path(tempfile.mkdtemp())
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "0.5", "-i", str(mp4), "-vf", "fps=1", str(tmpd / "f_%03d.png")], check=True)
    files = sorted(tmpd.glob("f_*.png"))
    avoid = cfg.get("avoid")
    def sticker(f):
        im = Image.open(f).convert("RGB").crop((avoid["x"], avoid["y"], avoid["x"] + avoid["w"], avoid["y"] + avoid["h"]))
        px = list(im.resize((270, 60)).getdata())   # 贴纸在成片里约 80×40 像素，分辨率太低会被平均掉
        return sum(1 for r, g, b in px if max(r, g, b) - min(r, g, b) > 80 and b > 110 and b >= g) / len(px) > 0.002
    cand = [f for f in files if not (avoid and sticker(f))] or files
    best, best_eye = cand[0], -1.0
    for r in vision.detect(cand) or []:
        if r["faces"] >= cfg.get("faces", 1) and r["min_eye"] > best_eye:
            best, best_eye = Path(r["file"]), r["min_eye"]
    img = Image.open(best).convert("RGB")
    global LAST_FRAME
    LAST_FRAME = img.copy()
    # 裁切起点：找到成片里标题层第一行字的位置（最上面出现亮像素的那一行），往上留 50px；单人满屏段标题更靠上
    g = img.convert("L")
    first = next((y for y in range(0, 700, 4) if sum(1 for x in range(0, W, 6) if g.getpixel((x, y)) > 200) > 8), None)
    top = max(0, first - 50) if first is not None else cfg.get("top", 150)
    top = min(top, 1920 - H)
    img = img.crop((0, top, W, top + H))
    k = files.index(best)
    for f in files:
        f.unlink()
    tmpd.rmdir()
    print(f"  成片取帧：第 {k + 0.5:.1f} 秒（候选 {len(cand)}/{len(files)} 帧，睁眼度 {best_eye:.2f}）")
    return img


def cover_bili(frame, plan, c, video):
    """B站 横版封面 1920×1080：B站 会同时裁成 4:3（首页）和 16:9（空间）展示，所以标题和画面都收在中间 75% 宽度内。
    画面 = 成片里的前景区域（按 cut_clips.make_layout 算，排除标题和字幕），标题两行在上，背景同帧模糊压暗。"""
    from .. import cut as cc
    from PIL import ImageFilter
    BW, BH = 1920, 1080
    over = {k: c[k] for k in cc.CLIP_LAYOUT_KEYS if k in c}
    lay = cc.make_layout(video, {**plan, **over}, None, cc.manifest_row(plan["bv"]).get("role"))
    bottom = lay["fg_y"] + lay["fg_h"]
    if lay["cap_y"] < bottom:   # 单人满屏时字幕压在画面里，封面不要带字幕
        bottom = lay["cap_y"] - 16
    pic = frame.crop((0, lay["fg_y"], W, bottom))
    bg = frame.resize((BW, int(frame.size[1] * BW / frame.size[0])))
    bg = bg.crop((0, (bg.size[1] - BH) // 2, BW, (bg.size[1] - BH) // 2 + BH)).filter(ImageFilter.GaussianBlur(30))
    img = ImageEnhance.Brightness(bg).enhance(0.4)
    d = ImageDraw.Draw(img)
    lines = [l.strip() for l in c["title"].split("\n") if l.strip()][:2]
    size_ = 104
    font = _font(size_)
    while max(d.textlength(l, font=font) for l in lines) > BW * 0.72 and size_ > 60:
        size_ -= 6
        font = _font(size_)
    lh = int(size_ * 1.22)
    text_h = lh * len(lines) + 40
    ph = BH - text_h - 50
    pw = int(pic.size[0] * ph / pic.size[1])
    if pw > BW * 0.72:   # 画面太宽（单人满屏裁得高）就按宽限
        pw = int(BW * 0.72); ph = int(pic.size[1] * pw / pic.size[0])
    pic = pic.resize((pw, ph), Image.LANCZOS)
    img.paste(pic, ((BW - pw) // 2, BH - 30 - ph))
    y = 30
    for i, line in enumerate(lines):
        d.text((BW // 2, y), line, font=font, fill=YELLOW if i == len(lines) - 1 else WHITE, anchor="ma",
               stroke_width=max(5, size_ // 16), stroke_fill=(0, 0, 0))
        y += lh
    return img


def main(argv=None):
    ap = argparse.ArgumentParser(prog="liveclip covers")
    ap.add_argument("clips", nargs="+")
    ap.add_argument("--out", default=str(C.DELIVER / "_covers"))
    ap.add_argument("--bili", action="store_true", help="另出一张 B站 横版封面 <名>_B站.jpg（1920×1080，4:3 裁切也完整）")
    ap.add_argument("--style", choices=["v2", "old"],
                    help="v2 = liveclip.cover.v2 新版（顶部标题区 + 原片干净帧）；默认直播切片 v2、其他 old")
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for p in map(Path, args.clips):
        bv, plan, idx = find_plan(p)
        c = plan["clips"][idx - 1]
        video = C.ws(plan.get("video") or C.VIDEOS / f"{bv}.mp4")
        vertical = bool(plan.get("live"))
        if (args.style or ("v2" if plan.get("live") else "old")) == "v2":
            from . import v2 as cover_v2
            from .. import cut as cc
            r = cover_v2.covers_all(cc.find_video(bv, plan), plan, c, cc.manifest_row(bv).get("role"))
            r["34"].save(out / (p.stem + ".jpg"), quality=92)
            r["43"].save(out / (p.stem + "_4比3.jpg"), quality=92)   # 抖音横封面
            print(out / (p.stem + ".jpg"))
            if args.bili:
                r["bili"].save(out / (p.stem + "_B站.jpg"), quality=92)
            continue
        probe = plan.get("cover_probe") or plan.get("duo_detect")

        def single(t):   # 横屏播客：单人满屏时取样区是浅色墙（亮、均匀）；双人时是另一个人的暗房间。按 cover_probe 区域平均亮度判断
            px = list(grab(video, t).convert("L").crop((probe["x"], probe["y"], probe["x"] + probe["w"], probe["y"] + probe["h"]))
                      .resize((60, 60)).getdata())
            return sum(px) / len(px) > probe.get("min_mean", 110)
        if plan.get("cover_from_video"):
            img = cover_from_video(p, plan, c)
        elif plan.get("cover_frame") or c.get("cover_frame"):
            img = cover_duo_frame(video, c, plan, single)
        elif plan.get("cover_split"):
            img = cover_duo_split(video, c, plan, single)
        else:
            t, face_x = c["start"] + 3, plan.get("cover_face_x")
            # 横屏双人播客：找一帧单人满屏的，要求前后各 1 秒也都是单人，避开画面切换的叠化
            while probe and t < c["end"] - 4 and not (single(t - 1.0) and single(t) and single(t + 1.0)):
                t += 1.5
            if probe and t >= c["end"] - 4:   # 整段都是双人画面：退回片段开头，裁切框对准右侧那个人
                t, face_x = c["start"] + 3, plan.get("cover_face_x_duo", face_x)
            img = crop_34(grab(video, t), vertical, face_x)
            dark = Image.new("RGB", img.size, (0, 0, 0))
            mask = Image.linear_gradient("L").resize(img.size).point(lambda v: int(v * 0.75))
            img = Image.composite(dark, ImageEnhance.Brightness(img).enhance(1.05), mask)
            draw_title(img, [l.strip() for l in c["title"].split("\n") if l.strip()][:2])
        dst = out / (p.stem + ".jpg")
        img.save(dst, quality=92)
        print(dst)
        if args.bili and LAST_FRAME is not None:
            cover_bili(LAST_FRAME, plan, c, video).save(out / (p.stem + "_B站.jpg"), quality=92)


if __name__ == "__main__":
    main()
