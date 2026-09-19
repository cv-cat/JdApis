# coding: utf-8
"""京东 PC 扫码登录。

三步纯 HTTP；二维码展示/轮询不碰 eid / fp / jsToken，ticket 兑换则按
登录页 Network 的源码链路补齐 passport h5st、_stk 与 AKS 加密 query：
    1. GET qr.m.jd.com/show   → 二维码 PNG + Set-Cookie wlfstk_smdl
    2. GET qr.m.jd.com/check  → jsonp 轮询，扫码确认后返回 ticket
    3. GET passport.jd.com/uc/qrCodeTicketValidation?aksParamsU=<密文>
       → Set-Cookie **thor + pin**（PC 端主票据；`pt_key`/`pt_pin` 是 M 端的，
         这条链路不产生）

第 3 步的八个 query 字段先按 jQuery 顺序编码，再由 `aks.js` 加密成单一
`aksParamsU`；明文 `?t=` 不再作为默认路径，避免发送浏览器没有的缺字段请求。
"""

import time
from urllib.parse import parse_qs, unquote, urlsplit

import requests
from loguru import logger

from builder.header import HeaderBuilder, HeaderType
from utils import http_client
from utils.jd_util import now_ms, parse_jsonp, random_jquery_callback
from utils.trace_headers import LoginTraceContext

requests.packages.urllib3.disable_warnings()


