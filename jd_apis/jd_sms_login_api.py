# coding: utf-8
"""京东 PC 手机号短信验证码登录。

浏览器源码链路：

1. ``GET /uc/graphic/sessionId/refresh`` 初始化短信图形验证码；
2. 用户完成人机验证后，``GET /uc/mobile/sendMessage`` 发送短信；
3. 用户输入六位短信码，``POST /uc/mobile/loginService`` 换取 ``thor/pin``。

URL query 与 form body 分别按登录页 ``aks.js`` 加密为 ``aksParamsU`` 和
``aksParamsB``。手机号和短信码只存在于进程内存中的 AKS 明文，不写日志。
"""

from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor
import ast
import base64
import html as html_lib
from html.parser import HTMLParser
import json
import random
import re
import uuid as uuid_lib
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit

from builder.header import HeaderBuilder, HeaderType
from jd_apis.jd_login_api import JdLoginAPI
from utils import aks, device_token, h5st5, http_client, summer_cryptico
from utils.jd_util import (
    APPID_PASSPORT, now_ms, parse_jsonp, random_jquery_callback,
)
from utils.trace_headers import LoginTraceContext


class _LoginInputParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = {}

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "input":
            return
        values = {str(key).lower(): (value or "") for key, value in attrs}
        value = values.get("value", "")
        for key in (values.get("id"), values.get("name")):
            if key:
                self.values[key] = value


class _SafeVerifyParser(HTMLParser):
    """Extract only contracts from a safe-verify page, never field values."""

    def __init__(self):
        super().__init__()
        self.title = ""
        self._in_title = False
        self.inputs = []
        self.forms = []
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        values = {str(key).lower(): (value or "") for key, value in attrs}
        tag = tag.lower()
        if tag == "title":
            self._in_title = True
        elif tag == "input":
            name = values.get("name") or values.get("id")
            if name and name not in self.inputs:
                self.inputs.append(name)
        elif tag == "form":
            self.forms.append((values.get("method", "get").lower(),
                               values.get("action", "")))
        elif tag == "script" and values.get("src"):
            self.scripts.append(values["src"])

    def handle_endtag(self, tag):
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def _extract_safe_web_config(html: str) -> dict:
    """Extract the JSON object assigned to ``window.safeWebConfig``.

    The certified page emits server state as a JSON object in an inline script.
    Use ``JSONDecoder.raw_decode`` so the object may be followed by a semicolon;
    deliberately do not evaluate JavaScript.
    """
    source = html or ""
    decoder = json.JSONDecoder()

    def json_object(value):
        if isinstance(value, dict):
            return value
        if not isinstance(value, str):
            return {}
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return {}
        return decoded if isinstance(decoded, dict) else {}

    def js_string(text):
        """Decode one leading JS string literal without executing code."""
        text = text.lstrip()
        if not text or text[0] not in ("'", '"'):
            return None
        quote = text[0]
        escaped = False
        end = None
        for index, char in enumerate(text[1:], 1):
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                end = index + 1
                break
        if end is None:
            return None
        literal = text[:end]
        try:
            return json.loads(literal) if quote == '"' else ast.literal_eval(literal)
        except (json.JSONDecodeError, SyntaxError, ValueError):
            return None

    def parse_candidate(candidate):
        variants = (candidate, html_lib.unescape(candidate))
        for variant in variants:
            try:
                value, _ = decoder.raw_decode(variant.lstrip())
            except (json.JSONDecodeError, TypeError):
                value = None
            if isinstance(value, dict):
                return value
            try:
                value = _parse_js_data_object(variant.lstrip())
            except ValueError:
                value = None
            if isinstance(value, dict):
                return value

            parse_match = re.match(r"JSON\s*\.\s*parse\s*\(\s*(.*)",
                                   variant, re.S)
            if not parse_match:
                continue
            argument = parse_match.group(1).lstrip()
            transform = ""
            call_match = re.match(
                r"(decodeURIComponent|decodeURI|atob)\s*\(\s*(.*)",
                argument, re.S,
            )
            if call_match:
                transform, argument = call_match.groups()
            serialized = js_string(argument)
            if not isinstance(serialized, str):
                continue
            if transform in ("decodeURIComponent", "decodeURI"):
                serialized = unquote(serialized)
            elif transform == "atob":
                try:
                    serialized = base64.b64decode(serialized).decode("utf-8")
                except (ValueError, UnicodeDecodeError):
                    continue
            value = json_object(serialized)
            if value:
                return value
        return {}
    pattern = re.compile(
        r"(?:(?:window\s*\.\s*)?safeWebConfig|"
        r"(?:var|let|const)\s+safeWebConfig)\s*=\s*"
    )
    for match in pattern.finditer(source):
        value = parse_candidate(source[match.end():].lstrip())
        if value:
            return value
    return {}


