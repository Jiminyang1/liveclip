"""封面（v2，参考 AutoClip cover_design 的版式）：顶部深色标题区 + 两行粗黑标题（第二行黄色）+ 短色条，
下面是从原片取的干净画面（不带成片里的标题、字幕层），上沿渐隐进标题区。不加名字标签。
取帧：片段正文里每 1.5 秒抽一帧，按段的 layout 裁出画面区（分屏只取视频带，单人去掉手机状态栏），
人脸检测（liveclip.vision：macOS Vision / OpenCV）要求人脸数够（单人 1、分屏 2）、都睁眼，避开直播间洋红/紫色贴纸，挑睁眼度最高的。
没有人脸检测后端时取中间一帧。
输出三张（同一帧）：竖 3:4 1080×1440（抖音竖封面、小红书、视频号）、横 4:3 1440×1080（抖音横封面）、B站 16:9 1920×1080。
用法: liveclip cover-v2 <ID> <plan.json> --out <目录> [--only 1,2]
"""
import argparse
import json
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

from .. import vision
from ..config import C
from ..cut import CLIP_LAYOUT_KEYS, find_video, make_layout, manifest_row
from ..fonts import font as _font
W, H = 1080, 1440                              # 3:4
CANVAS = (20, 20, 22)
WHITE = (240, 238, 232)
YELLOW = (255, 212, 0)


def font(size, weight="Black"):
    return _font(size, kind="cover", weight=weight)


def region(lay, sw, sh):
    """原片里的画面区（像素）：分屏取视频带；单人 full_v 去掉顶部 5% 状态栏和底部 4%；其他按 layout 的 crop。"""
    x, y, w, h = lay["crop"]
    if lay["name"] == "full_v":
        return (0, int(sh * 0.05), sw, int(sh * 0.96))
    return (int(sw * x), int(sh * y), int(sw * (x + w)), int(sh * (y + h)))


def sticker_ratio(im):
    """洋红/紫色高饱和像素占比（直播间贴纸、PK 特效）。"""
    import numpy as np
    hsv = np.asarray(im.resize((96, int(96 * im.height / im.width))).convert("HSV"), dtype=float) / 255
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    return float(((s > 0.45) & (v > 0.45) & (h >= 0.72) & (h <= 0.95)).mean())


