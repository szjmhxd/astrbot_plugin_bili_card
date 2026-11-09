"""加载冒烟测试：不启动 AstrBot，验证插件能被正确加载并跑通消息流程。

为什么需要它
------------
``main.py`` 的 ``@filter`` 装饰器、``metadata.yaml`` 字段、``html_render`` 调用方式只要有一处
写错，插件在 AstrBot 里就会加载失败——而单元测试（tests/）不碰这些边界。
本工具用桩替身模拟 AstrBot 的几个核心对象，在纯 Python 环境里跑一遍完整链路：

    消息 -> 链接识别 -> 接口调用(桩) -> 卡片渲染(桩) -> 出图 + 文字

用法
----
    python tools/smoke_test.py
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

# 需要把「插件的父目录」加进路径，才能 import astrbot_plugin_bili_card
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PLUGIN_ROOT.parent))

_FAILED: list[str] = []


def check(label: str, ok: bool) -> None:
    print(f"  {'✓' if ok else '✗'} {label}")
    if not ok:
        _FAILED.append(label)


def _load_metadata() -> dict[str, str]:
    """读 metadata.yaml 的顶层标量字段。

    AstrBot 以这个文件为准识别插件元数据，所以这里直接解析它，
    而不是从代码里的常量推断——常量写对了但 yaml 写错，线上一样出错。
    只处理 ``key: value`` 与行内注释，够用且不引入 PyYAML 依赖。
    """
    meta: dict[str, str] = {}
    path = _PLUGIN_ROOT / "metadata.yaml"
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith(("#", "-")):
            continue
        if line[0] in " \t":  # 缩进行属于列表或嵌套结构
            continue
        key, _, value = line.partition(":")
        value = value.strip()
        if value[:1] in ("'", '"'):
            quote = value[0]
            end = value.find(quote, 1)
            value = value[1:end] if end > 0 else value[1:]
        else:
            value = value.split(" #", 1)[0].strip()
        meta[key.strip()] = value
    return meta


# ---------------------------------------------------------------------------
# 桩替身
# ---------------------------------------------------------------------------


class _Logger:
    def info(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def exception(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


def _install_stubs() -> dict:
    """在 sys.modules 里装一套最小的 astrbot 假实现。"""
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = _Logger()

    event_mod = types.ModuleType("astrbot.api.event")

    class AstrMessageEvent:
        pass

    class EventMessageType:
        ALL = "all"
        PRIVATE_MESSAGE = "private"
        GROUP_MESSAGE = "group"

    class _Filter:
        """装饰器原样返回函数，只记录调用参数以便断言。"""

        def __init__(self):
            self.commands: list[dict] = []

        def command(self, name, **kw):
            self.commands.append({"name": name, **kw})

            def deco(fn):
                return fn

            return deco

        def event_message_type(self, *a, **k):
            def deco(fn):
                return fn

            return deco

    flt = _Filter()
    flt.EventMessageType = EventMessageType
    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.filter = flt

    star_mod = types.ModuleType("astrbot.api.star")
    state: dict = {}

    class Context:
        pass

    class Star:
        def __init__(self, context):
            self.context = context

        async def html_render(self, tmpl, data, options=None, **kw):
            state["render"] = (tmpl, data, options)
            return "/tmp/smoke-card.png"

    def register(name, author, desc, version, repo):
        def deco(cls):
            state["class"] = cls
            state["meta"] = (name, author, desc, version, repo)
            return cls

        return deco

    star_mod.Context = Context
    star_mod.Star = Star
    star_mod.register = register

    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.star"] = star_mod
    state["filter"] = flt
    return state


class _Result:
    def __init__(self, kind, payload):
        self.kind = kind
        self.payload = payload


class _MessageObj:
    def __init__(self, message):
        self.message = message


class FakeEvent:
    """只实现插件实际用到的那些方法。"""

    def __init__(self, text, message=None, group_id="123456"):
        self.message_str = text
        self.message_obj = _MessageObj(message or [])
        self.unified_msg_origin = f"aiocqhttp:GroupMessage:{group_id}"
        self.stopped = False
        self._group = group_id

    def get_sender_id(self):
        return "10001"

    def get_self_id(self):
        return "99999"

    def get_group_id(self):
        return self._group

    def plain_result(self, t):
        return _Result("plain", t)

    def image_result(self, p):
        return _Result("image", p)

    def chain_result(self, c):
        return _Result("chain", c)

    def stop_event(self):
        self.stopped = True


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


async def _run() -> None:
    state = _install_stubs()
    from astrbot_plugin_bili_card import main as plugin_main
    from astrbot_plugin_bili_card.core.models import BiliError, CardData

    print("[1] 元数据与类注册")
    meta = _load_metadata()
    check(f"插件名 {meta.get('name')}", meta.get("name") == "astrbot_plugin_bili_card")
    check(f"版本号带 v 前缀 {meta.get('version')}", str(meta.get("version", "")).startswith("v"))
    check(
        "六项必填齐备",
        all(meta.get(k) for k in ("name", "display_name", "desc", "version", "author", "repo")),
    )
    check("repo 指向 szjmhxd 账号", "szjmhxd" in meta.get("repo", ""))
    plugin_cls = plugin_main.BiliCardPlugin
    check("继承 Star", issubclass(plugin_cls, sys.modules["astrbot.api.star"].Star))
    check("类文档字符串存在（作为 /plugin 帮助信息）", bool((plugin_cls.__doc__ or "").strip()))
    check("html_render 存在", hasattr(plugin_cls, "html_render"))

    print("[2] 装饰器注册")
    commands = {c["name"] for c in state["filter"].commands}
    check(f"/bili 已注册 {sorted(commands)}", "bili" in commands)
    check("/bilihelp 已注册", "bilihelp" in commands)
    check("有别名", any(c.get("alias") for c in state["filter"].commands))

    config = {
        "basic": {"auto_parse": True, "reply_mode": "card_text", "parse_limit": 1},
        "types": {"video": True, "short_link": True, "json_card": True},
        "content": {"theme": "light"},
        "advanced": {},
    }
    plugin = plugin_cls(state_class_context(), config)
    check("实例化成功", plugin is not None)
    check(
        "配置读取正确",
        plugin._opt("basic", "parse_limit", 99) == 1
        and plugin._opt("basic", "auto_parse", False) is True,
    )
    check("缺省配置回落", plugin._opt("basic", "no_such_key", "dft") == "dft")

    async def fake_fetch(kind, key, **kw):
        return CardData(
            kind=kind,
            title=f"假标题 {key}",
            author="UP主",
            link=f"https://www.bilibili.com/video/{key}",
            link_label=key,
        )

    plugin.client.fetch = fake_fetch

    print("[3] 自动解析")
    ev = FakeEvent("看这个 https://www.bilibili.com/video/BV1xx411c7mD 好看")
    out = [r async for r in plugin.on_any_message(ev)]
    check(f"输出图+文 {[r.kind for r in out]}", [r.kind for r in out] == ["image", "plain"])
    check("事件已停止传播", ev.stopped is True)
    check("图片后那条带原始链接", "BV1xx411c7mD" in out[-1].payload)

    tmpl, data, options = state["render"]
    check("模板是 Jinja 语法", "{{ c.title | e }}" in tmpl)
    check("文本插值全部转义", "{{ c.title }}" not in tmpl and "{{ c.desc }}" not in tmpl)
    check("渲染数据带 c 键", "c" in data and data["c"]["theme"] == "light")
    # 模板把主题类挂在 <body> 上、写成 {{ theme }}（顶层变量）。
    # 少传这一项不会报错，只会让深色主题静默失效——所以必须显式校验。
    check("主题以顶层变量传入", data.get("theme") == data["c"]["theme"])
    check("评论开关已接线", "comments" in data["c"])
    check("渲染为 PNG 且透明底", options["type"] == "png" and options["omit_background"] is True)

    print("[4] 冷却去重")
    ev2 = FakeEvent("再看 https://www.bilibili.com/video/BV1xx411c7mD")
    out2 = [r async for r in plugin.on_any_message(ev2)]
    check("同会话重复链接被跳过", out2 == [])

    print("[5] 小程序卡片 -> 短链 -> 直播")
    plugin._in_cooldown = lambda *a, **k: False

    class Json:
        def __init__(self, data):
            self.data = data

    async def fake_short(url):
        return "https://live.bilibili.com/22637261"

    plugin.client.resolve_short = fake_short
    ev3 = FakeEvent("", message=[Json({"qqdocurl": "https://b23.tv/abcDEF"})])
    out3 = [r async for r in plugin.on_any_message(ev3)]
    check(f"卡片解析出图 {[r.kind for r in out3]}", bool(out3) and out3[0].kind == "image")

    print("[6] dict 形式的卡片组件")
    ev4 = FakeEvent(
        "", message=[Json({"app": "x", "jumpUrl": "https://www.bilibili.com/video/BV1yy411c7mE"})]
    )
    out4 = [r async for r in plugin.on_any_message(ev4)]
    check("能从 dict 中抽出链接", bool(out4))

    print("[7] 出图预算与重试")
    # 官方 t2i 端点池里有快有慢，而 AstrBot 只在请求**报错**时换端点，
    # 碰到"慢但最终成功"的端点会一直等（现场出现过 48s）。所以插件自己切预算：
    # 第一次用六成，超时后换端点再用剩下的四成。
    caps = plugin_main._attempt_caps(20)
    wall = sum(caps) + 2 * plugin_main._ATTEMPT_MARGIN_SECONDS
    check(f"总预算切成两次 {caps}，墙钟 {wall}s 不超 20s", caps == [10, 8] and wall == 20)
    check("递减：第一次给得比第二次宽", caps[0] > caps[1])
    check("最小预算不会被切到 3 秒以下", all(c >= 3 for c in plugin_main._attempt_caps(6)))
    check("超大预算被夹在 120 秒内", sum(plugin_main._attempt_caps(9999)) <= 120)

    attempts = {"n": 0}
    asked: list[int] = []

    async def flaky_render(tmpl, data, options=None, **kw):
        attempts["n"] += 1
        asked.append(options["timeout"])
        if attempts["n"] == 1:
            raise asyncio.TimeoutError
        return "/tmp/smoke-card.png"

    plugin.html_render = flaky_render
    path = await plugin._render_card(CardData(kind="video", title="t", link="x"))
    check(f"超时后重试成功（共 {attempts['n']} 次）", path.endswith(".png") and attempts["n"] == 2)
    check(f"每次请求的超时预算递减 {asked}", len(asked) == 2 and asked[0] > asked[1])

    async def always_slow(tmpl, data, options=None, **kw):
        raise asyncio.TimeoutError

    plugin.html_render = always_slow
    try:
        await plugin._render_card(CardData(kind="video", title="t", link="x"))
    except TimeoutError:
        check("两次都超时则抛出，由上层退回文字", True)
    else:
        check("两次都超时则抛出，由上层退回文字", False)

    print("[8] 异常兜底")

    async def boom(*a, **k):
        raise BiliError("测试错误")

    plugin.client.fetch = boom
    ev5 = FakeEvent("https://www.bilibili.com/video/BV1zz411c7mF")
    out5 = [r async for r in plugin.on_any_message(ev5)]
    check(f"失败给出提示 {[r.payload for r in out5]}", bool(out5) and "测试错误" in out5[0].payload)

    print("[9] 群屏蔽")
    config["advanced"]["blocked_groups"] = ["123456"]
    ev6 = FakeEvent("https://www.bilibili.com/video/BV1aa411c7mG")
    out6 = [r async for r in plugin.on_any_message(ev6)]
    check("屏蔽群内无响应", out6 == [])

    print("[10] 资源释放")
    await plugin.terminate()
    check("HTTP 会话已关闭", plugin.client._session is None)


def state_class_context():
    """拿到桩里定义的 Context 实例。"""
    return sys.modules["astrbot.api.star"].Context()


def main() -> int:
    asyncio.run(_run())
    print()
    if _FAILED:
        print(f"失败 {len(_FAILED)} 项：")
        for item in _FAILED:
            print(f"  - {item}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
