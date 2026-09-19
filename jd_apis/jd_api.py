# coding: utf-8
"""京东业务接口（商品 / 订单 / 咚咚会话）。

对齐 ../DouYin_Spider/dy_apis/douyin_api.py：类内全静态方法，第一个参数是 auth，
数据接口直接返回 res.json()。

请求契约取自 2026-08-15 对 jdcs.jd.com 的实抓：
    POST https://api.m.jd.com/client.action
        ?functionId=<fn>&client=<c>&appid=<c>&clientVersion=1.0.0&loginType=3
        &t=<ms>&h5st=<签名>&x-api-eid-token=<3AB9D23F7A4B3CSS>
    Content-Type: application/x-www-form-urlencoded
    body=<urlencode(紧凑 JSON)>
"""

import base64
import json
import re
import secrets
import time
from urllib.parse import quote, unquote, urlencode

from loguru import logger

from builder.header import Header, HeaderBuilder, HeaderType
from builder.params import Params
from utils import http_client

from utils.jd_util import (
    APPID_DONGDONG,
    APPID_PC_ITEM,
    APPID_PC_SEARCH,
    ORDER_PC_API,
    ORDER_PC_ITEM,
    ORDER_PC_ITEM_DIVINER_PAGE,
    ORDER_PC_ITEM_RELWORDS,
    ORDER_PC_ITEM_VENDER_FOLLOW,
    ORDER_PC_ORDER,
    ORDER_PC_SEARCH,
    ORDER_PC_SEARCH_PLAIN,
    ORDER_PC_SEARCH_RELWORDS,
    ORDER_DD_NO_TIME,
    ORDER_DD_WITH_TIME,
    RP_CLIENT_CHAT,
    RP_CLIENT_ITEM,
    RP_CLIENT_SEARCH,
    CLIENT_IMH5,
    CLIENT_PC_ITEM,
    CLIENT_PC_ITEM_V3,
    CLIENT_PC_SEARCH,
    CLIENT_ORDER,
    CLIENT_WH5,
    DEFAULT_AREA,
    area_of,
    search_uuid_of,
    sha256_hex,
    xor5_encode,
    now_ms,
    parse_jsonp,
    strip_tags,
)

# 免签名的纯查询接口在浏览器实抓里不带这两个 query 参数
UNSIGNED_DROP = ("loginType", "x-api-eid-token")