def _parse_js_data_object(source: str) -> dict:
    """Parse a restricted JS/JSON5 data literal without evaluating code."""

    class Parser:
        def __init__(self, text):
            self.text = text
            self.index = 0

        def skip(self):
            while self.index < len(self.text):
                if self.text[self.index].isspace():
                    self.index += 1
                elif self.text.startswith("//", self.index):
                    end = self.text.find("\n", self.index + 2)
                    self.index = len(self.text) if end < 0 else end + 1
                elif self.text.startswith("/*", self.index):
                    end = self.text.find("*/", self.index + 2)
                    if end < 0:
                        raise ValueError("unterminated comment")
                    self.index = end + 2
                else:
                    break

        def string(self):
            quote = self.text[self.index]
            start = self.index
            self.index += 1
            escaped = False
            while self.index < len(self.text):
                char = self.text[self.index]
                self.index += 1
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    literal = self.text[start:self.index]
                    try:
                        return (json.loads(literal) if quote == '"'
                                else ast.literal_eval(literal))
                    except (json.JSONDecodeError, SyntaxError, ValueError) as exc:
                        raise ValueError("invalid string") from exc
            raise ValueError("unterminated string")

        def identifier(self):
            match = re.match(r"[A-Za-z_$][A-Za-z0-9_$]*",
                             self.text[self.index:])
            if not match:
                raise ValueError("identifier expected")
            self.index += len(match.group(0))
            return match.group(0)

        def value(self):
            self.skip()
            if self.index >= len(self.text):
                raise ValueError("value expected")
            char = self.text[self.index]
            if char == "{":
                return self.object()
            if char == "[":
                return self.array()
            if char in ("'", '"'):
                return self.string()
            number = re.match(
                r"[-+]?(?:0[xX][0-9A-Fa-f]+|(?:\d+\.?\d*|\.\d+)"
                r"(?:[eE][-+]?\d+)?)",
                self.text[self.index:],
            )
            if number:
                token = number.group(0)
                self.index += len(token)
                if re.match(r"[-+]?0[xX]", token):
                    return int(token, 0)
                return float(token) if any(c in token for c in ".eE") else int(token)
            name = self.identifier()
            constants = {"true": True, "false": False,
                         "null": None, "undefined": None}
            if name not in constants:
                raise ValueError("executable identifier rejected")
            return constants[name]

        def object(self):
            result = {}
            self.index += 1
            while True:
                self.skip()
                if self.index >= len(self.text):
                    raise ValueError("unterminated object")
                if self.text[self.index] == "}":
                    self.index += 1
                    return result
                key = (self.string() if self.text[self.index] in ("'", '"')
                       else self.identifier())
                self.skip()
                if self.index >= len(self.text) or self.text[self.index] != ":":
                    raise ValueError("colon expected")
                self.index += 1
                result[str(key)] = self.value()
                self.skip()
                if self.index < len(self.text) and self.text[self.index] == ",":
                    self.index += 1
                    continue
                if self.index < len(self.text) and self.text[self.index] == "}":
                    continue
                raise ValueError("comma expected")

        def array(self):
            result = []
            self.index += 1
            while True:
                self.skip()
                if self.index >= len(self.text):
                    raise ValueError("unterminated array")
                if self.text[self.index] == "]":
                    self.index += 1
                    return result
                result.append(self.value())
                self.skip()
                if self.index < len(self.text) and self.text[self.index] == ",":
                    self.index += 1
                    continue
                if self.index < len(self.text) and self.text[self.index] == "]":
                    continue
                raise ValueError("comma expected")

    parser = Parser(source)
    parser.skip()
    value = parser.value()
    if not isinstance(value, dict):
        raise ValueError("top-level object expected")
    return value


def _safe_config_syntax_hints(html: str) -> list[str]:
    """Describe nearby assignment syntax while redacting opaque identifiers."""
    source = html or ""
    hints = []
    for match in re.finditer(r"safeWebConfig", source, re.I):
        snippet = source[max(0, match.start() - 120):match.end() + 500]
        snippet = re.sub(
            r"(?<![A-Z_])[A-Za-z0-9+/=_-]{24,}",
            lambda item: f"<opaque:{len(item.group(0))}>",
            snippet,
        )
        snippet = " ".join(snippet.split())[:800]
        if snippet and snippet not in hints:
            hints.append(snippet)
    return hints[:8]


def _safe_config_contract(config: dict) -> dict:
    """Return a log-safe description of safeWebConfig without token values."""
    if not isinstance(config, dict):
        return {}
    methods = []
    for item in config.get("list") or []:
        if not isinstance(item, dict):
            continue
        params = item.get("params")
        methods.append({
            "validateType": str(item.get("validateType") or "")[:80],
            "validateName": str(item.get("validateName") or "")[:80],
            "model": bool(item.get("model")),
            "keys": sorted(str(key) for key in item.keys()),
            "paramsKeys": (
                sorted(str(key) for key in params.keys())
                if isinstance(params, dict) else []
            ),
            "enPLength": len(str(item.get("enP") or "")),
        })
    return {
        "keys": sorted(str(key) for key in config.keys()),
        "oLength": len(str(config.get("o") or "")),
        "sLength": len(str(config.get("s") or "")),
        "methods": methods[:40],
    }


