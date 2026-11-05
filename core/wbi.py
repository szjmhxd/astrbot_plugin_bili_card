"""B 站 WBI 参数签名。

WBI 是 B 站 2023 年起对部分 Web 接口启用的签名机制，用来抬高爬取门槛。
算法（公开逆向结果，长期稳定）：

1. 请求 ``x/web-interface/nav``，从 ``data.wbi_img.img_url`` / ``sub_url``
   取出不带扩展名的文件名，分别得到 ``img_key``、``sub_key``。
2. 拼成 64 字符的原始串，用固定的 64 位置换表重排后取前 32 位，得到 ``mixin_key``。
3. 请求参数加入 ``wts``（秒级时间戳），按 key 升序排序，值里的 ``!'()*`` 全部剔除，
   再 urlencode 得到 query。
4. ``w_rid = md5(query + mixin_key)``，最终参数为 ``query + w_rid``。

未登录状态下 nav 接口返回的 key 也是有效的，所以本模块不强制要求 Cookie。
"""

from __future__ import annotations

import hashlib
import time
import urllib.parse
from typing import Any

# 64 位置换表：决定原始串重排后的取位顺序
MIXIN_KEY_ENC_TAB: list[int] = [
    46,
    47,
    18,
    2,
    53,
    8,
    23,
    32,
    15,
    50,
    10,
    31,
    58,
    3,
    45,
    35,
    27,
    43,
    5,
    49,
    33,
    9,
    42,
    19,
    29,
    28,
    14,
    39,
    12,
    38,
    41,
    13,
    37,
    48,
    7,
    16,
    24,
    55,
    40,
    61,
    26,
    17,
    0,
    1,
    60,
    51,
    30,
    4,
    22,
    25,
    54,
    21,
    56,
    59,
    6,
    63,
    57,
    62,
    11,
    36,
    20,
    34,
    44,
    52,
]

# 参与签名前必须剔除的字符，与 B 站前端行为一致
_FILTER_CHARS = "!'()*"

# WBI 密钥缓存时长（秒）。B 站每天轮换，半小时刷新足够，且避免频繁打 nav 接口。
KEY_TTL_SECONDS = 1800


def extract_key(url: str) -> str:
    """从 wbi_img 的 URL 中取出不带扩展名的文件名。"""
    name = url.rsplit("/", 1)[-1]
    return name.split(".", 1)[0]


def get_mixin_key(img_key: str, sub_key: str) -> str:
    """由 img_key + sub_key 推导 32 位 mixin_key。"""
    raw = img_key + sub_key
    return "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def _sanitize(value: Any) -> str:
    """剔除会破坏签名的特殊字符，并把非字符串转成字符串。"""
    text = str(value)
    for ch in _FILTER_CHARS:
        text = text.replace(ch, "")
    return text


def enc_wbi(params: dict[str, Any], img_key: str, sub_key: str) -> dict[str, str]:
    """为参数附加 wts 与 w_rid，返回可直接用于请求的新字典。"""
    mixin_key = get_mixin_key(img_key, sub_key)
    signed: dict[str, str] = {k: _sanitize(v) for k, v in params.items() if v is not None}
    signed["wts"] = str(int(time.time()))
    signed = dict(sorted(signed.items()))
    query = urllib.parse.urlencode(signed, quote_via=urllib.parse.quote)
    signed["w_rid"] = hashlib.md5((query + mixin_key).encode()).hexdigest()
    return signed


class WbiKeyCache:
    """持有 wbi_img 的两个 key，并按 TTL 判断是否需要刷新。"""

    __slots__ = ("_fetched_at", "img_key", "sub_key")

    def __init__(self) -> None:
        self.img_key: str = ""
        self.sub_key: str = ""
        self._fetched_at: float = 0.0

    @property
    def ready(self) -> bool:
        return bool(self.img_key and self.sub_key)

    @property
    def expired(self) -> bool:
        if not self.ready:
            return True
        return (time.time() - self._fetched_at) > KEY_TTL_SECONDS

    def update(self, img_url: str, sub_url: str) -> None:
        self.img_key = extract_key(img_url)
        self.sub_key = extract_key(sub_url)
        self._fetched_at = time.time()
