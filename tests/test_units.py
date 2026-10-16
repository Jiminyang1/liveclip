"""Pure-Python unit tests (no ffmpeg, no network, no model calls)."""
import importlib
import pkgutil

import liveclip


def test_every_module_imports():
    names = [m.name for m in pkgutil.walk_packages(liveclip.__path__, "liveclip.")]
    assert len(names) > 25
    for n in names:
        importlib.import_module(n)


def test_config_from_test_file():
    from liveclip.config import C
    assert C.name == "测试主播"
    assert C.publish_enabled() is False and C.publish_enabled("douyin") is False
    rx = C.name_variants_re()
    assert rx.sub(C.canonical_name, "我是alex，不是Alexander") == "我是阿力，不是Alexander"


def test_tags_per_platform():
    from liveclip import tags as T
    dy = T.for_platform("douyin", ["减脂", "直播", "测试主播", "睡眠"], ["某事件"])
    assert dy[:2] == ["测试主播", "测试理论"] and "直播" not in dy and len(dy) <= 5
    assert dy[2] == "某事件" and "身材" in dy          # conditional tag because 减脂 is in when_any
    bili = T.for_platform("bili", ["睡眠"], [])
    assert bili[:3] == ["测试主播", "测试理论", "健身"] and "睡眠" in bili


def test_title_cleanup_and_wrap():
    from liveclip.pick import clean_title
    assert clean_title("嘉宾：钱不是问题") == "钱不是问题"
    assert clean_title("测试主播：先想清楚\n再坚持") == "先想清楚\n再坚持"
    from PIL import Image, ImageDraw
    from liveclip.cut import clean_caption, wrap
    from liveclip.fonts import font
    d = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    lines = wrap("今天我们来聊一个非常重要而且很多人都会遇到的问题", font(62), 940, d)
    assert 1 <= len(lines) <= 2 and "".join(lines) == "今天我们来聊一个非常重要而且很多人都会遇到的问题"
    assert clean_caption("嗯。") == "" and clean_caption("你好。") == "你好"


def test_enforce_and_snap():
    from liveclip.pick import enforce, snap
    cues = [(i * 3.0, i * 3.0 + 2.4, f"这是第{i}句完整的话") for i in range(100)]
    clips = [{"start": 6.2, "end": 26.0, "score": 9, "title": "a", "hook_start": 15.1, "hook_end": 20.3},
             {"start": 30, "end": 50, "score": 8, "title": "b", "hook_start": None, "hook_end": None},     # too close to a
             {"start": 120, "end": 125, "score": 7, "title": "c", "hook_start": None, "hook_end": None}]    # too short
    dropped = []
    kept = enforce(snap(clips, cues), cues[-1][1], dropped)
    assert [c["title"] for c in kept] == ["a"]
    assert kept[0]["start"] == 6.0 and kept[0]["hook_start"] == 15.0
    assert {d["title"] for d in dropped} == {"b", "c"}


def test_caption_roundtrip(tmp_path):
    from liveclip.caption import caption_text
    from liveclip.publish.common import info
    d = tmp_path / "01_x"
    d.mkdir()
    (d / "视频.mp4").write_bytes(b"\0")
    (d / "文案.txt").write_text(caption_text({"title": "测试主播：先想清楚\n再坚持", "tags": ["睡眠"], "event_tags": []}, []),
                               encoding="utf-8")
    m = info(d)
    assert m["douyin"]["title"] == "先想清楚，再坚持"
    assert m["douyin"]["tags"][:2] == ["测试主播", "测试理论"] and "睡眠" in m["douyin"]["tags"]


def test_llm_fixture(tmp_path, monkeypatch):
    import json
    from liveclip import llm
    (tmp_path / "pick.json").write_text(json.dumps([{"n": 1}, {"n": 2}]))
    monkeypatch.setenv("LIVECLIP_LLM_FIXTURES", str(tmp_path))
    assert llm.ask_json("p", {}, tool="选段") == {"n": 1}
    assert llm.ask_json("p", {}, tool="选段") == {"n": 2}


def test_publish_refuses_when_disabled():
    import pytest
    from liveclip.config import C
    with pytest.raises(SystemExit):
        C.require_publish("douyin")


def test_cli_help(capsys):
    from liveclip.cli import main
    assert main(["--help"]) == 0
    assert "follow" in capsys.readouterr().out
