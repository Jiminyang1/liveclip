# Migration inventory: private pipeline → LiveClip

LiveClip was extracted from a private, single-streamer production pipeline. This file lists every file of that
pipeline that was considered, what happened to it, and why. The new repository was started **without history**;
nothing from the private repo's data, media, logs or notes was copied.

Legend: **include** = copied, refactored for configuration/paths only (algorithms unchanged) ·
**rewrite** = same purpose, new implementation or heavily generalised · **exclude** = not in LiveClip.

## Pipeline code

| Original file | Decision | New location | Reason / what changed |
|---|---|---|---|
| `asr.py` | include | `liveclip/asr.py` | Bailian (DashScope REST) whole-file ASR, region fallback, raw-result cache, diarization + CAM++ matching, single-person-frame host detection. Hot words, model, speaker labels from config; API key from `DASHSCOPE_API_KEY` (falls back to the `bl` CLI config); FunASR imported lazily so it is an optional extra; device auto-detected instead of hard-coded `mps`. |
| `pick_clips.py` | include | `liveclip/pick.py` | Prompt kept in Chinese but every persona-specific part (name, persona, topics, event examples, hot viewpoints, tag rules) comes from `streamer:` / `tags:` config; streamer-specific example quotes replaced by neutral ones. Danmaku hints (`--danmaku`, depended on a private corpus + `eval_clips.py`) and the title lookup in a private CSV removed. |
| `cut_clips.py` | include | `liveclip/cut.py` | Layouts, bounds snapping, hook concat, encoding settings, audio-timestamp fix unchanged. Fonts via `liveclip.fonts` (no hard-coded macOS font); end-card text from `branding:`; paths from config; optional `data/manifest.json` replaces the private work list. |
| `make_covers.py` | include | `liveclip/cover/make.py` | Older cover styles kept; face/eye detection through `liveclip.vision`; series/name examples neutralised. |
| `tools/cover_v2.py` | include | `liveclip/cover/v2.py` | Same design; font + vision abstractions. |
| `tools/auto_clip.py` | include | `liveclip/auto.py` + `liveclip/layout.py` + `liveclip/caption.py` | Orchestrator unchanged in logic; steps called as `python -m liveclip.<step>`; new `--asr-json` (skip ASR); stops cleanly when no candidate survives. Layout detection and caption text moved to their own modules. |
| `tools/align_subs.py` | include | `liveclip/align.py` | Unchanged apart from CLI. |
| `tools/review_subs.py` | include | `liveclip/review/subs.py` | Name-unification rule is now config-driven (`streamer.name_variants` → `canonical_name`); homophone rules moved to `review.extra_rules`. |
| `tools/review_captions.py` | include | `liveclip/review/captions.py` | Same, shares the rules with `review/subs.py`. |
| `tools/hook_pass.py` | include | `liveclip/hook.py` | Uses the common LLM wrapper; example hooks neutralised. |
| `tools/tight_edit.py` | include | `liveclip/tight.py` | Speaker labels / homophone rules from config; LLM wrapper. |
| `tools/qa_clips.py` | include | `liveclip/qa.py` | Face checks run only when a face detector is available (otherwise reported as skipped). |
| `tools/verify_clips.py` | include | `liveclip/verify.py` | Wrapped in `main()`. |
| `tools/make_album_html.py` | include | `liveclip/album.py` | Wrapped in `main()`. |
| `tools/tags.py` | rewrite | `liveclip/tags.py` | Same algorithm (must → events → conditional → content → soft, platform limits); all tag lists come from `tags:` config. |
| `tools/live_follow.py` | include | `liveclip/follow.py` | Remote, anchors, title template, windows, layout note from config; notifications/keep-awake/open via `liveclip.platform`; archive upload optional. |
| `tools/upload_full.py` | include | `liveclip/archive.py` | Remote from `live.archive_remote`. |
| `tools/after_record.py`, `tools/on_recorded.sh` | include | `liveclip/after_record.py`, `deploy/local/on_recorded.sh` | Anchor mapping / title / note from config; script uses `LIVECLIP_HOME` instead of an absolute path. |
| `tools/sim_live.py` | include | `liveclip/sim_live.py` | Genuinely useful to test live-follow; remote is now a required argument. |
| `tools/montage.py` | include | `liveclip/montage.py` | Aliases / exclusions / jieba words from `montage:` config; jieba optional. |
| `tools/points.py` | include | `liveclip/points.py` | Prompt neutralised; model from `models.themes`. |
| `tools/costlog.py` | include | `liveclip/cost.py` | Prices from config; generic session-ID regex. |
| `tools/clip_worker.py` | include | `liveclip/worker/clip.py` | Trash / notify via `liveclip.platform`; process detection via `ps -eo`. |
| `tools/publish_worker.py` | rewrite (partial) | `liveclip/worker/publish.py` | Kept: queue, locking, log-as-truth reconciliation, Douyin pacing, Bilibili back-off. **Removed**: the "Claude in Chrome" Douyin path (depended on a private agent skill and a specific browser profile). Refuses to run unless `publish.enabled`. Pacing limits from config. |
| `tools/publish_serve.py` | rewrite | `liveclip/publish/common.py` | Kept caption parsing, publish log, info/check/mark. Dropped the local HTTP file server (only needed by the removed browser-extension flow). |
| `tools/bili_upload.py` | include | `liveclip/publish/bili.py` | biliup from PATH (`[publish]` extra) instead of a private venv; category/copyright/source from config; `liveclip bili login`. Disabled by default. |
| `tools/douyin_pw.py` | include | `liveclip/publish/douyin.py` | The hard-coded account ID became `publish.douyin.account_id` (required; uploads refuse without it). Disabled by default. Time zone from config. |
| `tools/fetch_perf.py` | include | `liveclip/publish/perf.py` | Private plan-name hints moved to an optional `data/perf/plan_hints.json`; instructions no longer assume a browser extension (paste JS in DevTools). |
| `tools/dashboard/server.py`, `index.html` | include | `liveclip/dashboard/` | Recorder host / logs / services from `recorder:` config (optional); time zones from config; publish buttons disabled unless publishing is enabled; process detection matches `liveclip` commands. Private mock strings replaced. |
| `tools/eyes_open.swift` | include | `liveclip/assets/eyes_open.swift` | Compiled on demand by `liveclip.vision` (macOS only); OpenCV fallback added for Linux. |
| `tools/bin/eyes_open` | exclude | — | Compiled binary; rebuilt from source on first use. |
| `tools/vps/uploader.py` | include | `deploy/vps/uploader.py` | Base dir and remote via environment variables. |
| `tools/vps/*.service`, `*.timer` | rewrite | `deploy/vps/liveclip-*.{service,timer}` | Renamed, neutral paths. |
| `tools/vps/config.ini`, `tools/recorder_config/config.ini` | exclude | `deploy/README.md` | Contain platform cookies/tokens and machine paths. The handful of settings LiveClip relies on are documented instead. |
| `tools/vps/URL_config.ini`, `tools/recorder_config/URL_config.ini` | rewrite | `deploy/vps/URL_config.example.ini` | Real live-room URLs removed; placeholder example. |
| `tools/rclone_use_own_client.py` | exclude | `deploy/README.md` | Account-specific; the general advice (use your own Google OAuth client ID with rclone) is in the deploy notes. |
| `tools/watch_inbox.py` | exclude | — | Superseded by the dashboard upload + clip worker; was already switched off. |
| `tools/make_daily.py` | exclude | — | Hard-coded list of one creator's clips and captions. |
| `tools/viral_fetch.py`, `tools/viral_douyin.py` | exclude | — | One-off research scrapers for a private viral-clip corpus. |
| `tools/f2_probe.py`, `tools/douyin_batch.py` | exclude | — | One-off material collection with the F2 downloader (Apache-2.0); not part of the clipping pipeline. |
| `tools/douyin_accounts.json` | exclude | — | Private account list. |
| `tools/vendor/DouyinLiveRecorder` | exclude → dependency | `deploy/README.md` | Third-party (MIT, ihmily/DouyinLiveRecorder). Install it on the recording server from upstream. |
| `tools/vendor/f2` | exclude | — | Third-party (Apache-2.0); only used by the excluded collection scripts. |
| `tools/vendor/douyin_api` | exclude | — | Third-party (Apache-2.0); research only. |
| biliup (was a separate venv) | dependency | `[publish]` extra | MIT (biliup/biliup). |
| `collect.py`, `harvest_materials.py`, `build_material_catalog.py`, `eval_clips.py`, `add_speech.py`, `make_report.py`, `make_gallery.py` | exclude | — | One-off research / material collection / evaluation scripts tied to a private corpus. |
| `qrcode.png`, `*.log`, `clips_pick.log`, `run1.log`, `run2.log` | exclude | — | Personal artefacts / logs. |

