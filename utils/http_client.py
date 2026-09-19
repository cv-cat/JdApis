# coding: utf-8
"""HTTP 传输层：默认走 curl_cffi 冒充 Chrome，拿到和真浏览器一致的 TLS/HTTP2 指纹。

为什么不用 requests —— 2026-08-16 用 tls.peet.ws 实测：

| 客户端 | 协议 | JA4 | Akamai HTTP2 指纹 |
|---|---|---|---|
| 历史真 Chrome 151 基准 | h2 | `t13d1517h2_8daaf6152771_a87ad97598a9` | `1:65536;2:0;4:6291456;6:262144\|15663105\|0\|m,a,s,p` |
| requests | **HTTP/1.1** | `t13d1812h1_85036bcba153_…` | 无 |
| curl_cffi(chrome) | h2 | `t13d1516h2_8daaf6152771_…` | **与 Chrome 完全相同** |

`requests` 走 HTTP/1.1、JA4 带 `h1`、密码套件段也对不上，是明显的 Python 特征；
curl_cffi 的 H2 指纹和密码套件段与上述历史 Chrome 151 基准一致，只差一个 TLS 扩展（1517 vs 1516）；
业务字段基准已在 `builder/reference.py` 更新为当前 Chrome 152。

⚠️ 说明白：**这不是当前 403 的原因** —— 实测把浏览器签的 h5st 用裸 requests 发出去
一样能拿到真实数据。换 curl_cffi 是为了少一个可被识别的
特征、降低被风控标记的概率，不是为了"修"某个报错。

顺带解决 `accept-encoding` 的 zstd：requests 不会解，curl_cffi 会。
"""

from utils.fingerprint import get_profile

# 与当前 Chrome 会话对齐：UA 报 Chrome 152，就别冒充别的大版本
_IMPERSONATE = "chrome"

try:
    from curl_cffi import requests as _curl

    _HAS_CURL = True
except ImportError:  # pragma: no cover - 没装就退回 requests
    _curl = None
    _HAS_CURL = False

import requests as _requests

_requests.packages.urllib3.disable_warnings()


def _suppress_default_content_type(kwargs):
    """空体 POST 时别让 curl 自作主张加 `content-type`。

    浏览器发这类请求（body 在 query、表单体为空，如 `pcCart_jc_getCartNum`）
    **只带 `content-length: 0`，不带 content-type**；而 curl_cffi 会默认补一个
    `application/x-www-form-urlencoded`，JD 的 API 层会直接顶回
    `code:1 request Content-Type is not compatible with application/json`。
    将该 header 显式设为 ``None`` 会让 curl_cffi/libcurl 把它从 header
    链表中移除；传空字符串只会生成 ``Content-Type:``，仍与 Chrome 不同。
    """
    if kwargs.get("data") or kwargs.get("json"):
        return
    headers = dict(kwargs.get("headers") or {})
    if not any(k.lower() == "content-type" for k in headers):
        # curl_cffi treats None as a header suppression marker.  This is the
        # only form we have verified to produce no Content-Type line at all.
        headers["content-type"] = None
        kwargs["headers"] = headers


def _send(method, url, **kwargs):
    kwargs.setdefault("timeout", 20)
    # Several call sites pass ``verify=False`` explicitly because they also
    # work with a stateful Session.  Keep that override legal instead of
    # passing a second hard-coded value to ``request()``.
    kwargs.setdefault("verify", False)
    if _HAS_CURL:
        if method == "POST":
            _suppress_default_content_type(kwargs)
        # default_headers=False：只发我们自己按浏览器顺序排好的那套头，
        # 不让 curl_cffi 再塞它的默认头进来打乱顺序。
        return _curl.request(method, url, impersonate=_IMPERSONATE,
                             default_headers=False, **kwargs)
    return _requests.request(method, url, **kwargs)


def get(url, **kwargs):
    return _send("GET", url, **kwargs)


def post(url, **kwargs):
    return _send("POST", url, **kwargs)


def session(**kwargs):
    """Return a session using the same transport profile as one-shot calls.

    Login is stateful (the QR endpoint sets cookies that the poll and ticket
    endpoints reuse), so it cannot use the stateless ``get``/``post`` helpers.
    Keeping session construction here prevents that path from silently falling
    back to ``requests``/HTTP/1.1 while business calls use curl_cffi/Chrome.
    Callers may override normal Session options, but the impersonation and
    default-header policy remain explicit and deterministic.
    """
    if _HAS_CURL:
        options = {"impersonate": _IMPERSONATE, "default_headers": False,
                  "verify": False}
        options.update(kwargs)
        return _curl.Session(**options)
    options = {"verify": False}
    options.update(kwargs)
    return _requests.Session(**options)


def accept_encoding() -> str:
    """curl_cffi 能解 zstd，requests 不能 —— 按实际能力报，别报了解不开。"""
    return "gzip, deflate, br, zstd" if _HAS_CURL else "gzip, deflate, br"


def describe() -> str:
    ua = get_profile()["browser_version"]
    return (f"curl_cffi impersonate={_IMPERSONATE}（HTTP/2 + Chrome TLS 指纹）"
            if _HAS_CURL else f"requests（HTTP/1.1，TLS 指纹非 Chrome {ua}）")
