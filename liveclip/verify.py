"""成片验收：音视频轨、时长、完整解码（不用 -c copy，复制流不能证明每帧都正常）、抽帧拼图。
用法: liveclip verify <输出json> <拼图目录> mp4..."""
import json
import subprocess
import sys
import time
from pathlib import Path


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) < 3:
        sys.exit("usage: liveclip verify <out.json> <contact-sheet dir> clip.mp4 ...")
    out_json, sheet_dir, files = Path(argv[0]), Path(argv[1]), argv[2:]
    sheet_dir.mkdir(parents=True, exist_ok=True)
    rows = verify(files, sheet_dir)
    out_json.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0 if all(r["full_decode"] == "ok" for r in rows) else 1


def verify(files, sheet_dir):
    rows = []
    for f in files:
        p = Path(f).resolve()
        info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(p)],
                                         capture_output=True, text=True, check=True).stdout)
        v = [s for s in info["streams"] if s["codec_type"] == "video"]
        a = [s for s in info["streams"] if s["codec_type"] == "audio"]
        dur = float(info["format"]["duration"])
        t0 = time.time()
        dec = subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(p), "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"],
                             capture_output=True, text=True)
        dec_s = time.time() - t0
        # 抽 4 帧：开头 0.5s、预告后/推近结束 4s、中间、尾卡
        sheet = sheet_dir / (p.parent.parent.name + "_" + p.parent.name + "_" + p.stem + ".jpg")
        ts = [0.5, 4.0, dur / 2, dur - 1.0]
        ins = sum((["-ss", f"{t:.2f}", "-i", str(p)] for t in ts), [])
        subprocess.run(["ffmpeg", "-v", "error", "-y", *ins, "-filter_complex",
                        "".join(f"[{i}:v]trim=end_frame=1,scale=270:480[s{i}];" for i in range(4))
                        + "".join(f"[s{i}]" for i in range(4)) + "hstack=inputs=4", "-frames:v", "1", str(sheet)], check=True)
        rows.append({"file": str(p), "video": f"{v[0]['codec_name']} {v[0]['width']}x{v[0]['height']} {v[0]['r_frame_rate']}" if v else None,
                     "audio": f"{a[0]['codec_name']} {a[0]['sample_rate']}Hz" if a else None,
                     "duration": round(dur, 2), "size_mb": round(int(info["format"]["size"]) / 1e6, 1),
                     "full_decode": "ok" if dec.returncode == 0 and not dec.stderr.strip() else dec.stderr[-500:] or f"exit {dec.returncode}",
                     "decode_seconds": round(dec_s, 1), "contact_sheet": str(sheet.resolve())})
        print(json.dumps(rows[-1], ensure_ascii=False))
    return rows


if __name__ == "__main__":
    sys.exit(main())
