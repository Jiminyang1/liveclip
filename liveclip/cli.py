"""`liveclip` command line: one entry point, one subcommand per pipeline step.

Every step is also runnable as `python -m liveclip.<module>`; the pipeline itself calls steps that way.
"""
from __future__ import annotations

import importlib
import os
import shutil
import sys
from pathlib import Path

from . import __version__

# subcommand → (module, one-line help)
COMMANDS = {
    "clip": ("auto", "video file → candidate clips package (ASR → review → pick → hook → cut → covers → QA → captions)"),
    "follow": ("follow", "follow a live recording on an rclone remote and clip it while it is still live"),
    "dashboard": ("dashboard.server", "local web dashboard (status, decisions, clip wall, uploads, publish queue, costs)"),
    "montage": ("montage", "catch-phrase montage across sessions (--suggest / --phrase)"),
    "asr": ("asr", "speech-to-text → subtitle JSON (Bailian / SenseVoice, optional diarization)"),
    "review-subs": ("review.subs", "proofread a whole transcript with Claude (homophones, names)"),
    "review-captions": ("review.captions", "proofread the burned-in captions of single clips"),
    "pick": ("pick", "pick clips from a transcript with Claude"),
    "hook": ("hook", "re-pick the opening teaser of each clip"),
    "cut": ("cut", "render 9:16 clips from a plan"),
    "tight": ("tight", "tight edit: remove fillers, stutters, restarts, guest back-channel"),
    "qa": ("qa", "automatic QA of rendered clips (faces, sync, timestamps, mid-sentence cuts)"),
    "verify": ("verify", "full-decode check + contact sheets"),
    "covers": ("cover.make", "covers for rendered clips (3:4, 4:3, 16:9)"),
    "cover-v2": ("cover.v2", "v2 covers for a plan"),
    "album": ("album", "offline preview page for a delivery folder"),
    "align": ("align", "align external subtitles to the video timeline"),
    "archive": ("archive", "upload a finished session's full version with rclone"),
    "after-record": ("after_record", "hook for a recorder on the same machine: recording → clip"),
    "sim-live": ("sim_live", "simulate a live session on an rclone remote (for testing follow)"),
    "worker": (None, "background workers: `liveclip worker clip|publish`"),
    "publish": ("publish.common", "publishing helpers: info / check / mark"),
    "bili": ("publish.bili", "Bilibili upload via biliup (disabled by default)"),
    "douyin": ("publish.douyin", "Douyin upload via Playwright: login | check | upload (disabled by default)"),
    "perf": ("publish.perf", "pull performance stats of published clips"),
    "points": ("points", "rebuild the viewpoint library data/points.json"),
    "cost": ("cost", "cost ledger summary"),
    "doctor": (None, "check dependencies, keys and configuration"),
    "init": (None, "create config.yaml / .env / workspace folders in the current directory"),
}


def usage():
    w = max(map(len, COMMANDS))
    lines = [f"liveclip {__version__} — live-follow auto clipping for Chinese streamers", "",
             "usage: liveclip <command> [args...]   (liveclip <command> --help for details)", "", "commands:"]
    lines += [f"  {k:<{w}}  {v[1]}" for k, v in COMMANDS.items()]
    return "\n".join(lines)


def _absolutize(args):
    """Paths given relative to the current directory stay valid after we chdir into the workspace."""
    out = []
    for a in args:
        if a and not a.startswith("-") and os.path.exists(a) and not os.path.isabs(a):
            out.append(os.path.abspath(a))
        else:
            out.append(a)
    return out