## Non-code

| Original | Decision | Reason |
|---|---|---|
| `data/` (subtitles, transcripts, ASR benchmarks, plans' data, publish log/queue, perf snapshots, cost ledger, points, themes, viral corpus, browser profiles, biliup cookies, work lists) | exclude | Personal data, credentials, third-party content. |
| `clips/`, `deliver/`, `videos/`, `recordings/`, `logs/`, `inbox/`, `archive/` | exclude | Media and outputs derived from a real person's streams. |
| `docs/Agent工作交接.md`, `docs/*.md`, `docs/research/*`, `docs/archive/*` | exclude → distilled | Private notes about one creator and accounts. General, non-personal engineering lessons are distilled into `docs/lessons.md`. |
| `docs/screenshots/*` | exclude | They show the real streamer. README has placeholders + a TODO to capture screenshots from the synthetic demo. |
| `docs/发布问题记录.md` | exclude → distilled | Publishing pitfalls are summarised in `docs/lessons.md`; the dashboard can still display an optional `data/publish_issues.md`. |
| `AGENTS.md`, `CLAUDE.md`, `README.md` | rewrite | New README.md / README.zh-CN.md / CONTRIBUTING.md. |
| `.claude/skills/*`, `.claude/launch.json`, `.claude/settings.local.json` | exclude | Private agent skills and local settings. |
| `.gitignore` | rewrite | Generic ignores (workspace data, media, credentials, browser profile). |
| `assets/fonts/NotoSansSC.ttf` (+ OFL) | exclude (documented) | 17 MB; SIL OFL allows redistribution, but the repo stays small: README explains where to download it; system CJK fonts are found automatically. |
| `.venv*` | exclude | Environments. |

## What became configuration

| Hard-coded before | Config key |
|---|---|
| Streamer name, persona, topics, audience, event examples, hot viewpoints, live layout note | `streamer.*` |
| Speaker labels in diarized subtitles | `streamer.speaker_label`, `streamer.guest_label` |
| ASR spellings of the streamer's name to unify | `streamer.name_variants`, `streamer.canonical_name` |
| ASR hot words, model, prices | `asr.*` |
| Homophone rules in proofreading prompts | `review.extra_rules` |
| End-card text, Bilibili repost source | `branding.*` |
| Must-have / soft / conditional / generic tags, platform limits | `tags.*` |
| Claude model per step, CLI vs API, lean calls | `models.*`, `llm.*` |
| Recorder machine host, log paths, systemd units | `recorder.*` |
| rclone remotes (live segments, archive), anchors, title template, picking windows | `live.*` |
| Audience / recorder time zones (was Beijing / Los Angeles) | `timezones.*` |
| Douyin account to verify, posts per day / gap / next-day hour; Bilibili category, copyright | `publish.*` (all off by default) |
| Montage aliases / exclusions / jieba words | `montage.*` |
| Workspace and every data directory | `workspace`, `paths.*` |
| Fonts | `fonts.*` |
| API keys | environment only (`.env`) |