def parse_login_inputs(html: str) -> dict:
    parser = _LoginInputParser()
    parser.feed(html or "")
    if not parser.values.get("uuid"):
        raise ValueError("登录页缺少 uuid")
    return parser.values


def normalize_mobile(mobile: str, area_code="0086") -> str:
    digits = re.sub(r"[\s()-]", "", str(mobile or ""))
    if not re.fullmatch(r"\+?\d{6,30}", digits):
        raise ValueError("手机号格式不正确")
    area = re.sub(r"[\s()-]", "", str(area_code or "0086"))
    if area in ("86", "+86", "0086"):
        local = digits.lstrip("+")
        if local.startswith("0086"):
            local = local[4:]
        elif local.startswith("86") and len(local) > 11:
            local = local[2:]
        return local
    if not re.fullmatch(r"(?:00|\+)?\d{1,6}", area):
        raise ValueError("国家/地区代码格式不正确")
    prefix = area[2:] if area.startswith("00") else area.lstrip("+")
    local = digits.lstrip("+")
    if local.startswith("00" + prefix):
        local = local[2 + len(prefix):]
    elif local.startswith(prefix):
        local = local[len(prefix):]
    return f"+{prefix}{local}"


def _parse_response(response) -> dict:
    try:
        payload = response.json()
        if isinstance(payload, dict):
            return payload
    except (ValueError, TypeError):
        pass
    text = (getattr(response, "text", "") or "").strip().rstrip(";")
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    try:
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else {}
    except (ValueError, TypeError):
        return {"_invalid_response_length": len(text)}


def _parse_seq_sid(script: str) -> str:
    match = re.search(r'_jdtdmap_sessionId\s*=\s*["\'](\d+)["\']',
                      script or "")
    if not match:
        raise ValueError("seq.jd.com 响应缺少 _jdtdmap_sessionId")
    return match.group(1)


@dataclass
class SmsLoginContext:
    auth: object
    session: object
    trace: LoginTraceContext
    fields: dict
    public_key: str
    captcha_status: int = 0
    captcha_session_id: str = ""
    captcha_jwt_token: str = ""


@dataclass
class SafeVerifyPage:
    status_code: int
    final_url: str
    title: str
    input_names: list[str]
    forms: list[tuple[str, str]]
    script_urls: list[str]
    query_fields: list[tuple[str, int]]
    config_contract: dict
    config_syntax_hints: list[str]
    config: dict = field(repr=False, default_factory=dict)
    request_params: dict = field(repr=False, default_factory=dict)
    html: str = field(repr=False, default="")


