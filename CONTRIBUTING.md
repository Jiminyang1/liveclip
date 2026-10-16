# Contributing

Thanks for helping! A few ground rules:

- **No real people's data in the repo.** Tests and examples use synthetic media (`tests/sample.py`). Never commit
  transcripts, clips, screenshots, cookies or account IDs from real streams.
- **Keep the hard-won fixes.** Many odd-looking lines (audio timestamp rewrite, fps normalisation, padding clamps,
  negation guards) exist because something broke in production — see `docs/lessons.md` before "simplifying".
- **Steps stay independent.** Each pipeline step is a module with `main(argv)` that reads/writes JSON in the workspace
  and is called as `python -m liveclip.<module>`. New behaviour that is specific to one streamer belongs in config.
- **LLM calls** go through `liveclip.llm.ask_json` (cost ledger, CLI/API switch, test fixtures). Never call a paid
  API from tests; add a fixture instead (`LIVECLIP_LLM_FIXTURES` for Claude, `LIVECLIP_ASR_FIXTURES` for ASR —
  see `tests/sample.py` and `tests/test_e2e.py`).

Development:

```bash
uv venv && uv pip install -e '.[dev]'
.venv/bin/ruff check liveclip tests
.venv/bin/pytest -q            # unit tests + an offline end-to-end run (needs ffmpeg, ~1 min)
```

Commit messages in English; one logical change per PR; update README/CHANGELOG when behaviour or config changes.
