# coding: utf-8
"""Query 参数装配与 h5st 签名。对齐 ../DouYin_Spider/builder/params.py 的链式风格。"""

import json

from utils.jd_util import (
    APPID_PC_SEARCH,
    now_ms,
    random_jquery_callback,
    sha256_hex,
)

# 参与 h5st 签名的 key。签名后 5.3 会回一个 `_stk` 列出它实际签了哪些，
# 实测为 `appid,body,client,clientVersion,functionId,t`，与这里一致；
# 多签少签都会导致服务端校验不过。
H5ST_SIGN_KEYS = ("appid", "functionId", "body", "client", "clientVersion", "jsonp", "t")


class Params:
    def __init__(self):
        self.params = {}

    def add_param(self, key, value):
        self.params[key] = value
        return self

    def update_params(self, params):
        self.params.update(params)
        return self

    def with_client(self, preset):
        """套用 utils.jd_util 里的 CLIENT_* 预设（appid/client/clientVersion/loginType）。"""
        for key, value in preset.items():
            self.params.setdefault(key, value)
        return self

    def with_body(self, body):
        """body 统一用紧凑 JSON：签名和实际发送必须是同一个字符串，
        分隔符带空格会导致 sha256 不一致，签名直接失效。"""
        if not isinstance(body, str):
            body = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        self.params["body"] = body
        return self

    def with_jsonp(self, callback=None):
        self.params["jsonp"] = callback or random_jquery_callback()
        return self

    def with_h5st(self, app_id=APPID_PC_SEARCH):
        """挑出参与签名的字段算 h5st 并塞回 query（现网 5.3）。

        注意 query 里的 `t` 必须保持成「签名时用的那个 t」——网关按它重算签名。
        h5st 内部第 7 段是库自己的时间戳（与 t 差几毫秒），不要拿它覆盖 t。

        浏览器传给 ``PSign.sign`` 的 ``body`` 不是原 JSON，而是原 JSON 的
        SHA-256；query 仍发送原 JSON。实网 A/B 已确认原 JSON 参与签名会
        403，预哈希版本返回 code=0。
        """
        from utils.h5st5 import sign as _sign5

        sign_params = {k: self.params[k] for k in H5ST_SIGN_KEYS if k in self.params}
        if "body" in sign_params:
            sign_params["body"] = sha256_hex(sign_params["body"])
        self.params["h5st"] = _sign5(sign_params, app_id).get("h5st", "")
        return self

    def with_timestamp(self, key="t"):
        self.params[key] = str(now_ms())
        return self

    def reorder(self, order):
        """按浏览器实抓的顺序重排 query。不在 order 里的键按原顺序垫在后面。"""
        if not order:
            return self
        ordered = {k: self.params[k] for k in order if k in self.params}
        for key, value in self.params.items():
            ordered.setdefault(key, value)
        self.params = ordered
        return self

    def body_hash(self):
        return sha256_hex(self.params.get("body", ""))

    def get(self):
        return self.params

    def items(self):
        """Return query items in their current insertion order.

        The normal parameter set is a mapping, but the browser occasionally
        emits duplicate query keys (the search request has two ``t`` values).
        Callers that need to preserve those duplicates should use this list of
        pairs when handing parameters to the HTTP client rather than coercing
        it back to a dict.
        """
        return list(self.params.items())

    def to_string(self):
        return "&".join(f"{k}={v}" for k, v in self.params.items())
