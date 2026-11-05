"""astrbot_plugin_bili_card 的内部模块。

对外只暴露必要的入口，其余细节封装在各自模块内。
"""

from .api import BiliClient, fmt_duration, fmt_num
from .link_parser import detect_links, extract_from_card, retarget_from_url
from .models import BiliError, CardData, Comment, LinkMatch, Stat
from .renderer import build_context, load_template, plain_text

__all__ = [
    "BiliClient",
    "BiliError",
    "CardData",
    "Comment",
    "LinkMatch",
    "Stat",
    "build_context",
    "detect_links",
    "extract_from_card",
    "fmt_duration",
    "fmt_num",
    "load_template",
    "plain_text",
    "retarget_from_url",
]
