"""图片内联相关的测试：取图尺寸、图床缩略图参数、缓存。

这些逻辑决定卡片能不能完整渲染——原图直接内联会把模板撑到几百 KB，
渲染服务跟不上时截图会出现"封面只剩顶部一条、头像空白"。
"""

from __future__ import annotations

import unittest
from typing import Self

from core.api import BiliClient, _looks_like_image, _normalize_image_url, _with_thumb_suffix
from tests.stubs import QUIET_LOGGER


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
        client = BiliClient(logger=QUIET_LOGGER)
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
        client = BiliClient(logger=QUIET_LOGGER)

        async def fake_download(url, size):
            return b"", ""

        client._download_image = fake_download  # type: ignore[method-assign]
        self.assertEqual(await client.inline_image("https://i0.hdslb.com/a.jpg", (96, 96)), "")

    async def test_size_passed_through(self):
        client = BiliClient(logger=QUIET_LOGGER)
        seen = {}

        async def fake_download(url, size):
            seen["size"] = size
            return b"x", "image/webp"

        client._download_image = fake_download  # type: ignore[method-assign]
        await client.inline_image("//i0.hdslb.com/a.jpg", (96, 96))
        self.assertEqual(seen["size"], (96, 96))


class LooksLikeImageTest(unittest.TestCase):
    def test_accepts_common_formats(self):
        self.assertTrue(_looks_like_image(b"\xff\xd8\xff" + b"\x00" * 20))
        self.assertTrue(_looks_like_image(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20))
        self.assertTrue(_looks_like_image(b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 20))
        self.assertTrue(_looks_like_image(b"GIF89a" + b"\x00" * 20))

    def test_rejects_html_and_empty(self):
        # 图床出错时可能返回一段 HTML 错误页，不能当图片内联
        self.assertFalse(_looks_like_image(b"<!doctype html><html>"))
        self.assertFalse(_looks_like_image(b""))
        self.assertFalse(_looks_like_image(b"RIFF"))


class _FakeContent:
    """复刻 aiohttp 的 StreamReader 语义：read(n) 只返回第一个可用数据块。"""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            return b"".join(self._chunks)
        return self._chunks[0][:n]


class _FakeResponse:
    def __init__(self, chunks: list[bytes], mime: str = "image/webp", status: int = 200) -> None:
        self._chunks = chunks
        self.status = status
        self.headers = {"Content-Type": mime}
        self.content_length = sum(len(c) for c in chunks)
        self.content = _FakeContent(chunks)

    async def read(self) -> bytes:
        return b"".join(self._chunks)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False


class _FakeSession:
    def __init__(self, resp: _FakeResponse) -> None:
        self._resp = resp
        self.requested: list[str] = []

    def get(self, url: str, **_kwargs) -> _FakeResponse:
        self.requested.append(url)
        return self._resp


class DownloadFullBodyTest(unittest.IsolatedAsyncioTestCase):
    """回归：大图必须整段读完，不能被第一个 16KB 数据块截断。

    `resp.content.read(n)` 只保证拿到"至少 1 字节"，39KB 的封面会只拿到 16384 字节，
    剩下的变成一段坏图 —— 渲染端表现为封面整块空白。
    """

    async def test_multi_chunk_image_not_truncated(self):
        blob = b"RIFF\x00\x00\x00\x00WEBP" + b"x" * 40000
        chunks = [blob[i : i + 16384] for i in range(0, len(blob), 16384)]
        self.assertGreater(len(chunks), 1, "构造的测试数据必须多于一个数据块")

        session = _FakeSession(_FakeResponse(chunks))
        client = BiliClient(logger=QUIET_LOGGER)

        async def fake_session():
            return session

        client._ensure_session = fake_session  # type: ignore[method-assign]
        got, mime = await client._download_image("https://i0.hdslb.com/a.jpg", (720, 405))

        self.assertEqual(mime, "image/webp")
        self.assertEqual(len(got), len(blob), "整张图必须完整读回")
        self.assertEqual(got, blob)

    async def test_falls_back_to_original_when_thumb_is_html(self):
        html = _FakeResponse([b"<!doctype html><html>not an image</html>"], mime="text/html")
        original = _FakeResponse([b"\xff\xd8\xff" + b"j" * 300], mime="image/jpeg")

        client = BiliClient(logger=QUIET_LOGGER)
        responses = [html, original]

        class _Session:
            def get(self, url, **_kwargs):
                return responses.pop(0)

        async def fake_session():
            return _Session()

        client._ensure_session = fake_session  # type: ignore[method-assign]
        got, mime = await client._download_image("https://i0.hdslb.com/a.jpg", (720, 405))

        self.assertEqual(mime, "image/jpeg", "缩略图不可用时应退回原图")
        self.assertTrue(got.startswith(b"\xff\xd8\xff"))


if __name__ == "__main__":
    unittest.main()
