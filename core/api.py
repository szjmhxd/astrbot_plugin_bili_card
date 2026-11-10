"""B 站接口客户端。

职责
----
* 统一维护 aiohttp 会话、请求头、超时、Cookie、代理
* WBI 密钥的获取与缓存
* 短链展开
* **图片转内联 data URI**——这一步决定了卡片能不能正常显示

关于图片为什么要内联
--------------------
B 站图床 ``i0.hdslb.com`` 有 Referer 防盗链。如果直接把远程图片地址写进待渲染的
HTML，Playwright 加载时没有正确的 Referer，结果就是白块。因此这里先把图片下载下来，
再以 base64 内联，渲染阶段完全不产生外部请求。
"""

from __future__ import annotations

import asyncio
import base64
from html import unescape
from typing import Any, Protocol

import aiohttp

from .models import BiliError, CardData, Comment, Stat
from .wbi import WbiKeyCache, enc_wbi

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

API = "https://api.bilibili.com"
LIVE_API = "https://api.live.bilibili.com"
AUDIO_API = "https://api.bilibili.com"

# 常见错误码到人话的映射
_ERR_TEXT = {
    -101: "需要登录才能查看，动态请在配置里填入 SESSDATA",
    -400: "请求参数有误",
    -403: "访问被拒绝",
    -404: "内容不存在或已被删除",
    -352: "触发了 B 站风控，请稍后再试",
    -412: "请求被风控拦截，请稍后再试",
    62002: "稿件不可见",
    62004: "稿件正在审核中",
    87007: "充电专属内容，暂不支持",
    87008: "大会员专属内容，暂不支持",
}

# 卡片上图片的取图尺寸（CSS 像素）。**必须按显示尺寸取图**：
# 远端渲染服务要整段 HTML 一起收，原图 base64 动辄几百 KB，上传和解码都跟不上，
# 截图就会拍到"只解码一半"的图片——封面只剩顶部一条、头像空白。
# B 站图床自带缩略图参数（@720w_405h_1c.webp），封面 341KB → 39KB，头像 107KB → 2KB。
COVER_SIZE = (720, 405)  # 16:9 封面
AVATAR_SIZE = (96, 96)  # 作者行 30px、空间卡 92px 都够用
BIG_AVATAR_SIZE = (192, 192)  # UP 空间卡顶部的方形头像

# 单张图片的体积上限，超过就放弃内联（封面空白但文字照常显示）
_MAX_IMAGE_BYTES = 2 * 1024 * 1024

# 同一张头像会在多条消息里反复出现，缓存一下省掉重复下载
_IMG_CACHE_MAX = 128


