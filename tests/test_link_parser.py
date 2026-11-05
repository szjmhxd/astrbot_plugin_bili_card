"""链接识别测试。

覆盖八类内容 + 短链 + 裸编号 + 小程序卡片，以及"已匹配区间不能被二次命中"
这条最容易写错的规则。
"""

import unittest

from core.link_parser import detect_links, extract_from_card, retarget_from_url

ALL = {
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
}


class TestVideo(unittest.TestCase):
    def test_bv_url(self):
        hits = detect_links("看这个 https://www.bilibili.com/video/BV1xx411c7mD 哈哈", ALL)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].kind, "video")
        self.assertEqual(hits[0].key, "BV1xx411c7mD")

    def test_av_url(self):
        hits = detect_links("https://bilibili.com/video/av170001", ALL)
        self.assertEqual(hits[0].key, "av170001")

    def test_bare_bv(self):
        hits = detect_links("这个 BV1xx411c7mD 不错", ALL)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].kind, "video")

    def test_bare_av(self):
        hits = detect_links("av170001", ALL)
        self.assertEqual(hits[0].kind, "video")
        self.assertEqual(hits[0].key, "av170001")

    def test_mobile_domain(self):
        hits = detect_links("https://m.bilibili.com/video/BV1xx411c7mD", ALL)
        self.assertEqual(hits[0].kind, "video")

    def test_page_param(self):
        hits = detect_links("https://www.bilibili.com/video/BV1xx411c7mD?p=3", ALL)
        self.assertEqual(hits[0].extra["page"], "3")

    def test_url_does_not_double_match_bare_bv(self):
        # 整条 URL 已吃掉，裸 BV 规则不能再命中同一个 BV 号
        hits = detect_links("https://www.bilibili.com/video/BV1xx411c7mD", ALL)
        self.assertEqual(len(hits), 1)

    def test_two_links(self):
        text = "BV1xx411c7mD 和 https://www.bilibili.com/video/BV1yy411c7mE"
        hits = detect_links(text, ALL)
        self.assertEqual(len(hits), 2)

    def test_invalid_bv_length_ignored(self):
        self.assertEqual(detect_links("BV1xx411c7m", ALL), [])


class TestComment(unittest.TestCase):
    def test_comment_root_id(self):
        url = "https://www.bilibili.com/video/BV1xx411c7mD?comment_root_id=123456"
        hits = detect_links(url, ALL)
        self.assertEqual(hits[0].kind, "comment")
        self.assertEqual(hits[0].extra["root_id"], "123456")

    def test_comment_disabled_falls_back_to_video(self):
        url = "https://www.bilibili.com/video/BV1xx411c7mD?comment_root_id=123456"
        enabled = ALL - {"comment"}
        hits = detect_links(url, enabled)
        self.assertEqual(hits[0].kind, "video")


