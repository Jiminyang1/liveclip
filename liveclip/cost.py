"""Cost ledger: data/cost_log.jsonl, one line per paid call. The dashboard "costs" panel reads it.

Two currencies, never mixed:
  - Claude (usd): `claude -p` reports total_cost_usd. On a Claude subscription this is an API-price *equivalent*
    (useful to compare steps / check optimisations), not what you are billed. API-key calls record tokens only.
  - Bailian ASR (cny): billed per token; unit prices come from config asr.prices_cny_per_mtok.

  from liveclip.cost import record, label_of
  record("claude", "字幕校对", label="LIVE_20260101_2000_main", usd=0.07, model=..., tokens={...})

  liveclip cost --summary     print totals by step
  liveclip cost --backfill    rebuild entries from cached review results / raw ASR results (once)
"""
from __future__ import annotations

import argparse
import fcntl
import json
import re
import sys
from datetime import datetime

from .config import C

LEDGER = C.DATA / "cost_log.jsonl"
# Session / upload IDs used across the pipeline: LIVE_<YYYYMMDD>_<HHMM>_<key>, UP_<YYYYMMDD_HHMM>_<n>
LABEL_RE = re.compile(r"(UP_\d{8}_\d{4}_\d+|LIVE_\d{8}_\d{4}_[A-Za-z0-9]+|LIVE_[A-Za-z0-9_]+)")


def label_of(*texts):
    """Which session / video a command line or path belongs to."""
    for t in texts:
        m = LABEL_RE.search(str(t or ""))
        if m:
            return m.group(1)
    m = re.search(r"_(\d{4})-(\d\d)-(\d\d)_(\d\d)-(\d\d)-\d\d", " ".join(str(t) for t in texts))   # recorder segment names
    if m:
        return f"LIVE_{m[1]}{m[2]}{m[3]}_{m[4]}{m[5]}"
    return None


def bailian_cny(model, tokens):
    pin, pout = (C.get("asr.prices_cny_per_mtok") or {}).get(model, (0, 0))
    return round(((tokens or {}).get("input_tokens", 0) * pin + (tokens or {}).get("output_tokens", 0) * pout) / 1e6, 4)


def record(kind, tool, label=None, usd=None, cny=None, model=None, tokens=None, sec=None, t=None, src=None):
    """Append one entry. Never raises (bookkeeping must not break real work)."""
    try:
        row = {"t": t or datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, "tool": tool, "label": label,
               "usd": round(usd, 4) if usd is not None else None, "cny": cny, "model": model,
               "tokens": tokens, "sec": sec, "src": src}
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with open(LEDGER, "a", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(json.dumps({k: v for k, v in row.items() if v is not None}, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        print(f"cost ledger write failed (ignored): {e}", file=sys.stderr)


def claude_tokens(res):
    u = (res or {}).get("usage") or {}
    return {k: u.get(k) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
            if u.get(k) is not None} or None


def load():
    if not LEDGER.exists():
        return []
    rows = []
    for ln in LEDGER.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(ln))
        except json.JSONDecodeError:
            pass
    return rows


def backfill():
    if any(r.get("src") == "backfill" for r in load()):
        print("already backfilled")
        return
    n = 0
    for f in sorted((C.ASR / "review_cache").glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        if d.get("cost_usd"):
            record("claude", "字幕校对", usd=d["cost_usd"], model=d.get("model"),
                   t=(d.get("at") or "") + ":00" if d.get("at") else None, src="backfill")
            n += 1
    for f in list(C.DATA.rglob("part*.json")) + list(C.RECORDINGS.rglob("part*.json")):
        if "review_cache" in str(f):
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        u = d.get("usage") if isinstance(d, dict) else None
        if not (u and u.get("input_tokens")):
            continue
        meta = f.parent / "meta.json"
        model = json.loads(meta.read_text()).get("model") if meta.exists() else C.get("asr.bailian_model")
        tok = {"input_tokens": u.get("input_tokens"), "output_tokens": u.get("output_tokens")}
        sec = round((d.get("properties") or {}).get("original_duration_in_milliseconds", 0) / 1000) or None
        record("bailian", "语音识别", label=label_of(f), cny=bailian_cny(model, tok), model=model, tokens=tok, sec=sec,
               t=datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"), src="backfill")
        n += 1
    print(f"backfilled {n} entries")


def summary(rows=None, days=None):
    rows = load() if rows is None else rows
    if days:
        cut = datetime.now().timestamp() - days * 86400
        rows = [r for r in rows if datetime.strptime(r["t"][:19], "%Y-%m-%d %H:%M:%S").timestamp() >= cut]
    by = {}
    for r in rows:
        a = by.setdefault((r["kind"], r["tool"]), {"n": 0, "usd": 0.0, "cny": 0.0})
        a["n"] += 1
        a["usd"] += r.get("usd") or 0
        a["cny"] += r.get("cny") or 0
    return by


def main(argv=None):
    ap = argparse.ArgumentParser(prog="liveclip cost", description="cost ledger")
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--days", type=int)
    a = ap.parse_args(argv)
    if a.backfill:
        backfill()
    if a.summary or not a.backfill:
        for (kind, tool), v in sorted(summary(days=a.days).items(), key=lambda kv: -(kv[1]["usd"] + kv[1]["cny"] / 7)):
            amt = f"${v['usd']:.2f} (API-price equivalent)" if kind == "claude" else f"¥{v['cny']:.3f}"
            print(f"{tool:8s} {v['n']:4d} calls  {amt}")


if __name__ == "__main__":
    main()