def doctor():
    from .config import C
    ok = True

    def row(name, good, detail="", required=True):
        nonlocal ok
        mark = "✓" if good else ("✗" if required else "–")
        if required and not good:
            ok = False
        print(f"  {mark} {name}{(': ' + detail) if detail else ''}")

    print(f"liveclip {__version__}  python {sys.version.split()[0]}")
    print(f"config: {C.file or '(none — using defaults; run `liveclip init`)'}")
    print(f"workspace: {C.ROOT}")
    print("required:")
    row("python ≥ 3.11", sys.version_info >= (3, 11))
    row("ffmpeg", bool(shutil.which("ffmpeg")))
    row("ffprobe", bool(shutil.which("ffprobe")))
    try:
        from .fonts import resolve
        row("Chinese font", True, resolve("title")[0])
    except SystemExit as e:
        row("Chinese font", False, str(e))
    llm_ok = bool(os.environ.get("ANTHROPIC_API_KEY")) or bool(shutil.which("claude"))
    row("Claude (ANTHROPIC_API_KEY or `claude` CLI)", llm_ok,
        "API key" if os.environ.get("ANTHROPIC_API_KEY") else ("claude CLI" if shutil.which("claude") else "missing"))
    dash = bool(os.environ.get("DASHSCOPE_API_KEY")) or (Path.home() / ".bailian/config.json").exists()
    row("DashScope / Bailian key (ASR)", dash, "" if dash else "set DASHSCOPE_API_KEY, or use --asr-json / SenseVoice")
    print("optional:")
    try:
        import funasr  # noqa: F401
        row("FunASR (SenseVoice, VAD, speaker embeddings)", True, required=False)
    except ImportError:
        row("FunASR (SenseVoice, VAD, speaker embeddings)", False, "pip install 'liveclip[asr-local]'", required=False)
    from . import vision
    row("face detection", vision.available(), vision.backend() or "none (covers use the middle frame; QA skips face checks)",
        required=False)
    row("rclone (live follow / archive)", bool(shutil.which("rclone")), required=False)
    try:
        import jieba  # noqa: F401
        row("jieba (montage --suggest)", True, required=False)
    except ImportError:
        row("jieba (montage --suggest)", False, "pip install 'liveclip[montage]'", required=False)
    pub = C.publish_enabled()
    print(f"publishing: {'ENABLED' if pub else 'disabled'}"
          + (f" (douyin {'on' if C.publish_enabled('douyin') else 'off'}, bili {'on' if C.publish_enabled('bili') else 'off'})" if pub else ""))
    if pub:
        try:
            import playwright  # noqa: F401
            row("playwright (douyin)", True, required=False)
        except ImportError:
            row("playwright (douyin)", False, "pip install 'liveclip[publish]'", required=False)
        row("biliup (bili)", bool(shutil.which(C.get("publish.bili.biliup") or "biliup")), required=False)
        if C.publish_enabled("douyin") and not C.get("publish.douyin.account_id"):
            row("publish.douyin.account_id", False, "set it — uploads refuse to run without an account to verify")
    return 0 if ok else 1


def init():
    here = Path.cwd()
    assets = Path(__file__).resolve().parent / "assets"
    for src, dst in (("config.example.yaml", "config.yaml"), ("env.example", ".env")):
        d = here / dst
        if d.exists():
            print(f"exists, kept: {d}")
        else:
            shutil.copy(assets / src, d)
            print(f"created {d}")
    for sub in ("data", "clips", "deliver", "videos", "recordings", "logs", "assets/fonts"):
        (here / sub).mkdir(parents=True, exist_ok=True)
    print("next: edit config.yaml (streamer persona, tags) and .env (keys), then `liveclip doctor`")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(usage())
        return 0
    if argv[0] in ("-V", "--version"):
        print(__version__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd not in COMMANDS:
        print(f"unknown command: {cmd}\n\n{usage()}", file=sys.stderr)
        return 2
    if cmd == "init":
        return init()
    if cmd == "doctor":
        return doctor()
    module = COMMANDS[cmd][0]
    if cmd == "worker":
        if not rest or rest[0] not in ("clip", "publish"):
            print("usage: liveclip worker clip|publish [--once]", file=sys.stderr)
            return 2
        module, rest = f"worker.{rest[0]}", rest[1:]
    from .config import C
    rest = _absolutize(rest)
    if Path.cwd().resolve() != C.ROOT:
        os.chdir(C.ROOT)
    mod = importlib.import_module(f"liveclip.{module}")
    sys.argv = [f"liveclip {cmd}", *rest]
    ret = mod.main(rest)
    return ret if isinstance(ret, int) else 0


def entry():
    sys.exit(main())
