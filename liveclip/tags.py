"""Hashtags / tags per platform, driven by config `tags:`.

Order (Douyin / Shipinhao / Xiaohongshu): must-have tags → event tags (people / events / other theories, 0–2)
→ conditional tag (e.g. a niche tag only when the clip is about that niche) → content tags → soft tags, cut to the
platform limit. Bilibili: bili_must → events → content.

Lessons baked in:
- Douyin accepts at most 5 topics on the web upload page (a 6th can't be selected and garbles the text box).
- Generic words (直播、观点、金句、干货…) describe the format, not the content — they are dropped.
- Content tags come from the clip picker (`tags`), event tags from `event_tags`; old captions without groups are
  re-read from their existing hashtags.
"""
from __future__ import annotations

from .config import C

T = C.get("tags")
MUST = list(T.get("douyin_must") or [])
SOFT = list(T.get("soft") or [])
BILI_MUST = list(T.get("bili_must") or [])
COND = T.get("conditional") or {}
COND_TAG = COND.get("tag") or ""
COND_WHEN = set(COND.get("when_any") or [])
BRAND = {t.lower() for t in (T.get("brand") or [])} | {t.lower() for t in MUST + SOFT + BILI_MUST + ([COND_TAG] if COND_TAG else [])}
GENERIC = set(T.get("generic") or [])
LIMIT = {"douyin": 5, "xhs": 10, "bili": 10, "shipinhao": 6, **(T.get("limit") or {})}


def merge(*groups, limit=None):
    """Merge in order, de-duplicate case-insensitively, cut to `limit`."""
    seen, out = set(), []
    for g in groups:
        for t in g or []:
            t = str(t).strip().lstrip("#")
            if t and t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
    return out[:limit] if limit else out


def for_platform(platform, content, events=()):
    """content: content tags (may contain brand/generic words from old captions — they are filtered here)."""
    content = [t for t in merge(content) if t.lower() not in BRAND and t not in GENERIC]
    events = [t for t in merge(events) if t.lower() not in BRAND][:2]
    if platform == "bili":
        return merge(BILI_MUST, events, content, limit=LIMIT["bili"])
    cond = [COND_TAG] if COND_TAG and set(content) & COND_WHEN else []
    return merge(MUST, events, cond, content, SOFT, limit=LIMIT[platform])


def hashtags(ts):
    return " ".join(f"#{t}" for t in ts)
