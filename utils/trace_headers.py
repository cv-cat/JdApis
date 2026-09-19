# coding: utf-8
"""京东 PC 登录页的 SGM/JDAS 请求链路头。

实现依据是登录页当前加载的官方脚本：

* ``trace-chain-sdk.umd.min.js``：Page-Id 取 ``#uuid``；Trace-Id 每请求生成
  UUIDv7；Session-Id 在同一页面内复用，并把 UUID 末四位设为 ``0001``。
* ``sgm-web-3.3.0.js``：``Sgm-Context`` 为随机数字 key 的 ``key;key``。

这些值是请求链路标识，不是服务端秘密；本地生成即可，不需要从浏览器抓包回填。
"""

from dataclasses import dataclass
from html.parser import HTMLParser
import secrets
import time
import uuid


class _PageIdParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.page_id = ""

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "input" or self.page_id:
            return
        values = {str(key).lower(): value for key, value in attrs}
        if values.get("id") == "uuid" and values.get("value"):
            self.page_id = values["value"].strip()


def page_id_from_html(html: str) -> str:
    parser = _PageIdParser()
    parser.feed(html or "")
    if not parser.page_id:
        raise ValueError("登录页 HTML 缺少 #uuid，无法生成 JDAS-Page-Id")
    return parser.page_id


def uuid7() -> str:
    """生成 RFC 9562 UUIDv7；与页面 SDK 的 36 字符 UUIDv7 契约一致。"""
    timestamp_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)
    value = (timestamp_ms << 80) | (0x7 << 76) | (rand_a << 64)
    value |= (0b10 << 62) | rand_b
    return str(uuid.UUID(int=value))


def _sgm_key() -> str:
    """逐字符复刻 sgm-web 3.3.0 的数字 key 生成式。"""
    out = []
    for char in "1000-1000-4000-8000-1000000":
        if char not in "018":
            out.append(char)
            continue
        digit = int(char)
        shift = int(digit / 4)  # JS 位移会先把 0.25 截为 0
        random_part = secrets.randbits(8) & (15 >> shift)
        out.append(str(digit ^ random_part))
    # JS 源码先删连字符、截 18 字符，再用一元 + 转 Number；转回字符串时
    # 会去掉前导 0。这里保留同样的可见 header 形式。
    value = "".join(out).replace("-", "")[:18]
    return str(int(value or "0"))


@dataclass
class LoginTraceContext:
    page_id: str
    session_id: str = ""

    def __post_init__(self):
        if not self.page_id:
            raise ValueError("page_id 不能为空")
        if not self.session_id:
            generated = uuid7()
            self.session_id = generated[:-4] + "0001"

    @classmethod
    def from_html(cls, html: str):
        return cls(page_id=page_id_from_html(html))

    def next_headers(self, overrides=None) -> dict:
        key = _sgm_key()
        headers = {
            "sgm-context": f"{key};{key}",
            "jdas-trace-id": uuid7(),
            "jdas-page-id": self.page_id,
            "jdas-session-id": self.session_id,
        }
        headers.update({str(k).lower(): v for k, v in (overrides or {}).items()
                        if v not in (None, "")})
        return headers
