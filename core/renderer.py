"""卡片渲染层：把 ``CardData`` 变成可以直接交给 AstrBot 渲染的 HTML。

为什么不在模板里做判断
----------------------
模板只负责"长什么样"，所有取舍（要不要显示封面、角标位置、统计列数、
直播状态放哪）都在这里决定，模板拿到的是最终结构。这样改版式不用碰逻辑，
加内容类型也不用改模板。

渲染交给 AstrBot 的 ``Star.html_render()``（底层 Playwright），
所以本插件不引入任何浏览器依赖。
"""

from __future__ import annotations

import re
from dataclasses import asdict
from pathlib import Path

from .models import CardData

_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "card.html"
_cached_template: str | None = None

# 时长标签形如 3:07 或 1:02:33，用来把它从标签区挪到封面右下角
_RE_DURATION = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")


def load_template() -> str:
    """读取并缓存模板，避免每次渲染都碰磁盘。"""
    global _cached_template
    if _cached_template is None:
        _cached_template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    return _cached_template


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def build_context(
    card: CardData,
    *,
    theme: str = "light",
    show_cover: bool = True,
    show_avatar: bool = True,
    show_stats: bool = True,
    show_desc: bool = True,
    show_comments: bool = True,
    desc_max_chars: int = 96,
    comment_max_chars: int = 60,
) -> dict:
    """生成模板上下文。返回的 dict 直接作为 Jinja2 的渲染数据。"""

    cover = card.cover if show_cover else ""
    avatar = card.avatar if show_avatar else ""

    # 顶部视觉区形态：
    #   cover  —— 常规 16:9 封面
    #   avatar —— UP 主空间，方形头像居中更合适，硬裁成 16:9 很难看
    #   none   —— 没有可用图片，直接进正文
    if card.kind == "space":
        hero = "avatar" if cover else "none"
    elif card.kind == "comment":
        hero = "none"
    else:
        hero = "cover" if cover else "none"

    # 评论区只拿到评论者头像，正文里要复用，否则作者行只剩一个色块
    if card.kind == "comment" and not avatar:
        avatar = card.cover

    # 时长从标签区提到封面角标
    tags = list(card.tags)
    cover_chip = ""
    chip_live = False
    for tag in tags:
        if _RE_DURATION.match(tag):
            cover_chip = tag
            tags.remove(tag)
            break

    # 角标已经说明了类型，标签里再出现一次"UP主""评论"就是纯噪音
    tags = [t for t in dict.fromkeys(tags) if t and t != card.badge]

    highlight = card.highlight
    # 直播状态：有封面时用封面角标承载，作者行就不重复显示
    if card.kind == "live" and highlight:
        chip_live = highlight.startswith("●")
        # 两个前缀标记都要去掉，不能只用 lstrip("● ")，那样 ○ 会留在文字里
        status = highlight[1:].strip() if highlight[:1] in "●○" else highlight.strip()
        if cover:
            cover_chip = status
            highlight = ""
        else:
            highlight = status

    stats = [asdict(s) for s in card.stats] if show_stats else []
    # 列数要让每行都排满，不能出现半截空行：
    # 4 格用两列两行，5 格用三列（3+2 是唯一解），6 格两行三列
    count = len(stats)
    if count <= 3:
        cols = max(1, count)
    elif count % 3 == 0:
        cols = 3
    elif count % 2 == 0:
        cols = 2
    else:
        cols = 3

    # 评论只留两行：卡片高度一旦超过一屏，图在群里就被压得看不清了
    comments = (
        [
            {
                "user": _truncate(c.user, 16) or "匿名用户",
                "text": _truncate(" ".join(c.text.split()), comment_max_chars),
                "like": c.like,
            }
            for c in card.comments[:5]
        ]
        if show_comments
        else []
    )
    comments = [c for c in comments if c["text"]]

    return {
        "theme": "dark" if theme == "dark" else "light",
        "badge": card.badge,
        "accent": card.accent,
        "hero": hero,
        "cover": cover,
        "avatar": avatar,
        "cover_chip": cover_chip,
        "chip_live": chip_live,
        "title": _truncate(card.title, 120) or "（无标题）",
        "subtitle": _truncate(card.subtitle, 120),
        "author": card.author,
        "highlight": highlight,
        "desc": _truncate(card.desc, desc_max_chars) if show_desc else "",
        "stats": stats,
        "stat_cols": cols,
        "comments": comments,
        "tags": tags,
        "link": card.link,
        "link_label": card.link_label or card.link,
    }


def plain_text(card: CardData) -> str:
    """纯文本形态，用于 text_only 模式或渲染失败时的兜底。

    刻意不做花哨排版：这个分支的存在意义是"至少能看"，不是替代卡片。
    """
    lines: list[str] = [f"【{card.badge}】{card.title}"]
    if card.subtitle:
        lines.append(card.subtitle)
    if card.author or card.highlight:
        tail = " · ".join(x for x in (card.author, card.highlight) if x)
        if tail:
            lines.append(tail)
    if card.desc:
        lines.append(card.desc)
    if card.stats:
        lines.append("  ".join(f"{s.label} {s.value}" for s in card.stats))
    if card.tags:
        lines.append("  ".join(f"#{t}" for t in card.tags))
    for c in card.comments[:2]:
        like = f"（{c.like} 赞）" if c.like else ""
        lines.append(f"热评 @{c.user}{like}：{' '.join(c.text.split())}")
    if card.link:
        lines.append(card.link)
    return "\n".join(x for x in lines if x)
