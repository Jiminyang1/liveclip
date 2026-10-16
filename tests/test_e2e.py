"""Offline end-to-end runs on a synthetic 3-minute video: transcript → review → pick → hook → cut → covers → verify →
QA → tight edit → captions → album, then a catch-phrase montage. LLM answers come from fixtures; no ASR, no network,
nothing billed (~1–2 min, needs ffmpeg)."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import sample  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def run(ws, *args):
    env = {**os.environ, "LIVECLIP_CONFIG": str(ws / "config.yaml"), "LIVECLIP_LLM_FIXTURES": str(ws / "fixtures"),
           "LIVECLIP_VISION": "none"}
    env.pop("ANTHROPIC_API_KEY", None)
    return subprocess.run([sys.executable, "-m", "liveclip", *args], cwd=ws, env=env, capture_output=True, text=True,
                          timeout=900)


@pytest.fixture(scope="module")
def clipped(tmp_path_factory):
    ws = sample.make(tmp_path_factory.mktemp("ws"))
    r = run(ws, "clip", "videos/demo.mp4", "--asr-json", "data/asr/demo_bailian.json", "--title", "演示", "--top", "2",
            "--live", "--auto-layout", "--tight", "--tight-separate")
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    return ws, Path(r.stdout.strip().splitlines()[-1])


def test_clip_package(clipped):
    _, out = clipped
    clips = sorted(out.glob("[0-9][0-9]_*"))
    assert len(clips) == 2
    for d in clips:
        for f in ("视频.mp4", "视频_精剪.mp4", "封面.jpg", "封面_横4比3.jpg", "封面_B站.jpg", "文案.txt"):
            assert (d / f).exists(), d / f
    assert (out / "专辑.html").exists() and (out / "候选清单.txt").exists()
    assert all(x["full_decode"] == "ok" for x in json.loads((out / "验收.json").read_text()))
    assert all(x["full_decode"] == "ok" for x in json.loads((out / "验收_精剪.json").read_text()))


def test_qa_passes_on_synthetic_clips(clipped):
    _, out = clipped
    for name in ("质检.json", "质检_精剪.json"):
        for x in json.loads((out / name).read_text()):
            assert x["音频时间戳"]["result"] == "✓", x
            assert x["音画同步"]["result"] == "✓", x


def test_montage(clipped):
    ws, _ = clipped
    r = run(ws, "montage", "--phrase", "目标", "--sessions", "demo", "--max", "4", "--target", "10", "--max-hit", "3")
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    outs = list((ws / "deliver").glob("混剪_目标_*/01_*/视频.mp4"))
    assert len(outs) == 1


def test_live_follow_offline(clipped):
    """sim-live puts 2 minutes of the demo on a local "remote" (rclone accepts plain paths), follow --once picks,
    cuts and finishes the session. ASR answers come from LIVECLIP_ASR_FIXTURES."""
    ws, _ = clipped
    if not shutil.which("rclone"):
        pytest.skip("rclone not installed")
    remote = ws / "remote"
    r = run(ws, "sim-live", "videos/demo.mp4", "--minutes", "2", "--every", "0", "--remote", str(remote),
            "--anchor", "演示主播", "--prefix-time", "2026-01-01_20-00-00")
    assert r.returncode == 0, r.stderr[-2000:]
    fx = ws / "asrfx"
    fx.mkdir(exist_ok=True)
    shutil.copy(ws / "data/asr/demo_bailian.json", fx / "演示主播_2026-01-01_20-00-00_000.json")
    os.environ["LIVECLIP_ASR_FIXTURES"] = str(fx)
    try:
        r = run(ws, "follow", "--once", "--remote", str(remote))
    finally:
        os.environ.pop("LIVECLIP_ASR_FIXTURES")
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    out = ws / "deliver" / "直播跟播_LIVE_20260101_2000_demo"
    assert list(out.glob("01_*/视频.mp4")), r.stdout[-3000:]
    assert (out / "完整版" / "字幕.srt").exists() and (out / "专辑.html").exists()
    state = json.loads(next((ws / "recordings/live").glob("*/*/state.json")).read_text())
    assert state["done"] and state["rounds"] == 1
