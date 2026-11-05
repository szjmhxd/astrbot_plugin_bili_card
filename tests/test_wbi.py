"""WBI 签名测试。

mixin_key 用的是 B 站 API 文档社区的公开样例向量，
如果置换表写错，这个用例会直接失败——这是本模块唯一的外部校验点。
"""

import hashlib
import unittest
from unittest import mock

from core.wbi import (
    MIXIN_KEY_ENC_TAB,
    WbiKeyCache,
    enc_wbi,
    extract_key,
    get_mixin_key,
)

# 公开样例：img_key + sub_key -> mixin_key
SAMPLE_IMG = "7cd084941338484aae1ad9425b84077c"
SAMPLE_SUB = "4932caff0ff746eab6f01bf08b70ac45"
SAMPLE_MIXIN = "ea1db124af3c7062474693fa704f4ff8"


class TestExtractKey(unittest.TestCase):
    def test_strips_path_and_extension(self):
        url = "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png"
        self.assertEqual(extract_key(url), SAMPLE_IMG)

    def test_handles_query_string(self):
        url = "https://i0.hdslb.com/bfs/wbi/abc123.png?x=1"
        self.assertEqual(extract_key(url), "abc123")

    def test_handles_no_extension(self):
        self.assertEqual(extract_key("https://x/y/abc"), "abc")


class TestMixinKey(unittest.TestCase):
    def test_permutation_table_is_valid(self):
        # 必须是 0-63 的一个排列，否则取位会重复或越界
        self.assertEqual(len(MIXIN_KEY_ENC_TAB), 64)
        self.assertEqual(sorted(MIXIN_KEY_ENC_TAB), list(range(64)))

    def test_known_vector(self):
        self.assertEqual(get_mixin_key(SAMPLE_IMG, SAMPLE_SUB), SAMPLE_MIXIN)

    def test_length_is_32(self):
        self.assertEqual(len(get_mixin_key("a" * 32, "b" * 32)), 32)


class TestEncWbi(unittest.TestCase):
    def test_adds_wts_and_w_rid(self):
        with mock.patch("core.wbi.time.time", return_value=1_700_000_000):
            signed = enc_wbi({"mid": "123"}, SAMPLE_IMG, SAMPLE_SUB)
        self.assertEqual(signed["wts"], "1700000000")
        self.assertIn("w_rid", signed)
        self.assertEqual(len(signed["w_rid"]), 32)

    def test_params_sorted(self):
        with mock.patch("core.wbi.time.time", return_value=1_700_000_000):
            signed = enc_wbi({"b": "2", "a": "1", "c": "3"}, SAMPLE_IMG, SAMPLE_SUB)
        body = [k for k in signed if k not in ("w_rid",)]
        self.assertEqual(body, sorted(body))

    def test_filters_special_chars(self):
        with mock.patch("core.wbi.time.time", return_value=1_700_000_000):
            signed = enc_wbi({"q": "a!b'c(d)e*f"}, SAMPLE_IMG, SAMPLE_SUB)
        self.assertEqual(signed["q"], "abcdef")

    def test_none_values_dropped(self):
        with mock.patch("core.wbi.time.time", return_value=1_700_000_000):
            signed = enc_wbi({"a": "1", "b": None}, SAMPLE_IMG, SAMPLE_SUB)
        self.assertNotIn("b", signed)

    def test_w_rid_is_md5_of_query_plus_mixin(self):
        import urllib.parse

        with mock.patch("core.wbi.time.time", return_value=1_700_000_000):
            signed = enc_wbi({"mid": "42"}, SAMPLE_IMG, SAMPLE_SUB)
        query = urllib.parse.urlencode(
            {"mid": "42", "wts": "1700000000"}, quote_via=urllib.parse.quote
        )
        expected = hashlib.md5((query + SAMPLE_MIXIN).encode()).hexdigest()
        self.assertEqual(signed["w_rid"], expected)

    def test_original_dict_not_mutated(self):
        params = {"mid": "42"}
        with mock.patch("core.wbi.time.time", return_value=1_700_000_000):
            enc_wbi(params, SAMPLE_IMG, SAMPLE_SUB)
        self.assertEqual(params, {"mid": "42"})


class TestWbiKeyCache(unittest.TestCase):
    def test_initially_not_ready(self):
        cache = WbiKeyCache()
        self.assertFalse(cache.ready)
        self.assertTrue(cache.expired)

    def test_update_makes_ready(self):
        cache = WbiKeyCache()
        cache.update(
            f"https://i0.hdslb.com/bfs/wbi/{SAMPLE_IMG}.png",
            f"https://i0.hdslb.com/bfs/wbi/{SAMPLE_SUB}.png",
        )
        self.assertTrue(cache.ready)
        self.assertFalse(cache.expired)
        self.assertEqual(cache.img_key, SAMPLE_IMG)
        self.assertEqual(cache.sub_key, SAMPLE_SUB)

    def test_expires_after_ttl(self):
        cache = WbiKeyCache()
        cache.update(
            f"https://i0.hdslb.com/bfs/wbi/{SAMPLE_IMG}.png",
            f"https://i0.hdslb.com/bfs/wbi/{SAMPLE_SUB}.png",
        )
        with mock.patch("core.wbi.time.time", return_value=cache._fetched_at + 99999):
            self.assertTrue(cache.expired)


if __name__ == "__main__":
    unittest.main()
