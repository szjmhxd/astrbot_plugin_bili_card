"""链接识别：从任意文本中找出 B 站链接并判定类型。

设计要点
--------
1. **先长后短**。番剧、直播、空间这类 URL 里可能夹带 BV 号，必须先匹配整条 URL，
   再把已匹配片段"挖掉"（替换成空格），否则裸 BV 号规则会重复命中同一段内容。
2. **裸 BV / av 号也认**。群聊里经常只贴一个 ``BV1xx411c7mD``，不带域名。
3. **短链单独归类**。短链要发一次请求才能知道真实类型，所以识别阶段只标记
   ``short``，由上层展开后二次分派。
4. **小程序卡片是另一套**。QQ 分享卡片的 JSON 里链接藏在 ``qqdocurl`` / ``jumpUrl``
   等字段中，且带转义斜线和 HTML 实体，需要先还原再交给同一套正则。
"""

from __future__ import annotations

import html
import re
import urllib.parse
from dataclasses import dataclass

from .models import LinkMatch

# ---------------------------------------------------------------------------
# 正则表。注意顺序即优先级。
# ---------------------------------------------------------------------------

_RE_BANGUMI = re.compile(
    r"https?://(?:www\.|m\.)?bilibili\.com/bangumi/(?:play/ep(\d+)|play/ss(\d+)|media/md(\d+))"
)
_RE_LIVE = re.compile(r"https?://live\.bilibili\.com/(?:h5/|blanc/)?(\d+)")
_RE_SPACE = re.compile(r"https?://space\.bilibili\.com/(\d+)")
_RE_ARTICLE = re.compile(r"https?://(?:www\.)?bilibili\.com/read/cv(\d+)")
_RE_AUDIO = re.compile(r"https?://(?:www\.)?bilibili\.com/audio/(au|am)(\d+)")
_RE_OPUS = re.compile(r"https?://(?:www\.)?bilibili\.com/opus/(\d+)")
_RE_TBILI = re.compile(r"https?://t\.bilibili\.com/(\d+)")
_RE_VIDEO = re.compile(
    r"https?://(?:www\.|m\.)?bilibili\.com/video/(BV[0-9A-Za-z]{10}|av\d+)([^\s]*)"
)
_RE_SHORT = re.compile(
    r"https?://(?:b23\.tv|bili2233\.cn|bili22\.cn|bili23\.cn|bili33\.cn)/[0-9A-Za-z]+"
)

# 裸编号必须两侧都不是字母数字，否则会误伤 "BDV1xx411c7mD" 之类
_RE_BV_ONLY = re.compile(r"(?<![0-9A-Za-z])(BV[0-9A-Za-z]{10})(?![0-9A-Za-z])")
_RE_AV_ONLY = re.compile(r"(?<![0-9A-Za-z])av(\d{1,12})(?![0-9])", re.IGNORECASE)

# 卡片 JSON 里值得优先检查的字段
_CARD_URL_KEYS = ("qqdocurl", "jumpUrl", "jump_url", "url", "appurl", "app_url", "qqurl")


def _mask(text: str, start: int, end: int) -> str:
    """把已匹配区间替换成等长空格，保持其余内容的位置不变。"""
    return text[:start] + " " * (end - start) + text[end:]


def _pick(kind: str, key: str, raw: str, **extra: str) -> LinkMatch:
    return LinkMatch(kind=kind, key=key, raw=raw, extra=extra)


