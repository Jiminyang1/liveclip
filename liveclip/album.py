"""给 deliver/<专辑目录>/ 生成离线预览网页 专辑.html：每条一张卡片（封面做海报、点开播放、标题、时长、争议等级、话题、选段理由）。
用法: liveclip album <专辑目录> <计划json> "<页面标题>" ["<一句说明>"]
说明不传就用计划 json 里的 note。"""
import html
import json
import re
import subprocess
import sys
from pathlib import Path


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) < 3:
        sys.exit('usage: liveclip album <deliver dir> <plan.json> "<page title>" ["<note>"]')
    root, plan_path, title = Path(argv[0]), Path(argv[1]), argv[2]
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    page_note = argv[3] if len(argv) > 3 else plan.get("note", "")
    by_stem = {re.sub(r"[^\w]+", "", c["title"]): c for c in plan["clips"]}
    cards = []
    for d in sorted(p for p in root.iterdir() if p.is_dir() and re.match(r"\d\d_", p.name)):
        txt = (d / "文案.txt").read_text(encoding="utf-8")
        note = re.search(r"^⚠ 注意：(.+)$", txt, re.M)
        t = re.search(r"【抖音】.*?\n标题：\n(.+)", txt).group(1)
        tags = re.search(r"作品描述：\n(.+)", txt).group(1)
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(d / "视频.mp4")],
                                   capture_output=True, text=True).stdout)
        c = by_stem.get(d.name.split("_", 1)[1], {})
        orig = ""   # auto_clip --tight：视频.mp4 是精剪版，正常版在 视频_原版.mp4
        if (d / "视频_原版.mp4").exists():
            od = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(d / "视频_原版.mp4")],
                                      capture_output=True, text=True).stdout)
            orig = f" · 精剪版（原版 {od:.0f} 秒，见 视频_原版.mp4）"
        cards.append((d.name, t, tags, note.group(1) if note else "", dur, c.get("why", ""), c.get("start", 0), orig))
    hms = lambda s: f"{int(s)//3600}:{int(s)%3600//60:02d}:{int(s)%60:02d}"
    items = "".join(f'''
    <section class="card" id="c{n}">
      <video controls preload="metadata" poster="{html.escape(f)}/封面.jpg" src="{html.escape(f)}/视频.mp4"></video>
      <div class="info"><h2>{n:02d} · {html.escape(t)} <small>{dur:.0f} 秒 · 视频 {hms(st)} 起{orig}</small></h2>
        {f'<p class="warn">⚠ {html.escape(note)}</p>' if note else ''}<p class="tags">{html.escape(tags)}</p>
        <p class="why">{html.escape(w)}</p><p class="path">{html.escape(f)}/</p></div></section>'''
        for n, (f, t, tags, note, dur, w, st, orig) in enumerate(cards, 1))
    toc = "".join(f'<a href="#c{n}">{n:02d} {html.escape(t.split("：", 1)[-1])}</a>' for n, (f, t, *_) in enumerate(cards, 1))
    page = f'''<!doctype html><html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>{html.escape(title)}</title><style>
    :root{{--bg:#111;--fg:#eee;--muted:#999;--card:#1c1c1c;--accent:#ffd400}}
    body{{margin:0;padding:24px 16px;background:var(--bg);color:var(--fg);font:15px/1.6 -apple-system,"PingFang SC",sans-serif}}
    h1{{margin:0 0 4px}} .note{{color:var(--muted);margin:0 0 20px}}
    .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,360px));justify-content:center;gap:18px;max-width:1500px;margin:0 auto}}
    .card{{background:var(--card);border-radius:10px;overflow:hidden;display:flex;flex-direction:column}}
    video{{width:100%;aspect-ratio:9/16;background:#000;display:block;object-fit:contain}}
    .info{{padding:12px 14px}} h2{{font-size:17px;margin:0 0 6px}} h2 small{{display:block;color:var(--muted);font-weight:400;font-size:12px}}
    .warn{{color:var(--accent);margin:4px 0;font-size:13px}} .tags{{color:#8ab4f8;margin:4px 0;font-size:13px}}
    .why{{color:#bbb;margin:6px 0;font-size:13px}} .path{{color:#666;margin:4px 0 0;font-size:12px}}
    .toc{{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 18px}} .toc a{{color:var(--fg);background:#2a2a2a;border-radius:6px;padding:4px 10px;text-decoration:none;font-size:13px}}
    .toc a:hover{{background:var(--accent);color:#000}}</style></head><body>
    <h1>{html.escape(title)}</h1>
    <p class="note">{html.escape(page_note)} 全部未音画复核；⚠ 为争议等级，照常排期只作提示。文案见各文件夹 文案.txt，候选见 候选清单.txt。</p>
    <nav class="toc">{toc}</nav><div class="grid">{items}</div></body></html>'''
    (root / "专辑.html").write_text(page, encoding="utf-8")
    print("写入", root / "专辑.html", len(cards), "条")


if __name__ == "__main__":
    main()
