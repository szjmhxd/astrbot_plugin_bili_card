"""QQ 卡片抽取的回归测试。

这里锁的是线上真实踩过的坑：手机端转发的卡片经常**不含 bilibili 字样**，
旧实现只要原文里没有站名就直接放弃整张卡片，导致视频卡片静默不解析。
"""

from __future__ import annotations

import unittest

from core.link_parser import extract_from_card

ALL = {
    "video",
    "bangumi",
    "live",
    "article",
    "space",
    "audio",
    "comment",
    "short_link",
    "json_card",
}


def kinds(raw: str) -> list[str]:
    return [m.kind for m in extract_from_card(raw, ALL)]


class CardWithoutBrandTest(unittest.TestCase):
    def test_short_link_only(self) -> None:
        """只有一串 b23.tv，标题里没有 bilibili —— 旧实现会直接返回空。"""
        raw = '{"app":"com.tencent.miniapp_01","meta":{"detail_1":{"title":"【视频】某某视频","qqdocurl":"https://b23.tv/aBcDeF1"}}}'
        self.assertEqual(kinds(raw), ["short"])

    def test_bili2233_host(self) -> None:
        raw = '{"meta":{"news":{"title":"分享","jumpUrl":"https://bili2233.cn/XyZ987"}}}'
        self.assertEqual(kinds(raw), ["short"])

    def test_plain_video_url_without_brand(self) -> None:
        raw = '{"desc":"https://www.bilibili.com/video/BV1BXxf6wEmQ"}'
        self.assertEqual(kinds(raw), ["video"])


class CardEscapingTest(unittest.TestCase):
    def test_escaped_slashes(self) -> None:
        raw = '{"qqdocurl": "https:\\/\\/www.bilibili.com\\/video\\/BV1Fuxf6aENs"}'
        self.assertEqual(kinds(raw), ["video"])

    def test_html_entities(self) -> None:
        raw = '{"jumpUrl": "https://www.bilibili.com/video/BV1Fuxf6aENs?p=2&amp;t=3"}'
        self.assertEqual(kinds(raw), ["video"])

    def test_percent_encoded_target(self) -> None:
        """目标地址被整体编码进另一个 URL 的查询串里。"""
        raw = (
            '{"qqdocurl": "https://qun.qq.com/x?url='
            "https%3A%2F%2Fwww.bilibili.com%2Fvideo%2FBV1BXxf6wEmQ%22}"
        )
        self.assertEqual(kinds(raw), ["video"])

    def test_shell_field_does_not_block_deep_scan(self) -> None:
        """白名单字段里只有跳转壳时，不能因此跳过全文扫描。"""
        raw = '{"qqdocurl": "https://qun.qq.com/shell/abc", "preview": "https://b23.tv/QwErTy9"}'
        self.assertEqual(kinds(raw), ["short"])


class CardNegativeTest(unittest.TestCase):
    def test_empty(self) -> None:
        self.assertEqual(extract_from_card("", ALL), [])

    def test_unrelated_card(self) -> None:
        raw = '{"app":"com.tencent.structmsg","meta":{"news":{"title":"某新闻","jumpUrl":"https://news.qq.com/a/1"}}}'
        self.assertEqual(extract_from_card(raw, ALL), [])

    def test_respects_disabled_types(self) -> None:
        raw = '{"qqdocurl":"https://b23.tv/aBcDeF1"}'
        self.assertEqual(extract_from_card(raw, {"video"}), [])


if __name__ == "__main__":
    unittest.main()