def detect_links(text: str, enabled: set[str]) -> list[LinkMatch]:
    """从文本中识别全部 B 站链接。

    ``enabled`` 是启用的类型集合（来自配置）。被禁用的类型直接跳过，
    不产生任何匹配，也不会因为"部分匹配"而漏掉别的类型。
    """
    if not text:
        return []

    found: list[LinkMatch] = []
    work = text

    def take(pattern: re.Pattern[str], builder) -> None:
        """在 work 上反复匹配 pattern，命中后挖掉并交给 builder 生成结果。"""
        nonlocal work
        while True:
            m = pattern.search(work)
            if not m:
                return
            item = builder(m)
            if item is not None:
                found.append(item)
            work = _mask(work, m.start(), m.end())

    # ---- 1. 番剧：ep / ss / md 三种编号 ----
    if "bangumi" in enabled:

        def _bangumi(m: re.Match[str]) -> LinkMatch | None:
            if m.group(1):
                return _pick("bangumi", m.group(1), m.group(0), mode="ep")
            if m.group(2):
                return _pick("bangumi", m.group(2), m.group(0), mode="ss")
            return _pick("bangumi", m.group(3), m.group(0), mode="md")

        take(_RE_BANGUMI, _bangumi)

    # ---- 2. 直播 ----
    if "live" in enabled:
        take(_RE_LIVE, lambda m: _pick("live", m.group(1), m.group(0)))

    # ---- 3. UP 主空间 ----
    if "space" in enabled:
        take(_RE_SPACE, lambda m: _pick("space", m.group(1), m.group(0)))

    # ---- 4. 专栏 ----
    if "article" in enabled:
        take(_RE_ARTICLE, lambda m: _pick("article", m.group(1), m.group(0)))

    # ---- 5. 音频 / 歌单 ----
    if "audio" in enabled:
        take(
            _RE_AUDIO,
            lambda m: _pick("audio", m.group(2), m.group(0), mode=m.group(1).lower()),
        )

    # ---- 6. 动态：新版 opus 路径与老版 t.bilibili.com ----
    if "opus" in enabled:
        take(_RE_OPUS, lambda m: _pick("opus", m.group(1), m.group(0)))
        take(_RE_TBILI, lambda m: _pick("opus", m.group(1), m.group(0)))

    # ---- 7. 视频（可能带分 P 与评论根 ID）----
    if "video" in enabled or "comment" in enabled:

        def _video(m: re.Match[str]) -> LinkMatch | None:
            ident = m.group(1)
            tail = m.group(2) or ""
            query = urllib.parse.parse_qs(urllib.parse.urlparse(tail).query.lstrip("?"))
            # 评论链接：comment_root_id 存在时按评论处理，需要先拿到 aid
            root_id = (query.get("comment_root_id") or [""])[0]
            if root_id and "comment" in enabled:
                return _pick(
                    "comment",
                    ident,
                    m.group(0),
                    root_id=root_id,
                )
            if "video" not in enabled:
                return None
            page = (query.get("p") or ["1"])[0]
            return _pick("video", ident, m.group(0), page=page)

        take(_RE_VIDEO, _video)

    # ---- 8. 短链：只标记，上层展开后二次分派 ----
    if "short_link" in enabled:
        take(_RE_SHORT, lambda m: _pick("short", m.group(0), m.group(0)))

    # ---- 9. 裸编号：只在前面都没吃掉的位置上找 ----
    if "video" in enabled:
        take(_RE_BV_ONLY, lambda m: _pick("video", m.group(1), m.group(0), page="1"))
        take(_RE_AV_ONLY, lambda m: _pick("video", "av" + m.group(1), m.group(0), page="1"))

    return found


def _unescape_card_text(raw: str) -> str:
    """还原卡片 JSON / XML 中的转义，让链接变成普通文本。"""
    text = raw.replace("\\/", "/")
    text = text.replace("\\u0026", "&").replace("\\u003d", "=").replace("\\u003f", "?")
    # 反复解实体，应对 "&amp;amp;" 这类双重转义
    for _ in range(2):
        text = html.unescape(text)
    return text


def extract_from_card(raw: str, enabled: set[str]) -> list[LinkMatch]:
    """从 QQ 小程序 / JSON 卡片 / XML 卡片中提取 B 站链接。

    卡片没有统一格式，策略是：
    先看已知字段值里有没有站内链接，再对整段文本跑一遍通用正则。
    """
    if not raw or ("b23.tv" not in raw and "bilibili" not in raw.lower()):
        return []

    text = _unescape_card_text(raw)
    candidates: list[str] = []

    for key in _CARD_URL_KEYS:
        for m in re.finditer(rf'"{key}"\s*:\s*"([^"]+)"', text):
            candidates.append(m.group(1))

    # 字段抽取没结果时，退化为全文扫描
    if not candidates:
        candidates.append(text)

    seen: set[str] = set()
    result: list[LinkMatch] = []
    for chunk in candidates:
        for item in detect_links(chunk, enabled):
            marker = f"{item.kind}:{item.key}:{item.extra.get('root_id', '')}"
            if marker in seen:
                continue
            seen.add(marker)
            result.append(item)
    return result


@dataclass
class ParsedTarget:
    """短链展开后的最终目标。"""

    kind: str
    key: str
    page: str = "1"
    root_id: str = ""
    mode: str = ""


def retarget_from_url(url: str, enabled: set[str]) -> ParsedTarget | None:
    """把 b23.tv 展开后的真实 URL 重新分类。"""
    hits = detect_links(url, enabled)
    if not hits:
        return None
    first = hits[0]
    return ParsedTarget(
        kind=first.kind,
        key=first.key,
        page=first.extra.get("page", "1"),
        root_id=first.extra.get("root_id", ""),
        mode=first.extra.get("mode", ""),
    )