class TestOtherTypes(unittest.TestCase):
    def test_bangumi_ep(self):
        hits = detect_links("https://www.bilibili.com/bangumi/play/ep123456", ALL)
        self.assertEqual(
            (hits[0].kind, hits[0].key, hits[0].extra["mode"]), ("bangumi", "123456", "ep")
        )

    def test_bangumi_ss(self):
        hits = detect_links("https://www.bilibili.com/bangumi/play/ss28747", ALL)
        self.assertEqual(hits[0].extra["mode"], "ss")

    def test_bangumi_md(self):
        hits = detect_links("https://www.bilibili.com/bangumi/media/md28231846", ALL)
        self.assertEqual((hits[0].key, hits[0].extra["mode"]), ("28231846", "md"))

    def test_live(self):
        hits = detect_links("https://live.bilibili.com/22637261", ALL)
        self.assertEqual((hits[0].kind, hits[0].key), ("live", "22637261"))

    def test_live_h5(self):
        hits = detect_links("https://live.bilibili.com/h5/12345", ALL)
        self.assertEqual(hits[0].key, "12345")

    def test_space(self):
        hits = detect_links("https://space.bilibili.com/672328094", ALL)
        self.assertEqual((hits[0].kind, hits[0].key), ("space", "672328094"))

    def test_article(self):
        hits = detect_links("https://www.bilibili.com/read/cv12345678", ALL)
        self.assertEqual((hits[0].kind, hits[0].key), ("article", "12345678"))

    def test_opus(self):
        hits = detect_links("https://www.bilibili.com/opus/1234567890123456", ALL)
        self.assertEqual(hits[0].kind, "opus")

    def test_t_bilibili(self):
        hits = detect_links("https://t.bilibili.com/1234567890123456", ALL)
        self.assertEqual((hits[0].kind, hits[0].key), ("opus", "1234567890123456"))

    def test_audio_au(self):
        hits = detect_links("https://www.bilibili.com/audio/au123456", ALL)
        self.assertEqual(
            (hits[0].kind, hits[0].key, hits[0].extra["mode"]), ("audio", "123456", "au")
        )

    def test_audio_am(self):
        hits = detect_links("https://www.bilibili.com/audio/am12345", ALL)
        self.assertEqual(hits[0].extra["mode"], "am")

    def test_short_link(self):
        hits = detect_links("https://b23.tv/abcDEF123", ALL)
        self.assertEqual((hits[0].kind, hits[0].key), ("short", "https://b23.tv/abcDEF123"))

    def test_bili2233(self):
        hits = detect_links("https://bili2233.cn/xyz789", ALL)
        self.assertEqual(hits[0].kind, "short")


class TestTypeSwitches(unittest.TestCase):
    def test_disabled_video_yields_nothing(self):
        enabled = ALL - {"video"}
        self.assertEqual(detect_links("https://www.bilibili.com/video/BV1xx411c7mD", enabled), [])

    def test_disabled_short_yields_nothing(self):
        enabled = ALL - {"short_link"}
        self.assertEqual(detect_links("https://b23.tv/abc", enabled), [])

    def test_empty_text(self):
        self.assertEqual(detect_links("", ALL), [])

    def test_non_bilibili_text(self):
        self.assertEqual(detect_links("今天天气不错 https://example.com/a", ALL), [])


class TestCardExtraction(unittest.TestCase):
    def test_escaped_json_url(self):
        raw = '{"app":"com.tencent.miniapp","meta":{"detail_1":{"qqdocurl":"https:\\/\\/b23.tv\\/abcDEF"}}}'
        hits = extract_from_card(raw, ALL)
        self.assertTrue(any(h.kind == "short" for h in hits))

    def test_jump_url_with_entity(self):
        raw = '{"jumpUrl":"https://www.bilibili.com/video/BV1xx411c7mD?a=1&amp;b=2"}'
        hits = extract_from_card(raw, ALL)
        self.assertTrue(any(h.kind == "video" for h in hits))

    def test_no_bilibili_returns_empty(self):
        self.assertEqual(extract_from_card('{"qqdocurl":"https://example.com"}', ALL), [])

    def test_duplicate_urls_deduped(self):
        raw = '{"qqdocurl":"https://b23.tv/abc","url":"https://b23.tv/abc"}'
        hits = extract_from_card(raw, ALL)
        self.assertEqual(len([h for h in hits if h.kind == "short"]), 1)

    def test_empty_raw(self):
        self.assertEqual(extract_from_card("", ALL), [])


class TestRetarget(unittest.TestCase):
    def test_short_to_video(self):
        target = retarget_from_url("https://www.bilibili.com/video/BV1xx411c7mD?p=2", ALL)
        self.assertIsNotNone(target)
        self.assertEqual((target.kind, target.key, target.page), ("video", "BV1xx411c7mD", "2"))

    def test_short_to_unsupported(self):
        self.assertIsNone(retarget_from_url("https://example.com/game", ALL))

    def test_short_to_disabled_type(self):
        enabled = ALL - {"video"}
        self.assertIsNone(retarget_from_url("https://www.bilibili.com/video/BV1xx411c7mD", enabled))

    def test_short_to_bangumi(self):
        target = retarget_from_url("https://www.bilibili.com/bangumi/play/ep999", ALL)
        self.assertEqual((target.kind, target.mode), ("bangumi", "ep"))


if __name__ == "__main__":
    unittest.main()
