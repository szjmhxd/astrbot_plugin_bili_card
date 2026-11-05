"""图片内联相关的测试：取图尺寸、图床缩略图参数、缓存。

这些逻辑决定卡片能不能完整渲染——原图直接内联会把模板撑到几百 KB，
渲染服务跟不上时截图会出现"封面只剩顶部一条、头像空白"。
"""

from __future__ import annotations

import unittest

from core.api import BiliClient, _normalize_image_url, _with_thumb_suffix


class NormalizeImageUrlTest(unittest.TestCase):
    def test_protocol_relative(self):
        self.assertEqual(
            _normalize_image_url("//i0.hdslb.com/bfs/archive/a.jpg"),
            "https://i0.hdslb.com/bfs/archive/a.jpg",
        )

    def test_http_upgraded(self):
        self.assertEqual(
            _normalize_image_url("http://i1.hdslb.com/bfs/archive/a.jpg"),
            "https://i1.hdslb.com/bfs/archive/a.jpg",
        )

    def test_existing_suffix_stripped(self):
        # 接口偶尔会带尺寸后缀，先去掉再按我们的尺寸重新取，避免叠成 a.jpg@1w@2w
        self.assertEqual(
            _normalize_image_url("https://i0.hdslb.com/bfs/a.jpg@100w_100h_1c.webp"),
            "https://i0.hdslb.com/bfs/a.jpg",
        )

    def test_empty_and_invalid(self):
        self.assertEqual(_normalize_image_url(""), "")
        self.assertEqual(_normalize_image_url("ftp://x/a.jpg"), "")


class ThumbSuffixTest(unittest.TestCase):
    def test_plain_url(self):
        self.assertEqual(
            _with_thumb_suffix("https://i0.hdslb.com/a.jpg", 720, 405),
            "https://i0.hdslb.com/a.jpg@720w_405h_1c.webp",
        )

    def test_query_kept_after_suffix(self):
        # 参数必须挂在查询串之前，否则 @ 会被当成 query 的一部分，图床直接 404
        self.assertEqual(
            _with_thumb_suffix("https://i0.hdslb.com/a.jpg?x=1", 96, 96),
            "https://i0.hdslb.com/a.jpg@96w_96h_1c.webp?x=1",
        )


class InlineImageTest(unittest.IsolatedAsyncioTestCase):
    async def test_returns_data_uri_and_caches(self):
        client = BiliClient()
        calls: list[tuple[str, tuple[int, int] | None]] = []

        async def fake_download(url, size):
            calls.append((url, size))
            return b"\xff\xd8hello", "image/jpeg"

        client._download_image = fake_download  # type: ignore[method-assign]
        first = await client.inline_image("https://i0.hdslb.com/a.jpg", (720, 405))
        second = await client.inline_image("https://i0.hdslb.com/a.jpg", (720, 405))

        self.assertTrue(first.startswith("data:image/jpeg;base64,"))
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1, "同一尺寸的图片应命中缓存，不重复下载")

    async def test_download_failure_returns_empty(self):
        client = BiliClient()

        async def fake_download(url, size):
            return b"", ""

        client._download_image = fake_download  # type: ignore[method-assign]
        self.assertEqual(await client.inline_image("https://i0.hdslb.com/a.jpg", (96, 96)), "")

    async def test_size_passed_through(self):
        client = BiliClient()
        seen = {}

        async def fake_download(url, size):
            seen["size"] = size
            return b"x", "image/webp"

        client._download_image = fake_download  # type: ignore[method-assign]
        await client.inline_image("//i0.hdslb.com/a.jpg", (96, 96))
        self.assertEqual(seen["size"], (96, 96))


if __name__ == "__main__":
    unittest.main()
