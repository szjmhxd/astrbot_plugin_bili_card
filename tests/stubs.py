"""测试用的日志器替身。

正式运行时 ``BiliClient`` 的 logger 由 ``main.py`` 传入 ``astrbot.api.logger``。
离线测试不该为了一个日志参数去加载 AstrBot 运行时，所以这里提供同接口的替身。
"""

from __future__ import annotations

from typing import Any


class QuietLogger:
    """什么都不做，只为满足 ``logger`` 必传。"""

    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
        pass

    def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
        pass

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        pass


QUIET_LOGGER = QuietLogger()


class RecordingLogger:
    """记录所有日志调用，便于断言"慢请求有没有留下线索"这类行为。"""

    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def _record(self, level: str, msg: str, *args: Any, **kwargs: Any) -> None:
        try:
            text = msg % args if args else str(msg)
        except (TypeError, ValueError):
            text = str(msg)
        self.records.append((level, text))

    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._record("debug", msg, *args, **kwargs)

    def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._record("info", msg, *args, **kwargs)

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._record("warning", msg, *args, **kwargs)

    def texts(self, level: str | None = None) -> list[str]:
        return [t for lv, t in self.records if level is None or lv == level]
