"""B站卡片解析插件入口。

设计取舍
--------
* **只用 AstrBot 自带的渲染能力**。卡片通过 ``Star.html_render()`` 出图，
  渲染服务由 AstrBot 自己配置与承担，所以插件不引入 Playwright / Chromium / FFmpeg，
  安装即用，换平台也不受影响。注意 ``html_render`` 走的是宿主配置的文转图端点
  （默认是 AstrBot 官方 t2i 服务），不是本机渲染，因此渲染失败或断网时插件会退回纯文字。
* **不调用任何模型**。没有 AI 总结，没有 STT，插件的行为完全确定、可预期。
* **卡片 + 文字双发**。图片好看但链接点不动，所以图后面补一条带原始链接的文字，
  群友能直接复制。
* **自动解析静默失败，手动指令报错**。群里网络抖一下就刷一行报错很吵；
  用户主动发指令时则需要知道为什么没出来。
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncGenerator
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .core import (
    BiliClient,
    BiliError,
    CardData,
    LinkMatch,
    build_context,
    detect_links,
    extract_from_card,
    load_template,
    plain_text,
    retarget_from_url,
)

PLUGIN_VERSION = "v1.0.2"

ALL_TYPES = (
    "video",
    "bangumi",
    "live",
    "article",
    "opus",
    "space",
    "audio",
    "comment",
    "short_link",
    "json_card",
)

# 渲染选项：出 PNG、整页截图、关动画、透明背景（保留圆角）
RENDER_OPTIONS = {
    "type": "png",
    "full_page": True,
    "animations": "disabled",
    "omit_background": True,
}

# 封面内联串的硬上限。按 720×405 的 webp 取图只有 40KB 左右（base64 约 54KB），
# 超过这个量级说明图床没有按尺寸返回、拿的是原图，宁可不要封面也别让整段模板被截断。
_MAX_COVER_CHARS = 350_000


class BiliCardPlugin(Star):
    """B站卡片解析：自动识别群里的 B 站链接，渲染成图文卡片。

    支持视频（含分 P 与评论链接）、番剧、直播、专栏、动态、UP 空间、音频，
    以及 b23.tv / bili2233.cn 短链和 QQ 小程序卡片。

    指令：
        /bili <链接或BV号>   手动解析
        /bilihelp            查看用法与当前开关状态

    自动解析默认关闭，在插件配置里打开「自动解析」后生效。
    """

    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self.client = BiliClient(
            sessdata=str(self._opt("advanced", "cookie_sessdata", "") or ""),
            proxy_url=str(self._opt("advanced", "proxy_url", "") or ""),
            timeout=int(self._opt("advanced", "request_timeout", 10) or 10),
            logger=logger,
        )
        # 冷却表：(会话, 内容指纹) -> 上次解析时间
        self._cooldown: dict[tuple[str, str], float] = {}
        self._cooldown_sweep_at = 0.0
        logger.info("[bili_card] 插件已加载，版本 %s", PLUGIN_VERSION)

    # ------------------------------------------------------------------
    # 配置读取
    # ------------------------------------------------------------------

    def _opt(self, group: str, key: str, default: Any) -> Any:
        """安全读取分组配置；配置缺失或类型异常时一律回落默认值。"""
        try:
            section = self.config.get(group) or {}
            value = section.get(key, default)
            return default if value is None else value
        except Exception:  # 配置对象形态在不同版本间有差异，不能让读配置把插件打挂
            return default

    def _enabled_types(self) -> set[str]:
        return {t for t in ALL_TYPES if bool(self._opt("types", t, True))}

    def _blocked(self, event: AstrMessageEvent) -> bool:
        blocked = self._opt("advanced", "blocked_groups", []) or []
        if not blocked:
            return False
        try:
            group_id = str(event.get_group_id() or "")
        except Exception:
            return False
        if not group_id:
            return False
        return group_id in {str(x) for x in blocked}

    # ------------------------------------------------------------------
    # 链接收集
    # ------------------------------------------------------------------

    def _card_raws(self, event: AstrMessageEvent) -> list[str]:
        """取出消息里的结构化卡片原文（QQ 小程序 / JSON / XML）。

        不同适配器给出的组件类名不完全一致，所以按类名判断而不是死写 import，
        这样在缺组件的环境里也不会抛 ImportError。
        """
        raws: list[str] = []
        try:
            messages = event.message_obj.message or []
        except Exception:
            messages = []

        for comp in messages:
            name = type(comp).__name__.lower()
            if name not in ("json", "xml", "lightapp", "ark"):
                continue
            for attr in ("data", "text", "content", "raw"):
                value = getattr(comp, attr, None)
                if isinstance(value, str) and value.strip():
                    raws.append(value)
                    break
            else:
                # 少数适配器把结构化数据塞在 data 里。
                # 必须用 json.dumps 而不是 str()：后者的单引号 repr 会让卡片字段抽取失效。
                value = getattr(comp, "data", None)
                if isinstance(value, (dict, list)):
                    raws.append(json.dumps(value, ensure_ascii=False))
        return raws

    def _collect_links(self, event: AstrMessageEvent, enabled: set[str]) -> list[LinkMatch]:
        """从纯文本 + 卡片原文中提取全部链接，并按类型去重。"""
        candidates: list[LinkMatch] = []

        text = getattr(event, "message_str", "") or ""
        if text:
            candidates.extend(detect_links(text, enabled))

        if "json_card" in enabled:
            for raw in self._card_raws(event):
                if "bilibili" in raw.lower() or "b23.tv" in raw:
                    candidates.extend(extract_from_card(raw, enabled))

        seen: set[str] = set()
        result: list[LinkMatch] = []
        for item in candidates:
            marker = self._fingerprint(item.kind, item.key, item.extra.get("root_id", ""))
            if marker in seen:
                continue
            seen.add(marker)
            result.append(item)
        return result

    @staticmethod
    def _fingerprint(kind: str, key: str, root_id: str = "") -> str:
        return f"{kind}:{key}:{root_id}"

    # ------------------------------------------------------------------
    # 冷却
    # ------------------------------------------------------------------

    def _in_cooldown(self, umo: str, marker: str) -> bool:
        window = int(self._opt("basic", "cooldown_seconds", 60) or 0)
        if window <= 0:
            return False
        now = time.time()
        # 每五分钟清一次过期项，避免长期运行把字典撑大
        if now - self._cooldown_sweep_at > 300:
            self._cooldown_sweep_at = now
            self._cooldown = {k: v for k, v in self._cooldown.items() if now - v < max(window, 600)}
        last = self._cooldown.get((umo, marker))
        if last is not None and now - last < window:
            return True
        self._cooldown[(umo, marker)] = now
        return False

    # ------------------------------------------------------------------
    # 短链展开
    # ------------------------------------------------------------------

    async def _resolve(self, link: LinkMatch, enabled: set[str]) -> LinkMatch | None:
        """把短链还原成真实目标；无法还原时返回 None。"""
        if link.kind != "short":
            return link
        try:
            url = await self.client.resolve_short(link.raw)
        except BiliError as exc:
            logger.debug("[bili_card] 短链展开失败 %s: %s", link.raw, exc.message)
            return None
        if not url:
            return None
        target = retarget_from_url(url, enabled)
        if target is None:
            logger.debug("[bili_card] 短链指向未启用的类型：%s", url)
            return None
        return LinkMatch(
            kind=target.kind,
            key=target.key,
            raw=link.raw,
            extra={
                "page": target.page,
                "root_id": target.root_id,
                "mode": target.mode,
            },
        )

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------

    async def _render_card(self, card: CardData) -> str:
        """返回渲染后的图片地址；失败时抛异常由上层兜底。"""
        content = {
            "theme": str(self._opt("content", "theme", "light")),
            "show_cover": bool(self._opt("content", "show_cover", True)),
            "show_avatar": bool(self._opt("content", "show_avatar", True)),
            "show_stats": bool(self._opt("content", "show_stats", True)),
            "show_desc": bool(self._opt("content", "show_desc", True)),
            "desc_max_chars": int(self._opt("content", "desc_max_chars", 96) or 96),
        }
        ctx = build_context(card, **content)

        # 模板要整段发给渲染服务，封面内联串过大时对方会超时或截断，
        # 与其拿到半张图，不如主动去掉封面保住文字与数据。
        if len(ctx["cover"]) > _MAX_COVER_CHARS:
            logger.warning(
                "[bili_card] 封面内联过大（%.0fKB），本次不显示封面",
                len(ctx["cover"]) / 1024,
            )
            content["show_cover"] = False
            ctx = build_context(card, **content)

        options = dict(RENDER_OPTIONS)
        timeout = int(self._opt("advanced", "render_timeout", 20) or 20)
        options["timeout"] = timeout * 1000  # Playwright 的 timeout 单位是毫秒

        started = time.perf_counter()
        image = await self.html_render(load_template(), {"c": ctx}, options=options)
        cost = time.perf_counter() - started
        if cost > 8:
            # 慢到用户会以为没反应，留个线索方便排查是哪一段慢
            logger.info("[bili_card] 卡片渲染耗时 %.1fs（%s）", cost, card.kind)
        return image

    def _text_line(self, card: CardData) -> str:
        """卡片后补发的文字：标题 + 原始链接，方便直接复制。"""
        title = card.title if len(card.title) <= 60 else card.title[:59] + "…"
        return f"【{card.badge}】{title}\n{card.link}"

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    async def _emit_links(
        self, event: AstrMessageEvent, links: list[LinkMatch], *, manual: bool
    ) -> AsyncGenerator[Any, None]:
        """统一的"解析并输出"流程，自动与手动共用，避免两份逻辑各自漂移。"""
        enabled = self._enabled_types()
        if not links or not enabled:
            return

        try:
            umo = event.unified_msg_origin
        except Exception:
            umo = ""

        limit = int(self._opt("basic", "parse_limit", 1) or 1)
        limit = max(1, min(limit, 3))
        reply_mode = str(self._opt("basic", "reply_mode", "card_text"))
        show_error = bool(self._opt("basic", "error_hint", True))

        done = 0
        errors: list[str] = []

        for link in links:
            if done >= limit:
                break

            target = await self._resolve(link, enabled)
            if target is None:
                continue

            marker = self._fingerprint(target.kind, target.key, target.extra.get("root_id", ""))
            if not manual and self._in_cooldown(umo, marker):
                logger.debug("[bili_card] 冷却中，跳过 %s", marker)
                continue

            try:
                card = await self.client.fetch(
                    target.kind,
                    target.key,
                    page=target.extra.get("page", "1"),
                    root_id=target.extra.get("root_id", ""),
                    mode=target.extra.get("mode", ""),
                )
            except BiliError as exc:
                errors.append(exc.message)
                logger.info("[bili_card] 解析失败 %s: %s", marker, exc.message)
                continue
            except Exception as exc:  # 单条链接的意外异常不能拖垮整条消息
                errors.append("解析时发生未知错误")
                logger.exception("[bili_card] 解析异常 %s: %s", marker, exc)
                continue

            done += 1

            if reply_mode == "text_only":
                yield event.plain_result(plain_text(card))
                continue

            try:
                image = await self._render_card(card)
                yield event.image_result(image)
            except Exception as exc:
                # 渲染服务不可用时退回文字，至少信息还在
                logger.warning("[bili_card] 卡片渲染失败，退回文字：%s", exc)
                yield event.plain_result(plain_text(card))
                continue

            if reply_mode == "card_text":
                yield event.plain_result(self._text_line(card))

        if done == 0 and errors and show_error:
            yield event.plain_result(f"B站解析失败：{errors[0]}")

    async def _dispatch(
        self, event: AstrMessageEvent, *, manual: bool
    ) -> AsyncGenerator[Any, None]:
        """自动解析入口：先收集文本与卡片里的链接，再交给统一流程。"""
        enabled = self._enabled_types()
        if not enabled:
            return
        links = self._collect_links(event, enabled)
        async for result in self._emit_links(event, links, manual=manual):
            yield result

    # ------------------------------------------------------------------
    # 事件入口
    # ------------------------------------------------------------------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """自动解析：聊天里出现 B 站链接就出卡片。"""
        if not bool(self._opt("basic", "auto_parse", True)):
            return
        if self._blocked(event):
            return
        # 忽略自己发的消息，避免自触发
        try:
            if event.get_sender_id() and event.get_sender_id() == event.get_self_id():
                return
        except Exception:
            pass

        sent = False
        async for result in self._dispatch(event, manual=False):
            sent = True
            yield result

        if sent:
            # 已处理就不再让后面的插件和 LLM 重复响应同一条链接
            event.stop_event()

    @filter.command("bili", alias={"解析"})
    async def bili_command(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """手动解析：/bili <链接或BV号>。"""
        if self._blocked(event):
            return

        text = getattr(event, "message_str", "") or ""
        for token in ("/bili", "bili", "/解析", "解析"):
            if text.startswith(token):
                text = text[len(token) :].strip()
                break
        if not text:
            yield event.plain_result("用法：/bili <B站链接或BV号>\n例如：/bili BV1xx411c7mD")
            return

        # 手动模式下直接把输入当文本重新识别，不受自动解析开关影响
        sent = False
        async for result in self._dispatch_text(event, text, manual=True):
            sent = True
            yield result
        if not sent:
            yield event.plain_result("没有识别到可解析的 B 站链接。")

    async def _dispatch_text(
        self, event: AstrMessageEvent, text: str, *, manual: bool
    ) -> AsyncGenerator[Any, None]:
        """手动指令用：只按给定文本识别，不读卡片。"""
        links = detect_links(text, self._enabled_types())
        async for result in self._emit_links(event, links, manual=manual):
            yield result

    @filter.command("bilihelp")
    async def bili_help(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """查看插件状态与用法。"""
        enabled = self._enabled_types()
        type_names = {
            "video": "视频",
            "bangumi": "番剧",
            "live": "直播",
            "article": "专栏",
            "opus": "动态",
            "space": "UP空间",
            "audio": "音频",
            "comment": "评论",
            "short_link": "短链",
            "json_card": "小程序卡片",
        }
        on = "、".join(type_names[t] for t in ALL_TYPES if t in enabled) or "（无）"
        yield event.plain_result(
            f"B站卡片解析 {PLUGIN_VERSION}\n"
            f"自动解析：{'开' if self._opt('basic', 'auto_parse', True) else '关'}\n"
            f"已启用类型：{on}\n"
            f"用法：/bili <链接或BV号>，或直接发链接"
        )

    async def terminate(self) -> None:
        """插件卸载/重载时释放 HTTP 会话。"""
        await self.client.close()
