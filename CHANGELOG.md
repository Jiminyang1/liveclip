# Changelog

## 0.1.0 — 2026-10-16

First public version, extracted from a private production pipeline (fresh history).

- `liveclip clip`: one-shot pipeline for a video file — ASR (Bailian / SenseVoice / your own transcript), whole-transcript
  proofreading, chunked LLM clip picking, hook pass, 9:16 cutting with per-clip layout detection, covers (3:4 / 4:3 / 16:9),
  captions per platform, full-decode verification, QA with automatic full-frame re-cut, optional tight edit, preview page.
- `liveclip follow`: live-follow clipping from 5-minute segments on an rclone remote (rounds every 20 minutes of
  transcript, deferred tail, full version + SRT at the end, optional archive).
- `liveclip dashboard`: local dashboard with status, picking decisions, clip wall, uploads, publish queue, costs.
- Optional publishing (disabled by default): Bilibili via biliup, Douyin via Playwright with account verification and pacing.
- `liveclip montage`, `points`, `perf`, `cost`, `doctor`, `init`.
- Everything streamer-specific is configuration (`config.yaml`); API keys only from the environment.
- macOS-only pieces are optional: notifications, keep-awake and Vision face detection fall back gracefully on Linux.
