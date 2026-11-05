"""插件内部的数据模型。

把八类 B 站内容统一成 ``CardData``，渲染层因此只需要认识一种结构，
新增内容类型时不必再改模板。各类型的差异通过 ``badge`` / ``accent`` /
``rows`` 三个字段吸收。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 各类型的角标文字与主题色，保持整站视觉一致
KIND_META: dict[str, tuple[str, str, str]] = {
    # kind: (角标文字, 主色, 浅色背景)
    "video": ("视频", "#fb7299", "#fff1f5"),
    "bangumi": ("番剧", "#ff9f3a", "#fff6ea"),
    "live": ("直播", "#23ade5", "#eaf9ff"),
    "article": ("专栏", "#7c6cf5", "#f2f0ff"),
    "opus": ("动态", "#3ac0a0", "#eafaf5"),
    "space": ("UP主", "#fb7299", "#fff1f5"),
    "audio": ("音频", "#5b9df9", "#eef5ff"),
    "comment": ("评论", "#8f9bb3", "#f3f5f8"),
}


def kind_badge(kind: str) -> str:
    return KIND_META.get(kind, ("内容", "#fb7299", "#fff1f5"))[0]


def kind_accent(kind: str) -> str:
    return KIND_META.get(kind, ("内容", "#fb7299", "#fff1f5"))[1]


def kind_tint(kind: str) -> str:
    return KIND_META.get(kind, ("内容", "#fb7299", "#fff1f5"))[2]


@dataclass
class Stat:
    """卡片上的一格统计数据。"""

    label: str
    value: str


@dataclass
class CardData:
    """一张卡片的全部渲染数据。

    cover / avatar 存放的是 ``data:image/...;base64,`` 内联串而不是远程 URL：
    B 站图片带 Referer 防盗链，直接把远程地址写进 HTML 会渲染成空白。
    """

    kind: str
    title: str = ""
    subtitle: str = ""
    cover: str = ""
    avatar: str = ""
    author: str = ""
    author_link: str = ""
    desc: str = ""
    stats: list[Stat] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    link: str = ""
    link_label: str = "打开链接"
    # 直播状态等个性化的一行信息，放在标题下方
    highlight: str = ""
    highlight_kind: str = ""  # live / warning / normal

    @property
    def badge(self) -> str:
        return kind_badge(self.kind)

    @property
    def accent(self) -> str:
        return kind_accent(self.kind)

    @property
    def tint(self) -> str:
        return kind_tint(self.kind)


class BiliError(Exception):
    """业务或网络错误，message 会直接展示给用户，所以必须写人话。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass
class LinkMatch:
    """从一段文本里识别出来的一条 B 站链接。"""

    kind: str
    key: str  # BV 号 / av 号 / ep 号 / 房间号 / cv 号 / uid / 动态号 / au 号
    raw: str  # 原始匹配文本，用于日志与去重
    extra: dict[str, str] = field(default_factory=dict)
