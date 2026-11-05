"""本地预览工具：不启动 AstrBot 也能看到卡片长什么样。

用途
----
调整 ``templates/card.html`` 之后，用它快速出图对版式，省得每次都重启 AstrBot。

用法
----
    python tools/preview.py                  # 生成八类样张到 preview/
    python tools/preview.py --kind video     # 只出视频卡片
    python tools/preview.py --theme dark     # 深色主题
    python tools/preview.py --shot           # 顺便截图成 PNG

截图需要 Chromium 系浏览器（Chrome / Edge / Playwright Chromium 任一），
找不到时只输出 HTML，你可以直接用浏览器打开。

关于依赖
--------
本工具自带一个极简的 Jinja 子集渲染器，只支持 card.html 用到的语法：
``{{ 表达式 }}``、``{% if / elif / else / endif %}``、``{% for %}``、``{# 注释 #}``。
因此**不需要安装 jinja2**。真实运行时由 AstrBot 内置的 Jinja2 负责渲染，
这里只是为了让你本地能预览。
"""

from __future__ import annotations

import argparse
import base64
import html
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from core.models import CardData, Stat
from core.renderer import build_context, load_template

# ---------------------------------------------------------------------------
# 极简 Jinja 子集渲染器（仅本工具使用）
# ---------------------------------------------------------------------------


class _AttrDict(dict):
    """支持 ``c.title`` 这种点号访问的字典。"""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None


