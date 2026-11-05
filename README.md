# B站卡片解析

自动识别聊天里的 B 站链接，渲染成一张卡片图，再补一条可复制的链接文字。

覆盖八类内容：视频、番剧、直播、专栏、动态、UP 主空间、音频、视频评论。

![视频卡片](docs/sample-video.png)

## 和其他 B 站解析插件的区别

| | 本插件 | 典型实现 |
|---|---|---|
| 额外依赖 | **无**。借用 AstrBot 内置的 `html_render()` | 常需自行安装 Playwright + Chromium |
| 卡片形态 | 截图成图，16:9 封面 + 数据宫格 + 标签 | 多为文本或 emoji 排版 |
| 内容类型 | 八类，可逐类开关 | 通常只做视频 |
| 是否调模型 | **完全不调**。无 AI 总结、无语音转写 | 部分插件会顺带接 LLM |
| 卡片之外 | 补发一条带原始链接的文字 | 链接常常只存在于图片里，点不动 |

不发 AI 总结是刻意的：解析是确定性任务，多接一层模型只会增加延迟、费用和不确定性。需要视频内容总结的，应该用专门的插件。

「无额外依赖」的确切含义是：渲染交给宿主 `Star.html_render()`，插件自己既不带 Playwright，也不带 Chromium 和字体。但要注意，`html_render()` 走的是 **AstrBot 配置的文转图端点**（默认是 AstrBot 官方的 `t2i.soulter.top`），并非本机渲染——卡片是那台服务器截出来的图。介意卡片文本离开本机的，可以在 AstrBot 配置里把 `t2i_endpoint` 指向自建渲染服务。

## 支持的链接

| 类型 | 示例 |
|---|---|
| 视频 | `bilibili.com/video/BV...`、`av12345`、裸 `BV1xx411c7mD` |
| 番剧 | `bangumi/play/ep...`、`play/ss...`、`media/md...` |
| 直播间 | `live.bilibili.com/22637261` |
| 专栏 | `bilibili.com/read/cv...` |
| 动态 | `bilibili.com/opus/...`、`t.bilibili.com/...` |
| UP 主空间 | `space.bilibili.com/946974` |
| 音频 / 歌单 | `bilibili.com/audio/au...`、`am...` |
| 视频评论 | `video/BV...?comment_root_id=...` |
| 短链 | `b23.tv`、`bili2233.cn` 等，自动展开后再分派 |
| QQ 小程序卡片 | 从 JSON / XML 卡片里提取隐藏链接 |

## 安装

需要 AstrBot `>= 4.16`（`html_render` 的版本要求）。**不需要安装任何额外 Python 依赖。**

在 WebUI 插件管理页通过仓库地址安装，或把本目录放到 `AstrBot/data/plugins/astrbot_plugin_bili_card/` 后重载。

Docker 部署要注意一件事：**容器里必须有中文字体**，否则卡片上的中文会变成方块。

```bash
apt-get update && apt-get install -y fonts-noto-cjk
```

渲染走的是 AstrBot 自己的 Playwright 进程，所以宿主能渲染文转图，这个插件就能出卡片。

## 配置

全部在 WebUI 插件配置页修改，保存后重载生效。

### 基础

| 配置 | 默认 | 说明 |
|---|---|---|
| `auto_parse` | 开 | 关闭后只响应 `/bili` 指令 |
| `reply_mode` | `card_text` | `card_text` 图 + 文字；`card_only` 只发图；`text_only` 只发文字 |
| `parse_limit` | 1 | 单条消息最多解析几个链接（1-3） |
| `cooldown_seconds` | 60 | 同一会话重复分享同一内容的冷却，0 关闭 |
| `error_hint` | 开 | 失败时是否在聊天里说明原因 |

### 解析类型

`types` 分组下十个开关，默认全开：`video` `bangumi` `live` `article` `opus` `space` `audio` `comment` `short_link` `json_card`。

关掉某一类后，该类链接完全不触发，不会出现"解析了一半"的状态。

### 卡片内容

`show_cover` `show_avatar` `show_stats` `show_desc` `desc_max_chars`（默认 96）、`theme`（`light` / `dark`）。

### 高级

| 配置 | 说明 |
|---|---|
| `cookie_sessdata` | 可选的 `SESSDATA`，只附加到 B 站域名。不配也能解析大部分内容 |
| `proxy_url` | 直连被墙或被风控时使用。SOCKS 需额外装 `aiohttp-socks` |
| `request_timeout` | 单次接口超时，默认 10 秒 |
| `blocked_groups` | 这些群完全不启用插件 |
| `render_timeout` | 单张卡片渲染超时，默认 20 秒 |
| `debug` | 输出链接识别与接口响应的排查日志 |

## 指令

| 指令 | 说明 |
|---|---|
| `/bili <链接或BV号>` | 手动解析，不受自动解析开关限制 |
| `/bilihelp` | 查看版本、开关状态与已启用类型 |

## 卡片样式

六格数据（视频）用两行三列，四格（音频）用两行两列，两格（直播）一行铺满——不会出现半截空行。

| 评论（无封面） | 音频（四格） |
|---|---|
| ![评论卡片](docs/sample-comment.png) | ![音频卡片](docs/sample-audio.png) |

