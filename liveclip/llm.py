"""One way to ask Claude for JSON: the local Claude Code CLI (`claude -p`, uses your Claude login) or the Anthropic API.

- backend auto: ANTHROPIC_API_KEY set → Anthropic Python SDK; otherwise `claude -p`.
- `claude -p` runs in an empty temp dir (so it can't read project files / CLAUDE.md) and, with llm.lean, without
  tools, MCP servers and the default system prompt — a plain prompt-in / JSON-out call costs ~1/20 of the default.
- Every call is written to the cost ledger (liveclip.cost).
- Tests / dry runs: set LIVECLIP_LLM_FIXTURES=<dir>; the answer is read from <dir>/<step>.json (pick, review_subs,
  review_captions, hook, tight, themes) instead of calling
  any model (a list in that file is consumed one item per call, across processes). Nothing is billed.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from .config import C

LEAN_SYSTEM = "你是一个严谨的中文内容编辑助手。严格按用户消息里的要求完成任务，只按指定的 JSON 格式输出。"


def lean_args():
    if os.environ.get("CLAUDE_LEAN") == "0" or not C.get("llm.lean", True):
        return []
    return ["--tools", "", "--strict-mcp-config", "--disable-slash-commands", "--system-prompt", LEAN_SYSTEM]


class LLMError(RuntimeError):
    pass


_fixture_lock = threading.Lock()


# cost-ledger tool name → fixture file name
FIXTURE_KEYS = {"选段": "pick", "字幕校对": "review_subs", "单条校对": "review_captions", "挑预告": "hook",
                "精剪": "tight", "观点归类": "themes"}


def _fixture(tool):
    d = os.environ.get("LIVECLIP_LLM_FIXTURES")
    if not d:
        return None
    f = Path(d) / f"{FIXTURE_KEYS.get(tool, tool)}.json"
    if not f.exists():
        raise LLMError(f"LIVECLIP_LLM_FIXTURES is set but {f} does not exist (no model is called in fixture mode)")
    data = json.loads(f.read_text(encoding="utf-8"))
    if isinstance(data, list):
        # consumed in order across processes (each pipeline step is its own process): position kept in .<name>.pos
        import fcntl
        pos = f.with_name(f".{f.stem}.pos")
        with _fixture_lock, open(pos, "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            fh.seek(0)
            k = int(fh.read().strip() or 0)
            fh.seek(0)
            fh.truncate()
            fh.write(str(k + 1))
        return data[min(k, len(data) - 1)]
    return data


def _extract_json(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise LLMError(f"model did not return JSON: {(text or '')[:300]}")
    return json.loads(m.group(0))


def _record(tool, label, model, usd=None, tokens=None, sec=None):
    try:
        from . import cost
        cost.record("claude", tool, label=label or cost.label_of(*sys.argv[1:]), usd=usd, model=model, tokens=tokens, sec=sec)
    except Exception:  # noqa: BLE001 - bookkeeping must never break the pipeline
        pass


def backend():
    b = C.get("llm.backend", "auto")
    if b == "auto":
        return "api" if os.environ.get("ANTHROPIC_API_KEY") else "cli"
    return b


def ask_json(prompt, schema, *, model=None, tool="claude", label=None, timeout=900, meta=None):
    """Send one prompt, get one JSON object back (validated against `schema` by the backend where supported)."""
    model = model or C.model("pick")
    fx = _fixture(tool)
    if fx is not None:
        if meta is not None:
            meta.update(cost_usd=0.0, ms=0)
        return fx
    if backend() == "api":
        return _ask_api(prompt, schema, model, tool, label, meta)
    return _ask_cli(prompt, schema, model, tool, label, timeout, meta)


def _ask_cli(prompt, schema, model, tool, label, timeout, meta):
    with tempfile.TemporaryDirectory() as empty:
        try:
            out = subprocess.run(
                ["claude", "-p", "--model", model, "--output-format", "json", *lean_args(),
                 "--json-schema", json.dumps(schema, ensure_ascii=False)],
                input=prompt, capture_output=True, text=True, timeout=timeout, cwd=empty)
        except FileNotFoundError:
            raise LLMError("`claude` CLI not found. Install Claude Code and log in, or set ANTHROPIC_API_KEY") from None
    if out.returncode != 0:
        raise LLMError(f"claude CLI failed:\n{out.stderr[-2000:]}")
    res = json.loads(out.stdout)
    u = res.get("usage") or {}
    tokens = {k: u.get(k) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
              if u.get(k) is not None} or None
    _record(tool, label, model, res.get("total_cost_usd"), tokens, round((res.get("duration_ms") or 0) / 1000, 1))
    if meta is not None:
        meta.update(cost_usd=res.get("total_cost_usd"), ms=res.get("duration_ms"))
    if res.get("is_error"):
        raise LLMError(f"claude CLI returned an error: {str(res.get('result'))[:500]}")
    if res.get("structured_output"):
        return res["structured_output"]
    return _extract_json(res.get("result", ""))


def _ask_api(prompt, schema, model, tool, label, meta):
    try:
        import anthropic
    except ImportError:
        raise LLMError("ANTHROPIC_API_KEY is set but the `anthropic` package is missing: pip install 'liveclip[api]'") from None
    import time
    client = anthropic.Anthropic()
    kw = dict(model=model, max_tokens=64000, messages=[{"role": "user", "content": prompt}],
              output_config={"effort": C.get("llm.effort", "high"),
                             "format": {"type": "json_schema", "schema": schema}})
    t0 = time.time()
    msg = None
    if C.get("llm.api_fallbacks", True):
        try:
            with client.beta.messages.stream(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kw) as s:
                msg = s.get_final_message()
        except anthropic.BadRequestError:
            msg = None          # model / account without fallbacks: retry plainly
    if msg is None:
        with client.messages.stream(**kw) as s:
            msg = s.get_final_message()
    if msg.stop_reason == "refusal":
        raise LLMError(f"model refused: {getattr(msg, 'stop_details', None)}")
    u = msg.usage
    tokens = {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens}
    _record(tool, label, model, None, tokens, round(time.time() - t0, 1))
    if meta is not None:
        meta.update(cost_usd=None, ms=int((time.time() - t0) * 1000))
    text = "".join(b.text for b in msg.content if b.type == "text")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return _extract_json(text)
