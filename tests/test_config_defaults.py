"""配置默认值与错误提示的回归测试。

这两处都属于「改错了不会有异常，但用户会看到莫名其妙的失败」，
所以单独锁住：
- 动态接口强制要求登录，默认不能是开启状态；
- -101 必须翻成人话，不能把 code 直接丢给用户。
"""

from __future__ import annotations

import json
import pathlib
import unittest

from core.api import _ERR_TEXT

_ROOT = pathlib.Path(__file__).resolve().parent.parent


class SchemaDefaultsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = json.loads((_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))

    def test_opus_default_off(self) -> None:
        types = self.schema["types"]["items"]
        self.assertIs(types["opus"]["default"], False)

    def test_other_types_default_on(self) -> None:
        types = self.schema["types"]["items"]
        for key, node in types.items():
            if key == "opus":
                continue
            self.assertIs(node["default"], True, f"{key} 默认应为开启")

    def test_opus_hint_mentions_login(self) -> None:
        hint = self.schema["types"]["items"]["opus"]["hint"]
        self.assertIn("登录", hint)

    def test_cookie_hint_scopes_to_opus(self) -> None:
        hint = self.schema["advanced"]["items"]["cookie_sessdata"]["hint"]
        self.assertIn("动态", hint)
        self.assertIn("留空", hint)

    def test_all_types_documented(self) -> None:
        """types 分组的每一项都必须带 description，否则 WebUI 里会显示为空标签。"""
        for key, node in self.schema["types"]["items"].items():
            self.assertTrue(node.get("description"), f"{key} 缺少 description")


class ErrorTextTest(unittest.TestCase):
    def test_not_logged_in_is_readable(self) -> None:
        text = _ERR_TEXT[-101]
        self.assertIn("登录", text)
        self.assertIn("SESSDATA", text)

    def test_no_raw_code_leak_for_known_codes(self) -> None:
        for code, text in _ERR_TEXT.items():
            self.assertNotIn("code=", text, f"{code} 的提示里残留了原始错误码")


if __name__ == "__main__":
    unittest.main()