class JdLoginAPI:
    qr_url = "https://qr.m.jd.com"
    passport_url = "https://passport.jd.com"
    # Chrome 地址栏与 Network 都保留 ReturnUrl 的未预编码形式；预先转义会把
    # Referer 从 79 变成 87 字符，并让 loginService 的 aksParamsU 由 292 变 312。
    login_page = "https://passport.jd.com/new/login.aspx?ReturnUrl=https://home.jd.com/index.html"

    # 扫码业务标识，实抓固定 133
    APPID = 133
    # 登录页实测轮询间隔，调得过快容易触发限流。
    POLL_INTERVAL = 3
    # 二维码服务端有效期约 2 分钟，到点自动换一张，免得用户扫到过期的
    QR_REFRESH = 100
    PAGE_SOURCE = "login2025"
    PAGE_LOCATION = ""
    FIRST_SHOW_ACCOUNT_LOGIN_PAGE = "f"
    # Current login page's getAliveSsoDomains() result (captured in Chrome).
    # Callers can override this when their Network page exposes a different
    # list; an empty default would be a missing browser field.
    SSO_DOMAINS = ",".join((
        "sso.jd.hk", "sso.jkcsjd.com", "sso.healthjd.com", "sso.jingxi.com",
        "sso.jdh.com", "sso.jingdong.com", "ssa.7fresh.com", "sso.jdpay.com",
        "sso.jingdonghealth.cn", "sso.vipmro.com", "sso.yiyaojd.com",
        "sso.jdcloud.com", "sso.jdl.com", "sso.jhscm.com", "sso.jddj.com",
    ))

    @staticmethod
    def get_qrcode(auth, session=None, size=147):
        """取二维码图片字节，并把 wlfstk_smdl 等 cookie 写回 auth。

        :return: (success, msg, png_bytes)
        """
        session = session or http_client.session()
        headers = HeaderBuilder.build(HeaderType.QR_IMAGE)
        params = {
            "appid": JdLoginAPI.APPID,
            "size": size,
            "t": now_ms(),
        }
        resp = session.get(
            f"{JdLoginAPI.qr_url}/show", headers=headers.get(),
            params=params, cookies=auth.cookie, verify=False, timeout=15,
        )
        auth.absorb_response(resp, session=session)
        if resp.status_code != 200 or not resp.content:
            return False, f"取二维码失败 HTTP {resp.status_code}", None
        if not auth.cookie.get("wlfstk_smdl"):
            return False, "响应里没有 wlfstk_smdl，无法轮询", None
        return True, "ok", resp.content

    @staticmethod
    def check_qrcode(auth, session=None):
        """轮询一次扫码状态。返回服务端原始 dict。

        实测响应形如 {"code":201,"msg":"二维码未扫描 ..."}；
        扫码并确认后 code=200 且带 ticket。
        """
        session = session or http_client.session()
        headers = HeaderBuilder.build(HeaderType.QR_JSONP)
        params = {
            "callback": random_jquery_callback(),
            "appid": JdLoginAPI.APPID,
            "token": auth.cookie.get("wlfstk_smdl", ""),
            "_": now_ms(),
        }
        resp = session.get(
            f"{JdLoginAPI.qr_url}/check", headers=headers.get(),
            params=params, cookies=auth.cookie, verify=False, timeout=15,
        )
        auth.absorb_response(resp, session=session)
        return parse_jsonp(resp.text) or {"code": -1, "raw": resp.text}

    @staticmethod
    def _return_url():
        raw = parse_qs(urlsplit(JdLoginAPI.login_page).query).get("ReturnUrl", [""])[0]
        return unquote(raw)

    @staticmethod
    def _prepare_trace_context(auth, session):
        """加载登录页，取得页面下发的 ``#uuid`` 并建立本地 JDAS 会话。"""
        headers = HeaderBuilder.build(HeaderType.DOC)
        resp = session.get(
            JdLoginAPI.login_page, headers=headers.get(), cookies=auth.cookie,
            verify=False, timeout=15,
        )
        auth.absorb_response(resp, session=session)
        if resp.status_code != 200:
            raise RuntimeError(f"登录页初始化失败 HTTP {resp.status_code}")
        try:
            return LoginTraceContext.from_html(resp.text)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc

    @staticmethod
    def _public_key(auth, session, trace_headers=None):
        """Fetch the exact public key used by passport's ``aks.js`` hook."""
        required_trace = ("sgm-context", "jdas-trace-id", "jdas-page-id", "jdas-session-id")
        missing_trace = [k for k in required_trace
                         if not (trace_headers or {}).get(k)]
        if missing_trace:
            raise ValueError(
                "publicKey/init 缺少当前登录页 Network 的动态 trace headers："
                + ", ".join(missing_trace)
            )
        headers = HeaderBuilder.build_qr_validation(trace_headers)
        # ``publicKey/init`` is also an AJAX call, but its captured accept is
        # the generic ``*/*`` value rather than jQuery's JSON value used by
        # ticket validation.
        headers.set_header("accept", "*/*")
        resp = session.get(
            f"{JdLoginAPI.passport_url}/publicKey/init",
            headers=headers.get(), cookies=auth.cookie, verify=False, timeout=15,
        )
        auth.absorb_response(resp, session=session)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise RuntimeError(f"publicKey/init 返回非 JSON：{resp.text[:200]}") from exc
        public_key = payload.get("data") if isinstance(payload, dict) else None
        if not public_key:
            raise RuntimeError(f"publicKey/init 缺少 data：{payload!r}")
        return public_key

    @staticmethod
    def validate_ticket(auth, ticket, session=None, *, page_source=None,
                        page_location=None, return_url=None,
                        first_show_account_login_page=None, sso_domains=None,
                        trace_headers=None, trace_context=None):
        """用 ticket 换登录态 cookie（thor / pin）。

        :return: (success, msg, res_json)
        """
        session = session or http_client.session()
        before = auth.cookie.get("thor")
        if not ticket:
            return False, "ticket 为空，拒绝发送 validation 请求", {"error": "missing ticket"}
        page_source = (JdLoginAPI.PAGE_SOURCE if page_source is None else page_source)
        page_location = (JdLoginAPI.PAGE_LOCATION if page_location is None else page_location)
        return_url = JdLoginAPI._return_url() if return_url is None else return_url
        first_show_account_login_page = (
            JdLoginAPI.FIRST_SHOW_ACCOUNT_LOGIN_PAGE
            if first_show_account_login_page is None else first_show_account_login_page
        )
        sso_domains = JdLoginAPI.SSO_DOMAINS if sso_domains is None else sso_domains
        # The browser signs the one-field object {t: ticket} with passport's
        # appId before assembling the eight-field jQuery query.
        from utils import aks, h5st5
        from utils.jd_util import APPID_PASSPORT

        h5st5.configure(auth.cookie_str, JdLoginAPI.passport_url,
                        JdLoginAPI.login_page)
        signed = h5st5.sign({"t": ticket}, APPID_PASSPORT)
        h5st = signed.get("h5st", "")
        stk = signed.get("_stk", "")
        if not h5st or not stk:
            return False, "passport h5st/_stk 生成失败，拒绝发送缺字段请求", {
                "error": "missing h5st/_stk", "signed": signed,
            }

        pairs = [
            ("t", ticket),
            ("pageSource", page_source),
            ("pageLocation", page_location),
            ("ReturnUrl", return_url),
            ("h5st", h5st),
            ("_stk", stk),
            ("firstShowAccountLoginPage", first_show_account_login_page),
            ("ssoDomains", sso_domains),
        ]
        encoded_query = aks.encode_query_pairs(pairs)
        public_trace_headers = (trace_context.next_headers(trace_headers)
                                if trace_context else trace_headers)
        public_key = JdLoginAPI._public_key(auth, session, public_trace_headers)
        encrypted = aks.encrypt_query(encoded_query, public_key)
        validation_trace_headers = (trace_context.next_headers(trace_headers)
                                    if trace_context else trace_headers)
        headers = HeaderBuilder.build_qr_validation(validation_trace_headers)
        headers.set_referer(JdLoginAPI.login_page)
        resp = session.get(
            f"{JdLoginAPI.passport_url}/uc/qrCodeTicketValidation",
            headers=headers.get(), params=[("aksParamsU", encrypted)],
            cookies=auth.cookie, verify=False, timeout=15,
        )
        # 登录态沿 SSO 跳转链逐跳下发，统一入口会同时吸收 history、最终响应
        # 和 Session 当前 CookieJar，并立即写入 auth 文件。
        auth.absorb_response(resp, session=session)
        try:
            res_json = resp.json()
        except ValueError:
            res_json = {"raw": resp.text[:500]}

        # The official success payload is ``returnCode: 0`` and may reuse the
        # same thor when the account was already signed in.  Requiring a
        # changed thor would reject a valid equivalent re-login; using
        # the response code prevents an old thor in the jar from masking a
        # failed ticket exchange.
        if isinstance(res_json, dict) and "returnCode" in res_json:
            return_code = res_json.get("returnCode")
            if return_code not in (0, "0"):
                return False, f"ticket 兑换失败 returnCode={return_code}：{res_json}", res_json
        after = auth.cookie.get("thor")
        if not after:
            return False, f"ticket 兑换未拿到 thor：{res_json}", res_json
        if before and after == before:
            logger.info("ticket 兑换成功；当前会话复用了原 thor")
        return True, "登录成功", res_json

    @staticmethod
    def qr_login(auth, qr_path="qrcode.png", timeout=300, on_qrcode=None,
                 validation_kwargs=None):
        """扫码登录全链路编排。二维码过期会自动换一张。

        :param on_qrcode: 拿到二维码后的回调，签名 (png_bytes, qr_path)
        :return: (success, msg, auth)
        """
        # Keep QR show → poll → ticket validation on the same Chrome-like
        # transport/session.  A plain requests.Session here would switch the
        # login flow back to HTTP/1.1 and a different TLS fingerprint.
        session = http_client.session()
        trace_context = JdLoginAPI._prepare_trace_context(auth, session)
        validation_kwargs = dict(validation_kwargs or {})
        validation_kwargs["trace_context"] = trace_context
        deadline = time.time() + timeout
        last_code = None
        next_refresh = 0.0

        while time.time() < deadline:
            if time.time() >= next_refresh:
                ok, msg, png = JdLoginAPI.get_qrcode(auth, session)
                if not ok:
                    return False, msg, auth
                with open(qr_path, "wb") as f:
                    f.write(png)
                next_refresh = time.time() + JdLoginAPI.QR_REFRESH
                last_code = None
                logger.info(f"二维码已刷新 → {qr_path}，请用京东 App 扫码并确认")
                if on_qrcode:
                    on_qrcode(png, qr_path)

            time.sleep(JdLoginAPI.POLL_INTERVAL)
            res = JdLoginAPI.check_qrcode(auth, session)
            code = res.get("code")
            if code != last_code:
                logger.info(f"check → {res}")
                last_code = code
            if code == 200 and res.get("ticket"):
                ok, msg, _ = JdLoginAPI.validate_ticket(
                    auth, res["ticket"], session,
                    **validation_kwargs,
                )
                return ok, msg, auth

        return False, f"{timeout}s 内未完成扫码", auth