def fmt_num(value: Any) -> str:
    """把播放量这类大数字缩写成 万 / 亿。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return "0"
    if n >= 100_000_000:
        return f"{n / 100_000_000:.1f}亿".replace(".0亿", "亿")
    if n >= 10_000:
        return f"{n / 10_000:.1f}万".replace(".0万", "万")
    return str(n)


def fmt_duration(seconds: Any) -> str:
    """秒数转 3:07 / 1:02:33。"""
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _clip(text: Any, limit: int) -> str:
    """截断长文本，保留完整词边界附近的字符。"""
    if not text:
        return ""
    s = str(text).replace("\r", "").strip()
    if len(s) <= limit:
        return s
    return s[: max(1, limit - 1)].rstrip() + "…"


def _normalize_image_url(url: str) -> str:
    """统一协议，并去掉图床地址上原有的 ``@`` 尺寸参数。"""
    if not url:
        return ""
    clean = url.split("@", 1)[0].strip()
    if clean.startswith("//"):
        return "https:" + clean
    if clean.startswith("http://"):
        return "https://" + clean[len("http://") :]
    return clean if clean.startswith("https://") else ""


def _with_thumb_suffix(url: str, width: int, height: int) -> str:
    """给图床地址挂上缩略图参数；参数必须在查询串之前。"""
    base, sep, query = url.partition("?")
    spec = f"@{width}w_{height}h_1c.webp"
    return f"{base}{spec}{sep}{query}"


def _looks_like_image(blob: bytes) -> bool:
    """按文件头判断是不是一张完整的图片。

    半截数据也会被当成图片塞进模板，然后在渲染端变成空白——
    这里挡一道，宁可退回无封面，也不要交一张坏图给渲染服务。
    """
    if len(blob) < 12:
        return False
    if blob.startswith(b"\xff\xd8\xff"):  # JPEG
        return True
    if blob.startswith(b"\x89PNG\r\n\x1a\n"):  # PNG
        return True
    if blob.startswith(b"GIF8"):  # GIF
        return True
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":  # WebP
        return True
    return blob[4:8] == b"ftyp"  # AVIF / HEIF


class LoggerLike(Protocol):
    """本模块用到的日志接口，正式运行时传入 ``astrbot.api.logger``。

    插件规范要求日志只能经由 AstrBot 的 logger 输出，禁止使用标准库 ``logging``。
    因此这里**刻意不提供默认值**：漏传会立刻 ``TypeError``，而不是悄悄换一个
    日志来源、让插件日志跑到别处去。
    """

    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None: ...

    def info(self, msg: str, *args: Any, **kwargs: Any) -> None: ...

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None: ...


class BiliClient:
    """所有网络访问的唯一出口。"""

    def __init__(
        self,
        *,
        logger: LoggerLike,
        sessdata: str = "",
        proxy_url: str = "",
        timeout: int = 10,
        comment_count: int = 3,
    ) -> None:
        self._sessdata = (sessdata or "").strip()
        self._comment_count = max(0, min(int(comment_count), 5))
        self._proxy = (proxy_url or "").strip()
        self._timeout = max(3, int(timeout))
        self._log = logger
        self._session: aiohttp.ClientSession | None = None
        self._wbi = WbiKeyCache()
        self._wbi_lock = asyncio.Lock()
        self._img_cache: dict[tuple[str, tuple[int, int]], str] = {}

    # ------------------------------------------------------------------
    # 会话
    # ------------------------------------------------------------------

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is not None and not self._session.closed:
            return self._session
        jar = aiohttp.CookieJar()
        headers = {
            "User-Agent": UA,
            "Referer": "https://www.bilibili.com/",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        if self._sessdata:
            # 只放 SESSDATA，其余 Cookie 由 aiohttp 自行管理
            jar.update_cookies({"SESSDATA": self._sessdata}, response_url=aiohttp.helpers.URL(API))
        self._session = aiohttp.ClientSession(
            headers=headers,
            cookie_jar=jar,
            timeout=aiohttp.ClientTimeout(total=self._timeout),
        )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
        self._img_cache.clear()

    async def _request_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        *,
        wbi: bool = False,
        ok_codes: tuple[int, ...] = (0,),
    ) -> dict[str, Any]:
        params = dict(params or {})
        if wbi:
            await self._ensure_wbi()
            params = enc_wbi(params, self._wbi.img_key, self._wbi.sub_key)

        session = await self._ensure_session()
        try:
            async with session.get(url, params=params, proxy=self._proxy or None) as resp:
                if resp.status != 200:
                    raise BiliError(f"接口返回 HTTP {resp.status}")
                data = await resp.json(content_type=None)
        except TimeoutError as exc:
            raise BiliError("请求 B 站超时，请稍后再试") from exc
        except aiohttp.ClientError as exc:
            raise BiliError(f"网络异常：{exc.__class__.__name__}") from exc
        except ValueError as exc:
            raise BiliError("B 站返回了无法解析的数据") from exc

        code = data.get("code", 0)
        if code not in ok_codes:
            raise BiliError(_ERR_TEXT.get(code, f"B 站接口错误（code={code}）"))
        return data

    async def _ensure_wbi(self) -> None:
        """按需刷新 WBI 密钥，加锁避免并发请求重复拉取。"""
        if self._wbi.ready and not self._wbi.expired:
            return
        async with self._wbi_lock:
            if self._wbi.ready and not self._wbi.expired:
                return
            # nav 在未登录时固定返回 -101，但 data.wbi_img 照样给，
            # 所以这里不能把 -101 当失败——否则匿名用户会被报"需要登录"，
            # 而这个错误其实是签名密钥环节产生的，跟内容权限无关。
            data = await self._request_json(f"{API}/x/web-interface/nav", ok_codes=(0, -101))
            img_url = (((data.get("data") or {}).get("wbi_img") or {}).get("img_url")) or ""
            sub_url = (((data.get("data") or {}).get("wbi_img") or {}).get("sub_url")) or ""
            if not img_url or not sub_url:
                raise BiliError("无法获取 B 站签名密钥")
            self._wbi.update(img_url, sub_url)

    # ------------------------------------------------------------------
    # 图片内联
    # ------------------------------------------------------------------

    async def inline_image(self, url: str, size: tuple[int, int] | None = None) -> str:
        """下载图片并转成 data URI；失败返回空串，不抛异常。

        ``size`` 给出时优先向图床要缩略图（``@720w_405h_1c.webp``），
        拿不到再退回原图。缩略图能把模板体积压到原来的十分之一，
        这是渲染速度和完整性的关键——不要把原图直接塞进模板。
        """
        clean = _normalize_image_url(url)
        if not clean:
            return ""

        key = (clean, size or (0, 0))
        cached = self._img_cache.get(key)
        if cached is not None:
            return cached

        blob, mime = await self._download_image(clean, size)
        uri = f"data:{mime};base64,{base64.b64encode(blob).decode()}" if blob else ""

        if len(self._img_cache) >= _IMG_CACHE_MAX:
            self._img_cache.pop(next(iter(self._img_cache)), None)
        self._img_cache[key] = uri
        return uri

    async def _download_image(self, url: str, size: tuple[int, int] | None) -> tuple[bytes, str]:
        """依次尝试缩略图与原图，返回原始字节与 mime；全失败返回空。"""
        candidates: list[str] = []
        if size and size[0] > 0 and size[1] > 0:
            candidates.append(_with_thumb_suffix(url, size[0], size[1]))
        candidates.append(url)

        session = await self._ensure_session()
        for candidate in candidates:
            try:
                async with session.get(
                    candidate,
                    headers={
                        "Referer": "https://www.bilibili.com/",
                        "Accept": "image/webp,image/avif,image/*,*/*;q=0.8",
                    },
                    proxy=self._proxy or None,
                    timeout=aiohttp.ClientTimeout(total=12),
                ) as resp:
                    if resp.status != 200:
                        continue
                    if resp.content_length and resp.content_length > _MAX_IMAGE_BYTES:
                        self._log.debug("图片过大，跳过 %s", candidate)
                        continue
                    # 必须用 resp.read() 读完整响应体。
                    # resp.content.read(n) 只保证"拿到至少 1 字节"，会返回第一个
                    # 可用数据块（实测 16KB），大图被截断后仍然是一段合法前缀，
                    # 渲染出来就是空白封面；小头像恰好一个块读完，所以看起来正常。
                    blob = await resp.read()
                    if not blob or len(blob) > _MAX_IMAGE_BYTES:
                        continue
                    mime = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
            except (TimeoutError, aiohttp.ClientError) as exc:
                self._log.debug("图片下载失败 %s: %s", candidate, exc)
                continue
            if not mime.startswith("image/"):
                continue
            if not _looks_like_image(blob):
                self._log.warning(
                    "图片数据不完整，已跳过（%s，%d 字节）",
                    candidate.rsplit("/", 1)[-1][-40:],
                    len(blob),
                )
                continue
            return blob, mime
        return b"", ""

    # ------------------------------------------------------------------
    # 短链
    # ------------------------------------------------------------------

    async def resolve_short(self, url: str) -> str:
        """展开 b23.tv 短链，返回 Location 指向的真实地址。"""
        session = await self._ensure_session()
        try:
            # 关闭自动跳转，直接读 Location，避免跟到最终页面浪费流量
            async with session.get(
                url,
                allow_redirects=False,
                proxy=self._proxy or None,
            ) as resp:
                location = resp.headers.get("Location") or ""
                if not location and resp.status == 200:
                    # 少数情况下返回 200 并由前端跳转，退化为读正文里的链接
                    text = await resp.text()
                    import re

                    m = re.search(r"https?://(?:www\.)?bilibili\.com/[^\s\"'<>]+", text)
                    return m.group(0) if m else ""
                return location
        except (TimeoutError, aiohttp.ClientError) as exc:
            raise BiliError("短链展开失败，请直接发完整链接") from exc

    # ------------------------------------------------------------------
    # 八类内容抓取
    # ------------------------------------------------------------------

    async def fetch(self, kind: str, key: str, **extra: Any) -> CardData:
        handler = {
            "video": self.fetch_video,
            "bangumi": self.fetch_bangumi,
            "live": self.fetch_live,
            "article": self.fetch_article,
            "opus": self.fetch_opus,
            "space": self.fetch_space,
            "audio": self.fetch_audio,
            "comment": self.fetch_comment,
        }.get(kind)
        if handler is None:
            raise BiliError(f"暂不支持的类型：{kind}")
        return await handler(key, **extra)

    # ---- 视频 ----

    # ---- 评论 ----

    async def _top_comments(self, oid: Any, ctype: int = 1) -> list[Comment]:
        """取热门评论。

        评论是锦上添花：接口挂了、被风控了、评论关了，都只返回空列表，
        不能让整张卡片跟着失败。

        ``ctype`` 是 B 站评论区的业务类型，和内容形态一一对应：
        1=视频/番剧单集，12=专栏，17=纯图文的动态。动态要按
        ``basic.comment_type`` 与 ``basic.comment_id_str`` 取，不能拿动态 id 硬套 17
        ——转载专栏的动态，评论其实挂在 cv 号上。
        """
        limit = self._comment_count
        if limit <= 0 or not oid:
            return []
        try:
            data = await self._request_json(
                f"{API}/x/v2/reply",
                {"type": ctype, "oid": oid, "sort": 2, "ps": limit, "pn": 1},
            )
        except BiliError as exc:
            self._log.debug("热门评论获取失败：%s", exc.message)
            return []

        out: list[Comment] = []
        for row in ((data.get("data") or {}).get("replies") or [])[:limit]:
            # 接口里的正文是 HTML 实体转义过的（&#34; &gt; 之类），这里先还原；
            # 模板输出时还会再过一遍 |e，不还原就会把 &amp;#34; 印在卡片上。
            text = unescape(((row.get("content") or {}).get("message")) or "").strip()
            if not text:
                continue
            out.append(
                Comment(
                    user=unescape(((row.get("member") or {}).get("uname")) or "匿名用户"),
                    text=_clip(text, 120),
                    like=fmt_num(row.get("like")),
                )
            )
        return out

    # ---- 视频 ----

    async def fetch_video(self, key: str, page: str = "1", **_kw: Any) -> CardData:
        params = {"bvid": key} if key.upper().startswith("BV") else {"aid": key.lstrip("avAV")}
        data = await self._request_json(f"{API}/x/web-interface/view", params)
        d = data.get("data") or {}
        if not d:
            raise BiliError("视频不存在或已被删除")

        bvid = d.get("bvid") or key
        total_p = int(d.get("videos") or 1)
        try:
            p_index = max(1, min(int(page or 1), total_p))
        except ValueError:
            p_index = 1

        subtitle = ""
        if total_p > 1:
            pages = d.get("pages") or []
            part = ""
            if 0 < p_index <= len(pages):
                part = (pages[p_index - 1] or {}).get("part") or ""
            subtitle = f"P{p_index}/{total_p}" + (f" · {part}" if part else "")

        stat = d.get("stat") or {}
        owner = d.get("owner") or {}
        link = f"https://www.bilibili.com/video/{bvid}"
        if total_p > 1:
            link += f"?p={p_index}"

        # 封面、头像、热评互不依赖，并发取，避免串行多等两轮往返
        cover, avatar, comments = await asyncio.gather(
            self.inline_image(d.get("pic") or "", COVER_SIZE),
            self.inline_image(owner.get("face") or "", AVATAR_SIZE),
            self._top_comments(d.get("aid")),
        )

        return CardData(
            kind="video",
            title=_clip(d.get("title"), 120),
            subtitle=subtitle,
            cover=cover,
            avatar=avatar,
            comments=comments,
            author=owner.get("name") or "",
            author_link=f"https://space.bilibili.com/{owner.get('mid')}"
            if owner.get("mid")
            else "",
            desc=_clip(d.get("desc"), 200),
            stats=[
                Stat("播放", fmt_num(stat.get("view"))),
                Stat("点赞", fmt_num(stat.get("like"))),
                Stat("投币", fmt_num(stat.get("coin"))),
                Stat("收藏", fmt_num(stat.get("favorite"))),
                Stat("评论", fmt_num(stat.get("reply"))),
                Stat("分享", fmt_num(stat.get("share"))),
            ],
            tags=[
                t
                for t in (
                    d.get("tname") or "",
                    fmt_duration(d.get("duration")),
                    "原创" if d.get("copyright") == 1 else "转载",
                )
                if t
            ],
            link=link,
            link_label=f"BV{bvid[2:]}" if bvid.upper().startswith("BV") else bvid,
        )

    # ---- 番剧 ----

    async def fetch_bangumi(self, key: str, mode: str = "ep", **_kw: Any) -> CardData:
        param_name = {"ep": "ep_id", "ss": "season_id", "md": "media_id"}.get(mode, "ep_id")
        data = await self._request_json(f"{API}/pgc/view/web/season", {param_name: key})
        r = data.get("result") or {}
        if not r:
            raise BiliError("番剧不存在或已下架")

        episodes = r.get("episodes") or []
        current: dict[str, Any] = {}
        if mode == "ep":
            current = next((e for e in episodes if str(e.get("ep_id")) == str(key)), {})

        # 单集视角优先用当集封面与标题
        cover_url = current.get("cover") or r.get("cover") or ""
        subtitle = ""
        if current:
            ep_title = current.get("title") or ""
            long_title = current.get("long_title") or ""
            subtitle = f"第{ep_title}话" + (f" {long_title}" if long_title else "")

        rating = (r.get("rating") or {}).get("score")
        stat = r.get("stat") or {}
        ep_id = current.get("ep_id") or (episodes[0].get("ep_id") if episodes else "")
        link = (
            f"https://www.bilibili.com/bangumi/play/ep{ep_id}"
            if ep_id
            else f"https://www.bilibili.com/bangumi/media/md{r.get('media_id')}"
        )

        tags = ["番剧"]
        if rating:
            tags.append(f"{rating}分")
        new_ep = (r.get("new_ep") or {}).get("desc") or ""
        if new_ep:
            tags.append(new_ep)

        # 番剧评论区 oid 是 ep_id（不是 season_id），和视频同属 type=1
        cover, comments = await asyncio.gather(
            self.inline_image(cover_url, COVER_SIZE),
            self._top_comments(ep_id, 1),
        )

        return CardData(
            kind="bangumi",
            title=_clip(r.get("title"), 120),
            subtitle=subtitle,
            cover=cover,
            comments=comments,
            desc=_clip(r.get("evaluate"), 200),
            stats=[
                Stat("播放", fmt_num(stat.get("views"))),
                Stat("追番", fmt_num(stat.get("favorites"))),
                Stat("点赞", fmt_num(stat.get("likes"))),
                Stat("投币", fmt_num(stat.get("coins"))),
                Stat("弹幕", fmt_num(stat.get("danmakus"))),
                Stat("分享", fmt_num(stat.get("share"))),
            ],
            tags=tags,
            link=link,
            link_label="番剧详情",
        )

    # ---- 直播 ----

    async def fetch_live(self, key: str, **_kw: Any) -> CardData:
        data = await self._request_json(f"{LIVE_API}/room/v1/Room/get_info", {"room_id": key})
        d = data.get("data") or {}
        if not d:
            raise BiliError("直播间不存在")

        live_status = int(d.get("live_status") or 0)
        cover_url = d.get("keyframe") or d.get("user_cover") or ""

        # 主播昵称与头像需要额外一次接口
        uname = ""
        face = ""
        try:
            info = await self._request_json(
                f"{LIVE_API}/live_user/v1/UserInfo/get_anchor_in_room",
                {"roomid": key},
            )
            base = ((info.get("data") or {}).get("info")) or {}
            uname = base.get("uname") or ""
            face = base.get("face") or ""
        except BiliError:
            # 拿不到主播信息不影响主流程
            pass

        uid = d.get("uid")
        return CardData(
            kind="live",
            title=_clip(d.get("title"), 120) or "（无标题）",
            cover=await self.inline_image(cover_url, COVER_SIZE),
            avatar=await self.inline_image(face, AVATAR_SIZE),
            author=uname,
            author_link=f"https://space.bilibili.com/{uid}" if uid else "",
            desc=_clip(d.get("description"), 160),
            highlight="● 直播中" if live_status == 1 else "○ 未开播",
            highlight_kind="live" if live_status == 1 else "normal",
            stats=[
                Stat("人气", fmt_num(d.get("online"))),
                Stat("关注", fmt_num(d.get("attention"))),
            ],
            tags=[f"房间 {d.get('room_id') or key}"],
            link=f"https://live.bilibili.com/{key}",
            link_label="进入直播间",
        )

    # ---- 专栏 ----

    async def fetch_article(self, key: str, **_kw: Any) -> CardData:
        data = await self._request_json(f"{API}/x/article/viewinfo", {"id": key})
        d = data.get("data") or {}
        if not d:
            raise BiliError("专栏不存在或已被删除")

        images = d.get("image_urls") or []
        cover_url = d.get("banner_url") or (images[0] if images else "")
        stats = d.get("stats") or {}
        mid = d.get("author_mid")
        cv_id = key[2:] if key.lower().startswith("cv") else key

        # 专栏评论区 type=12，oid 是 cv 号
        cover, avatar, comments = await asyncio.gather(
            self.inline_image(cover_url, COVER_SIZE),
            self.inline_image(d.get("author_face") or "", AVATAR_SIZE),
            self._top_comments(cv_id, 12),
        )

        return CardData(
            kind="article",
            title=_clip(d.get("title"), 120),
            cover=cover,
            avatar=avatar,
            comments=comments,
            author=d.get("author_name") or "",
            author_link=f"https://space.bilibili.com/{mid}" if mid else "",
            desc=_clip(d.get("summary"), 200),
            stats=[
                Stat("阅读", fmt_num(stats.get("view"))),
                Stat("点赞", fmt_num(stats.get("like"))),
                Stat("投币", fmt_num(stats.get("coin"))),
                Stat("收藏", fmt_num(stats.get("favorite"))),
                Stat("评论", fmt_num(stats.get("reply"))),
                Stat("分享", fmt_num(stats.get("share"))),
            ],
            tags=["专栏"],
            link=f"https://www.bilibili.com/read/cv{key}",
            link_label=f"cv{key}",
        )

    # ---- 动态 ----

    async def fetch_opus(self, key: str, **_kw: Any) -> CardData:
        data = await self._request_json(
            f"{API}/x/polymer/web-dynamic/v1/detail",
            {"id": key, "timezone_offset": "-480"},
            wbi=True,
        )
        item = (data.get("data") or {}).get("item") or {}
        if not item:
            raise BiliError("动态不存在或需要登录后才能查看")

        modules = item.get("modules") or {}
        author = modules.get("module_author") or {}
        dynamic = modules.get("module_dynamic") or {}
        stat = modules.get("module_stat") or {}
        major = dynamic.get("major") or {}

        desc_text = ((dynamic.get("desc") or {}).get("text")) or ""
        cover_url = ""
        extra_title = ""
        tags: list[str] = ["动态"]

        mtype = major.get("type") or ""

        if mtype == "MAJOR_TYPE_OPUS":
            opus = major.get("opus") or {}
            desc_text = desc_text or ((opus.get("summary") or {}).get("text")) or ""
            pics = opus.get("pics") or []
            if pics:
                cover_url = pics[0].get("url") or ""
        elif mtype == "MAJOR_TYPE_DRAW":
            draw = major.get("draw") or {}
            items = draw.get("items") or []
            if items:
                cover_url = items[0].get("src") or ""
        elif mtype == "MAJOR_TYPE_ARCHIVE":
            archive = major.get("archive") or {}
            extra_title = archive.get("title") or ""
            cover_url = archive.get("cover") or ""
            tags.append("视频")
            play = ((archive.get("stat") or {}).get("play")) or ""
            if play:
                tags.append(f"播放 {fmt_num(play)}")
        elif mtype == "MAJOR_TYPE_ARTICLE":
            article = major.get("article") or {}
            extra_title = article.get("title") or ""
            covers = article.get("covers") or []
            cover_url = covers[0] if covers else ""
            tags.append("专栏")
        elif mtype == "MAJOR_TYPE_LIVE_RCMD":
            tags.append("直播")

        mid = author.get("mid")

        # 动态的评论区不总挂在动态 id 上：转载专栏的动态，评论实际在 cv 号上，
        # 所以必须用接口回的 comment_type / comment_id_str，不能拿动态 id 硬套 type=17。
        basic = item.get("basic") or {}
        try:
            ctype = int(basic.get("comment_type") or 0)
        except (TypeError, ValueError):
            ctype = 0
        cid = basic.get("comment_id_str") or ""

        cover, avatar, comments = await asyncio.gather(
            self.inline_image(cover_url, COVER_SIZE),
            self.inline_image(author.get("face") or "", AVATAR_SIZE),
            self._top_comments(cid, ctype or 1),
        )

        return CardData(
            kind="opus",
            title=_clip(unescape(extra_title or desc_text) or "（无正文）", 120),
            cover=cover,
            avatar=avatar,
            comments=comments,
            author=author.get("name") or "",
            author_link=f"https://space.bilibili.com/{mid}" if mid else "",
            desc=_clip(unescape(desc_text) if extra_title else "", 200),
            stats=[
                Stat("点赞", fmt_num((stat.get("like") or {}).get("count"))),
                Stat("评论", fmt_num((stat.get("comment") or {}).get("count"))),
                Stat("转发", fmt_num((stat.get("forward") or {}).get("count"))),
            ],
            tags=tags,
            link=f"https://www.bilibili.com/opus/{key}",
            link_label="查看动态",
        )

    # ---- UP 主空间 ----

    async def fetch_space(self, key: str, **_kw: Any) -> CardData:
        """优先用不需要签名的 card 接口，失败再退到 wbi 的 acc/info。"""
        name = face = sign = ""
        level = ""
        follower = ""
        archives = ""

        try:
            data = await self._request_json(f"{API}/x/web-interface/card", {"mid": key})
            d = data.get("data") or {}
            card = d.get("card") or {}
            name = card.get("name") or ""
            face = card.get("face") or ""
            sign = card.get("sign") or ""
            level = str(((card.get("level_info") or {}).get("current_level")) or "")
            follower = fmt_num(d.get("follower"))
            archives = fmt_num(d.get("archive_count"))
        except BiliError:
            data = await self._request_json(f"{API}/x/space/wbi/acc/info", {"mid": key}, wbi=True)
            d = data.get("data") or {}
            name = d.get("name") or ""
            face = d.get("face") or ""
            sign = d.get("sign") or ""
            level = str(d.get("level") or "")

        if not name:
            raise BiliError("该用户不存在或空间已隐藏")

        return CardData(
            kind="space",
            title=name,
            subtitle=sign,
            cover=await self.inline_image(face, BIG_AVATAR_SIZE),
            avatar="",
            author=f"UID {key}",
            highlight=f"LV{level}" if level else "",
            highlight_kind="normal",
            desc="",
            stats=[
                Stat("粉丝", follower or "—"),
                Stat("投稿", archives or "—"),
            ],
            tags=["UP主"],
            link=f"https://space.bilibili.com/{key}",
            link_label="进入空间",
        )

    # ---- 音频 / 歌单 ----

    async def fetch_audio(self, key: str, mode: str = "au", **_kw: Any) -> CardData:
        if mode == "am":
            data = await self._request_json(
                f"{AUDIO_API}/audio/music-service-c/web/menu/info", {"sid": key}
            )
            d = data.get("data") or {}
            if not d:
                raise BiliError("歌单不存在或已下架")
            stat = d.get("statistic") or {}
            songs = d.get("songs") or []
            return CardData(
                kind="audio",
                title=_clip(d.get("title"), 120),
                cover=await self.inline_image(d.get("cover") or "", COVER_SIZE),
                author=d.get("uname") or "",
                desc=_clip(d.get("intro"), 160),
                stats=[
                    Stat("播放", fmt_num(stat.get("play"))),
                    Stat("收藏", fmt_num(stat.get("collect"))),
                    Stat("分享", fmt_num(stat.get("share"))),
                ],
                tags=["歌单", f"{len(songs)} 首"],
                link=f"https://www.bilibili.com/audio/am{key}",
                link_label=f"am{key}",
            )

        data = await self._request_json(
            f"{AUDIO_API}/audio/music-service-c/web/song/info", {"sid": key}
        )
        d = data.get("data") or {}
        if not d:
            raise BiliError("音频不存在或已下架")
        stat = d.get("statistic") or {}
        return CardData(
            kind="audio",
            title=_clip(d.get("title"), 120),
            subtitle=f"歌手：{d.get('author')}" if d.get("author") else "",
            cover=await self.inline_image(d.get("cover") or "", COVER_SIZE),
            author=d.get("uname") or "",
            stats=[
                Stat("播放", fmt_num(stat.get("play"))),
                Stat("收藏", fmt_num(stat.get("collect"))),
                Stat("投币", fmt_num(stat.get("coin_num"))),
                Stat("分享", fmt_num(stat.get("share"))),
            ],
            tags=[t for t in ("音频", fmt_duration(d.get("duration"))) if t],
            link=f"https://www.bilibili.com/audio/au{key}",
            link_label=f"au{key}",
        )

    # ---- 视频评论 ----

    async def fetch_comment(self, key: str, root_id: str = "", **_kw: Any) -> CardData:
        if not root_id:
            raise BiliError("评论链接缺少 comment_root_id 参数")

        # 先拿 aid，评论接口只认 oid
        vparams = {"bvid": key} if key.upper().startswith("BV") else {"aid": key.lstrip("avAV")}
        vdata = await self._request_json(f"{API}/x/web-interface/view", vparams)
        vd = vdata.get("data") or {}
        aid = vd.get("aid")
        if not aid:
            raise BiliError("无法定位评论所属视频")

        data = await self._request_json(
            f"{API}/x/v2/reply/reply",
            {"oid": aid, "type": 1, "root": root_id, "ps": 3, "pn": 1},
        )
        d = data.get("data") or {}
        root = d.get("root") or {}
        if not root:
            raise BiliError("评论不存在或已被删除")

        member = root.get("member") or {}
        message = ((root.get("content") or {}).get("message")) or ""
        replies = d.get("replies") or []

        reply_lines = [
            f"└ {(r.get('member') or {}).get('uname', '')}："
            f"{_clip(((r.get('content') or {}).get('message')), 50)}"
            for r in replies[:3]
        ]

        bvid = vd.get("bvid") or key
        return CardData(
            kind="comment",
            title=_clip(vd.get("title"), 80) or "视频评论",
            subtitle=f"@{member.get('uname', '')} 的评论",
            cover=await self.inline_image(member.get("avatar") or "", AVATAR_SIZE),
            author=member.get("uname") or "",
            desc="\n".join([_clip(message, 220), *reply_lines]),
            stats=[
                Stat("点赞", fmt_num(root.get("like"))),
                Stat("回复", fmt_num(root.get("rcount"))),
            ],
            tags=["评论"],
            link=f"https://www.bilibili.com/video/{bvid}#reply{root_id}",
            link_label="查看评论",
        )
