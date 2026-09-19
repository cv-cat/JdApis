# coding: utf-8
"""请求头构造器。对齐 ../DouYin_Spider/builder/header.py 的 Header / HeaderBuilder 范式。"""

from enum import Enum

from utils.fingerprint import get_profile
from utils.http_client import accept_encoding


class HeaderType(Enum):
    GET = "GET"
    POST = "POST"
    FORM = "FORM"
    DOC = "DOC"
    JSONP = "JSONP"
    # Passport QR endpoints use image/script fetches whose header set differs
    # from both document navigation and the generic JSONP API profile.
    QR_IMAGE = "QR_IMAGE"
    QR_JSONP = "QR_JSONP"
    QR_VALIDATION = "QR_VALIDATION"
    # 逐字段照抄 Chrome 实抓，连顺序一起。FETCH/AXIOS 两档见 HeaderBuilder 注释。
    XHR = "XHR"
    XHR_FORM = "XHR_FORM"
    AXIOS = "AXIOS"
    AXIOS_FORM = "AXIOS_FORM"
    ORDER_DOC = "ORDER_DOC"


class Header:
    def __init__(self):
        self.headers = {}

    def set_header(self, key, value):
        self.headers[key] = value
        return self

    def set_referer(self, url):
        return self.set_header("referer", url)

    def set_origin(self, url):
        return self.set_header("origin", url)

    def remove_header(self, key):
        self.headers.pop(key, None)
        return self

    def update(self, mapping):
        self.headers.update(mapping)
        return self

    def reorder(self, order):
        """按给定顺序重排。浏览器的头是有固定顺序的，`requests` 按 dict 顺序发，
        所以想和浏览器一致就得在这里排一次。不在 order 里的键按原顺序垫在后面。"""
        ordered = {k: self.headers[k] for k in order if k in self.headers}
        for k, v in self.headers.items():
            ordered.setdefault(k, v)
        self.headers = ordered
        return self

    def get(self):
        return self.headers

    def __call__(self):
        return self.headers