def pick_frame(video, c, lay, tmp, want, box=None):
    sw, sh = lay["src"]
    if box:   # plan 的 cover_frame.box [x, y, w, h]：横屏播客原片底部烧着双语字幕，封面取景要避开（2026-10-12）
        box = (box[0], box[1], box[0] + box[2], box[1] + box[3])
    else:
        box = region(lay, sw, sh)
    t0, t1 = c["start"] + 1, c["end"] - 1
    n = max(6, min(30, int((t1 - t0) / 1.5)))
    files = []
    for k in range(n):
        t = t0 + (t1 - t0) * k / (n - 1)
        p = Path(tmp) / f"c{k:02d}.png"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
                        "-vf", f"crop={box[2] - box[0]}:{box[3] - box[1]}:{box[0]}:{box[1]}", str(p)])
        if p.exists():
            files.append(p)
    rows = vision.detect(files) or []
    best, best_key = None, None
    for r in rows:
        if r["faces"] < want:
            continue
        im = Image.open(r["file"]).convert("RGB")
        st = sticker_ratio(im)
        # 先要都睁眼，再要贴纸最少（贴纸只占画面零点几，按 0.01% 分档），最后比睁眼度
        key = (r["min_eye"] >= 0.2, -round(st / 0.0001), r["min_eye"])
        if best_key is None or key > best_key:
            best, best_key = (im, r["boxes"], r["min_eye"], st), key
    if best is None:   # 一张够数的脸都没有：取中间一帧
        im = Image.open(files[len(files) // 2]).convert("RGB")
        best = (im, [], 0.0, sticker_ratio(im))
    return best


def photo(im, boxes, w, h, duet):
    """把画面裁成 w×h：分屏整条视频带等比放大到宽度；单人按脸取头到肩（脸宽约占 40%，脸顶在上方 22% 处）。"""
    iw, ih = im.size
    if duet and iw / ih > (w / h) * 1.6:
        from PIL import ImageEnhance, ImageFilter
        scale = max(w / iw, h / ih)
        bg = im.resize((int(iw * scale) + 1, int(ih * scale) + 1)).crop((0, 0, w, h)).filter(ImageFilter.GaussianBlur(40))
        bg = ImageEnhance.Brightness(bg).enhance(0.35)
        fit = im.resize((w, int(ih * w / iw)), Image.Resampling.LANCZOS)
        bg.paste(fit, (0, (h - fit.height) // 2))
        return bg
    if duet or not boxes:
        scale = max(w / iw, h / ih)
        cx, cy = iw / 2, ih / 2
    else:
        x, y, bw, bh = max(boxes, key=lambda b: b[2] * b[3])
        fx, fy, fw = (x + bw / 2) * iw, y * ih, bw * iw
        scale = max(min(w * 0.40, h * 0.42) / fw, w / iw, h / ih)
        cx = fx
        cy = fy + (h * 0.5 - h * 0.22) / scale
    cw, ch = w / scale, h / scale
    left = min(max(0, cx - cw / 2), iw - cw)
    top = min(max(0, cy - ch / 2), ih - ch)
    return im.crop((int(left), int(top), int(left + cw), int(top + ch))).resize((w, h), Image.Resampling.LANCZOS)


def fit(draw, lines, max_w, start):
    size = start
    while size > 56 and any(draw.textlength(l, font=font(size)) > max_w for l in lines):
        size -= 4
    return size


def design(im, boxes, title, duet, w=W, h=H):
    """w×h 封面：顶部标题区 + 下方照片。B站 横版（1920×1080）会被裁成 4:3，标题宽度收在中间 1300 以内。"""
    lines = [l.strip() for l in title.split("\n") if l.strip()][:2] or [title]
    canvas = Image.new("RGB", (w, h), CANVAS)
    d = ImageDraw.Draw(canvas)
    size = fit(d, lines, min(w - 120, 1300), 118 if h >= w else 100)
    gap = int(size * 0.24)
    top = round(h * 0.064)
    block = len(lines) * size + (len(lines) - 1) * gap
    band = top + block + int(size * 0.62) + round(h * 0.076)   # 标题、色条下面再留一点，照片渐隐从这里开始
    over = round(h * 0.083)                                     # 照片顶上叠进标题区再渐隐
    ph_h = h - band + over
    if w > h:   # 横版：竖拍画面铺满会放大到只剩眼睛；中间放头到肩（分屏按视频带比例），两侧用同一帧模糊压暗补满
        from PIL import ImageEnhance, ImageFilter
        inner_w = min(w, round(ph_h * (im.width / im.height if duet else 1.05)))
        ph = ImageEnhance.Brightness(photo(im, [], w, ph_h, True).filter(ImageFilter.GaussianBlur(40))).enhance(0.35)
        inner = photo(im, boxes, inner_w, ph_h, duet)
        edge = Image.linear_gradient("L").rotate(90).resize((60, ph_h))      # 中间画面左右边缘也渐隐
        m = Image.new("L", inner.size, 255)
        m.paste(edge, (0, 0)); m.paste(edge.transpose(Image.Transpose.FLIP_LEFT_RIGHT), (inner_w - 60, 0))
        ph.paste(inner, ((w - inner_w) // 2, 0), m)
    else:
        ph = photo(im, boxes, w, ph_h, duet)
    fade = Image.linear_gradient("L").resize((w, round(h * 0.15)))  # 上黑下白：上面透明
    mask = Image.new("L", ph.size, 255)
    mask.paste(fade, (0, 0))
    canvas.paste(ph, (0, band - over), mask)
    sh_h = round(h * 0.18)
    shade = Image.linear_gradient("L").resize((w, sh_h)).point(lambda v: int(v * 0.55))
    canvas.paste(CANVAS, (0, h - sh_h, w, h), shade)
    d = ImageDraw.Draw(canvas)
    y, bottom = top, top
    for i, line in enumerate(lines):
        f = font(size)
        tw = d.textlength(line, font=f)
        colour = YELLOW if (i == len(lines) - 1 and len(lines) > 1) else WHITE
        d.text(((w - tw) / 2, y), line, font=f, fill=colour)
        bottom = d.textbbox(((w - tw) / 2, y), line, font=f)[3]     # 字形实际下沿（Noto 的字会画到字号以下）
        y += size + gap
    bar_w, bar_h = 96, max(8, size // 12)
    bar_y = bottom + int(size * 0.28)
    d.rectangle([(w - bar_w) / 2, bar_y, (w + bar_w) / 2, bar_y + bar_h], fill=YELLOW)
    return canvas


def covers_all(video, plan, c, role=None):
    """一段的全部封面（同一帧）：竖 3:4 1080×1440（抖音竖封面、小红书、视频号）、横 4:3 1440×1080（抖音上传要横竖两张）、
    B站 16:9 1920×1080。返回 dict，另带睁眼度和贴纸占比。"""
    over = {k: c[k] for k in CLIP_LAYOUT_KEYS if k in c}
    lay = make_layout(video, {**plan, **over}, None, role)
    cf = c.get("cover_frame") or plan.get("cover_frame") or {}
    duet = lay["name"] == "duet_v" or (lay["name"] in ("full_h", "talk_h") and bool(cf))   # 横屏双人播客
    with tempfile.TemporaryDirectory() as tmp:
        im, boxes, eye, st = pick_frame(video, c, lay, tmp, 2 if duet else 1, cf.get("box"))
    return {"34": design(im, boxes, c["title"], duet), "43": design(im, boxes, c["title"], duet, 1440, 1080),
            "bili": design(im, boxes, c["title"], duet, 1920, 1080), "eye": eye, "sticker": st}


def covers(video, plan, c, role=None):
    """兼容旧调用：(3:4, B站 16:9, 睁眼度, 贴纸占比)。"""
    r = covers_all(video, plan, c, role)
    return r["34"], r["bili"], r["eye"], r["sticker"]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="liveclip cover-v2")
    ap.add_argument("bv"); ap.add_argument("plan"); ap.add_argument("--out", required=True)
    ap.add_argument("--only")
    a = ap.parse_args(argv)
    plan = json.loads((C.CLIPS / a.bv / a.plan).read_text(encoding="utf-8"))
    video = find_video(a.bv, plan)
    role = manifest_row(a.bv).get("role")
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    only = {int(x) for x in a.only.split(",")} if a.only else None
    for i, c in enumerate(plan["clips"], 1):
        if only and i not in only:
            continue
        r = covers_all(video, plan, c, role)
        eye, st = r["eye"], r["sticker"]
        f = out / f"{i:02d}_{''.join(ch for ch in c['title'] if ch.isalnum())}.jpg"
        r["34"].save(f, quality=92)
        r["43"].save(f.with_name(f.stem + "_4比3.jpg"), quality=92)
        r["bili"].save(f.with_name(f.stem + "_B站.jpg"), quality=92)
        print(f"{f.name}  睁眼 {eye:.2f}  贴纸 {st:.3%}", flush=True)


if __name__ == "__main__":
    main()