class JdSmsLoginAPI:
    SMS_CAPTCHA_APP_ID = "1000802"
    SAFE_LOGIN_SMS_TYPES = {
        "DANGEROUS_DOWN", "PARENT_DANGEROUS_DOWN", "HISTORY_MOBILE",
    }

    @staticmethod
    def _update_cookies(context, response=None):
        if response is not None:
            context.auth.absorb_response(response, session=context.session)

    @staticmethod
    def _passport_cookies(context):
        """使用当前 auth 的完整 Cookie，不按接口维护字段白名单。"""
        return {str(key): str(value)
                for key, value in context.auth.cookie.items()
                if value not in (None, "")}

    @staticmethod
    def _ajax_headers(context, accept="application/json, text/javascript, */*; q=0.01",
                      form=False):
        headers = HeaderBuilder.build_qr_validation(context.trace.next_headers())
        headers.set_referer(JdLoginAPI.login_page)
        headers.set_header("accept", accept)
        if form:
            headers.set_header("content-type",
                               "application/x-www-form-urlencoded; charset=UTF-8")
            headers.set_header("origin", JdLoginAPI.passport_url)
        order = [
            "sgm-context", "sec-ch-ua-platform", "jdas-trace-id", "referer",
            "sec-ch-ua", "sec-ch-ua-mobile", "x-requested-with", "user-agent",
            "accept", "jdas-page-id",
        ]
        if form:
            order.append("content-type")
        order.extend(("jdas-session-id", "accept-encoding", "accept-language"))
        if form:
            order.append("origin")
        order.extend(("priority", "sec-fetch-dest", "sec-fetch-mode",
                      "sec-fetch-site"))
        return headers.reorder(order)

    @staticmethod
    def _encrypted_query(context, pairs):
        plain = aks.encode_query_pairs(pairs)
        return aks.encrypt_query(plain, context.public_key)

    @staticmethod
    def _load_seq_sid(context):
        headers = HeaderBuilder.build(HeaderType.QR_JSONP)
        headers.set_referer(JdLoginAPI.login_page)
        response = context.session.get(
            "https://seq.jd.com/jseqf.html",
            headers=headers.get(),
            params=(("bizId", "passport_jd_com_login_pc"),
                    ("platform", "js"), ("version", "1")),
            cookies=context.auth.cookie, verify=False, timeout=15,
        )
        JdSmsLoginAPI._update_cookies(context, response)
        if response.status_code != 200:
            raise RuntimeError(f"登录行为序列初始化失败 HTTP {response.status_code}")
        try:
            context.fields["seqSid"] = _parse_seq_sid(response.text)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc

    @staticmethod
    def _load_qr_cookie(context):
        """复刻登录页首屏 QR 图片请求，只保留可跨子域的 wlfstk_smdl。"""
        headers = HeaderBuilder.build(HeaderType.QR_IMAGE)
        response = context.session.get(
            f"{JdLoginAPI.qr_url}/show", headers=headers.get(),
            params=(("appid", JdLoginAPI.APPID), ("size", 147), ("t", now_ms())),
            cookies=context.auth.cookie, verify=False, timeout=15,
        )
        JdSmsLoginAPI._update_cookies(context, response)
        token = response.cookies.get("wlfstk_smdl")
        if response.status_code != 200 or not token:
            raise RuntimeError(
                f"登录页 QR 设备 Cookie 初始化失败 HTTP {response.status_code}"
            )
        context.auth.update_cookies({"wlfstk_smdl": token}, persist=True)

    @staticmethod
    def _load_sso_domains(context):
        """复刻 ssoDomain.js：取动态列表，并保留 /alive 返回 success 的域。"""
        headers = JdSmsLoginAPI._ajax_headers(context)
        response = context.session.get(
            f"{JdLoginAPI.passport_url}/ssoDomain/getList",
            headers=headers.get(),
            params=(("ReturnUrl", JdLoginAPI._return_url()),
                    ("r", random.random())),
            cookies=JdSmsLoginAPI._passport_cookies(context),
            verify=False, timeout=15,
        )
        JdSmsLoginAPI._update_cookies(context, response)
        try:
            candidates = response.json()
        except (ValueError, TypeError):
            candidates = []
        candidates = [
            str(domain) for domain in candidates[:32]
            if re.fullmatch(r"[A-Za-z0-9.-]{3,253}", str(domain))
        ] if isinstance(candidates, list) else []

        def alive(domain):
            callback = random_jquery_callback()
            probe_headers = HeaderBuilder.build(HeaderType.QR_JSONP)
            probe_headers.set_referer(JdLoginAPI.login_page)
            try:
                probe = http_client.get(
                    f"https://{domain}/alive", headers=probe_headers.get(),
                    params=(("callback", callback), ("_", now_ms())),
                    verify=False, timeout=8,
                )
                outer = parse_jsonp(probe.text)
                if outer is None:
                    match = re.match(r"^[^(]*\((.*)\)[;\s]*$", probe.text, re.S)
                    quoted = match.group(1).strip() if match else ""
                    if (len(quoted) >= 2 and quoted[0] == quoted[-1]
                            and quoted[0] in ("'", '"')):
                        outer = quoted[1:-1].replace("\\'", "'")
                payload = json.loads(outer) if isinstance(outer, str) else outer
                reachable = (probe.status_code == 200
                             and isinstance(payload, dict)
                             and payload.get("result") == "success")
                return (domain if reachable else ""), probe
            except (ValueError, TypeError, RuntimeError):
                return "", None

        with ThreadPoolExecutor(max_workers=min(8, len(candidates) or 1)) as pool:
            results = list(pool.map(alive, candidates))
        # 网络并发只负责请求；Cookie 在主线程按候选列表顺序统一吸收，避免并发
        # 改 auth，也确保失败的 /alive 响应若下发 Set-Cookie 仍会被持久化。
        reachable = []
        for domain, probe in results:
            if probe is not None:
                JdSmsLoginAPI._update_cookies(context, probe)
            if domain:
                reachable.append(domain)
        context.fields["ssoDomains"] = ",".join(reachable)

    @staticmethod
    def start(auth) -> SmsLoginContext:
        session = http_client.session()
        headers = HeaderBuilder.build(HeaderType.DOC)
        response = session.get(
            JdLoginAPI.login_page,
            headers=headers.get(), cookies=auth.cookie,
            verify=False, timeout=15,
        )
        auth.absorb_response(response, session=session)
        if response.status_code != 200:
            raise RuntimeError(f"登录页初始化失败 HTTP {response.status_code}")
        fields = parse_login_inputs(response.text)
        trace = LoginTraceContext.from_html(response.text)
        context = SmsLoginContext(auth, session, trace, fields, public_key="")
        JdSmsLoginAPI._load_qr_cookie(context)
        context.public_key = JdLoginAPI._public_key(
            auth, session, trace.next_headers())
        # login 页的 eid.js 会异步执行 pc-tk.js，把这三项写进隐藏域：
        # eid=getJdEid()，eid2=getJsToken().jsToken，sessionId/fp=getJsToken().fp。
        # 这里在本机 Node 补同一套浏览器环境，禁止空字段继续请求。
        device_token.configure(auth.cookie_str)
        refresh_device = auth.device_profile != device_token.DEVICE_PROFILE
        device = device_token.get_device_fields(force_refresh=refresh_device)
        fields.update(device)
        fields["sessionId"] = device["fp"]
        auth.update_cookies({
            "3AB9D23F7A4B3C9B": device["eid"],
            "3AB9D23F7A4B3CSS": device["eid2"],
            **({"_gia_d": device["giaD"]} if device["giaD"] else {}),
        })
        auth.set_device_profile(device_token.DEVICE_PROFILE, persist=True)
        JdSmsLoginAPI._load_sso_domains(context)
        JdSmsLoginAPI._load_seq_sid(context)
        JdSmsLoginAPI.refresh_captcha(context)
        return context

    @staticmethod
    def refresh_captcha(context):
        encrypted = JdSmsLoginAPI._encrypted_query(
            context, (("appId", JdSmsLoginAPI.SMS_CAPTCHA_APP_ID),))
        headers = JdSmsLoginAPI._ajax_headers(context)
        response = context.session.get(
            f"{JdLoginAPI.passport_url}/uc/graphic/sessionId/refresh",
            headers=headers.get(), params=(("aksParamsU", encrypted),),
            cookies=JdSmsLoginAPI._passport_cookies(context),
            verify=False, timeout=15,
        )
        JdSmsLoginAPI._update_cookies(context, response)
        payload = _parse_response(response)
        if response.status_code != 200 or payload.get("code") not in (1, "1"):
            raise RuntimeError(
                f"短信图形验证码初始化失败 HTTP {response.status_code} "
                f"code={payload.get('code')}"
            )
        context.captcha_status = int(payload.get("status") or 0)
        context.captcha_session_id = str(payload.get("sessionId") or "")
        context.captcha_jwt_token = str(payload.get("jwtToken") or "")
        if context.captcha_status == 1 and (
                not context.captcha_session_id or not context.captcha_jwt_token):
            raise RuntimeError("短信图形验证码响应缺少 sessionId/jwtToken")
        return payload

    @staticmethod
    def send_code(context, mobile: str, verify_token=""):
        if context.captcha_status == 1 and not verify_token:
            return False, "发送短信前必须完成人机验证", {}
        fields = context.fields
        pairs = [
            ("source", fields.get("source", "")),
            ("eid", fields.get("eid", "")),
            ("uuid", fields.get("uuid", "")),
            ("mobile", mobile),
            ("imageAuthCodeToken", ""),
            ("firstShowAccountLoginPage",
             fields.get("firstShowAccountLoginPage", JdLoginAPI.FIRST_SHOW_ACCOUNT_LOGIN_PAGE)),
            ("pageSource", fields.get("pageSource", JdLoginAPI.PAGE_SOURCE)),
            ("pageLocation", fields.get("pageLocation", JdLoginAPI.PAGE_LOCATION)),
        ]
        if context.captcha_session_id:
            pairs.append(("graphicCaptchaSessionId", context.captcha_session_id))
        if context.captcha_jwt_token:
            pairs.append(("graphicCaptchaJwtToken", context.captcha_jwt_token))
        if verify_token:
            pairs.append(("graphicCaptchaVerifyToken", verify_token))
        # mobileLogin.js uses cache:false; jQuery appends this before aks.js
        # encrypts the complete GET query.
        pairs.append(("_", now_ms()))
        encrypted = JdSmsLoginAPI._encrypted_query(context, pairs)
        headers = JdSmsLoginAPI._ajax_headers(context, accept="*/*")
        response = context.session.get(
            f"{JdLoginAPI.passport_url}/uc/mobile/sendMessage",
            headers=headers.get(), params=(("aksParamsU", encrypted),),
            cookies=JdSmsLoginAPI._passport_cookies(context),
            verify=False, timeout=15,
        )
        JdSmsLoginAPI._update_cookies(context, response)
        payload = _parse_response(response)
        success = response.status_code == 200 and payload.get("code") in (1, "1")
        message = str(payload.get("msg") or ("短信已发送" if success else "短信发送失败"))
        message = message.replace(mobile, "<mobile>")
        return success, message, payload

    @staticmethod
    def _device_fields(context):
        fields = context.fields
        cookies = context.auth.cookie
        return {
            "eid": fields.get("eid") or cookies.get("3AB9D23F7A4B3C9B", ""),
            "eid2": fields.get("eid2") or cookies.get("3AB9D23F7A4B3CSS", ""),
            "fp": fields.get("fp", ""),
        }

    @staticmethod
    def submit_code(context, mobile: str, sms_code: str):
        if not re.fullmatch(r"\d{6}", str(sms_code or "")):
            return False, "短信验证码必须是六位数字", {}
        fields = context.fields
        device = JdSmsLoginAPI._device_fields(context)
        h5st5.configure(context.auth.cookie_str, JdLoginAPI.passport_url,
                        JdLoginAPI.login_page)
        signed = h5st5.sign({"mobile": mobile}, APPID_PASSPORT)
        if not signed.get("h5st") or signed.get("_stk") != "mobile":
            return False, "手机号登录 h5st/_stk 生成失败", {}

        body_pairs = [
            ("uuid", fields.get("uuid", "")),
            ("eid", device["eid"]),
            ("eid2", device["eid2"]),
            ("fp", device["fp"]),
            ("_t", fields.get("_t", "_t")),
            ("mobile", mobile),
            ("mobileCode", sms_code),
            ("pageSource", fields.get("pageSource", JdLoginAPI.PAGE_SOURCE)),
            ("pageLocation", fields.get("pageLocation", JdLoginAPI.PAGE_LOCATION)),
            ("loginType", fields.get("loginType", "f")),
            ("sa_token", fields.get("sa_token", "")),
            ("seqSid", fields.get("seqSid", "")),
            ("useSlideAuthCode", fields.get("useRandomSlideAuthCode", "")),
            ("authcode", ""),
            ("ssoDomains", fields.get("ssoDomains", "")),
            ("h5st", signed["h5st"]),
            ("_stk", signed["_stk"]),
        ]
        return_query = urlsplit(JdLoginAPI.login_page).query
        query = (
            f"{return_query}&r={random.random()}&version=2015"
            if return_query else f"r={random.random()}&version=2015"
        )
        encrypted_url = aks.encrypt_query(query, context.public_key)
        encrypted_body = JdSmsLoginAPI._encrypted_query(context, body_pairs)
        headers = JdSmsLoginAPI._ajax_headers(
            context, accept="text/plain, */*; q=0.01", form=True)
        response = context.session.post(
            f"{JdLoginAPI.passport_url}/uc/mobile/loginService",
            headers=headers.get(), params=(("aksParamsU", encrypted_url),),
            data=(("aksParamsB", encrypted_body),),
            cookies=JdSmsLoginAPI._passport_cookies(context),
            verify=False, timeout=15,
        )
        JdSmsLoginAPI._update_cookies(context, response)
        payload = _parse_response(response)
        if payload.get("_t"):
            fields["_t"] = str(payload["_t"])
        target = payload.get("success") or payload.get("transfer")
        if target:
            resolved = urljoin(JdLoginAPI.login_page, str(target))
            parsed = urlsplit(resolved)
            if parsed.scheme not in ("http", "https"):
                return False, "登录成功跳转地址协议异常", payload
            follow_headers = HeaderBuilder.build(HeaderType.DOC)
            follow_headers.set_header("referer", JdLoginAPI.login_page)
            follow = context.session.get(
                resolved, headers=follow_headers.get(), cookies=context.auth.cookie,
                verify=False, timeout=20, allow_redirects=True,
            )
            JdSmsLoginAPI._update_cookies(context, follow)
            if context.auth.is_login:
                return True, "手机号短信登录成功", payload
            return False, "登录跳转完成，但未取得 thor/pin", payload

        for key in ("username", "authcode2", "emptyAuthcode", "pwd", "msg"):
            if payload.get(key):
                message = str(payload[key]).replace(mobile, "<mobile>")
                return False, message, payload
        if payload.get("newSafeVerify"):
            return False, "账号需要额外安全验证", payload
        return False, f"手机号登录失败 HTTP {response.status_code}", payload

    @staticmethod
    def load_safe_verify(context, payload: dict) -> SafeVerifyPage:
        """Follow the official safeVerifyUrl in the same in-memory session."""
        raw_target = str((payload or {}).get("safeVerifyUrl") or "")
        target = urljoin(JdLoginAPI.login_page, raw_target)
        parsed = urlsplit(target)
        host = (parsed.hostname or "").lower()
        allowed = ("jd.com", "jdpay.com", "jingdong.com")
        if (parsed.scheme != "https" or not any(
                host == suffix or host.endswith("." + suffix)
                for suffix in allowed)):
            raise RuntimeError(
                "额外安全验证跳转地址异常："
                f"scheme={parsed.scheme or '<empty>'} host={host or '<empty>'}"
            )
        headers = HeaderBuilder.build(HeaderType.DOC)
        headers.set_header("referer", JdLoginAPI.login_page)
        response = context.session.get(
            target,
            headers=headers.get(),
            cookies=context.auth.cookie,
            verify=False,
            timeout=20,
            allow_redirects=True,
        )
        JdSmsLoginAPI._update_cookies(context, response)
        if response.status_code != 200:
            raise RuntimeError(
                f"额外安全验证页面加载失败 HTTP {response.status_code}"
            )
        final_url = str(getattr(response, "url", "") or target)
        final_host = (urlsplit(final_url).hostname or "").lower()
        if not any(final_host == suffix or final_host.endswith("." + suffix)
                   for suffix in allowed):
            raise RuntimeError("额外安全验证页面重定向域异常")
        parser = _SafeVerifyParser()
        parser.feed(response.text or "")
        forms = [(method, urljoin(final_url, action) if action else final_url)
                 for method, action in parser.forms]
        scripts = list(dict.fromkeys(
            urljoin(final_url, source) for source in parser.scripts
        ))
        config = _extract_safe_web_config(response.text or "")
        request_pairs = parse_qsl(urlsplit(target).query, keep_blank_values=True)
        return SafeVerifyPage(
            status_code=response.status_code,
            final_url=final_url,
            title=" ".join(parser.title.split())[:120],
            input_names=parser.inputs[:80],
            forms=forms[:20],
            script_urls=scripts[:80],
            query_fields=[
                (str(name)[:80], len(str(value)))
                for name, value in request_pairs[:80]
            ],
            config_contract=_safe_config_contract(config),
            config_syntax_hints=([] if config else _safe_config_syntax_hints(
                response.text or ""
            )),
            config=config,
            request_params=dict(request_pairs),
            html=response.text or "",
        )

    @staticmethod
    def _safe_headers(context, page: SafeVerifyPage, form=False):
        headers = HeaderBuilder.build(HeaderType.DOC)
        parsed = urlsplit(page.final_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        headers.set_header("accept", "application/json, text/plain, */*")
        headers.set_header("referer", page.final_url)
        headers.set_header("origin", origin)
        headers.set_header("x-requested-with", "XMLHttpRequest")
        if form:
            headers.set_header(
                "content-type", "application/x-www-form-urlencoded;charset=UTF-8"
            )
        xsrf = context.auth.cookie.get("XSRF-TOKEN", "")
        if xsrf:
            headers.set_header("x-xsrf-token", xsrf)
        return headers.get()

    @staticmethod
    def _safe_common_fields(context, page: SafeVerifyPage) -> dict:
        parsed = urlsplit(page.final_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        device_token.configure(
            context.auth.cookie_str,
            page_url=page.final_url,
            origin=origin,
            referer=page.final_url,
        )
        device = device_token.get_device_fields()
        return {
            "fp": device["fp"],
            "eid": device["eid"],
            "eid2": device["eid2"],
            "uuid": str(uuid_lib.uuid4()),
        }

    @staticmethod
    def _safe_method(page: SafeVerifyPage, validate_type="") -> dict:
        methods = page.config.get("list") or []
        if validate_type:
            methods = [item for item in methods if isinstance(item, dict)
                       and item.get("validateType") == validate_type]
        for item in methods:
            params = item.get("params") if isinstance(item, dict) else None
            if (isinstance(params, dict) and params.get("m")
                    and item.get("enP") and item.get("validateType")):
                if item.get("validateType") in JdSmsLoginAPI.SAFE_LOGIN_SMS_TYPES:
                    return item
        return {}

    @staticmethod
    def _safe_result_message(payload: dict, fallback: str, secret="") -> str:
        result = payload.get("resultData")
        message = ""
        if isinstance(result, dict):
            message = str(result.get("msg") or "")
        message = message or str(payload.get("resultMsg") or fallback)
        if secret:
            message = message.replace(str(secret), "<masked>")
        return message

    @staticmethod
    def send_safe_mobile_code(context, page: SafeVerifyPage,
                              validate_type="", delivery="msg"):
        """Send the Authentication Cube's second SMS entirely over HTTP."""
        method = JdSmsLoginAPI._safe_method(page, validate_type)
        if not method:
            return False, "额外安全验证没有可直接执行的手机号分支", {}, {}
        params = method["params"]
        mobile = str(params["m"])
        headers = JdSmsLoginAPI._safe_headers(context, page)
        key_response = context.session.get(
            "https://aq.jd.com/pwd/gmpk",
            headers=headers,
            params=(("s", "1"),),
            cookies=context.auth.cookie,
            verify=False,
            timeout=20,
        )
        JdSmsLoginAPI._update_cookies(context, key_response)
        key_payload = _parse_response(key_response)
        public_key = str(key_payload.get("resultData") or "")
        if (key_response.status_code != 200
                or key_payload.get("resultCode") != "10000"
                or not public_key):
            return False, "额外安全验证加密公钥获取失败", method, key_payload

        config = page.config
        fields = {
            "p": method["enP"],
            "m": summer_cryptico.encrypt(public_key, mobile),
            "v": method["validateType"],
            "o": config.get("o") or page.request_params.get("o", ""),
            "s": config.get("s") or page.request_params.get("s", ""),
        }
        fields.update(JdSmsLoginAPI._safe_common_fields(context, page))
        fields["f"] = delivery
        response = context.session.post(
            "https://aq.jd.com/mobile/getCode",
            headers=JdSmsLoginAPI._safe_headers(context, page, form=True),
            data=fields,
            cookies=context.auth.cookie,
            verify=False,
            timeout=20,
        )
        JdSmsLoginAPI._update_cookies(context, response)
        payload = _parse_response(response)
        success = response.status_code == 200 and payload.get("success") is True
        return (
            success,
            JdSmsLoginAPI._safe_result_message(
                payload,
                "额外安全验证短信已发送" if success else "额外安全验证短信发送失败",
                mobile,
            ),
            method,
            payload,
        )

    @staticmethod
    def submit_safe_mobile_code(context, page: SafeVerifyPage,
                                method: dict, code: str):
        """Validate Authentication Cube SMS and follow its auth return page."""
        if not re.fullmatch(r"\d{6}", str(code or "")):
            return False, "额外安全验证短信验证码必须是六位数字", {}
        config = page.config
        fields = {
            "p": method.get("enP", ""),
            "c": str(code),
            "v": method.get("validateType", ""),
            "o": config.get("o") or page.request_params.get("o", ""),
            "s": config.get("s") or page.request_params.get("s", ""),
        }
        fields.update(JdSmsLoginAPI._safe_common_fields(context, page))
        fields["rnd"] = random.random()
        response = context.session.post(
            "https://aq.jd.com/mobile/validateCode",
            headers=JdSmsLoginAPI._safe_headers(context, page, form=True),
            data=fields,
            cookies=context.auth.cookie,
            verify=False,
            timeout=20,
        )
        JdSmsLoginAPI._update_cookies(context, response)
        payload = _parse_response(response)
        if response.status_code != 200 or payload.get("success") is not True:
            return False, JdSmsLoginAPI._safe_result_message(
                payload, "额外安全验证短信校验失败"
            ), payload

        result = payload.get("resultData") or {}
        target = result.get("page") if isinstance(result, dict) else ""
        if target:
            resolved = urljoin(page.final_url, str(target))
            parsed = urlsplit(resolved)
            if parsed.scheme != "https" or not parsed.hostname:
                return False, "额外安全验证返回地址异常", payload
            follow = context.session.get(
                resolved,
                headers=JdSmsLoginAPI._safe_headers(context, page),
                cookies=context.auth.cookie,
                verify=False,
                timeout=20,
                allow_redirects=True,
            )
            JdSmsLoginAPI._update_cookies(context, follow)
        return True, "额外安全验证短信校验通过", payload

    @staticmethod
    def login(auth, mobile: str, code_provider, safe_code_provider=None,
              area_code="0086", timeout=180, on_status=None):
        """Complete SMS login, including JCAP and optional safe verification.

        ``code_provider`` and ``safe_code_provider`` are callables receiving a
        short prompt and returning the six-digit code.  This keeps terminal or
        GUI interaction outside the protocol layer while giving demos a small
        one-call entry point.
        """
        from utils.jcap_solver import solve_graphic_captcha

        if not callable(code_provider):
            raise ValueError("code_provider 必须是可调用对象")
        safe_code_provider = safe_code_provider or code_provider
        if not callable(safe_code_provider):
            raise ValueError("safe_code_provider 必须是可调用对象")

        def emit(stage, message):
            if on_status:
                on_status(stage, message)

        normalized = normalize_mobile(mobile, area_code)
        emit("prepare", "正在初始化手机号登录")
        context = JdSmsLoginAPI.start(auth)
        verify_token = ""
        if context.captcha_status == 1:
            emit("captcha", "正在纯程序计算人机验证")
            verify_token = solve_graphic_captcha(
                context.captcha_session_id,
                normalized,
                context.auth.cookie_str,
                timeout=timeout,
                page_url=JdLoginAPI.login_page,
                cookie_callback=lambda cookies: context.auth.update_cookies(
                    cookies, persist=True),
                local_storage=context.auth.local_storage_for(
                    JdLoginAPI.login_page),
                storage_callback=lambda values: context.auth.replace_local_storage(
                    JdLoginAPI.login_page, values, persist=True),
            )

        success, message, _ = JdSmsLoginAPI.send_code(
            context, normalized, verify_token
        )
        if not success:
            return False, message, auth
        emit("sms", message)
        sms_code = str(code_provider("短信验证码（6位）: ") or "").strip()
        success, message, payload = JdSmsLoginAPI.submit_code(
            context, normalized, sms_code
        )
        if success:
            return True, message, auth
        if not payload.get("newSafeVerify"):
            return False, message, auth

        emit("safe", "账号需要额外安全验证")
        page = JdSmsLoginAPI.load_safe_verify(context, payload)
        safe_ok, safe_message, method, _ = JdSmsLoginAPI.send_safe_mobile_code(
            context, page
        )
        if not safe_ok:
            return False, safe_message, auth
        emit("safe_sms", safe_message)
        safe_code = str(safe_code_provider(
            "额外安全验证短信验证码（6位）: "
        ) or "").strip()
        safe_ok, safe_message, _ = JdSmsLoginAPI.submit_safe_mobile_code(
            context, page, method, safe_code
        )
        if not safe_ok:
            return False, safe_message, auth
        if not auth.is_login:
            return False, "额外安全验证通过，但未取得 thor/pin", auth
        return True, "手机号短信登录成功", auth