class HeaderBuilder:
    ua = get_profile()["ua"]
    sec_ch_ua = get_profile()["sec_ch_ua"]
    sec_ch_ua_mobile = get_profile()["sec_ch_ua_mobile"]
    sec_ch_ua_platform = get_profile()["sec_ch_ua_platform"]

    # 京东 PC 前端有**两套**发请求的代码，头不一样，必须按接口分档。
    # 当前 Chrome 152 抓包中 searchWare 也属于 AXIOS 档；不要按站点名称猜，
    # 由调用方传 rp_client 决定是否带 x-referer-page / x-rp-client。
    # dict 保持插入顺序，HTTP client 会按这个顺序发送。
    FETCH_ORDER = ("sec-ch-ua-platform", "referer", "sec-ch-ua", "sec-ch-ua-mobile",
                   "user-agent", "accept", "accept-encoding",
                   "accept-language", "content-type", "origin", "priority",
                   "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site")
    AXIOS_ORDER = ("sec-ch-ua-platform", "referer", "sec-ch-ua", "sec-ch-ua-mobile",
                   "user-agent", "accept", "x-referer-page",
                   "content-type", "x-rp-client", "accept-encoding",
                   "accept-language", "origin", "priority",
                   "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site")

    ORDER_DOC_ORDER = (
        "upgrade-insecure-requests", "user-agent", "accept", "accept-encoding",
        "accept-language", "priority", "sec-fetch-dest", "sec-fetch-mode",
        "sec-fetch-site", "sec-fetch-user",
    )

    @staticmethod
    def build_xhr(axios=False, form=False, content_type=None, accept=None):
        """PC / 咚咚接口用这个，别用 build(GET)——后者带了浏览器不发的头。"""
        header = Header()
        header.update({
            "sec-ch-ua-platform": HeaderBuilder.sec_ch_ua_platform,
            "sec-ch-ua": HeaderBuilder.sec_ch_ua,
            "sec-ch-ua-mobile": get_profile()["sec_ch_ua_mobile"],
            "user-agent": HeaderBuilder.ua,
            "accept": (accept or ("application/json, text/plain, */*"
                                  if axios else "*/*")),
            # 浏览器报 `gzip, deflate, br, zstd`。走 curl_cffi 时能解 zstd 就照报，
            # 退回 requests 时去掉 zstd（它解不开，报了会拿到一段乱码）。
            "accept-encoding": accept_encoding(),
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,zh-TW;q=0.7,ja;q=0.6",
            "priority": "u=1, i",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
        })
        if form:
            header.set_header(
                "content-type",
                content_type or "application/x-www-form-urlencoded;charset=UTF-8",
            )
        elif content_type is not None:
            # Some browser GET requests explicitly carry a form content type
            # while others omit it.  Keep this opt-in so each endpoint can
            # follow its captured Network request exactly.
            header.set_header("content-type", content_type)
        return header

    @staticmethod
    def build(header_type=HeaderType.GET):
        if header_type in (HeaderType.XHR, HeaderType.XHR_FORM):
            return HeaderBuilder.build_xhr(form=header_type is HeaderType.XHR_FORM)
        if header_type in (HeaderType.AXIOS, HeaderType.AXIOS_FORM):
            return HeaderBuilder.build_xhr(
                axios=True, form=header_type is HeaderType.AXIOS_FORM)
        if header_type is HeaderType.ORDER_DOC:
            return HeaderBuilder.build_order_doc()
        if header_type is HeaderType.QR_IMAGE:
            return HeaderBuilder.build_qr_image()
        if header_type is HeaderType.QR_JSONP:
            return HeaderBuilder.build_qr_jsonp()
        if header_type is HeaderType.QR_VALIDATION:
            return HeaderBuilder.build_qr_validation()
        header = Header()
        header.update({
            "user-agent": HeaderBuilder.ua,
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
            "cache-control": "no-cache",
            "pragma": "no-cache",
            "sec-ch-ua": HeaderBuilder.sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": HeaderBuilder.sec_ch_ua_platform,
        })
        if header_type == HeaderType.GET:
            header.update({
                "accept": "application/json, text/plain, */*",
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-site",
            })
        elif header_type == HeaderType.POST:
            header.update({
                "accept": "application/json, text/plain, */*",
                "content-type": "application/json; charset=UTF-8",
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-site",
            })
        elif header_type == HeaderType.FORM:
            header.update({
                "accept": "application/json, text/plain, */*",
                "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-site",
            })
        elif header_type == HeaderType.JSONP:
            # 扫码轮询走 <script> 注入，浏览器发的是 script 请求而非 xhr
            header.update({
                "accept": "*/*",
                "sec-fetch-dest": "script",
                "sec-fetch-mode": "no-cors",
                "sec-fetch-site": "same-site",
            })
        elif header_type == HeaderType.DOC:
            header.update({
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                          "image/avif,image/webp,image/apng,*/*;q=0.8",
                "sec-fetch-dest": "document",
                "sec-fetch-mode": "navigate",
                "sec-fetch-site": "none",
                "sec-fetch-user": "?1",
                "upgrade-insecure-requests": "1",
            })
        return header

    @staticmethod
    def build_qr_image():
        """Chrome headers for ``qr.m.jd.com/show`` image fetches.

        Captured Network order/value (Chrome 152, passport login) includes
        the three ``sec-ch-ua*`` client-hint headers.  It is an image fetch,
        not a document navigation, so it must not carry
        ``upgrade-insecure-requests``.
        """
        header = Header()
        header.update({
            "sec-ch-ua-platform": HeaderBuilder.sec_ch_ua_platform,
            "user-agent": HeaderBuilder.ua,
            "referer": "https://passport.jd.com/",
            "sec-ch-ua": HeaderBuilder.sec_ch_ua,
            "sec-ch-ua-mobile": HeaderBuilder.sec_ch_ua_mobile,
            "accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "accept-encoding": accept_encoding(),
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,zh-TW;q=0.7,ja;q=0.6",
            "priority": "u=1, i",
            "sec-fetch-dest": "image",
            "sec-fetch-mode": "no-cors",
            "sec-fetch-site": "same-site",
        })
        return header.reorder(("sec-ch-ua-platform", "referer", "user-agent",
                               "sec-ch-ua", "sec-ch-ua-mobile", "accept",
                               "accept-encoding", "accept-language", "priority",
                               "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site"))

    @staticmethod
    def build_qr_jsonp():
        """Chrome headers for the QR ``<script>`` polling request.

        The real request has no ``priority`` or content type; retaining the
        generic JSONP profile here would add a header Chrome did not send.
        Chrome 152 does include the three ``sec-ch-ua*`` client hints.
        """
        header = Header()
        header.update({
            "sec-ch-ua-platform": HeaderBuilder.sec_ch_ua_platform,
            "user-agent": HeaderBuilder.ua,
            "referer": "https://passport.jd.com/",
            "sec-ch-ua": HeaderBuilder.sec_ch_ua,
            "sec-ch-ua-mobile": HeaderBuilder.sec_ch_ua_mobile,
            "accept": "*/*",
            "accept-encoding": accept_encoding(),
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,zh-TW;q=0.7,ja;q=0.6",
            "sec-fetch-dest": "script",
            "sec-fetch-mode": "no-cors",
            "sec-fetch-site": "same-site",
        })
        return header.reorder(("sec-ch-ua-platform", "referer", "user-agent",
                               "sec-ch-ua", "sec-ch-ua-mobile", "accept",
                               "accept-encoding", "accept-language",
                               "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site"))

    @staticmethod
    def build_qr_validation(trace_headers=None):
        """Static portion of the passport ticket-validation AJAX headers.

        ``$.getJSON`` is an XMLHttpRequest and the Network capture shows the
        jQuery accept value plus ``X-Requested-With``.  SGM/JDAS tracing
        headers are per-page values and can be supplied by the caller through
        ``extra_headers``; inventing them here would make the request unlike
        the browser.  The returned order matches the captured static portion.
        """
        trace_headers = trace_headers or {}
        header = Header()
        # SGM/JDAS headers are page-scoped and must never be fabricated.  When
        # the caller has captured them, retain Chrome's placement around the
        # ordinary jQuery headers.
        for key in ("sgm-context", "jdas-trace-id"):
            if trace_headers.get(key) not in (None, ""):
                header.set_header(key, trace_headers[key])
        header.set_header("sec-ch-ua-platform", HeaderBuilder.sec_ch_ua_platform)
        header.set_header("referer", "https://passport.jd.com/new/login.aspx?ReturnUrl=https://home.jd.com/index.html")
        header.set_header("sec-ch-ua", HeaderBuilder.sec_ch_ua)
        header.set_header("sec-ch-ua-mobile", HeaderBuilder.sec_ch_ua_mobile)
        header.set_header("x-requested-with", "XMLHttpRequest")
        header.set_header("user-agent", HeaderBuilder.ua)
        header.set_header("accept", "application/json, text/javascript, */*; q=0.01")
        for key in ("jdas-page-id", "jdas-session-id"):
            if trace_headers.get(key) not in (None, ""):
                header.set_header(key, trace_headers[key])
        header.update({
            "accept-encoding": accept_encoding(),
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,zh-TW;q=0.7,ja;q=0.6",
            "priority": "u=1, i",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        })
        return header

    @staticmethod
    def build_order_doc():
        """订单中心 HTML 导航请求的专用 header。

        这是 Chrome 的 document 请求，不是 XHR：没有 referer、origin、
        cache-control、pragma 或 sec-ch-ua；``priority`` 也与 API 请求不同。
        """
        header = Header()
        header.update({
            "upgrade-insecure-requests": "1",
            "user-agent": HeaderBuilder.ua,
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                      "image/avif,image/webp,image/apng,*/*;q=0.8,"
                      "application/signed-exchange;v=b3;q=0.7",
            "accept-encoding": accept_encoding(),
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,zh-TW;q=0.7,ja;q=0.6",
            "priority": "u=0, i",
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "none",
            "sec-fetch-user": "?1",
        })
        return header

    @staticmethod
    def build_ws():
        """咚咚 WebSocket 握手头。"""
        return {
            "User-Agent": HeaderBuilder.ua,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
