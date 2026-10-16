"""Configuration: config.yaml (+ .env) → one object every module reads.

Lookup order for the config file:
  1. $LIVECLIP_CONFIG
  2. ./config.yaml (or ./liveclip.yaml) in the current directory or any parent
  3. built-in defaults (generic persona, everything optional disabled)

The workspace (where data/, clips/, deliver/, videos/, recordings/, logs/ live) defaults to the directory that
contains the config file, or the current directory when there is none. Every path can be overridden in `paths:`.
API keys are never read from config.yaml — only from the environment (.env next to the config is loaded first).
"""
from __future__ import annotations

import copy
import os
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import yaml
except ImportError:  # pragma: no cover - pyyaml is a hard dependency, but keep import errors readable
    yaml = None

DEFAULTS = {
    "workspace": None,
    "paths": {"data": "data", "clips": "clips", "deliver": "deliver", "videos": "videos",
              "recordings": "recordings", "logs": "logs", "assets": "assets"},
    "streamer": {
        # 用在提示词、尾卡、文案里的称呼
        "name": "主播",
        # 说话人分离后主播那一路的标签（字幕里的 [主播]）、连麦对方的标签
        "speaker_label": "主播",
        "guest_label": "嘉宾",
        # 识别常把主播名字写成别的样子：这里列出要统一改成 canonical 的写法（正则，整词匹配英文）
        "name_variants": [],
        "canonical_name": None,          # 默认同 speaker_label
        "persona": "一位中文口播/直播博主，主做观点类口播和直播连麦",
        "topics": "个人成长、观点评论、生活方式",
        "audience": "短视频平台（抖音、B站、视频号、小红书）的观众",
        # 选段时“事件和联动”一类的例子（人、理论、热点）；空就用通用说法
        "event_examples": [],
        # 别的号反复切、反复火的观点：命中就优先选，不去重
        "hot_points": [],
        # 少选的方向（离观众远的话题）
        "avoid_topics": "",
        # 直播连麦画面的说明（给选段模型）
        "live_layout_note": "竖屏直播。连麦时画面左右分屏，左边是主播，右边是连麦对象；字幕是机器识别。",
    },
    "branding": {
        "end_card": "完整版见主页",           # 剪好的口播片段最后 2 秒的尾卡
        "end_card_live": "直播精选",          # 直播切片的尾卡
        "source": "",                          # B站转载来源（创作声明「转载」时要填）
    },
    "asr": {
        "engine": "bailian",                   # bailian | sensevoice
        "bailian_model": "qwen-audio-3.1-asr-flash-filetrans",
        "hotwords": {},                        # 词 → 权重（1–5），百炼热词
        "device": "auto",                      # SenseVoice: auto | cpu | cuda | mps
        "prices_cny_per_mtok": {"qwen-audio-3.1-asr-flash-filetrans": [0.8, 2.7]},
    },
    "review": {
        # 额外的校对规则（同音词、专有名词），每条一句话，会拼进字幕校对提示词
        "extra_rules": [],
    },
    "models": {
        "pick": "claude-opus-5-5",
        "review": "claude-opus-5-5",
        "hook": "claude-opus-5-5",
        "tight": "claude-opus-5-5",
        "themes": "claude-sonnet-5-5",
        "publish_agent": "claude-sonnet-5-5",
    },
    "llm": {
        "backend": "auto",                     # auto | cli | api   (auto: API when ANTHROPIC_API_KEY is set, else claude CLI)
        "lean": True,                          # claude -p 不带工具/插件/默认系统提示，便宜很多
        "effort": "high",
        "api_fallbacks": True,                 # API 路径：server-side refusal fallbacks
    },
    "tags": {
        "douyin_must": [],                     # 抖音/视频号/小红书必带话题
        "soft": [],                            # 名额够才加
        "bili_must": [],                       # B站必带标签
        "brand": [],                           # 品牌词：内容话题里出现就剔掉（必带组会自动算进来）
        "conditional": {"tag": "", "when_any": []},   # 内容话题命中 when_any 时才加 tag
        "generic": ["直播", "观点", "金句", "干货", "成长", "切片", "直播切片", "分享", "认知"],
        "limit": {"douyin": 5, "xhs": 10, "bili": 10, "shipinhao": 6},
    },
    "timezones": {
        "audience": "Asia/Shanghai",           # 发布时间、每日上限按这个时区算
        "audience_label": "北京",
        "recorder": "Asia/Shanghai",           # 录制机器的本地时区（分段文件名里的时间）
    },
    "live": {
        "remote": "",                          # rclone 远端目录，如 myremote:liveclip-live
        "archive_remote": "",                  # 完整版归档目录，如 myremote:liveclip-archive/full
        "anchors": [],                         # [{match: 录制器里主播名的子串, key: main}]；空 = 全部处理
        "title": "{name}直播（{date:%Y年%m月%d日}）",
        "window_sec": 1200, "tail_sec": 300, "lookback_sec": 300, "poll_sec": 60,
        "event_note_file": "live_note.txt",    # 放在 data/ 下
    },
    "recorder": {
        # 面板上显示录制机器状态（ssh 读日志）。留空就不查
        "ssh_host": "",
        "recorder_log": "/opt/liveclip/recorder.log",
        "uploader_log": "/opt/liveclip/uploader.log",
        "services": ["liveclip-recorder.service", "liveclip-uploader.timer"],
        "disk_path": "/opt",
    },
    "publish": {
        "enabled": False,                      # 总开关：不打开，任何发布命令都会拒绝
        "douyin": {"enabled": False, "account_id": "", "per_day": 5, "gap_min": 60, "next_hour": 19,
                   "chrome_channel": "chrome"},
        "bili": {"enabled": False, "biliup": "biliup", "tid": 21, "copyright": 2},
    },
    "montage": {
        "speaker": None,                       # 默认 streamer.speaker_label
        "aliases": {},                         # 正字 → [识别常见错字]
        "exclude": {},                         # 正字 → [字面含它其实是别的词的]
        "jieba_words": [],                     # --suggest 时加进 jieba 词典的专有词
    },
    "fonts": {"title": None, "title_index": None, "cover": None},
    "notify": {"backend": "auto"},             # auto | macos | linux | none
    "dashboard": {"port": 8771, "min_free_gb": 20},
}


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _find_config():
    env = os.environ.get("LIVECLIP_CONFIG")
    if env:
        return Path(env).expanduser().resolve()
    d = Path.cwd().resolve()
    for p in [d, *d.parents]:
        for name in ("config.yaml", "liveclip.yaml"):
            if (p / name).is_file():
                return p / name
    return None