def _wrap(value: Any) -> Any:
    if isinstance(value, dict):
        return _AttrDict({k: _wrap(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_wrap(v) for v in value]
    return value


_TOKEN = re.compile(r"(\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\})", re.DOTALL)


def _eval(expr: str, ns: dict[str, Any]) -> Any:
    """在受限命名空间里求值。模板只用到比较、and/or、就地条件表达式与少量过滤器。"""
    base, *filters = [part.strip() for part in expr.split("|")]
    try:
        value = eval(base, {"__builtins__": {}}, ns)
    except Exception:
        return ""
    if value is None:
        value = ""
    for name in filters:
        # 只实现模板里实际用到的过滤器，其余原样透传
        if name.split("(")[0].strip() in ("e", "escape"):
            value = html.escape(str(value))
    return value


def _parse(tokens: list[str], i: int = 0) -> tuple[list[Any], int, str, str]:
    nodes: list[Any] = []
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("{#"):
            i += 1
            continue
        if tok.startswith("{{"):
            nodes.append(("expr", tok[2:-2].strip()))
            i += 1
            continue
        if tok.startswith("{%"):
            inner = tok[2:-2].strip()
            parts = inner.split(None, 1)
            head = parts[0] if parts else ""
            rest = parts[1] if len(parts) > 1 else ""

            if head in ("endif", "endfor", "else", "elif"):
                return nodes, i, head, rest

            if head == "if":
                branches: list[tuple[str, list[Any]]] = []
                else_body: list[Any] = []
                cond = rest
                cur = i + 1
                while True:
                    body, j, marker, tail = _parse(tokens, cur)
                    branches.append((cond, body))
                    if marker == "elif":
                        cond = tail
                        cur = j + 1
                        continue
                    if marker == "else":
                        else_body, k, _m, _t = _parse(tokens, j + 1)
                        i = k
                        break
                    i = j
                    break
                nodes.append(("if", branches, else_body))
                i += 1
                continue

            if head == "for":
                m = re.match(r"(\w+)\s+in\s+(.+)", rest)
                if m:
                    body, j, _m2, _t2 = _parse(tokens, i + 1)
                    nodes.append(("for", m.group(1), m.group(2), body))
                    i = j + 1
                    continue
            i += 1
            continue

        nodes.append(("text", tok))
        i += 1
    return nodes, i, "", ""


def _render_nodes(nodes: list[Any], ns: dict[str, Any]) -> str:
    out: list[str] = []
    for node in nodes:
        kind = node[0]
        if kind == "text":
            out.append(node[1])
        elif kind == "expr":
            out.append(str(_eval(node[1], ns)))
        elif kind == "if":
            for cond, body in node[1]:
                if _eval(cond, ns):
                    out.append(_render_nodes(body, ns))
                    break
            else:
                out.append(_render_nodes(node[2], ns))
        elif kind == "for":
            _, var, expr, body = node
            seq = _eval(expr, ns) or []
            for item in seq:
                sub = dict(ns)
                sub[var] = _wrap(item)
                out.append(_render_nodes(body, sub))
    return "".join(out)


def render_template(template: str, context: dict[str, Any]) -> str:
    tokens = _TOKEN.split(template)
    nodes, _i, _m, _t = _parse(tokens)
    ns = {"c": _wrap(context), "theme": context.get("theme", "light")}
    return _render_nodes(nodes, ns)


# ---------------------------------------------------------------------------
# 样张数据
# ---------------------------------------------------------------------------


def _img(width: int, height: int, top: tuple[int, int, int], bottom: tuple[int, int, int]) -> str:
    """用 PIL 画一张渐变占位图，避免预览依赖网络。"""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (width, height), top)
    draw = ImageDraw.Draw(image)
    for y in range(height):
        ratio = y / max(1, height - 1)
        color = tuple(int(top[c] + (bottom[c] - top[c]) * ratio) for c in range(3))
        draw.line([(0, y), (width, y)], fill=color)
    # 加两个色块，方便看出裁切是否正常
    draw.ellipse(
        [width * 0.62, height * 0.18, width * 0.94, height * 0.86],
        fill=(255, 255, 255, 40),
    )
    draw.rectangle(
        [width * 0.06, height * 0.62, width * 0.34, height * 0.88],
        outline=(255, 255, 255),
        width=max(2, width // 160),
    )
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=88)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _cover() -> str:
    return _img(660, 371, (92, 132, 214), (33, 48, 92))


def _avatar() -> str:
    return _img(160, 160, (250, 148, 176), (168, 62, 104))


def samples() -> dict[str, CardData]:
    cover, avatar = _cover(), _avatar()
    base = "https://www.bilibili.com/video/BV1GJ411x7h7"
    return {
        "video": CardData(
            kind="video",
            title="终于知道为什么听到某些歌，反派会愣住了",
            subtitle="P1/3 · 第一集：童年回声",
            cover=cover,
            avatar=avatar,
            author="一罐蠢乃酱",
            desc="有些旋律一响起来，画面就自动回来了。这一期聊聊那些年我们反复循环的 BGM，"
            "以及它们为什么能精准击中某一段记忆。",
            stats=[
                Stat("播放", "359.4万"),
                Stat("点赞", "28.5万"),
                Stat("投币", "4.1万"),
                Stat("收藏", "13.0万"),
                Stat("评论", "2422"),
                Stat("分享", "1.2万"),
            ],
            tags=["音乐", "12:34", "原创"],
            link=base,
            link_label="BV1GJ411x7h7",
        ),
        "bangumi": CardData(
            kind="bangumi",
            title="葬送的芙莉莲",
            subtitle="第 28 话 · 又要踏上旅途",
            cover=cover,
            desc="魔王讨伐结束后，精灵魔法使芙莉莲重新踏上理解人类的旅程。",
            stats=[
                Stat("播放", "2.1亿"),
                Stat("追番", "896.3万"),
                Stat("点赞", "142.7万"),
                Stat("投币", "88.4万"),
                Stat("弹幕", "512.9万"),
                Stat("分享", "12.1万"),
            ],
            tags=["番剧", "9.6分", "更新至第28话"],
            link="https://www.bilibili.com/bangumi/play/ep1234567",
            link_label="番剧详情",
        ),
        "live": CardData(
            kind="live",
            title="【突击】一起看 jojo 吧",
            cover=cover,
            avatar=avatar,
            author="嘉然今天吃什么",
            desc="今天状态不错，随便聊聊，顺便把上次没看完的补上。",
            highlight="● 直播中",
            highlight_kind="live",
            stats=[Stat("人气", "12.4万"), Stat("关注", "186.5万")],
            tags=["房间 22637261"],
            link="https://live.bilibili.com/22637261",
            link_label="进入直播间",
        ),
        "article": CardData(
            kind="article",
            title="关于新版本排版的碎碎念",
            cover=cover,
            avatar=avatar,
            author="夜航工作室",
            desc="这次更新主要调整了排版与过滤逻辑，顺手把几个长期存在的对齐问题一并处理了。",
            stats=[
                Stat("阅读", "2.0万"),
                Stat("点赞", "1834"),
                Stat("投币", "412"),
                Stat("收藏", "976"),
                Stat("评论", "231"),
                Stat("分享", "88"),
            ],
            tags=["专栏"],
            link="https://www.bilibili.com/read/cv12345678",
            link_label="cv12345678",
        ),
        "opus": CardData(
            kind="opus",
            title="时间流转，与你的心意依旧在舞台灯光亮起的此刻共鸣",
            cover=cover,
            avatar=avatar,
            author="A-SOUL_Official",
            desc="本次演唱会流程安排及场贩情报公开，请小伙伴们仔细阅读！\n"
            "10月24日-10月25日，和小伙伴们不见不散~",
            stats=[Stat("点赞", "4.2万"), Stat("评论", "3120"), Stat("转发", "8867")],
            tags=["动态", "图文"],
            link="https://www.bilibili.com/opus/1259699575671750656",
            link_label="查看动态",
        ),
        "space": CardData(
            kind="space",
            title="影视飓风",
            subtitle="用影像记录世界",
            cover=avatar,
            author="UID 946974",
            highlight="LV6",
            stats=[Stat("粉丝", "1289.4万"), Stat("投稿", "1268")],
            tags=["UP主"],
            link="https://space.bilibili.com/946974",
            link_label="进入空间",
        ),
        "audio": CardData(
            kind="audio",
            title="夜航星 (Live)",
            subtitle="歌手：三无 Marblue",
            cover=cover,
            author="三无Marblue",
            stats=[
                Stat("播放", "86.3万"),
                Stat("收藏", "4.8万"),
                Stat("投币", "1.2万"),
                Stat("分享", "3421"),
            ],
            tags=["音频", "4:12"],
            link="https://www.bilibili.com/audio/au123456",
            link_label="au123456",
        ),
        "comment": CardData(
            kind="comment",
            title="终于知道为什么听到某些歌，反派会愣住了",
            subtitle="@摸鱼的老王 的评论",
            cover=avatar,
            author="摸鱼的老王",
            desc="这段 BGM 一出来我直接破防了，小时候的夏天就是这种声音。\n"
            "└ 阿卷：同感，前奏三秒就知道是哪首\n"
            "└ 路人甲：建议出个合集",
            stats=[Stat("点赞", "2.4万"), Stat("回复", "386")],
            tags=["评论"],
            link="https://www.bilibili.com/video/BV1GJ411x7h7#reply123456",
            link_label="查看评论",
        ),
    }


# ---------------------------------------------------------------------------
# 截图
# ---------------------------------------------------------------------------

_BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


def find_browser() -> str | None:
    env = os.environ.get("BILI_CARD_BROWSER")
    if env and Path(env).exists():
        return env
    for path in _BROWSERS:
        if Path(path).exists():
            return path
    for name in ("chrome", "chromium", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    return None


def screenshot(html_path: Path, png_path: Path, browser: str) -> bool:
    """用无头浏览器截图，并按 alpha 通道裁掉多余留白。"""
    profile = Path(tempfile.mkdtemp(prefix="bili_card_"))
    cmd = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-first-run",
        "--no-default-browser-check",
        "--force-device-scale-factor=2",
        "--window-size=700,2200",
        "--default-background-color=00000000",
        f"--user-data-dir={profile}",
        f"--screenshot={png_path}",
        html_path.as_uri(),
    ]
    try:
        subprocess.run(cmd, capture_output=True, timeout=90, check=False)
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"  截图失败：{exc}")
        return False
    finally:
        shutil.rmtree(profile, ignore_errors=True)

    if not png_path.exists():
        return False

    try:
        from PIL import Image

        with Image.open(png_path) as image:
            image = image.convert("RGBA")
            bbox = image.getbbox()
            if bbox:
                pad = 16
                box = (
                    max(0, bbox[0] - pad),
                    max(0, bbox[1] - pad),
                    min(image.width, bbox[2] + pad),
                    min(image.height, bbox[3] + pad),
                )
                image.crop(box).save(png_path)
    except Exception as exc:  # 裁切失败不影响出图
        print(f"  裁切跳过：{exc}")
    return True


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 B 站卡片预览")
    parser.add_argument("--kind", default="", help="只生成指定类型")
    parser.add_argument("--theme", default="light", choices=["light", "dark"])
    parser.add_argument("--out", default="preview", help="输出目录")
    parser.add_argument("--shot", action="store_true", help="同时截图成 PNG")
    parser.add_argument("--dark-both", action="store_true", help="两种主题都出")
    args = parser.parse_args()

    template = load_template()
    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = _PLUGIN_ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    data = samples()
    kinds = [args.kind] if args.kind else list(data)
    themes = ["light", "dark"] if args.dark_both else [args.theme]

    browser = find_browser() if args.shot else None
    if args.shot and not browser:
        print("未找到可用浏览器，只输出 HTML。可用 BILI_CARD_BROWSER 指定路径。")

    for theme in themes:
        for kind in kinds:
            if kind not in data:
                print(f"跳过未知类型：{kind}")
                continue
            card = data[kind]
            # 空间卡片用方形头像，其他类型给渐变封面
            ctx = build_context(card, theme=theme)
            html = render_template(template, ctx)

            name = f"{kind}_{theme}"
            html_path = out_dir / f"{name}.html"
            html_path.write_text(html, encoding="utf-8")
            line = f"  {html_path.relative_to(_PLUGIN_ROOT)}"

            if browser:
                png_path = out_dir / f"{name}.png"
                if screenshot(html_path, png_path, browser):
                    line += f"  ->  {png_path.name}"
            print(line)

    print(f"\n完成，输出目录：{out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
