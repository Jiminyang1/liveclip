"""Per-clip publishing copy (文案.txt): title + hashtags for Douyin, Xiaohongshu, Bilibili and WeChat Channels.

Copy is deliberately minimal — title + topics only (long descriptions did not help). Hashtags come from
liveclip.tags (must-have + event + content tags). The file format is parsed back by liveclip.publish.common, so keep
the 【平台】 / 「键：」 structure when editing.
"""
from __future__ import annotations

import re

from . import tags as T
from .config import C

CATEGORY_TAGS = {"自创比喻": ["金句"], "极端推演": ["认知"], "真人或数字": ["认知"], "亲身经历": ["成长"],
                 "干货清单": ["干货"], "引战点": ["观点"], "开场论点": ["观点"]}


def short_title(title):
    """视频号短标题：6–16 个字，不能用逗号。"""
    t = title.replace("，", " ").replace("：", " ").replace("？", "").replace("！", "")
    if len(t) > 16:
        t = t.replace(" ", "")
    return t[:16] if len(t) >= 6 else title[:16]


def caption_text(c, tags, source=None):
    """四平台文案。c：计划里的一段（title / tags / event_tags / risky / category / context_check / full_of）。"""
    source = C.get("branding.source") if source is None else source
    title = re.sub(r"([？！。，、])，", r"\1", c["title"].replace("\n", "，"))   # 第一行以问号等结尾时不再补逗号
    note = ""
    if c.get("risky"):
        note = f"⚠ 注意：争议等级：{'高' if c.get('category') == '引战点' else '中'}。{c.get('context_check', '')[:80]}\n\n"
    if c.get("full_of"):   # 跟播：和已剪的短版重叠的完整长版，两版都留
        note += f"ℹ 这是「{c['full_of']}」的完整版（长版）。两版挑一个发，或短版发抖音、长版发B站。\n\n"
    content = T.merge(tags, c.get("tags"), CATEGORY_TAGS.get(c.get("category"), []))
    events = c.get("event_tags") or []
    tg = lambda p: T.hashtags(T.for_platform(p, content, events))
    bili_decl = f"创作声明选「内容为转载」，转载来源填：{source}" if source else "创作声明按实际情况选择（自制 / 转载）"
    return note + "\n".join([
        "【抖音】直接传 视频.mp4。竖封面用 封面.jpg（3:4），横封面用 封面_横4比3.jpg（4:3）。", "标题：", title, "", "作品描述：", tg("douyin"), "",
        "【小红书】网页端上传 视频.mp4，封面用 封面.jpg。", "标题：", title.replace("，", " ")[:20], "", "正文：", tg("xhs"), "",
        f"【B站】直接传 视频.mp4。封面上传 封面_B站.jpg（16:9，4:3 裁切也能看到标题）。{bili_decl}", "标题：", title, "", "简介：", tg("douyin"), "",
        "标签：", "，".join(T.for_platform("bili", content, events)), "",
        "【视频号】直接传 视频.mp4。封面选 封面.jpg（个人主页和分享卡片 3:4）。",
        "视频描述：", tg("shipinhao"), "", "短标题：", short_title(title), ""])