def _load_env(path):
    """Minimal .env loader (KEY=VALUE, # comments). Existing environment variables win."""
    if not path or not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip().removeprefix("export ").strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


class Config:
    def __init__(self):
        self.file = _find_config()
        raw = {}
        if self.file:
            if yaml is None:
                sys.exit("pyyaml is required to read config.yaml (pip install pyyaml)")
            raw = yaml.safe_load(self.file.read_text(encoding="utf-8")) or {}
        self.raw = _merge(DEFAULTS, raw)
        base = self.file.parent if self.file else Path.cwd().resolve()
        _load_env(base / ".env")
        ws = os.environ.get("LIVECLIP_HOME") or self.raw.get("workspace")
        self.ROOT = (base / Path(ws).expanduser()).resolve() if ws else base
        p = self.raw["paths"]
        res = lambda k: (self.ROOT / Path(p[k]).expanduser()).resolve()
        self.DATA, self.CLIPS, self.DELIVER = res("data"), res("clips"), res("deliver")
        self.VIDEOS, self.RECORDINGS, self.LOGS, self.ASSETS = res("videos"), res("recordings"), res("logs"), res("assets")
        self.ASR = self.DATA / "asr"

    # ---- generic access
    def get(self, dotted, default=None):
        cur = self.raw
        for k in dotted.split("."):
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur

    # ---- frequently used values
    @property
    def name(self):
        return self.raw["streamer"]["name"]

    @property
    def host_label(self):
        return self.raw["streamer"]["speaker_label"]

    @property
    def guest_label(self):
        return self.raw["streamer"]["guest_label"]

    @property
    def canonical_name(self):
        return self.raw["streamer"].get("canonical_name") or self.host_label

    def name_variants_re(self):
        """Regex for ASR spellings of the streamer's name that should be unified (None when not configured)."""
        vs = [v for v in self.raw["streamer"].get("name_variants") or [] if v]
        if not vs:
            return None
        parts = [rf"(?<![A-Za-z]){re.escape(v)}(?![A-Za-z])" if re.match(r"^[A-Za-z]+$", v) else re.escape(v) for v in vs]
        return re.compile("|".join(parts), re.I)

    def model(self, step):
        return self.raw["models"].get(step) or self.raw["models"]["pick"]

    @property
    def audience_tz(self):
        return ZoneInfo(self.raw["timezones"]["audience"])

    @property
    def audience_tz_label(self):
        return self.raw["timezones"].get("audience_label") or self.raw["timezones"]["audience"]

    @property
    def recorder_tz(self):
        return ZoneInfo(self.raw["timezones"]["recorder"])

    def publish_enabled(self, platform=None):
        if not self.raw["publish"].get("enabled"):
            return False
        return True if platform is None else bool(self.raw["publish"].get(platform, {}).get("enabled"))

    def require_publish(self, platform):
        if not self.publish_enabled(platform):
            sys.exit(f"Publishing to {platform} is disabled. Set publish.enabled: true and publish.{platform}.enabled: true "
                     f"in config.yaml (only publish content you have the rights to — see README).")

    def rel(self, p):
        """Path relative to the workspace when it is inside it (plans store workspace-relative paths)."""
        p = Path(p).resolve()
        return str(p.relative_to(self.ROOT)) if p.is_relative_to(self.ROOT) else str(p)

    def ws(self, p):
        """Workspace-relative string/Path → absolute Path."""
        p = Path(p)
        return p if p.is_absolute() else self.ROOT / p

    def ensure_dirs(self):
        for d in (self.DATA, self.CLIPS, self.DELIVER, self.VIDEOS, self.LOGS, self.ASR):
            d.mkdir(parents=True, exist_ok=True)


C = Config()


def py_mod(module, *args):
    """Command line to run another liveclip module in a subprocess (the pipeline steps call each other this way)."""
    return [sys.executable, "-m", f"liveclip.{module}", *map(str, args)]
