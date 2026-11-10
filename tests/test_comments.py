"""热门评论的取值规则。

这里锁住两个容易回退的点：

1. **评论区的 ``type`` 不是常量**。视频/番剧是 1、专栏是 12、纯图文动态是 17，
   而且动态要按 ``basic.comment_type`` + ``basic.comment_id_str`` 取——转载专栏
   的动态，评论实际挂在 cv 号上，拿动态 id 硬套 17 会直接 -404。
2. **评论正文是 HTML 实体转义过的**，模板输出时还会再过一遍 ``|e``，
   不在取值阶段还原，卡片上就会印出 ``&amp;#34;`` 这种东西。
"""

from __future__ import annotations

import unittest
from typing import Any

from core.api import _ERR_TEXT, BiliClient
from core.models import BiliError
from tests.stubs import QUIET_LOGGER


def reply_row(user: str, text: str, like: int = 10) -> dict[str, Any]:
    return {"member": {"uname": user}, "content": {"message": text}, "like": like}


# 匿名请求 /nav 的真实返回：code=-101，但 wbi_img 照样给
NAV_ANON_OK: dict[str, Any] = {
    "code": -101,
    "data": {
        "wbi_img": {
            "img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
            "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png",
        }
    },
}


class FakeClient(BiliClient):
    """把网络出口换成记录器，其余逻辑全部走真实代码。"""

    def __init__(self, responses: dict[str, dict[str, Any]], **kwargs: Any) -> None:
        kwargs.setdefault("logger", QUIET_LOGGER)
        super().__init__(**kwargs)
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def _request_json(self, url, params=None, *, wbi=False, ok_codes=(0,)):  # type: ignore[override]
        self.calls.append((url, dict(params or {})))
        for suffix, payload in self.responses.items():
            if url.endswith(suffix):
                code = payload.get("code", 0)
                if code not in ok_codes:
                    raise BiliError(_ERR_TEXT.get(code, f"B 站接口错误（code={code}）"))
                return payload
        raise AssertionError(f"未预期的请求：{url}")

    async def inline_image(self, url: str, size=None):  # type: ignore[override]
        return "data:image/jpeg;base64,STUB"


class TopCommentsTest(unittest.IsolatedAsyncioTestCase):
    async def test_uses_given_type_and_oid(self):
        client = FakeClient(
            {
                "/x/v2/reply": {
                    "code": 0,
                    "data": {"replies": [reply_row("甲", "第一条")]},
                }
            }
        )
        out = await client._top_comments(43254098, 12)
        self.assertEqual(len(out), 1)
        _, params = client.calls[0]
        self.assertEqual(params["type"], 12)
        self.assertEqual(params["oid"], 43254098)
        self.assertEqual(params["sort"], 2)

    async def test_skips_request_without_oid(self):
        client = FakeClient({})
        self.assertEqual(await client._top_comments(""), [])
        self.assertEqual(await client._top_comments(None), [])
        self.assertEqual(client.calls, [])

    async def test_disabled_when_count_is_zero(self):
        client = FakeClient({}, comment_count=0)
        self.assertEqual(await client._top_comments(123, 1), [])
        self.assertEqual(client.calls, [])

    async def test_html_entities_unescaped(self):
        client = FakeClient(
            {
                "/x/v2/reply": {
                    "code": 0,
                    "data": {
                        "replies": [
                            reply_row("A&amp;B", "应该写 &#34;screen&#34; &gt; 别用 &lt;"),
                        ]
                    },
                }
            }
        )
        out = await client._top_comments(1, 1)
        self.assertEqual(out[0].user, "A&B")
        self.assertEqual(out[0].text, '应该写 "screen" > 别用 <')

    async def test_blank_comments_dropped(self):
        client = FakeClient(
            {
                "/x/v2/reply": {
                    "code": 0,
                    "data": {
                        "replies": [
                            reply_row("空", "   "),
                            reply_row("表情", "[笑]"),
                        ]
                    },
                }
            }
        )
        out = await client._top_comments(1, 1)
        self.assertEqual([c.user for c in out], ["表情"])

    async def test_failure_degrades_to_empty(self):
        client = FakeClient({"/x/v2/reply": {"code": -404, "message": "啥都木有"}})
        self.assertEqual(await client._top_comments(1, 1), [])