深色主题（`theme: dark`）：

![深色卡片](docs/sample-dark.png)

## 已知限制

**卡片里的链接不可点。** 图片是位图，没有超链接能力。所以 `reply_mode: card_text` 默认会在图后面补一条带原始地址的文字，这是这个默认值存在的原因。

**动态（opus）需要 Cookie 才完整。** B 站对动态接口有风控，未配置 `SESSDATA` 时可能返回 `-352` 或只给简略数据。视频、番剧、专栏、音频不需要 Cookie。

**评论解析只认带 `comment_root_id` 的链接。** 普通 `#reply123456` 锚点无法还原到接口参数。

**大会员专属、充电专属、付费内容不支持。** 接口层面就拿不到播放数据。

**渲染失败会自动退回文字。** 宿主渲染服务异常时不会静默无响应，而是发一条纯文本，信息不丢。卡片渲染依赖宿主的文转图端点，断网或端点不可用时同样走这条退路。

**出图速度取决于宿主的文转图端点。** 插件这边已经压到最省：封面按显示尺寸向图床要缩略图（341KB → 39KB），并跳过了宿主给模板无条件注入的 2.4MB 代码高亮运行时——这两项合计让一次渲染从 20 秒级降到 2~3 秒。如果仍然慢，多半是端点本身排队，换自建端点更快。

**卡片里的文本会随模板提交给文转图端点。** 视频标题、UP 名、简介、评论正文都在其中。这是 `html_render()` 的工作方式，不是本插件额外发送的；要求数据不出内网的部署，请自建渲染端点。

**同一平台不要同时装两个 B 站解析插件。** 两者都会监听同一条消息，结果是机器人对同一个视频回两次。要么只留一个，要么用各自的群名单隔开。

## 开发

```bash
# 单元测试（93 个用例，纯离线）
python -m unittest discover -s tests -v

# 加载冒烟：不启动 AstrBot，验证 @filter / metadata / html_render 的用法正确
python tools/smoke_test.py

# 本地预览卡片（不需要启动 AstrBot，也不需要装 jinja2）
python tools/preview.py --shot
python tools/preview.py --theme dark --kind video --shot

# 静态检查
ruff check . && ruff format --check .
```

`tools/smoke_test.py` 用桩替身模拟 AstrBot 的核心对象，跑通「消息 → 识别 → 取数据 → 渲染 → 出图+文字」整条链路。改完 `main.py` 先跑它，比在 AstrBot 里反复重载快得多。

`tools/preview.py` 自带一个只支持本模板语法的极简渲染器，用 Chromium 系浏览器截图，方便调版式。它只用于开发，不参与插件运行。

项目结构：

```
astrbot_plugin_bili_card/
├── main.py              # Star 子类，事件入口与流程编排
├── core/
│   ├── api.py           # 八类接口客户端、图片内联
│   ├── wbi.py           # WBI 参数签名
│   ├── link_parser.py   # 链接识别与类型分派
│   ├── models.py        # CardData 等数据模型
│   └── renderer.py      # 卡片上下文构建
├── templates/card.html  # 卡片版式（Jinja2）
├── tools/preview.py     # 本地预览
└── tests/               # 单元测试
```

改版式只需要动 `templates/card.html`，改数据取舍动 `core/renderer.py`，两者职责不重叠。

## 参考与致谢

本插件是独立实现，没有复制任何现有插件的代码。下列项目与资料影响了设计取舍，在此说明来源：

- **AstrBot 官方文档** — [插件开发指南](https://docs.astrbot.app/dev/star/plugin-new.html) 与 [文转图](https://docs.astrbot.app/dev/star/guides/html-to-pic.html)。卡片走 `Star.html_render()` 这条路径来自官方文档，不是自行摸索出的偏方。
- **[jkfujr/astrbot_plugin_bili_parser](https://github.com/jkfujr/astrbot_plugin_bili_parser)** — 它把 B 站链接拆成视频/直播/番剧/专栏/动态/空间/音频八类分别处理，并给出每类的独立开关。本插件的类型划分与「按类型分别启停」的配置形态参照了这个思路。
- **[muyuawa/astrbot_plugin_bilicard](https://github.com/muyuawa/astrbot_plugin_bilicard)** — 证明了「B 站信息值得用截图卡片呈现」这件事，也是本插件坚持保留 16:9 封面、时长角标、数据宫格这一视觉结构的起因。它的封面/头像必须内联才能绕过防盗链，这一点本插件用同样的办法解决。
- **[Volundio/astrbot-plugin-bilibili-analysis](https://github.com/Volundio/astrbot-plugin-bilibili-analysis)** — 参考了它对「同一消息里多种来源（完整链接 / 仅 BV 号 / 短链 / 卡片）」分别开关的做法。

B 站 WBI 签名的置换表与算法属于公开逆向结果，本仓库中的实现依据公开资料重写，未取用上述任何项目的具体代码。`LICENSE` 为 MIT；上述项目各自适用自己的许可协议（其中两个是 AGPL-3.0），如需借鉴它们的代码，请遵守各自的协议。

## 许可证

MIT。B 站接口的可用性与字段稳定性不由本项目控制，接口变更可能导致部分内容解析失败。
