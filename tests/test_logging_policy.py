"""日志必须走 AstrBot 的 logger，不能用标准库 logging。

插件规范明确：日志器只能从 ``astrbot.api`` 导入，禁止内置 ``logging`` 模块。
这条规则很容易被"兜底"写法带回来——``logger or logging.getLogger(__name__)``
看起来无害，实际会让部分日志绕开宿主的日志配置。所以用测试钉死。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_FILES = [PLUGIN_ROOT / "main.py", *sorted((PLUGIN_ROOT / "core").glob("*.py"))]

BANNED = (
    (re.compile(r"^\s*import\s+logging\b", re.MULTILINE), "import logging"),
    (re.compile(r"^\s*from\s+logging\s+import\b", re.MULTILINE), "from logging import"),
    (re.compile(r"\blogging\.(getLogger|Logger)\b"), "logging.getLogger / logging.Logger"),
)


class LoggingPolicyTest(unittest.TestCase):
    def test_no_stdlib_logging_in_runtime_code(self):
        self.assertTrue(RUNTIME_FILES[0].exists(), "找不到 main.py，路径约定变了？")
        for path in RUNTIME_FILES:
            src = path.read_text(encoding="utf-8")
            for pattern, label in BANNED:
                with self.subTest(file=path.name, rule=label):
                    self.assertIsNone(
                        pattern.search(src),
                        f"{path.name} 使用了 {label}；日志请从 astrbot.api 取",
                    )

    def test_bili_client_requires_logger(self):
        """logger 是必传参数，漏传要立刻炸，而不是静默换个日志来源。"""
        from core.api import BiliClient

        with self.assertRaises(TypeError):
            BiliClient()  # type: ignore[call-arg]


if __name__ == "__main__":
    unittest.main()
