"""Synthetic sample for offline end-to-end runs (no ASR, no LLM, no network, nothing billed).

Creates in <workspace>:
  config.yaml                      minimal config (publishing off)
  videos/demo.mp4                  180 s, 720x1280 test pattern + noise bursts aligned with the fake sentences (silent pauses)
  data/asr/demo_bailian.json       fake transcript (60 lines, one every 3 s)
  data/asr/demo_bailian_raw/       fake word-level ASR result in the DashScope format (lets the tight edit run)
  fixtures/*.json                  canned LLM answers for LIVECLIP_LLM_FIXTURES

    python tests/sample.py /tmp/lc-demo
    cd /tmp/lc-demo && LIVECLIP_LLM_FIXTURES=fixtures LIVECLIP_VISION=none \\
        liveclip clip videos/demo.mp4 --asr-json data/asr/demo_bailian.json --title 演示 --top 2 --live --tight --tight-separate
"""
import json
import subprocess
import sys
from pathlib import Path

CONFIG = """streamer:
  name: 演示主播
  persona: 一位做测试用的中文直播博主
  topics: 测试、演示
branding:
  end_card: 完整版见主页
  end_card_live: 直播精选
tags:
  douyin_must: [演示]
  bili_must: [演示, 测试]
live:
  anchors: [{match: 演示主播, key: demo}]
publish:
  enabled: false
notify: {backend: none}
"""

CHARS = "今天我们来聊一个很重要的问题就是怎么把一件事情做好其实答案非常简单你要先想清楚目标然后每天坚持去做"


def lines():
    out = []
    for i in range(60):
        a = 3.0 * i
        text = "".join(CHARS[(i * 8 + k) % len(CHARS)] for k in range(8))
        out.append((a, a + 2.4, text))
    return out


def make(ws):
    ws = Path(ws)
    for d in ("videos", "data/asr", "fixtures"):
        (ws / d).mkdir(parents=True, exist_ok=True)
    (ws / "config.yaml").write_text(CONFIG, encoding="utf-8")
    video = ws / "videos" / "demo.mp4"
    if not video.exists():
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=720x1280:rate=30",
                        "-f", "lavfi", "-i", "aevalsrc=exprs='if(lt(mod(t\\,3)\\,2.4)\\,0.3*(random(0)*2-1)\\,0)':s=44100", "-t", "180",
                        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                        str(video)], check=True)
    body = [{"from": a, "to": b, "content": t + "。"} for a, b, t in lines()]
    (ws / "data/asr/demo_bailian.json").write_text(json.dumps({"source": "synthetic", "body": body}, ensure_ascii=False),
                                                    encoding="utf-8")
    raw = ws / "data/asr/demo_bailian_raw"
    raw.mkdir(parents=True, exist_ok=True)
    sents = []
    for a, b, t in lines():
        words = [{"text": ch, "begin_time": int((a + k * 0.3) * 1000), "end_time": int((a + k * 0.3 + 0.28) * 1000),
                  "punctuation": "。" if k == len(t) - 1 else ""} for k, ch in enumerate(t)]
        sents.append({"begin_time": int(a * 1000), "end_time": int(b * 1000), "text": t + "。", "words": words})
    (raw / "meta.json").write_text(json.dumps({"engine": "dashscope", "parts": [[0, 180000]]}), encoding="utf-8")
    (raw / "part0.json").write_text(json.dumps({"transcripts": [{"sentences": sents}]}, ensure_ascii=False), encoding="utf-8")
    fx = ws / "fixtures"
    clip = lambda s, e, title, hs=None, he=None: {
        "start": s, "end": e, "score": 8, "title": title, "category": "开场论点", "why": "演示", "risky": False,
        "hook_start": hs, "hook_end": he, "context_check": "不会", "tags": ["效率"], "event_tags": [], "point": "先想清楚目标"}
    (fx / "pick.json").write_text(json.dumps({"clips": [clip(6, 26.4, "先想清楚目标\n再每天坚持", 15, 20.4),
                                                        clip(90, 114.4, "答案非常简单")], "typo_fixes": []},
                                             ensure_ascii=False), encoding="utf-8")
    (fx / "review_subs.json").write_text(json.dumps({"lines": [{"i": 0, "text": "今天我们来聊一个很", "status": "fixed", "note": "演示"}]},
                                                    ensure_ascii=False), encoding="utf-8")
    (fx / "hook.json").write_text(json.dumps({"candidates": [{"first_line": 2, "last_line": 3, "text": "演示预告", "type": "反常识",
                                                              "score": 8, "reason": "演示"}], "best": 0, "why": "演示"},
                                             ensure_ascii=False), encoding="utf-8")
    (fx / "tight.json").write_text(json.dumps({"cuts": [{"line": 1, "text": "一个", "why": "演示删除"}], "captions": []},
                                              ensure_ascii=False), encoding="utf-8")
    return ws


if __name__ == "__main__":
    print(make(sys.argv[1] if len(sys.argv) > 1 else "demo-workspace"))