class JdAPI:
    api_url = "https://api.m.jd.com"
    item_origin = "https://item.jd.com"
    item_referer = "https://item.jd.com/"
    search_origin = "https://search.jd.com"
    search_referer = ("https://search.jd.com/Search?keyword=%E6%89%8B%E6%9C%BA"
                      "&enc=utf-8")
    chat_origin = "https://jdcs.jd.com"
    chat_referer = "https://jdcs.jd.com/"

    @staticmethod
    def _api_cookies(auth):
        """发送当前 auth 的完整 Cookie，不再维护接口字段白名单。"""
        return {str(key): str(value) for key, value in auth.cookie.items()
                if value not in (None, "")}

    @staticmethod
    def _search_page_referer(auth, keyword: str = "") -> str:
        """Return the full search-page Referer for the current session.

        ``searchWare`` records the keyword on the auth session so follow-up
        widgets (hotwords, cart, equity, …) can carry the same Referer rather
        than falling back to a guessed URL.
        """
        keyword = keyword or getattr(auth, "search_keyword", "")
        if keyword:
            return (f"https://search.jd.com/Search?keyword={quote(keyword)}"
                    "&enc=utf-8")
        return JdAPI.search_referer

    @staticmethod
    def _item_page_referer(sku: str = "") -> str:
        """商品页请求的真实 Referer；Network 中是具体 `.html` 页面 URL。"""
        return f"https://item.jd.com/{sku}.html" if sku else JdAPI.item_referer

    # ---------- 通用底座 ----------

    @staticmethod
    def call_api(auth, function_id, body=None, client_preset=None, extra_params=None,
                 app_id=APPID_PC_ITEM, path="/client.action", jsonp=None,
                 referer=None, origin=None, referer_page=None,
                 rp_client=None, method="POST", with_time=True,
                 sign=True, body_in_query=False, retry=2, param_order=None,
                 with_uuid=True, drop_params=None, append_time=False,
                 content_type=None, accept=None, uuid_value=None,
                 axios=None):
        """api.m.jd.com 通用调用：装配 query → 算 h5st → 发请求。

        :param sign: 少数接口（relsearch / pc_search_hotwords）不需要 h5st
        :param body_in_query: POST 但 body 留在 query 里（表单体为空）。
            咚咚那几个接口是 form-encoded body，而 PC 侧的 `pcCart_jc_getCartNum`
            实抓是「POST + body 在 query」——发成表单会被 API 层顶回
            `code:1 request Content-Type is not compatible with application/json`。
        :param retry: 403 空 body 时重签重试几次。边缘层用同一个 403 表达
            「签名不对」和「打太频了被限」两件事，后者是偶发的（实测连打十几次
            会漏一两个），重签一次基本就过。签名真的不对时重试也救不回来。
        :param rp_client: 只有咚咚那几个接口要 `x-referer-page` + `x-rp-client`；
            PC 站的接口浏览器**根本不发**这两个头，传 None 就不发。
        :param axios: 只选择 Axios 的 Accept/头顺序，不隐含添加 `x-rp-*`。
            留空时为兼容旧调用，仍由 ``rp_client`` 判断。
        :param param_order: query 参数顺序，照抄浏览器实抓（见 utils.jd_util 的
            `ORDER_*`）。不传就用装配顺序。
        :param append_time: 在已签名的 query 末尾再追加一个 `t`。搜索页
            `pc_search_searchWare` 的浏览器 URL 确实出现两个同名 `t`，
            第二个由调用层追加，不能用 dict 覆盖掉第一个。
        :param content_type: 显式覆盖请求的 Content-Type。不同接口的 GET
            请求有的带 `application/x-www-form-urlencoded`，有的完全不带，
            所以不能按 HTTP method 一刀切。
        :param accept: 显式覆盖 Accept。订单中心的 jQuery 调用分别报
            `*/*` 或 `application/json, text/javascript, */*; q=0.01`。
        :param uuid_value: 覆盖 query 中的 uuid。订单页使用运行时生成的复合
            uuid，不能把 `__jdu` 猜成订单 uuid；未传时才使用 `__jdu`。
        """
        params = Params()
        params.with_client(client_preset or CLIENT_WH5)
        params.add_param("functionId", function_id)
        # uuid 就是 __jdu cookie。多数 PC 接口要带，但 axios 那一档（getCartNum
        # 之类）浏览器实抓是**不带**的，多带一个参数就和抓包对不上了。
        if with_uuid:
            actual_uuid = (auth.cookie.get("__jdu") if uuid_value is None
                           else uuid_value)
            if actual_uuid:
                params.add_param("uuid", actual_uuid)
        if body is not None:
            params.with_body(body)
        if jsonp:
            params.with_jsonp(jsonp)
        if extra_params:
            params.update_params(extra_params)
        if sign:
            # 签名进程要用同一套 cookie 去 cactus 换 token，来源也得和业务请求同域
            from utils.h5st5 import configure as _cfg5
            _cfg5(auth.cookie_str, origin or JdAPI.item_origin,
                  referer or JdAPI.item_referer)

        # 风控指纹令牌，浏览器每个请求都带；取自 cookie，不参与 h5st 签名
        eid_token = auth.cookie.get("3AB9D23F7A4B3CSS")
        if eid_token:
            params.add_param("x-api-eid-token", eid_token)

        # 免签名的纯查询接口（hotwords / relwords / aiPicTagInfo / getUmcEquity）
        # 浏览器实抓里既不发 loginType 也不发 x-api-eid-token，这里按需摘掉。
        for key in (drop_params or ()):
            params.params.pop(key, None)

        # 头分两档，按浏览器实抓走：原生 fetch 的接口用 FETCH 档、axios 封装的
        # 用 AXIOS 档（多 x-referer-page/x-rp-client，accept 也不同）。
        # 判据就是 rp_client 有没有传 —— 带这两个头的就是 axios 那一档。
        axios = bool(rp_client) if axios is None else bool(axios)
        if jsonp:
            headers = HeaderBuilder.build(HeaderType.JSONP)
        elif method.upper() != "POST" or body_in_query:
            # body 留在 query 的那类 POST（getCartNum）不带表单体，
            # 报 form-urlencoded 会被 API 层顶回 code:1 Content-Type 不兼容。
            headers = HeaderBuilder.build_xhr(axios=axios, accept=accept)
        else:
            headers = HeaderBuilder.build_xhr(
                axios=axios, form=True, accept=accept)
        if content_type is not None:
            headers.set_header("content-type", content_type)
        headers.set_referer(referer or JdAPI.item_referer)
        if origin:
            headers.set_origin(origin)
        if rp_client:
            headers.set_header("x-referer-page", referer_page or (origin or "") + "/")
            headers.set_header("x-rp-client", rp_client)
        headers.reorder(HeaderBuilder.AXIOS_ORDER if axios
                        else HeaderBuilder.FETCH_ORDER)

        url = f"{JdAPI.api_url}{path}"
        for attempt in range(retry + 1):
            # t 和 h5st 每次重发都重算：重放同一个签名不会有别的结果
            if with_time:
                params.add_param("t", str(now_ms()))
            if sign:
                params.with_h5st(app_id)

            # Use a list of pairs all the way to the HTTP client.  A dict would
            # silently collapse the duplicate trailing `t` in searchWare.
            query_items = (params.reorder(param_order).items()
                           if param_order else params.items())
            if append_time:
                query_items.append(("t", str(now_ms())))
            if method.upper() == "POST":
                # body 走表单，其余留在 query —— 与浏览器一致
                form_body = ""
                if not body_in_query:
                    for key, value in query_items:
                        if key == "body":
                            form_body = value
                            break
                    query_items = [(key, value) for key, value in query_items
                                   if key != "body"]
                resp = http_client.post(
                    url, headers=headers.get(),
                    cookies=JdAPI._api_cookies(auth),
                    params=query_items,
                    data=None if body_in_query else {"body": form_body})
            else:
                resp = http_client.get(url, headers=headers.get(),
                                       cookies=JdAPI._api_cookies(auth), params=query_items)
            JdAPI._absorb_response(auth, resp)
            rejected = resp.status_code == 403 and not resp.content
            risk = JdAPI._read_disposal(resp)
            if risk:
                logger.error(f"{function_id} 被风控拦下：{risk}")
                return JdAPI._parse(resp)
            if not rejected or attempt == retry:
                if rejected:
                    logger.warning(f"{function_id} 连续 {retry + 1} 次 403 空 body："
                                   f"跑 JdAPI.diagnose(auth) 分清是限流、风控还是签名问题")
                return JdAPI._parse(resp)
            time.sleep(0.6 * (attempt + 1))

    @staticmethod
    def _absorb_response(auth, resp, session=None):
        """持久化全部 Set-Cookie，并处理非 Cookie 的 sdtoken 响应头。"""
        auth.absorb_response(resp, session=session, persist=False)
        raw = resp.headers.get("x-rp-sdtoken")
        if raw and raw.startswith("set;"):
            parts = raw.split(";", 2)
            if len(parts) == 3:
                auth.update_cookies({"sdtoken": parts[2]})
        auth.flush()

    @staticmethod
    def verification_url(res, referer: str) -> str:
        """从 605 disposal 还原风险页上下文 URL。

        该 URL 仅作为纯程序 JCAP 的 Origin/Referer 上下文，不会打开浏览器。
        参数集来自 PC 风险页当前协议：returnurl / rqhost / rpid / evtype /
        evapi / source / forceCurrentView / evsid。
        """
        disposal = (res or {}).get("disposal") or {}
        try:
            ev = json.loads(disposal.get("evContent") or "{}")
        except ValueError:
            return ""
        # Search 605 currently has two valid shapes: older responses include
        # evSid, while newer PC responses omit it and let createSid establish
        # the JCAP session from rpId.  The official page still sends an empty
        # evSid field in the latter case.
        if not ev.get("evUrl"):
            return ""
        query = {
            "returnurl": referer,
            "rqhost": JdAPI.api_url,
            "rpid": disposal.get("rpId", ""),
            "evtype": ev.get("evType", "2"),
            "evapi": ev.get("evApi", ""),
            "source": "1",
            "forceCurrentView": "1",
            "evsid": ev.get("evSid") or "",
        }
        return ev["evUrl"] + "?" + urlencode(query)

    @staticmethod
    def _risk_post(auth, referer: str, function_id: str, inner: dict) -> dict:
        """Call the risk_h5 envelope used by the PC verification page."""
        from Crypto.Cipher import AES

        raw = json.dumps(inner, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
        padding = 16 - len(raw) % 16
        encrypted = AES.new(
            b"rhiasnkdhandrisk", AES.MODE_CBC, b"r-s-h-n_r_isnkdk"
        ).encrypt(raw + bytes([padding]) * padding)
        outer = {
            "sdkClient": "pc",
            "sdkVersion": "pc_2.1.0",
            "enbody": base64.urlsafe_b64encode(encrypted).decode().rstrip("="),
        }
        form = {
            "appid": "risk_h5",
            "functionId": function_id,
            "body": json.dumps(outer, ensure_ascii=False,
                               separators=(",", ":")),
        }
        eid_token = auth.cookie.get("3AB9D23F7A4B3CSS")
        if eid_token:
            form["x-api-eid-token"] = eid_token
        headers = HeaderBuilder.build_xhr(form=True, accept="application/json")
        headers.set_referer(referer)
        headers.set_origin("https://cfe.m.jd.com")
        response = http_client.post(
            f"{JdAPI.api_url}/api", headers=headers.get(),
            cookies=JdAPI._api_cookies(auth), data=form,
        )
        JdAPI._absorb_response(auth, response)
        return response.json()

    @staticmethod
    def _risk_page_eid(auth) -> str:
        """Read the optional PC risk-page eid from unionwsws.

        The page does not substitute the C9B device id when this cookie is
        absent; C9B remains the separate x-api-eid-token input.
        """
        value = str(auth.cookie.get("unionwsws") or "")
        if not value:
            return ""
        try:
            parsed = json.loads(unquote(value))
        except (TypeError, ValueError):
            return ""
        return str(parsed.get("devicefinger") or "") if isinstance(parsed, dict) else ""

    @staticmethod
    def solve_search_risk(auth, response: dict, keyword: str = "",
                          *, timeout: int = 90, attempts: int = 3) -> dict:
        """Solve a search 605 challenge and retain only x-rp-evtoken.

        Official JCAP JS/WASM and local image models execute in a Node
        subprocess. Tickets and cookies stay in private pipes; only lengths
        and status codes are exposed to callers.
        """
        disposal = (response or {}).get("disposal") or {}
        if not isinstance(disposal, dict):
            return {"code": -1, "stage": "disposal"}
        try:
            event = json.loads(disposal.get("evContent") or "{}")
        except (TypeError, ValueError):
            return {"code": -1, "stage": "disposal"}
        page_url = JdAPI.verification_url(
            response, JdAPI._search_page_referer(auth, keyword))
        if not page_url or not disposal.get("rpId"):
            return {"code": -1, "stage": "disposal"}

        JdAPI.ensure_webm_token(auth, keyword)
        common = {
            "requestId": disposal.get("rpId") or "",
            "evApi": quote(str(event.get("evApi") or ""), safe="~()*!.'-"),
            "evType": str(event.get("evType") or "2"),
            "shshshfpx": auth.cookie.get("shshshfpx") or "",
            "eid": JdAPI._risk_page_eid(auth),
            "evSid": event.get("evSid") or "",
        }
        try:
            created = JdAPI._risk_post(auth, page_url, "createSid", common)
            session_id = str(created.get("data") or "")
            if created.get("code") != 0 or not session_id:
                return {"code": created.get("code"), "stage": "createSid"}

            from utils.jcap_solver import solve_graphic_captcha
            verify_token = solve_graphic_captcha(
                session_id, "", auth.cookie_str, timeout=timeout,
                page_url=page_url, max_solve_attempts=attempts,
                cookie_callback=lambda cookies: auth.update_cookies(
                    cookies, persist=True),
                local_storage=auth.local_storage_for(page_url),
                storage_callback=lambda values: auth.replace_local_storage(
                    page_url, values, persist=True),
            )
            pin = (auth.cookie.get("pwdt_id") or auth.cookie.get("pin")
                   or auth.cookie.get("pt_pin") or "")
            if "*" in pin:
                pin = ""
            checked = JdAPI._risk_post(auth, page_url, "checkToken", {
                "sid": session_id,
                "token": quote(verify_token, safe="~()*!.'-"),
                **common,
                "pin": pin,
            })
            event_token = str(checked.get("data") or "")
            if checked.get("code") != 0 or not event_token:
                return {"code": checked.get("code"), "stage": "checkToken"}
            auth.update_cookies({"x-rp-evtoken": event_token}, persist=True)
            return {"code": 0, "stage": "complete",
                    "token_length": len(event_token)}
        except Exception as exc:
            safe_result = {"code": -1, "stage": "jcap",
                           "error": type(exc).__name__}
            # jcap_solver's exception contains redacted stage metadata only.
            # Surface the service subcode so callers can distinguish an
            # incorrect/blocked interaction (16100) from a retry cap (16130)
            # without exposing the ticket, Cookie or encrypted payload.
            match = re.search(r'"sCode":(\d+)', str(exc))
            if match:
                safe_result["service_subcode"] = int(match.group(1))
            suffix = (f"，服务端子码 {safe_result['service_subcode']}"
                      if "service_subcode" in safe_result else "")
            logger.warning(
                f"纯程序搜索验证未通过：{type(exc).__name__}{suffix}"
            )
            return safe_result

    @staticmethod
    def _read_disposal(resp):
        """解 `x-rp-content` 头，把风控处置意图读出来。

        风控命中时服务端会回 HTTP 200 + 一个 base64 的 `x-rp-content`，里面是
        `{"code":"605","disposal":{"evContent":"{…\\"title\\":\\"京东验证\\"…}"}}`，
        意思是「要人机验证」。**这条信息只在这个头里**，body 可能是空的、
        状态码也可能是 200，光看状态码完全看不出来（我为此白查了一轮）。
        """
        raw = resp.headers.get("x-rp-content")
        if not raw:
            return None
        info = None
        # 这个头的 base64 长度不总是 4 的倍数（末尾可能被多截/少截一位），
        # 直接补 `=` 会抛 binascii.Error，所以按 0~3 位裁剪各试一次。
        for cut in range(4):
            chunk = raw[:len(raw) - cut] if cut else raw
            try:
                decoded = base64.urlsafe_b64decode(chunk + "=" * (-len(chunk) % 4))
                info = json.loads(decoded.decode("utf-8", "replace"))
                break
            except Exception:
                continue
        if info is None:
            return f"x-rp-content 解不开：{raw[:60]}"
        code = info.get("code")
        disposal = info.get("disposal") or {}
        try:
            ev = json.loads(disposal.get("evContent") or "{}")
        except ValueError:
            ev = {}
        title = ev.get("title") or ""
        tip = ev.get("evTypeTip") or ""
        api = ev.get("evApi") or ""
        return (f"code={code} {title} {tip}（接口 {api}）—— 账号/IP 被标记了，"
                f"需要运行纯程序 JCAP 验证，或者等待风控解除")

    @staticmethod
    def _parse(resp):
        try:
            return resp.json()
        except ValueError:
            parsed = parse_jsonp(resp.text)
            if parsed is not None:
                return parsed
            return {"_status": resp.status_code, "_raw": resp.text[:1000]}

    @staticmethod
    def ensure_webm_token(auth, keyword: str = "", *, force: bool = False) -> dict:
        """Run the official PC WebM collector and persist its fingerprint.

        The search page's ``jdwebm.js`` posts ``wsgw_getinfo`` before the
        search request and stores the returned ``whwswswws`` value as
        ``shshshfpb``.  It is not an h5st segment and it is not returned by
        the login endpoints. The vendor bundle runs in Node/JSDOM so fields
        such as ``browser_info`` and the bot-detection block are generated by
        the same code as the search page instead of hand-written guesses.
        """
        # jdwebm.js creates one UUID-shaped local fingerprint and initializes
        # both fpa/fpx from it.  The risk page accepts only
        # 8-4-4-4-12-10digits; a plain UUID without the timestamp is invalid.
        fpa = str(auth.cookie.get("shshshfpa") or "")
        fpx = str(auth.cookie.get("shshshfpx") or "")
        if not fpa:
            raw = secrets.token_hex(16)
            fpa = (f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-"
                   f"{raw[20:]}-{int(time.time())}")
            auth.update_cookies({"shshshfpa": fpa})
        if not fpx:
            auth.update_cookies({"shshshfpx": fpa})
        auth.flush()

        page_url = JdAPI._search_page_referer(auth, keyword)
        webm_version = "6.0.0-exact-1"
        storage = auth.local_storage_for(page_url)
        try:
            valid_until = int(storage.get("hf_time") or 0)
        except (TypeError, ValueError):
            valid_until = 0
        current = str(auth.cookie.get("shshshfpb") or "")
        if (current and not force
                and storage.get("__jdapis_webm_runtime__") == webm_version
                and valid_until > now_ms()):
            auth.flush()
            return {"code": 0, "cached": True, "token_length": len(current)}

        now = now_ms()
        headers = HeaderBuilder.build_xhr(form=True, accept="application/json")
        headers.set_referer(page_url)
        headers.set_origin(JdAPI.search_origin)

        # jdwebm first fetches a domain-specific encrypted control block. It
        # selects/excludes collectors and is consumed by the vendor runtime.
        config_data = ""
        try:
            config_headers = HeaderBuilder.build_xhr(accept="application/json")
            config_headers.set_referer(page_url)
            config_headers.set_origin(JdAPI.search_origin)
            config_response = http_client.get(
                JdAPI.api_url,
                headers=config_headers.get(),
                cookies=JdAPI._api_cookies(auth),
                params={
                    "appid": "risk_h5_info",
                    "functionId": "getCustomCtrl",
                    "t": str(now),
                    "body": json.dumps({"domain": ".jd.com"},
                                       separators=(",", ":")),
                },
            )
            JdAPI._absorb_response(auth, config_response)
            config_result = config_response.json()
            if config_result.get("code") == 0:
                config_data = str(config_result.get("data") or "")
        except Exception as exc:
            logger.warning(f"WebM 控制配置获取失败：{type(exc).__name__}")

        try:
            from utils.webm import build_search_payload

            runtime_storage = dict(storage)
            if force:
                # ``hf_time`` is the vendor runtime's own early-return gate.
                # Removing it only from this subprocess input makes force
                # actually collect again without discarding the persisted
                # token unless the refresh succeeds.
                runtime_storage.pop("hf_time", None)
                runtime_storage.pop("__jdapis_webm_runtime__", None)
            payload = build_search_payload(
                auth, page_url, config_data=config_data,
                local_storage=runtime_storage,
            )
            response = http_client.post(
                JdAPI.api_url,
                headers=headers.get(),
                cookies=JdAPI._api_cookies(auth),
                data={
                    "appid": "risk_h5",
                    "functionId": "wsgw_getinfo",
                    "t": str(now_ms()),
                    "body": json.dumps(payload, ensure_ascii=False,
                                       separators=(",", ":")),
                },
            )
            JdAPI._absorb_response(auth, response)
            result = response.json()
        except Exception as exc:
            logger.warning(f"WebM 指纹初始化失败：{type(exc).__name__}")
            return {"code": -1, "error": type(exc).__name__}

        token = str(result.get("whwswswws") or "")
        if result.get("code") == 0 and token:
            auth.update_cookies({"shshshfpb": token})
            runtime_storage = auth.local_storage_for(page_url)
            runtime_storage["__jdapis_webm_runtime__"] = webm_version
            try:
                interval = max(1, int(result.get("interval") or 24 * 60))
            except (TypeError, ValueError):
                interval = 24 * 60
            runtime_storage["hf_time"] = str(now_ms() + interval * 60_000)
            auth.replace_local_storage(
                page_url, runtime_storage, persist=True,
            )
            return {"code": 0, "cached": False, "token_length": len(token)}
        logger.warning(f"WebM 指纹初始化未返回令牌：code={result.get('code')}")
        return {"code": result.get("code"), "cached": False,
                "token_length": 0}

    # ---------- 风控埋点 ----------

    @staticmethod
    def behavior_report(auth, page_url=None, referer=None, origin=None,
                        duration_ms: int = 1100, *, page_script_version="20260818",
                        bu1=None, bu2=None, winkey=None, labels=None,
                        plabel=None, glabels=None, random_value=None) -> dict:
        """上报「像人在浏览」的行为埋点（`cactus.jd.com/behavior_report`）。

        网页每加载一个页面会发送多次行为上报。它和签发 h5st token 的
        `request_algo` 是同一个域，
        几乎可以确定是同一套信誉系统的输入 —— 「只调赚钱接口、从不上报行为」
        正是最典型的机器人特征，也是我们把 fp 信誉打坏的原因之一。

        载荷格式（2026-08-16 reqid=212 实抓逐字段还原）：
            body   = `data=<urlencode(xor5(紧凑JSON))>`
            编码   = 逐字符 XOR 5，对合（见 utils.jd_util.xor5_encode）
            头     = accept: application/json
                     content-type: application/x-www-form-urlencoded（**不带 charset**）

        JSON 里的字段分三类：
          1. 鼠标/键盘事件数组（kmC/kmMD/kmMM/kmTS/kmI…）—— 无头环境本来就没有，
             实抓里页面刚加载那几发也全是空数组，照发空的即可；
          2. 环境画像（ua/屏幕/时区/字体/canvas/webglFp）—— 与 h5st 第 8 段同源，
             这里复用同一套种子，保证两处自洽（对不上反而是特征）；
          3. 身份（eid / jsToken / uuid / pin）—— 从 cookie 现取。

        ⚠️ 故意**不**把它做成每个业务请求的自动前置：那样又变成一个固定节律的
        机器特征。由调用方在「进页面」这种语义点上调，才和浏览器的形态一致。
        """
        from utils.fingerprint import get_profile

        profile = get_profile()
        page_url = page_url or JdAPI.search_referer
        origin = origin or JdAPI.search_origin
        now = now_ms()
        bu1 = bu1 if bu1 is not None else ("Error: test err\n"
            "    at HTMLDocument._$mW (https://storage.360buyimg.com/webcontainer/js_security_v3_0.1.6.js?v=2024-06-20-17:5:8920)\n"
            "    at document.querySelector (https://storage.360buyimg.com/jsresource/ws_js/jdwebm.js?v=pcSearch:1:68279)\n"
            "    at o (https://storage.360buyimg.com/bjd-utils-sdk/bjdcommon/aishoparound/1.0.3/index.js:1:20914)\n"
            "    at https://storage.360buyimg.com/bjd-utils-sdk/bjdcommon/aishoparound/1.0.3/index.js:1:21050\n"
            "    at d (https://storage.360buyimg.com/bjd-utils-sdk/bjdcommon/aishoparound/1.0.3/index.js:1:21734)\n"
            "    at g (https://storage.360buyimg.com/bjd-utils-sdk/bjdcommon/aishoparound/1.0.3/index.js:1:22707)\n"
            "    at y (https://storage.360buyimg.com/bjd-utils-sdk/bjdcommon/aishoparound/1.0.3/index.js:1:23456)\n"
            "    at p (https://storage.360buyimg.com/bjd-utils-sdk/bjdcommon/aishoparound/1.0.3/index.js:1:21467)\n"
            "    at t.exports (https://storage.360buyimg.com/bjd-utils-sdk/bjdcommon/aishoparound/1.0.3/index.js:1:23990)\n"
            "    at Object.<anonymous> (https://storage.360buyimg.com/bjd-utils-sdk/bjdcommon/aishoparound/1.0.3/index.js:1:62361)")
        bu2 = bu2 if bu2 is not None else "    at https://storage.360buyimg.com/webcontainer/main/js-security-v3-rac-beta.js?v=20260906:7:2260"
        winkey = winkey if winkey is not None else "RecommendTrans,alert,getAliveSsoDomains,locationbar,getStorage,localStorage,poplogin_getCurrentProductGroup,EventEmitterPcItem,closeLoginPage,clearImmediate,queueMicrotask,seajs,webkitResolveLocalFileSystemURL,searchMainConfig,fingerprint,jdtRiskContext,clickReport,PSign,innerHeight,screenLeft"
        labels = [] if labels is None else labels
        plabel = "" if plabel is None else plabel
        glabels = [] if glabels is None else glabels
        random_value = "" if random_value is None else random_value

        payload = {
            # 1) 交互事件：页面刚加载时浏览器发的也是空数组
            "kmC": [], "kmMD": [], "kmMM": [], "kmMMkd": [], "kmMMku": [],
            "kmTS": [], "kmTM": [], "kmI": [], "kmMC": "", "kmMCF": "",
            "initTs": now - duration_ms, "reTs": now,
            "kmTEC": 0, "kmIEC": 0, "wc": 0, "wd": 0,
            # 2) 环境
            "l": profile["browser_language"],
            "ls": "zh-CN,zh,en,zh-TW,ja",
            "ml": 2, "pl": 5,
            "ua": profile["ua"],
            "pp": {"p2": auth.pin or ""},
            "webglFp": JdAPI._seed_fp("WQ_gather_wgl1"),
            "extend": {
                "wd": 0, "l": 0, "ls": 5, "wk": 0,
                "bu3": 105, "bu4": 0, "bu5": 0, "bu6": 22,
                "bu7": 0, "bu8": 0, "bu9": 0, "bu12": -8,
                "uuid": auth.cookie.get("__jdu", ""),
                "memory": 32, "vendor": "Google Inc.",
                "promise": 0, "connection": 0, "notjdip": "",
                "winkeynum": 422, "scripttagnum": 38,
                "winkey": winkey,
                "wur": "ANGLE (NVIDIA, NVIDIA GeForce RTX 5060 Ti (0x00002D04) Direct3D11 vs_5_0 ps_5_0, D3D11)",
                "position": 0, "v": page_script_version, "eidfrom": 3,
                "paste": "", "msg": "", "historyfailed": "{}",
            },
            "pp1": "", "bu1": bu1,
            "w": int(profile["screen_width"]), "h": int(profile["screen_height"]),
            "ow": int(profile["screen_width"]), "oh": 1392,
            "url": page_url, "og": origin,
            "pf": profile["browser_platform"], "pr": 1,
            "re": "", "referer": referer or "", "bu2": bu2,
            "canvas": JdAPI._seed_fp("WQ_gather_cv1"),
            "ccn": 20, "lsc": 1, "ssc": 1, "csc": 1,
            "tz": "Asia/Shanghai",
            "pld": ("PDF Viewer,Chrome PDF Viewer,Chromium PDF Viewer,"
                    "Microsoft Edge PDF Viewer,WebKit built-in PDF"),
            "notjd": "", "extension": "", "eidfrom": 3,
            # 3) 身份
            "eid": auth.cookie.get("3AB9D23F7A4B3C9B", ""),
            "jsToken": auth.cookie.get("3AB9D23F7A4B3CSS", ""),
            "num": 0,
            "labels": labels, "plabel": plabel, "glabels": glabels,
            "random": random_value,
        }

        raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        headers = HeaderBuilder.build_xhr()
        headers.set_header("accept", "application/json")
        # 浏览器这里的 content-type **不带 charset**，与业务接口那档不同
        headers.set_header("content-type", "application/x-www-form-urlencoded")
        headers.set_referer(referer or page_url)
        headers.set_origin(origin)
        # cactus uses its own jQuery order (referer precedes user-agent and
        # content-type precedes accept-encoding), distinct from the API FETCH
        # profile above.
        headers.reorder(("sec-ch-ua-platform", "referer", "user-agent", "accept",
                         "sec-ch-ua", "content-type", "sec-ch-ua-mobile",
                         "accept-encoding", "accept-language", "origin", "priority",
                         "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site"))
        try:
            resp = http_client.post(
                "https://cactus.jd.com/behavior_report",
                headers=headers.get(), cookies=JdAPI._api_cookies(auth),
                data={"data": xor5_encode(raw)})
            JdAPI._absorb_response(auth, resp)
            return JdAPI._parse(resp)
        except Exception as e:
            logger.warning(f"behavior_report 发送失败（不致命）：{type(e).__name__}: {e}")
            return {"_error": str(e)}

    @staticmethod
    def refresh_fingerprint(auth, payload_a: str, payload_d: str,
                            referer=None) -> dict:
        """Exchange a browser-collected ``jra.jd.com/jsTk.do`` payload.

        ``a`` and ``d`` are opaque anti-risk collections produced by the page;
        they must come from the same Network capture.  The method therefore
        rejects missing values instead of synthesising a partial request.
        """
        if not payload_a or not payload_d:
            raise ValueError("jsTk.do 必须传同一条 Network 请求的完整 a/d 负载")
        headers = Header()
        headers.update({
            "sec-ch-ua-platform": HeaderBuilder.sec_ch_ua_platform,
            "referer": referer or JdAPI._search_page_referer(auth),
            "user-agent": HeaderBuilder.ua,
            "sec-ch-ua": HeaderBuilder.sec_ch_ua,
            "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
            "sec-ch-ua-mobile": HeaderBuilder.sec_ch_ua_mobile,
            "accept": "*/*",
            "accept-encoding": http_client.accept_encoding(),
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,zh-TW;q=0.7,ja;q=0.6",
            "origin": JdAPI.search_origin,
            "priority": "u=1, i",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
        })
        resp = http_client.post("https://jra.jd.com/jsTk.do", headers=headers.get(),
                                cookies=JdAPI._api_cookies(auth),
                                data=[("a", str(payload_a)), ("d", str(payload_d))])
        JdAPI._absorb_response(auth, resp)
        parsed = JdAPI._parse(resp)
        data = (parsed or {}).get("data") if isinstance(parsed, dict) else None
        if isinstance(data, dict):
            if data.get("eid"):
                auth.update_cookies({"3AB9D23F7A4B3C9B": data["eid"]})
            if data.get("token"):
                auth.update_cookies({"3AB9D23F7A4B3CSS": data["token"]})
        auth.flush()
        return parsed

    @staticmethod
    def _seed_fp(key: str) -> str:
        """从 `static/fp_seed.json` 取 canvas / webgl 指纹值。

        种子里存的是 `{"v":"<指纹>","t":…,"e":…}`，埋点要的就是那个 `v`。
        与 h5st 第 8 段用同一份种子 —— 两处报的指纹必须一致，
        对不上本身就是可被识别的特征。
        """
        import os

        if not hasattr(JdAPI, "_seed_cache"):
            path = os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "static", "fp_seed.json")
            try:
                with open(path, "r", encoding="utf-8") as f:
                    JdAPI._seed_cache = json.load(f)
            except Exception:
                JdAPI._seed_cache = {}
        try:
            return json.loads(JdAPI._seed_cache.get(key) or "{}").get("v", "")
        except ValueError:
            return ""

    # ---------- 会话活性 ----------

    @staticmethod
    def check_session(auth) -> tuple:
        """探一下登录态还活着没。

        必须有这个探测：会话失效时京东**不报「未登录」，而是让 h5st 校验不过、
        业务接口回 403 空 body**，极易误判成签名写错。
        这里用 passport 的免签名接口，返回 (alive, pin_or_msg)。
        """
        headers = HeaderBuilder.build(HeaderType.JSONP)
        headers.set_referer("https://www.jd.com/")
        try:
            resp = http_client.get(
                "https://passport.jd.com/loginservice.aspx",
                params={"method": "Login", "callback": "jsonpLogin"},
                headers=headers.get(), cookies=auth.cookie, verify=False, timeout=15)
            JdAPI._absorb_response(auth, resp)
        except Exception as e:
            return False, f"探测失败：{type(e).__name__}: {e}"
        data = parse_jsonp(resp.text) or {}
        nick = data.get("Identity", {}).get("Name") if isinstance(
            data.get("Identity"), dict) else None
        alive = bool(data.get("Identity", {}).get("IsAuthenticated")) if isinstance(
            data.get("Identity"), dict) else False
        if alive:
            return True, nick or auth.pin
        return False, f"登录态已失效，请重跑 qr-login（响应：{resp.text[:120]}）"

    @staticmethod
    def diagnose(auth) -> tuple:
        """大面积 403 时先跑这个，判断到底是哪一层出了问题。

        403 空 body 在这套接口上被复用了三种含义，光看它区分不出来，
        必须靠「免签名接口」和「签名接口」的组合来定位：

            登录态挂了            → check_session 就报 False
            账号被风控标记         → 响应头 x-rp-content 里有 code:605 + 「京东验证」
                                    ⚠️ 风控是**按 functionId** 下的，可能只拦一个接口
            签名通道被限流         → 免签名接口通、签名接口全挂
            画像/签名真的不对      → 免签名接口通、签名接口部分通部分挂

        后两者由免签名、其它签名接口及 `x-rp-content` 联合判断；搜索接口
        命中 605 时会由 `search()` 直接启动纯程序 JCAP 验证，无需外部页面。

        返回 (verdict, detail)，verdict ∈ {ok, no_session, risk, throttled}。
        """
        alive, info = JdAPI.check_session(auth)
        if not alive:
            return "no_session", info

        unsigned = JdAPI.get_search_hotwords(auth)
        if not (isinstance(unsigned, dict) and unsigned.get("code") == 0):
            return "throttled", f"连免签名接口都不通了，先歇会儿（{str(unsigned)[:80]}）"

        # 签名通道的探针用 `getCartNum`，**别用搜索**：风控的处置是按 functionId
        # 下的（disposal 里那个 `evApi` 就是接口名），实测出现过
        # 「searchWare 被拦、其它签名接口照样出数据」的局面，
        # 拿被拦的那个当探针会把整条通道误判成挂了。
        # 另外必须带重试：签名接口本来就有约七分之一的偶发 403。
        signed = JdAPI.get_cart_num(auth)
        if signed.get("_status") == 403:
            return "throttled", (
                "免签名接口通、签名接口挂 —— 被限流或被风控标记了，"
                "密集调试之后就会这样，等十几分钟到几小时。"
                "裸 403 没有处置详情，期间的签名实验结果不具备判定价值；"
                "若搜索返回 code:605，search() 会自动执行本地 JCAP 并重试。")
        return "ok", f"登录态 {info}，签名通道正常"

    # ---------- 商品 ----------

    @staticmethod
    def get_product_detail(auth, sku: str, area=None, num="1",
                           retry: int = 2) -> dict:
        """商品详情。

        实抓自 item.jd.com：`functionId=pc_detailpage_wareBusiness`，
        `appid=pc-item-soa`，走根路径 `/`。返回价格、库存、图片、规格、店铺等全量。
        （老代码用的 H5 `mview_switch` 在 PC 登录态下会被风控挡回 code:601。）
        """
        body = {
            "skuId": str(sku),
            "area": area or area_of(auth),
            "num": str(num),
            "clientSource": "PC",
            "userAgent": "Windows",
            "sfTime": "1,0,0",
        }
        return JdAPI.call_api(
            auth, "pc_detailpage_wareBusiness", body=body, path="/",
            client_preset=CLIENT_PC_ITEM, app_id=APPID_PC_ITEM,
            extra_params={"scval": str(sku)},
            referer=JdAPI.item_referer,
            referer_page=f"https://item.jd.com/{sku}.html",
            rp_client=RP_CLIENT_ITEM, origin=JdAPI.item_origin, method="GET",
            content_type="application/x-www-form-urlencoded",
            param_order=ORDER_PC_ITEM,
            retry=retry,
        )

    @staticmethod
    def get_product_comments(auth, sku: str, count: int = 5) -> dict:
        """商品评论（functionId=getLegoWareDetailComment）。"""
        body = {"shopType": "0", "sku": int(sku), "commentNum": count, "source": "pc"}
        return JdAPI.call_api(
            auth, "getLegoWareDetailComment", body=body, path="/",
            client_preset=CLIENT_PC_ITEM_V3, app_id=APPID_PC_ITEM,
            extra_params={"build": "100000"},
            referer=JdAPI.item_referer,
            referer_page=f"https://item.jd.com/{sku}.html",
            rp_client=RP_CLIENT_ITEM, origin=JdAPI.item_origin, method="GET",
            param_order=ORDER_PC_ITEM,
        )

    @staticmethod
    def check_chat(auth, sku: str, key: str) -> dict:
        """商品页客服可用性探测（functionId=checkChat）。

        ``key`` 是商品页运行时生成的 `JDPC_*` 值，不能从 SKU 推导；
        未提供就拒绝发出一个缺字段请求。Network 实抓 body 顺序为
        source → key → pid → returnCharset。
        """
        if not key:
            raise ValueError("checkChat 必须传商品页实抓的 key（JDPC_*），不能猜")
        body = {
            "source": "jd_pc_item",
            "key": str(key),
            "pid": str(sku),
            "returnCharset": "utf-8",
        }
        return JdAPI.call_api(
            auth, "checkChat", body=body, path="/",
            client_preset=CLIENT_PC_ITEM_V3, app_id=APPID_PC_ITEM,
            referer=JdAPI.item_referer,
            referer_page=f"https://item.jd.com/{sku}.html",
            rp_client=RP_CLIENT_ITEM, origin=JdAPI.item_origin, method="GET",
            param_order=ORDER_PC_ITEM,
        )

    @staticmethod
    def get_vender_follow_status(auth, vender_id, sku: str, sys_name="item.jd.com") -> dict:
        """商品页商家关注状态（functionId=pctradesoa_vender_batchIsFollow）。

        浏览器实抓的 `venderIds` 是单个数字而不是数组；保留这个形态，
        同时把 `build=100000` 放在 query 末尾。
        """
        raw_vender = int(vender_id) if str(vender_id).isdigit() else str(vender_id)
        body = {"venderIds": raw_vender, "sysName": str(sys_name)}
        return JdAPI.call_api(
            auth, "pctradesoa_vender_batchIsFollow", body=body, path="/api",
            client_preset=CLIENT_PC_ITEM_V3, app_id=APPID_PC_ITEM,
            extra_params={"build": "100000"},
            referer=JdAPI.item_referer,
            referer_page=f"https://item.jd.com/{sku}.html",
            rp_client=RP_CLIENT_ITEM, origin=JdAPI.item_origin, method="GET",
            param_order=ORDER_PC_ITEM_VENDER_FOLLOW,
        )

    @staticmethod
    def get_recommend_coupon(auth, sku: str, area=None) -> dict:
        """商品页推荐优惠券（functionId=getRecommendCoupon）。"""
        return JdAPI.call_api(
            auth, "getRecommendCoupon", body={"client": "pc"}, path="/",
            client_preset=CLIENT_PC_ITEM_V3, app_id=APPID_PC_ITEM,
            extra_params={"area": area or area_of(auth)},
            referer=JdAPI.item_referer,
            referer_page=f"https://item.jd.com/{sku}.html",
            rp_client=RP_CLIENT_ITEM, origin=JdAPI.item_origin, method="GET",
            param_order=ORDER_PC_ITEM,
        )

    @staticmethod
    def get_diviner(auth, sku: str, body=None, variant="initial",
                    security_token="", p=None, shop_id=None, page: int = 1,
                    limit: int = 12) -> dict:
        """商品页推荐/联想（functionId=pctradesoa_diviner）。

        该接口有两套浏览器调用契约：首请求走根路径 `/`，分页请求走
        `/api`；两套 body 和 query 顺序都保留。对于动态的
        `securityToken`、类目 `p`、店铺 `shopId`，调用方应传 Network
        实抓值；不提供就不发送一个不完整 body。
        """
        variant = str(variant or "initial").lower()
        if body is None:
            if variant in ("initial", "root", "first"):
                if not security_token or p is None:
                    raise ValueError("diviner 首请求必须传实抓 securityToken 和 p")
                body = {
                    "ec": "utf-8",
                    "lid": 1,
                    "uuid": str(auth.cookie.get("__jdu", "")),
                    "securityToken": str(security_token),
                    "clientChannel": "3",
                    "clientPageId": "item.jd.com",
                    "ck": "pin",
                    "p": int(p) if str(p).isdigit() else p,
                    "sku": int(sku) if str(sku).isdigit() else str(sku),
                    "lim": int(limit),
                }
                path = "/"
                order = ORDER_PC_ITEM
            elif variant in ("page", "paged", "api"):
                if p is None or shop_id is None:
                    raise ValueError("diviner 分页请求必须传实抓 p 和 shopId")
                body = {
                    "lim": int(limit),
                    "ec": "utf-8",
                    "lid": 1,
                    "p": int(p) if str(p).isdigit() else p,
                    "ck": "pin,bview",
                    "clientChannel": "3",
                    "clientPageId": "item.jd.com",
                    "page": int(page),
                    "sku": int(sku) if str(sku).isdigit() else str(sku),
                    "shopId": int(shop_id) if str(shop_id).isdigit() else shop_id,
                }
                path = "/api"
                order = ORDER_PC_ITEM_DIVINER_PAGE
            else:
                raise ValueError("diviner variant 只能是 initial 或 page")
        else:
            if not isinstance(body, dict) or not body:
                raise ValueError("diviner body 必须是完整的 Network JSON 对象")
            if variant in ("page", "paged", "api"):
                path, order = "/api", ORDER_PC_ITEM_DIVINER_PAGE
                expected_keys = ("lim", "ec", "lid", "p", "ck", "clientChannel",
                                 "clientPageId", "page", "sku", "shopId")
            else:
                path, order = "/", ORDER_PC_ITEM
                expected_keys = ("ec", "lid", "uuid", "securityToken", "clientChannel",
                                 "clientPageId", "ck", "p", "sku", "lim")
            actual_keys = tuple(body)
            if actual_keys != expected_keys:
                raise ValueError(
                    f"diviner {variant} body 字段/顺序必须严格匹配 Network："
                    f"期望 {list(expected_keys)}，收到 {list(actual_keys)}"
                )
            missing = [key for key in expected_keys if body.get(key) in (None, "")]
            if missing:
                raise ValueError(f"diviner body 动态/必需字段为空：{missing}")
        return JdAPI.call_api(
            auth, "pctradesoa_diviner", body=body, path=path,
            client_preset=CLIENT_PC_ITEM_V3, app_id=APPID_PC_ITEM,
            referer=JdAPI.item_referer,
            referer_page=(f"https://item.jd.com/{sku}.html"),
            rp_client=RP_CLIENT_ITEM, origin=JdAPI.item_origin, method="GET",
            param_order=order,
        )

    @staticmethod
    def get_related_search(auth, sku: str, num: int = 6) -> dict:
        """商品相关搜索词。这个接口**不需要 h5st**，也**不能带 `t`**（浏览器就不带）。"""
        return JdAPI.call_api(
            auth, "relsearch", body={}, path="/api",
            client_preset=CLIENT_PC_ITEM_V3, sign=False, with_time=False,
            extra_params={"skuid": str(sku), "num": num,
                          "rettype": "json", "type_name": "relsearch"},
            referer=JdAPI.item_referer,
            referer_page=f"https://item.jd.com/{sku}.html",
            rp_client=RP_CLIENT_ITEM, origin=JdAPI.item_origin, method="GET",
            with_uuid=True, drop_params=UNSIGNED_DROP,
            content_type="application/x-www-form-urlencoded",
            param_order=ORDER_PC_ITEM_RELWORDS,
        )

    # ---------- 搜索 ----------

    @staticmethod
    def search(auth, keyword: str, page: int = 1, area=None,
               sort: str = "", retry: int = 2, auto_verify: bool = True,
               **extra) -> dict:
        """商品搜索（functionId=pc_search_searchWare，appid=search-pc-java）。

        商品列表在 `data.wareList`（一页 30 条），总数在 `data.resultCount`。

        :param area: 收货地区。默认从 `ipLoc-djd` cookie 现取（截断到四段，
            与浏览器实抓一致）；显式传值才覆盖。
        :param page: 从 1 开始；`s` 是起始序号
        :param sort: 排序，空=综合；`sort_totalsales15_desc` 销量、
            `sort_price_asc` 价格升、`sort_price_desc` 价格降、`sort_commentcount_desc` 评论数
        :param auto_verify: 命中 605 时纯程序完成 JCAP、保存
            `x-rp-evtoken` 并自动重试一次；默认开启。
        """
        # The real PC page runs WebM 6.0.0 before searchWare.  Login cookies,
        # eid/jsToken and h5st do not replace this page fingerprint cookie.
        JdAPI.ensure_webm_token(auth, keyword)
        body = {
            # Current searchMainConfig whitelists this URL parameter and the
            # request layer removes only keyword/ev before signing the body.
            "enc": str(extra.pop("enc", "utf-8")),
            "area": area or area_of(auth),
            "page": page,
            "mode": "",
            "concise": False,
            # Values from the current search page configuration.  These used
            # to be true in the old capture but are false in pro/0.0.11.
            "hoverPictures": False,
            "newAdvRepeat": False,
            "mixerParam": False,
            "new_interval": True,
            "s": (page - 1) * 30 + 1,
            "pageSize": 30,
        }
        if sort:
            body["sort"] = sort
        body.update(extra)
        auth.search_keyword = keyword
        result = JdAPI.call_api(
            auth, "pc_search_searchWare", body=body, path="/api",
            client_preset=CLIENT_PC_SEARCH, app_id=APPID_PC_SEARCH,
            extra_params={"cthr": "1", "keyword": keyword},
            referer=JdAPI._search_page_referer(auth, keyword),
            origin=JdAPI.search_origin,
            method="GET", retry=retry, append_time=True,
            param_order=ORDER_PC_SEARCH,
            uuid_value=search_uuid_of(auth), axios=True,
            content_type="application/x-www-form-urlencoded",
        )
        if auto_verify and isinstance(result, dict) and result.get("disposal"):
            verified = JdAPI.solve_search_risk(auth, result, keyword)
            if verified.get("code") == 0:
                retried = JdAPI.call_api(
                    auth, "pc_search_searchWare", body=body, path="/api",
                    client_preset=CLIENT_PC_SEARCH, app_id=APPID_PC_SEARCH,
                    extra_params={"cthr": "1", "keyword": keyword},
                    referer=JdAPI._search_page_referer(auth, keyword),
                    origin=JdAPI.search_origin,
                    method="GET", retry=retry, append_time=True,
                    param_order=ORDER_PC_SEARCH,
                    uuid_value=search_uuid_of(auth), axios=True,
                    content_type="application/x-www-form-urlencoded",
                )
                if isinstance(retried, dict):
                    retried["_verification"] = verified
                return retried
            if isinstance(result, dict):
                result["_verification"] = verified
        return result

    @staticmethod
    def search_wares(auth, keyword: str, page: int = 1, **kwargs) -> tuple:
        """搜索并抽出商品列表，返回 (total, [{skuId, name, price, shop, ...}])。

        字段名取 `wareList` 里实际有值的那几个：`wareId`/`wareName`/`jdPrice`/
        `shopName`/`venderId`/`good`（好评率）/`comment`（评价数）。
        """
        res = JdAPI.search(auth, keyword, page=page, **kwargs)
        data = (res or {}).get("data") or {}
        wares = []
        for item in data.get("wareList") or []:
            wares.append({
                "skuId": item.get("wareId") or item.get("skuId"),
                # wareName 里命中的关键词被包了 <font class="skcolor_ljg">，要去掉
                "name": strip_tags(item.get("wareName") or item.get("shortName")),
                "price": item.get("jdPrice") or item.get("finalPrice"),
                "shop": item.get("shopName"),
                "venderId": item.get("venderId"),
                "good": item.get("good"),
                "comment": item.get("comment"),
                "url": item.get("productUrl"),
            })
        return data.get("resultCount", 0), wares

    @staticmethod
    def get_search_hotwords(auth) -> dict:
        """搜索热词。**不需要 h5st**，也不需要登录。"""
        return JdAPI.call_api(
            auth, "pc_search_hotwords", body=None, path="/api",
            client_preset=CLIENT_PC_SEARCH, sign=False,
            referer=JdAPI._search_page_referer(auth), origin=JdAPI.search_origin,
            referer_page="https://search.jd.com/Search", rp_client=RP_CLIENT_SEARCH,
            method="GET", content_type="application/x-www-form-urlencoded",
            param_order=ORDER_PC_SEARCH_PLAIN, drop_params=UNSIGNED_DROP,
        )

    @staticmethod
    def get_search_relwords(auth, keyword: str = "", num: int = 10) -> dict:
        """搜索页的相关搜索词（functionId=pc_search_relwords）。

        ⚠️ 与商详页那个 `relsearch` **不是同一个接口**，别混用：
        搜索页实抓（2026-08-16 reqid=200）走 `pc_search_relwords` +
        `appid=search-pc-java`，body 是 `{"keyword":"<词>"}`；
        商详页走 `relsearch` + `appid=item-v3`、参数在 query 里（见
        `get_related_search`）。两个都不需要 h5st。

        浏览器进搜索页时会发两次：一次空 keyword（reqid=38，页面初始化），
        一次带真实关键词（reqid=200，搜索结果回来之后）。
        """
        return JdAPI.call_api(
            auth, "pc_search_relwords", body={"keyword": keyword}, path="/api",
            client_preset=CLIENT_PC_SEARCH, sign=False,
            extra_params={"keyword": keyword, "num": num,
                          "rettype": "json", "type_name": "relsearch"},
            referer=JdAPI._search_page_referer(auth, keyword),
            origin=JdAPI.search_origin, referer_page="https://search.jd.com/Search",
            rp_client=RP_CLIENT_SEARCH, method="GET",
            content_type="application/x-www-form-urlencoded",
            param_order=ORDER_PC_SEARCH_RELWORDS, drop_params=UNSIGNED_DROP,
        )

    @staticmethod
    def get_search_ai_pic_tags(auth, keyword: str, sku_list) -> dict:
        """搜索结果的 AI 图标签（functionId=pc_search_aiPicTagInfo）。不需要 h5st。

        `sku_list` 是逗号分隔的 skuId 串——浏览器拿本页 15 个商品去问。
        实抓 reqid=201。
        """
        if not isinstance(sku_list, str):
            sku_list = ",".join(str(s) for s in sku_list)
        return JdAPI.call_api(
            auth, "pc_search_aiPicTagInfo",
            body={"keyword": keyword, "sku_list": sku_list}, path="/api",
            client_preset=CLIENT_PC_SEARCH, sign=False,
            referer=JdAPI._search_page_referer(auth, keyword),
            origin=JdAPI.search_origin, referer_page="https://search.jd.com/Search",
            rp_client=RP_CLIENT_SEARCH, method="GET",
            content_type="application/x-www-form-urlencoded",
            param_order=ORDER_PC_SEARCH_PLAIN, drop_params=UNSIGNED_DROP,
        )

    @staticmethod
    def get_umc_equity(auth, area=None) -> dict:
        """搜索页的会员权益角标（functionId=pc_search_getUmcEquity）。不需要 h5st。

        实抓 reqid=202，body 只有 `{"area":"1_2800_55812_0"}`（截断版 area）。
        """
        return JdAPI.call_api(
            auth, "pc_search_getUmcEquity",
            body={"area": area or area_of(auth)}, path="/api",
            client_preset=CLIENT_PC_SEARCH, sign=False,
            referer=JdAPI._search_page_referer(auth), origin=JdAPI.search_origin,
            referer_page="https://search.jd.com/Search", rp_client=RP_CLIENT_SEARCH,
            method="GET", content_type="application/x-www-form-urlencoded",
            param_order=ORDER_PC_SEARCH_PLAIN,
            drop_params=UNSIGNED_DROP,
        )

    @staticmethod
    def get_search_coupon(auth, coupon_body) -> dict:
        """搜索页动态优惠券（``pc_search_getCoupon``）。

        The four values are minted by the search page and are opaque to the
        client.  Accept the complete Network body and reject partial data so
        a request can never be sent with guessed/missing risk fields.
        """
        if not isinstance(coupon_body, dict):
            raise ValueError("search coupon 必须传完整 Network JSON body")
        expected = ("encryptedKey", "ruleId", "dynamicCouponUUID", "encryptedPin")
        if tuple(coupon_body) != expected:
            raise ValueError(
                "search coupon body 字段/顺序必须严格匹配 Network："
                f"期望 {list(expected)}，收到 {list(coupon_body)}"
            )
        missing = [key for key in expected if coupon_body.get(key) in (None, "")]
        if missing:
            raise ValueError(f"search coupon body 动态字段为空：{missing}")
        return JdAPI.call_api(
            auth, "pc_search_getCoupon", body=coupon_body, path="/api",
            client_preset=CLIENT_PC_SEARCH, sign=False,
            referer=JdAPI._search_page_referer(auth), origin=JdAPI.search_origin,
            referer_page="https://search.jd.com/Search", rp_client=RP_CLIENT_SEARCH,
            method="GET", content_type="application/x-www-form-urlencoded",
            param_order=ORDER_PC_SEARCH_PLAIN, drop_params=UNSIGNED_DROP,
        )

    @staticmethod
    def query_address(auth, address_body) -> dict:
        """Search-page address list (``pc_address_cmpnt_queryAddress``).

        The endpoint uses a form envelope and an opaque, encrypted address
        payload.  Both layers are preserved in the captured order; callers
        must provide the complete Network JSON instead of allowing defaults.
        """
        # Exact Chrome 152 Network body order (reqid=99). ``appid`` inside
        # the encrypted address object is present but legitimately empty.
        expected = ("deviceUUID", "appId", "bizModelCode", "token",
                    "externalLoginType", "jdCombineSign", "listVersion",
                    "appid", "keyId", "serialNumber")
        if not isinstance(address_body, dict) or tuple(address_body) != expected:
            raise ValueError(
                "address body 字段/顺序必须严格匹配 Network："
                f"期望 {list(expected)}，收到 {list(address_body or {})}"
            )
        missing = [key for key in expected
                   if key != "appid" and address_body.get(key) in (None, "")]
        if missing:
            raise ValueError(f"address body 动态字段为空：{missing}")
        import json as _json
        from utils import h5st5
        from utils.jd_util import CLIENT_PC_SEARCH
        body_json = _json.dumps(address_body, separators=(",", ":"), ensure_ascii=False)
        t = str(now_ms())
        uuid = auth.cookie.get("__jdu", "")
        eid_token = auth.cookie.get("3AB9D23F7A4B3CSS", "")
        if not uuid or not eid_token:
            raise ValueError("address 请求缺少浏览器必需的 __jdu 或 3AB9D23F7A4B3CSS")
        signed = h5st5.sign({
            "appid": CLIENT_PC_SEARCH["appid"], "body": sha256_hex(body_json),
            "client": "pc", "clientVersion": "1.0.26",
            "functionId": "pc_address_cmpnt_queryAddress", "t": t,
        }, APPID_PC_SEARCH)
        h5st = signed.get("h5st", "")
        if not h5st:
            raise ValueError("address 请求 h5st 生成失败，拒绝发送缺字段请求")
        headers = HeaderBuilder.build_xhr(axios=True, form=True)
        headers.set_header("content-type", "application/x-www-form-urlencoded")
        headers.set_referer(JdAPI._search_page_referer(auth))
        headers.set_origin(JdAPI.search_origin)
        headers.set_header("x-referer-page", "https://search.jd.com/Search")
        headers.set_header("x-rp-client", RP_CLIENT_SEARCH)
        headers.reorder(HeaderBuilder.AXIOS_ORDER)
        form = [
            ("appid", "search-pc-java"), ("body", body_json),
            ("client", "pc"), ("clientVersion", "1.0.26"),
            ("functionId", "pc_address_cmpnt_queryAddress"), ("h5st", h5st),
            ("loginType", "3"), ("t", t), ("uuid", uuid),
            ("x-api-eid-token", eid_token), ("xAPIScval2", "pc"),
        ]
        resp = http_client.post(
            "https://api.m.jd.com/client.action?fid=pc_address_cmpnt_queryAddress",
            headers=headers.get(), cookies=JdAPI._api_cookies(auth), data=form)
        JdAPI._absorb_response(auth, resp)
        return JdAPI._parse(resp)

    @staticmethod
    def get_equity_info(auth, page_context: str = "search", sku: str = "") -> dict:
        """交易侧权益信息（functionId=pctradesoa_equityInfo）。

        实抓 reqid=65：**要 h5st（fb5df）**，走 AXIOS 档的头，
        query 顺序与 `getCartNum` 同一套（ORDER_PC_API），body 是空对象。
        """
        item = page_context == "item"
        return JdAPI.call_api(
            auth, "pctradesoa_equityInfo", body={}, path="/api",
            client_preset=CLIENT_PC_ITEM_V3 if item else CLIENT_PC_SEARCH,
            app_id=APPID_PC_ITEM,
            referer=(JdAPI.item_referer if item else JdAPI.search_referer),
            origin=JdAPI.item_origin if item else JdAPI.search_origin,
            referer_page=(f"https://item.jd.com/{sku}.html" if item and sku
                          else (JdAPI.item_referer if item else
                                "https://search.jd.com/Search")),
            rp_client=RP_CLIENT_ITEM if item else RP_CLIENT_SEARCH,
            with_uuid=False, method="GET", param_order=ORDER_PC_API,
        )

    @staticmethod
    def query_plus_info(auth, page_id: str = "Search_ProductList",
                        page_context: str = "search", sku: str = "") -> dict:
        """PLUS 会员信息（functionId=pctradesoa_queryPlusInfo）。

        实抓 reqid=66，与 `pctradesoa_equityInfo` 同一档，
        body 是 `{"pageId":"Search_ProductList"}`。
        """
        item = page_context == "item"
        if item and page_id == "Search_ProductList":
            page_id = "JD_SXmain"
        return JdAPI.call_api(
            auth, "pctradesoa_queryPlusInfo", body={"pageId": page_id}, path="/api",
            client_preset=CLIENT_PC_ITEM_V3 if item else CLIENT_PC_SEARCH,
            app_id=APPID_PC_ITEM,
            referer=(JdAPI.item_referer if item else JdAPI.search_referer),
            origin=JdAPI.item_origin if item else JdAPI.search_origin,
            referer_page=(f"https://item.jd.com/{sku}.html" if item and sku
                          else (JdAPI.item_referer if item else
                                "https://search.jd.com/Search")),
            rp_client=RP_CLIENT_ITEM if item else RP_CLIENT_SEARCH,
            with_uuid=False, method="GET", param_order=ORDER_PC_API,
        )

    # ---------- 账号 ----------

    # ---------- 订单 ----------

    order_url = "https://order.jd.com/center/list.action"
    order_origin = "https://order.jd.com"
    order_referer = "https://order.jd.com/"

    @staticmethod
    def get_order_list(auth, page: int = 1, date_range: str = "1") -> dict:
        """订单列表。

        ⚠️ **这个不是 JSON 接口，是服务端渲染的 HTML 页面。**
        2026-08-16 实抓 order.jd.com/center/list.action 全量 19 条请求，
        没有任何一个 functionId 返回订单列表本身：页面直接把 20 条订单
        渲染进 `<tbody id="tb-<订单号>">`，那些 `pcorder_*` 接口
        （`findOrdersHaveDetailsNew` / `getPpesLabelByShopIds` /
        `bbpbjcInfo` …）全都是**拿着已知的订单号去补充信息**的，
        它们的入参就是从 DOM 里读出来的 orderIds。

        所以这里走 HTML 解析。早先 README 里写的
        `get_order_list → getOrderByPage` 是臆测的接口名，
        实际并不存在（`main.py order` 一直报 AttributeError）。

        :param page: 页码，从 1 开始
        :param date_range: 时间范围，照抄页面下拉框的 `_val`：
            `1`=近三个月（默认）、`2`=今年内、`2025`/`2024`/…=某年
        :return: {"orders": [...], "count": n, "page": p}
        """
        # 订单中心是顶层 document 导航，header 与 API/XHR 完全不同，且
        # Chrome 这条请求没有 referer（不能把 order.jd.com/ 猜进去）。
        headers = HeaderBuilder.build(HeaderType.ORDER_DOC)
        params = {"search": "0", "d": date_range, "s": "4096", "page": page}
        resp = http_client.get(JdAPI.order_url, headers=headers.get(),
                               cookies=auth.cookie, params=params)
        JdAPI._absorb_response(auth, resp)
        html = resp.text or ""
        if "passport.jd.com" in html[:2000] and "login" in html[:2000]:
            return {"orders": [], "count": 0, "page": page,
                    "_error": "被跳到登录页，登录态失效了，重跑 qr-login"}
        orders = JdAPI.parse_orders(html)
        return {"orders": orders, "count": len(orders), "page": page}

    @staticmethod
    def _call_order_api(auth, function_id: str, order_uuid: str,
                        body=None, method="GET", accept=None) -> dict:
        """调用订单页的 `pcorder_*` 补充接口。

        订单页的 uuid 不是 `__jdu`，而是页面运行时拼出的复合值；
        因此这里强制要求调用方从同一条 Chrome Network 请求传入，
        避免发出缺 uuid/错 uuid 的风控请求。订单接口不带 h5st、
        x-api-eid-token，POST 也把 body 留在 query。
        """
        if not order_uuid:
            raise ValueError("订单接口必须传订单页 Network 实抓的 uuid")
        method = method.upper()
        return JdAPI.call_api(
            auth, function_id, body=body, path="/api",
            client_preset=CLIENT_ORDER, sign=False, with_time=True,
            body_in_query=(method == "POST"), method=method, with_uuid=True,
            uuid_value=order_uuid, drop_params=("x-api-eid-token",),
            referer=JdAPI.order_referer, origin=JdAPI.order_origin,
            referer_page="https://order.jd.com/center/list.action",
            rp_client=RP_CLIENT_SEARCH,
            accept=(accept or "application/json, text/javascript, */*; q=0.01"),
            param_order=ORDER_PC_ORDER, retry=0,
        )

    @staticmethod
    def get_order_shop_labels(auth, order_uuid: str, shop_ids) -> dict:
        """订单页店铺标签（`pcorder_getPpesLabelByShopIds`，GET）。"""
        shop_ids = JdAPI._csv_value(shop_ids)
        return JdAPI._call_order_api(
            auth, "pcorder_getPpesLabelByShopIds", order_uuid,
            body={"shopIds": shop_ids}, method="GET", accept="*/*")

    @staticmethod
    def get_parent_order_list(auth, order_uuid: str, parent_ids) -> dict:
        """订单页父订单补充（`pcorder_getParentOrderList`，POST 空体）。"""
        parent_ids = JdAPI._csv_value(parent_ids, trailing=True)
        return JdAPI._call_order_api(
            auth, "pcorder_getParentOrderList", order_uuid,
            body={"pin": auth.pin or "", "parentIds": parent_ids}, method="POST")

    @staticmethod
    def get_pop_tel_info(auth, order_uuid: str, pop_vender_ids,
                         cz_order_shop_ids="") -> dict:
        """订单页 POP 电话信息（`pcorder_getPopTelInfo`）。"""
        return JdAPI._call_order_api(
            auth, "pcorder_getPopTelInfo", order_uuid,
            body={"popVenderIds": JdAPI._csv_value(pop_vender_ids),
                  "czOrderShopIds": JdAPI._csv_value(cz_order_shop_ids)},
            method="POST")

    @staticmethod
    def get_jd_go_home_tel_info(auth, order_uuid: str, vender_ids) -> dict:
        """订单页京东到家电话信息（`pcorder_getJdGoHomeTelInfo`）。"""
        return JdAPI._call_order_api(
            auth, "pcorder_getJdGoHomeTelInfo", order_uuid,
            body={"jdGoHomeVenderIds": JdAPI._csv_value(vender_ids)}, method="POST")

    @staticmethod
    def find_orders_have_details(auth, order_uuid: str, order_ids) -> dict:
        """订单页详情补充（`pcorder_findOrdersHaveDetailsNew`）。"""
        return JdAPI._call_order_api(
            auth, "pcorder_findOrdersHaveDetailsNew", order_uuid,
            body={"orderids": JdAPI._csv_value(order_ids)}, method="POST")

    @staticmethod
    def get_nps_survey(auth, order_uuid: str) -> dict:
        """订单页 NPS 调查开关（`pcorder_getNPSSurvey`）。"""
        return JdAPI._call_order_api(
            auth, "pcorder_getNPSSurvey", order_uuid, method="GET")

    @staticmethod
    def is_plus_member(auth, order_uuid: str) -> dict:
        """订单页 PLUS 状态（`pcorder_isPlusMember`）。"""
        return JdAPI._call_order_api(
            auth, "pcorder_isPlusMember", order_uuid, method="GET")

    @staticmethod
    def query_plan_detail(auth, order_uuid: str, order_ids="") -> dict:
        """订单页计划详情（`pcorder_queryPlanDetailByOrderId`）。"""
        return JdAPI._call_order_api(
            auth, "pcorder_queryPlanDetailByOrderId", order_uuid,
            body={"orderIds": JdAPI._csv_value(order_ids)}, method="GET")

    @staticmethod
    def get_order_gift_assets(auth, order_uuid: str, order_ids) -> dict:
        """订单页礼品资产（`pcorder_getOrderGiftAssetsByOrderIds`）。"""
        return JdAPI._call_order_api(
            auth, "pcorder_getOrderGiftAssetsByOrderIds", order_uuid,
            body={"orderIds": JdAPI._csv_value(order_ids)}, method="GET")

    @staticmethod
    def get_bbpbjc_info(auth, order_uuid: str, ware_ids) -> dict:
        """订单页商品权益信息（`pcorder_bbpbjcInfo`）。"""
        return JdAPI._call_order_api(
            auth, "pcorder_bbpbjcInfo", order_uuid,
            body={"wareIds": JdAPI._csv_value(ware_ids)}, method="POST")

    @staticmethod
    def _csv_value(value, trailing=False) -> str:
        """把列表转为订单页 body 使用的逗号串；字符串原样保留。"""
        if isinstance(value, (list, tuple, set)):
            result = ",".join(str(item) for item in value)
        else:
            result = str(value or "")
        if trailing and result and not result.endswith(","):
            result += ","
        return result

    @staticmethod
    def parse_orders(html: str) -> list:
        """从订单页 HTML 里抽订单。结构逐字对着实抓的 DOM 写。

        每个订单是一个 `<tbody id="tb-<订单号>">`，里面：
            .dealtime          下单时间
            a[name=orderIdLinks]   订单号（也在 tbody 的 id 里）
            .consignee         收货人
            .amount            金额（形如 `¥23.28`）
            .status            状态（已完成 / 待收货 …）
            .p-name a          商品名（一单可能多件）
        """
        import re as _re
        from html import unescape as _ue

        def clean(s):
            return _ue(strip_tags(s)).strip() if s else ""

        orders = []
        for match in _re.finditer(
                r'<tbody id="tb-(\d+)"[^>]*>(.*?)</tbody>', html, _re.S):
            order_id, chunk = match.group(1), match.group(2)

            def pick(pattern, default=""):
                m = _re.search(pattern, chunk, _re.S)
                return clean(m.group(1)) if m else default

            names = [clean(m) for m in _re.findall(
                r'<div class="p-name">\s*<a[^>]*>(.*?)</a>', chunk, _re.S)]
            if not names:
                names = [clean(m) for m in _re.findall(
                    r'class="p-name"[^>]*>\s*<a[^>]*>(.*?)</a>', chunk, _re.S)]

            # 金额字段混杂空白和行内元素，先取 div 内全文再清理
            amount_raw = pick(r'<div class="amount"[^>]*>(.*?)</div>')
            # 把"在线支付"等支付方式从金额字段里分离（实际是同一 div 的两行）
            amount_lines = [l.strip() for l in amount_raw.splitlines() if l.strip()]
            amount = amount_lines[0] if amount_lines else amount_raw
            pay_type = amount_lines[1] if len(amount_lines) > 1 else ""

            # 状态在 `<span class="order-status ftx-0N">` 里；直接取 .status 整块会
            # 把后面那个「订单详情」链接一起捞进来（踩过一次）。
            status = pick(r'<span class="order-status[^"]*"[^>]*>(.*?)</span>')

            # 收货人在 `<span class="txt">`；.consignee 整块里还嵌着一个
            # 带完整地址和手机号的 tooltip，不能整块取。
            consignee = pick(
                r'<div class="consignee[^"]*"[^>]*>\s*<span class="txt">(.*?)</span>')

            orders.append({
                "orderId": order_id,
                "time": pick(r'<span class="dealtime"[^>]*>(.*?)</span>'),
                "consignee": consignee,
                "amount": amount,
                "payType": pay_type,
                "status": status,
                "products": [n for n in names if n][:10],
                "url": f"https://details.jd.com/normal/item.action?orderid={order_id}",
            })
        return orders

    @staticmethod
    def get_cart_num(auth, area=None) -> dict:
        """购物车商品数（functionId=pcCart_jc_getCartNum）。

        契约逐字取自搜索页实抓（2026-08-16 reqid=76，返回 200 + cartNum）：
        POST 但 body 留在 query、表单体为空，query 的 `appid` 是 `search-pc-java`
        而 h5st 用 `fb5df`，走 axios 那一档的头（带 x-referer-page / x-rp-client）。

        ⚠️ area 这里必须带 addressId 后缀（`full=True`）：
        `serInfo.area = "1_2800_55812_0.<addressId>"`（第五段来自当前账户 Cookie），
        与搜索侧截断到四段的用法不同。少掉后缀不报错但不与浏览器完全对齐。
        """
        body = {"serInfo": {"area": area or area_of(auth, full=True), "user-key": ""},
                "cartExt": {"specialId": 1}}
        return JdAPI.call_api(auth, "pcCart_jc_getCartNum", body=body, path="/api",
                              client_preset=CLIENT_PC_SEARCH, app_id=APPID_PC_ITEM,
                              referer=JdAPI._search_page_referer(auth),
                              origin=JdAPI.search_origin,
                              referer_page="https://search.jd.com/Search",
                              rp_client=RP_CLIENT_SEARCH, with_uuid=False,
                              body_in_query=True, param_order=ORDER_PC_API,
                              content_type="application/json")

    @staticmethod
    def get_browse_history(auth, page: int = 1, page_size: int = 20,
                           area=None, retry: int = 2, sku: str = "") -> dict:
        """我的浏览历史（functionId=pc_myjd_getBrowseHistory）。"""
        body = {"pageNo": page, "pageSize": page_size, "tag": 1,
                "source": "pc_sx_history"}
        return JdAPI.call_api(auth, "pc_myjd_getBrowseHistory", body=body, path="/",
                              client_preset=CLIENT_PC_ITEM_V3, app_id=APPID_PC_ITEM,
                              extra_params={"area": area or area_of(auth)},
                              referer=JdAPI.item_referer,
                              referer_page=(f"https://item.jd.com/{sku}.html" if sku
                                            else JdAPI.item_referer),
                              rp_client=RP_CLIENT_ITEM, origin=JdAPI.item_origin,
                              method="GET", param_order=ORDER_PC_ITEM,
                              retry=retry)

    @staticmethod
    def get_follow_products(auth, page: int = 1, page_size: int = 1,
                            area=None, retry: int = 2, sku: str = "") -> dict:
        """我关注的商品（functionId=pc_follow_product_new）。

        当前商品页 Network 请求只取一条（``pageSize=1``）；调用方仍可显式
        传更大的分页值，但默认保持与页面初始化请求一致。
        """
        body = {"pageNo": page, "pageSize": page_size, "tag": 1,
                "source": "pc_sx_follow_product"}
        return JdAPI.call_api(auth, "pc_follow_product_new", body=body, path="/",
                              client_preset=CLIENT_PC_ITEM_V3, app_id=APPID_PC_ITEM,
                              extra_params={"area": area or area_of(auth)},
                              referer=JdAPI.item_referer,
                              referer_page=(f"https://item.jd.com/{sku}.html" if sku
                                            else JdAPI.item_referer),
                              rp_client=RP_CLIENT_ITEM, origin=JdAPI.item_origin,
                              method="GET", param_order=ORDER_PC_ITEM,
                              retry=retry)

    # ---------- 咚咚 ----------

    @staticmethod
    def get_aid_info(auth) -> dict:
        """取咚咚 aid（WS 建连必需）。

        响应形态：{"code":"0","pin":"<账号>","aid":"<会话 aid>","subCode":"0", ...}
        """
        body = {
            "aidClientType": "comet",
            "aidClientVersion": "comet -v1.0.0",
            "appId": "im.customer",
            "os": "comet",
            "entry": "",
            "reqSrc": "s_comet",
            "siteId": -1,
            "customerAppId": "im.customer",
        }
        res = JdAPI.call_api(auth, "getAidInfo", body=body,
                             client_preset=CLIENT_IMH5, app_id=APPID_DONGDONG,
                             referer=JdAPI.chat_referer, origin=JdAPI.chat_origin,
                             referer_page=JdAPI.chat_referer,
                             rp_client=RP_CLIENT_CHAT, with_uuid=False,
                             with_time=True, param_order=ORDER_DD_WITH_TIME)
        if isinstance(res, dict) and res.get("aid"):
            auth.set_chat_info(aid=res["aid"], app_id="im.customer", client_type="comet")
            logger.success(f"拿到咚咚 aid={res['aid']} pin={res.get('pin')}")
        else:
            logger.warning(f"getAidInfo 未返回 aid：{res}")
        return res

    @staticmethod
    def get_chat_info(auth, vender_id="1", pid="", order_id="", shop_id="",
                      group_id="", entry="", dd_page_code="") -> dict:
        """咚咚会话初始化，返回商家信息、会话类型、客服身份等。"""
        body = {
            "lang": "zh_CN",
            "venderId": str(vender_id),
            "groupId": group_id,
            "pid": pid,
            "ppid": "",
            "shopId": shop_id,
            "siteId": -1,
            "entry": entry,
            "orderId": order_id,
            "reqSrc": "s_comet",
            "cAppId": "",
            "bbtf": "",
            "uniformBizInfo": {},
            "customerAppId": "im.customer",
        }
        res = JdAPI.call_api(auth, "getChatInfo", body=body,
                             client_preset=CLIENT_WH5, app_id=APPID_DONGDONG,
                             referer=JdAPI.chat_referer, origin=JdAPI.chat_origin,
                             referer_page=JdAPI.chat_referer,
                             rp_client=RP_CLIENT_CHAT, with_uuid=False,
                             with_time=False, param_order=ORDER_DD_NO_TIME)
        vender = (((res or {}).get("body") or {}).get("cache") or {}).get("vender") or {}
        if vender.get("appId"):
            logger.info(f"商家 {vender.get('name')} (id={vender.get('id')}) "
                        f"appId={vender['appId']}")
        return res

    @staticmethod
    def get_vender_app(auth, vender_id="1", default="jd.waiter") -> str:
        """取商家侧 appId —— 咚咚消息信封的 `to.app`。京东自营是 jd.waiter。"""
        res = JdAPI.get_chat_info(auth, vender_id=vender_id)
        vender = (((res or {}).get("body") or {}).get("cache") or {}).get("vender") or {}
        return vender.get("appId") or default

    @staticmethod
    def get_chat_session_log(auth, vender_id="1", count=20) -> dict:
        """历史会话消息。"""
        body = {
            "reqSrc": "s_comet",
            "appId": "im.customer",
            "showMsg": 1,
            "lastMsg": 1,
            "uniformBizInfo": {},
            "siteId": -1,
            "customerAppId": "im.customer",
        }
        return JdAPI.call_api(auth, "getChatSessionLog", body=body,
                              client_preset=CLIENT_IMH5, app_id=APPID_DONGDONG,
                              referer=JdAPI.chat_referer, origin=JdAPI.chat_origin,
                              referer_page=JdAPI.chat_referer,
                              rp_client=RP_CLIENT_CHAT, with_uuid=False,
                              with_time=True, param_order=ORDER_DD_WITH_TIME)

    @staticmethod
    def query_last_logs(auth, vender_id="1", num: int = 10,
                        start_timestamp: int = 0, reverse: bool = True) -> dict:
        """拉取咚咚历史消息（functionId=queryLastLogs）。

        字段顺序和值逐项来自 jdcs.jd.com 2026-08-31 的 reqid=76；
        ``aid``/``uid`` 使用当前 ``JdAuth`` 会话状态，绝不省略。
        """
        pin = auth.pin or ""
        body = {
            "terminal": {"version": "wh5", "pullType": 1},
            "aid": auth.aid or "",
            "uid": {
                "app": "im.customer",
                "pin": pin,
                "clientType": auth.client_type or "comet",
                "art": "",
            },
            "customer": pin,
            "venderId": str(vender_id),
            "startTimeStamp": start_timestamp,
            "reverse": bool(reverse),
            "num": num,
            "siteId": -1,
            "customerAppId": "im.customer",
        }
        return JdAPI.call_api(
            auth, "queryLastLogs", body=body, path="/api",
            client_preset=CLIENT_IMH5, app_id=APPID_DONGDONG,
            referer=JdAPI.chat_referer, origin=JdAPI.chat_origin,
            referer_page=JdAPI.chat_referer, rp_client=RP_CLIENT_CHAT,
            with_uuid=False, with_time=True, param_order=ORDER_DD_WITH_TIME,
        )

    @staticmethod
    def get_order_by_page(auth, vender_id="1", month: int = 24,
                          page: int = 1, page_size: int = 10) -> dict:
        """咚咚侧订单选择列表（functionId=getOrderByPage）。"""
        body = {
            "reqSrc": "s_h5",
            "appId": "im.customer",
            "venderId": str(vender_id),
            "month": month,
            "currentPage": page,
            "pageSize": page_size,
            "entry": "",
            "siteId": -1,
            "extParam": {},
            "uniformBizInfo": {},
            "customerAppId": "im.customer",
        }
        return JdAPI.call_api(
            auth, "getOrderByPage", body=body,
            client_preset=CLIENT_WH5, app_id=APPID_DONGDONG,
            referer=JdAPI.chat_referer, origin=JdAPI.chat_origin,
            referer_page=JdAPI.chat_referer, rp_client=RP_CLIENT_CHAT,
            with_uuid=False, with_time=False, param_order=ORDER_DD_NO_TIME,
        )

    @staticmethod
    def query_vender_recommend(auth, vender_id="1", keyword="",
                               page: int = 1, page_size: int = 10) -> dict:
        """咚咚商家推荐列表（functionId=queryVenderRecommend）。"""
        body = {
            "venderId": str(vender_id),
            "keyword": keyword,
            "currentPage": page,
            "pageSize": page_size,
            "reqSrc": "s_h5",
            "appId": "im.customer",
            "uniformBizInfo": {},
            "siteId": -1,
            "customerAppId": "im.customer",
        }
        return JdAPI.call_api(
            auth, "queryVenderRecommend", body=body,
            client_preset=CLIENT_WH5, app_id=APPID_DONGDONG,
            referer=JdAPI.chat_referer, origin=JdAPI.chat_origin,
            referer_page=JdAPI.chat_referer, rp_client=RP_CLIENT_CHAT,
            with_uuid=False, with_time=False, param_order=ORDER_DD_NO_TIME,
        )

    @staticmethod
    def dd_soa_request_fc(auth, vender_id="1", page: int = 1,
                          page_size: int = 10, action="browseProducts") -> dict:
        """咚咚侧统一业务请求（functionId=ddSoaRequestFC）。"""
        pin = auth.pin or ""
        body = {
            "dataType": "product",
            "action": action,
            "clientType": auth.client_type or "comet",
            "_pin_": pin,
            "pin": pin,
            "_token_": auth.aid or "",
            "appId": "im.customer",
            "page": page,
            "pageSize": page_size,
            "venderId": str(vender_id),
            "siteId": -1,
            "uniformBizInfo": {},
            "customerAppId": "im.customer",
        }
        return JdAPI.call_api(
            auth, "ddSoaRequestFC", body=body,
            client_preset=CLIENT_IMH5, app_id=APPID_DONGDONG,
            referer=JdAPI.chat_referer, origin=JdAPI.chat_origin,
            referer_page=JdAPI.chat_referer, rp_client=RP_CLIENT_CHAT,
            with_uuid=False, with_time=True, param_order=ORDER_DD_WITH_TIME,
        )