class OpusCommentsTest(unittest.IsolatedAsyncioTestCase):
    def payload(self, ctype: Any, cid: str) -> dict[str, Any]:
        return {
            "code": 0,
            "data": {
                "item": {
                    "basic": {"comment_type": ctype, "comment_id_str": cid},
                    "modules": {
                        "module_author": {"mid": 6905864, "name": "UP", "face": ""},
                        "module_dynamic": {
                            "desc": {"text": "动态正文"},
                            "major": {"type": "MAJOR_TYPE_ARTICLE"},
                        },
                        "module_stat": {
                            "like": {"count": 1},
                            "comment": {"count": 155},
                            "forward": {"count": 2},
                        },
                    },
                }
            },
        }

    async def test_article_backed_dynamic_uses_comment_id(self):
        client = FakeClient(
            {
                "/web-dynamic/v1/detail": self.payload(12, "43254098"),
                "/x/v2/reply": {"code": 0, "data": {"replies": [reply_row("甲", "求攻略")]}},
            }
        )
        card = await client.fetch_opus("1119905060273258502")
        reply_calls = [(u, p) for u, p in client.calls if u.endswith("/x/v2/reply")]
        self.assertEqual(len(reply_calls), 1)
        self.assertEqual(reply_calls[0][1]["type"], 12)
        self.assertEqual(reply_calls[0][1]["oid"], "43254098")
        self.assertEqual(len(card.comments), 1)

    async def test_plain_dynamic_without_comment_id_skips_request(self):
        client = FakeClient({"/web-dynamic/v1/detail": self.payload(0, "")})
        card = await client.fetch_opus("1119905060273258502")
        self.assertEqual(card.comments, [])
        self.assertEqual([u for u, _ in client.calls if u.endswith("/x/v2/reply")], [])


class BangumiCommentsTest(unittest.IsolatedAsyncioTestCase):
    async def test_comments_use_ep_id(self):
        client = FakeClient(
            {
                "/pgc/view/web/season": {
                    "code": 0,
                    "result": {
                        "title": "葬送的芙莉莲",
                        "cover": "",
                        "media_id": 1,
                        "episodes": [{"ep_id": 779775, "title": "1", "long_title": ""}],
                        "stat": {},
                    },
                },
                "/x/v2/reply": {"code": 0, "data": {"replies": [reply_row("甲", "好看")]}},
            }
        )
        card = await client.fetch_bangumi("46089", "ss")
        reply_calls = [(u, p) for u, p in client.calls if u.endswith("/x/v2/reply")]
        self.assertEqual(reply_calls[0][1]["type"], 1)
        self.assertEqual(reply_calls[0][1]["oid"], 779775)
        self.assertEqual(len(card.comments), 1)


class ArticleCommentsTest(unittest.IsolatedAsyncioTestCase):
    async def test_comments_use_cv_id_with_type_12(self):
        client = FakeClient(
            {
                "/x/article/viewinfo": {
                    "code": 0,
                    "data": {"title": "专栏", "stats": {}, "author_mid": 1},
                },
                "/x/v2/reply": {"code": 0, "data": {"replies": [reply_row("甲", "写得")]}},
            }
        )
        card = await client.fetch_article("cv2")
        reply_calls = [(u, p) for u, p in client.calls if u.endswith("/x/v2/reply")]
        self.assertEqual(reply_calls[0][1]["type"], 12)
        self.assertEqual(reply_calls[0][1]["oid"], "2")
        self.assertEqual(len(card.comments), 1)


class WbiNavTest(unittest.IsolatedAsyncioTestCase):
    """/nav 在匿名态固定返回 -101，这不能算签名失败。

    这个分支以前会让"没填 SESSDATA"的动态解析报出"需要登录才能查看"，
    错误信息看起来对、实际来源完全不相干，排查时会被带偏。
    """

    async def test_anonymous_nav_still_yields_keys(self):
        client = FakeClient({"/x/web-interface/nav": NAV_ANON_OK})
        await client._ensure_wbi()
        self.assertTrue(client._wbi.ready)

    async def test_other_error_codes_still_raise(self):
        client = FakeClient({"/x/web-interface/nav": {"code": -400, "data": {}}})
        with self.assertRaises(BiliError):
            await client._ensure_wbi()


if __name__ == "__main__":
    unittest.main()
