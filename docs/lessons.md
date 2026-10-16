# Lessons baked into the code

Things that went wrong in production and how the pipeline now prevents them. Each item points at the code that
implements the fix, so you know what not to "simplify" away.

## Audio / video

- **Audio timestamps after concat.** Hook + main part concatenated and then loudness-normalised/resampled leaves a run
  of 1-sample AAC packets in the MP4. ffmpeg and QuickTime play it fine; editors and short-video platforms that place
  audio by timestamp shift it by up to ~2 s. Fix: `asetpts=N/SR/TB` right before encoding (`cut._render`).
  QA checks that every AAC packet is 1024 samples apart (`qa.audio_ok`). "Plays fine locally" proves nothing.
- **Variable-frame-rate recordings.** Live recordings are VFR (15–27 fps). `zoompan` emits one frame per input frame
  and labels it 30 fps, so the picture runs ahead of the audio. Fix: `fps=30` before anything else (`cut._render`).
  Tight-edit cut points are snapped to the 1/30 s grid (`cut.snap_segs`) so audio and video segments have equal length.
- **Gaps at segment seams.** Each recorded TS segment starts audio ~0.1 s before video; lossless concat leaves
  ~100 ms holes that the timestamp fix then squeezes out, so clips crossing a seam drift. Fix: re-encode audio with
  `aresample=async=1:min_hard_comp=0.01` when concatenating segments (`follow.build_video`).
- **Leading undecodable audio.** Some replays start with audio that cannot be decoded; decoding it "continuously"
  shifts every subtitle earlier. Fix: `aresample=async=1:first_pts=0` when extracting audio for ASR (`asr.load_audio`).
- **Encoding for platforms.** Export like the mobile editors do: 1080p30, 1-second GOP, ~5.5 Mbps (Bilibili and Douyin
  re-compress above ~6 Mbps average), no source metadata (`cut.ENC`). A slow zoom-in on the first seconds avoids
  stutter when the platform serves a low-bitrate start.
- **A/V sync QA.** Comparing only audio offsets flags every clip whose start moved a few frames. Measure audio offset
  (cross-correlation) *and* video offset (native frames, median over many frame changes) and compare the two
  (`qa.av_offset`, `qa.video_offset`). Single-frame matching on low-fps co-stream footage gave ±150 ms false alarms.

## Cutting

- **Don't swallow neighbouring words.** Fixed padding (−0.3 / +0.6 s) pulls the next sentence's first syllable and
  subtitle into the clip. Padding is clamped to the previous/next subtitle boundary, and an end that lands mid-sentence
  is extended to the sentence end (or pulled back) (`cut.bounds`).
- **Jump cuts in talking-head videos** are detected with scene scores relative to the window median, and clip edges
  snap to them (`cut.find_cut`); livestreams have no edits, so snapping is disabled for them.
- **Hooks**: hard limit 12 s, must start after the clip start, must be a full sentence; the separate hook pass picks
  from 3 scored candidates per clip (`hook.py`). Opening with 所以/但是 is fine — only "does it grab you" matters.
- **Framing**: single-person frames always use the full frame — zoomed crops cut chins whenever the streamer leans in.
  Split-screen co-streams are detected per clip (`layout.split_band`), and a face pre-check falls back to full frame
  before cutting (`layout.face_cut_frames`). Co-stream guests leave mid-session, so every clip is checked separately.
- **Subtitles**: wrap at punctuation, after 的/了/吗…, or before pronouns/conjunctions; never leave a 1–2 character
  orphan line; don't split digit runs (`cut.wrap`). Pure filler lines (嗯、对吧) are not burned in.

## Transcripts and LLM steps

- **Proofread the whole transcript once** before picking: titles, burned subtitles and the full-length SRT all use the
  reviewed text. The model returns only changed/uncertain lines; changes that drop a negation (不/没/别/未/无/非) or that
  rewrite more than half of a line are rejected (`review/subs.guard`). Chunks are cached by prompt hash.
- **Homophones of hot words** (a hot word and a common word with the same pinyin) can't be told apart by sound; the
  reviewer must decide from context or mark the line `check`. Put such pairs in `review.extra_rules`.
- **Don't show the reviewer the clip title** — it was produced from the same (wrong) transcript and the model will
  "correct" towards it.
- **Run `claude -p` in an empty directory, lean.** Otherwise it reads project files (and may see answers), and the
  default Claude Code system prompt + tools cost ~35k tokens per call. Lean flags cut a trivial call ~20×
  (`llm.lean_args`).
- **Live picking windows**: pick every 20 minutes of transcript from `[last − 5 min, now]`, but only accept clips that
  end before `now − 5 min`; clips in the tail are deferred and re-compete next round (the model does not reliably
  re-select them). Overlapping clips are skipped unless the new one is a ≥1.5× longer "full version".
- **Diarization**: one task for the whole recording keeps speaker IDs consistent. When it has to be split, match
  speakers across parts with speaker embeddings; identify the streamer as whoever talks during single-person frames.

## Covers

- Douyin wants a 3:4 and a 4:3 cover; Bilibili crops the cover to both 4:3 and 16:9 — keep the title inside the middle
  ~72 % (`cover/v2.design`). Pick frames with open eyes and without live-room stickers.

## Publishing

- Douyin's web form accepts **5 topics** at most; a 6th garbles the text box.
- Posting many clips the same day starved later ones of recommendation traffic; pace posts
  (`publish.douyin.per_day`, `gap_min`).
- The publish log is the source of truth: check before posting, record right after the confirmed redirect, and never
  auto-retry an item whose result is unknown — ask a human to check the platform first.
- Stop at any captcha / SMS / QR prompt. Never let automation fill them.
- End cards must not mention other platforms (counted as off-site promotion and throttled).
