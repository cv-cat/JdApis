# coding: utf-8
"""京东纯算工具集：常量、cookie 解析、jsonp 解包、随机量生成。

h5st 签名不在这里——它需要常驻 node 进程和服务端换 token，独立在 `utils/h5st5.py`。
"""

import hashlib
import json
import random
import re
import string
import time

# ---------------- h5st 业务标识 ----------------
# 同一套算法，不同业务各自向 cactus 换取独立 token。以下均为实抓。
APPID_PASSPORT = "73806"    # passport.jd.com 登录侧
APPID_DONGDONG = "2b51e"    # 咚咚 jdcs.jd.com
APPID_PC_ITEM = "fb5df"     # PC 商品页 item.jd.com
APPID_PC_SEARCH = "f06cc"   # PC 搜索页 search.jd.com

# ---------------- api.m.jd.com 固定 query 预设 ----------------
CLIENT_WH5 = {"appid": "wh5", "client": "wh5", "clientVersion": "1.0.0",
              "loginType": "3"}
CLIENT_IMH5 = {"client": "imh5", "appid": "imh5", "clientVersion": "1.0.0",
               "loginType": "3"}
CLIENT_PC_ITEM = {"appid": "pc-item-soa", "client": "pc", "clientVersion": "1.0.0",
                  "loginType": "3"}
CLIENT_PC_ITEM_V3 = {"appid": "item-v3", "client": "pc", "clientVersion": "1.0.0",
                     "loginType": "3"}
CLIENT_PC_SEARCH = {"appid": "search-pc-java", "client": "pc",
                    "clientVersion": "1.0.0", "loginType": "3"}
CLIENT_ORDER = {"appid": "order-jd-com", "client": "pc",
                "clientVersion": "1.0.0", "loginType": "3"}

# 默认收货地区仅用于缺少定位 Cookie 时兜底；改地区会影响价格与库存。
#
# ⚠️ 这只是**没有 cookie 时的兜底**。真实请求里的 area 必须由 `area_of(auth)`
# 从 `ipLoc-djd` 现取。Cookie 的第五段是账户自己的 addressId，属于私有
# 数据，不应硬编码或写进日志。
DEFAULT_AREA = "1_2800_55812_0"


def area_of(auth, full: bool = False) -> str:
    """从 `ipLoc-djd` cookie 现取收货地区，转成 body 里的下划线写法。

    同一次页面加载的字段形态：

        cookie ipLoc-djd            = 1-2800-55812-0.<addressId>
        searchWare      body.area   = 1_2800_55812_0            ← 截断
        getCartNum      body.area   = 1_2800_55812_0.<addressId> ← 带 addressId

    **同一个页面里两个接口用的 area 不一样**，所以不能一把梭用一个常量。
    `full=True` 保留 `.addressId` 后缀（购物车 / 下单侧要），
    `full=False` 截到四段（搜索 / 商详侧要）。

    早先我们把 `DEFAULT_AREA` 硬编码进两边的 body，等于给购物车侧少发了
    addressId —— 属于「不传也没事？其实是慢性风控」那一类字段。
    """
    raw = (auth.cookie.get("ipLoc-djd") or "").strip() if auth else ""
    area = raw.replace("-", "_") if raw else DEFAULT_AREA
    if not full:
        area = area.split(".")[0]
    return area


def search_uuid_of(auth) -> str:
    """Return the UUID used by the current PC search bundle.

    Search no longer reads ``__jdu`` directly.  Its ``Zw()`` helper reads the
    second dot-separated field of ``__jda`` and falls back to ``-1``.  Keep
    this separate from the UUID rules used by item/order endpoints.
    """
    raw = str(auth.cookie.get("__jda") or "") if auth else ""
    fields = raw.split(".")
    if len(fields) > 1 and fields[1] not in ("", "-"):
        return fields[1]
    return "-1"


def xor5_encode(text: str) -> str:
    """京东风控埋点（`cactus.jd.com/behavior_report`）的载荷编码。

    实抓的 body 是 `data=<urlencode(密文)>`，密文看着像乱码，其实是
    **逐字符 XOR 5** 后的 JSON。对照（2026-08-16 reqid=212 实抓片段）：

        密文 ~'lklqQv'?42=3=2625<163
        明文 {"initTs":1786873709436

        '~'(126)^5 = '{'(123)     "'"(39)^5 = '"'(34)
        '?'(63) ^5 = ':'(58)      ')'(41)^5 = ','(44)

    对合运算，编解码同一个函数。这就是 §14.11 里「浏览器每次页面浏览都发
    5~10 次、我们一次都没发过」的那个包 —— 它和发 h5st token 的
    `request_algo` 同域，是同一套信誉系统的输入。
    """
    return "".join(chr(ord(ch) ^ 5) for ch in text)


# 对合：解码就是再编一次。留个别名，读代码时意图更清楚。
xor5_decode = xor5_encode

# ---------------- query 参数顺序（浏览器实抓） ----------------
# 浏览器发的 query 是有固定顺序的，这里照抄，方便和抓包逐字对拍。
# 实测顺序本身不影响服务端校验（换过顺序照样 200），但对齐了排查时少一个变量。
ORDER_PC_SEARCH = ("appid", "t", "client", "clientVersion", "cthr", "uuid",
                   "loginType", "keyword", "functionId", "body",
                   "x-api-eid-token", "h5st")
ORDER_PC_ITEM = ("functionId", "body", "h5st", "uuid", "loginType", "appid",
                 "clientVersion", "client", "t", "x-api-eid-token", "scval")
ORDER_PC_ITEM_RELWORDS = ("appid", "functionId", "client", "clientVersion",
                           "uuid", "skuid", "num", "rettype", "type_name",
                           "body")
ORDER_PC_API = ("functionId", "appid", "loginType", "x-api-eid-token", "h5st",
                "t", "client", "clientVersion", "body")

# 商品页另一个 axios 调用层（商家关注状态）实际顺序与普通 item 接口不同。
ORDER_PC_ITEM_VENDER_FOLLOW = (
    "appid", "t", "uuid", "functionId", "body", "x-api-eid-token",
    "h5st", "client", "clientVersion", "loginType", "build",
)

# pctradesoa_diviner 的分页请求由另一段前端代码发起，query 顺序和根路径
# 的首个请求不同；两套都保留，调用方按实抓请求选择。
ORDER_PC_ITEM_DIVINER_PAGE = (
    "appid", "t", "client", "clientVersion", "uuid", "loginType",
    "functionId", "body", "x-api-eid-token", "h5st",
)

# 订单中心的补充接口没有 h5st，也没有 x-api-eid-token；body 全部留在 query，
# POST 只是空体 POST。uuid 是订单页运行时生成的复合值，不能拿 __jdu 猜。
ORDER_PC_ORDER = ("functionId", "appid", "client", "clientVersion", "uuid",
                  "loginType", "t", "body")

# 不需要 h5st 的搜索侧接口（hotwords / aiPicTagInfo / getUmcEquity 等）。
# 浏览器实抓（2026-08-16 reqid=38/39/201/202）：
#   appid t client clientVersion uuid  functionId body
# 注意 loginType 在这类接口里**不发**（逐字取自抓包，和 ORDER_PC_SEARCH 不同）。
ORDER_PC_SEARCH_PLAIN = ("appid", "functionId", "client", "clientVersion",
                         "uuid", "body", "t")

# pc_search_relwords 多了 keyword/num/rettype/type_name（这些在 extra_params 里，
# reorder 会把不认识的 key 按原顺序垫在后面，这里列全是为了和抓包完全对齐）。
# 浏览器实抓 reqid=38 参数顺序：
#   appid functionId client clientVersion uuid keyword num rettype type_name body t
ORDER_PC_SEARCH_RELWORDS = ("appid", "functionId", "client", "clientVersion",
                            "uuid", "keyword", "num", "rettype", "type_name",
                            "body", "t")

# `x-rp-client` 的取值按页面分：搜索页 axios 报 h5_2.1.0，咚咚报 h5_1.0.0。
# 只有走 axios 那一档的接口才带这个头（见 builder/header.py 的分档说明）。
RP_CLIENT_CHAT = "h5_1.0.0"
RP_CLIENT_SEARCH = "h5_2.1.0"
RP_CLIENT_ITEM = "h5_2.2.0"

# 咚咚接口的 query 顺序不是统一的一套：接口由两个前端调用层发起，
# 带时间戳的请求把 client/appid/t 放在 functionId 后面；不带 t 的
# 请求则是 functionId/appid/client/...。这些顺序来自同一页的 Chrome
# Network 抓包（2026-08-31），不是按代码结构推出来的。
ORDER_DD_WITH_TIME = ("functionId", "client", "appid", "t", "clientVersion",
                      "loginType", "h5st", "x-api-eid-token")
ORDER_DD_NO_TIME = ("functionId", "appid", "client", "clientVersion", "loginType",
                    "h5st", "x-api-eid-token")


def sha256_hex(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def trans_cookies(cookie_str: str) -> dict:
    """把浏览器复制出来的 Cookie 头解析成 dict。"""
    cookies = {}
    if not cookie_str:
        return cookies
    for item in cookie_str.split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        key, value = item.split("=", 1)
        cookies[key.strip()] = value.strip()
    return cookies


def cookies_to_str(cookies: dict) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def parse_jsonp(text: str):
    """解开 jQuery jsonp 包装：`jQuery123({...})` → dict。"""
    if text is None:
        return None
    text = text.strip()
    match = re.match(r"^[^(]*\((.*)\)[;\s]*$", text, re.S)
    payload = match.group(1) if match else text
    try:
        return json.loads(payload)
    except (ValueError, TypeError):
        return None


def now_ms() -> int:
    return int(time.time() * 1000)


def strip_tags(text) -> str:
    """去掉搜索结果里的关键词高亮标签（`<font class="skcolor_ljg">…</font>`）。"""
    if not text:
        return ""
    return re.sub(r"<[^>]+>", "", str(text))


def random_jquery_callback() -> str:
    """仿浏览器的 jQuery jsonp 回调名（实抓形如 jQuery2905208）。"""
    return f"jQuery{random.randint(1000000, 9999999)}"


def random_str(length: int,
               alphabet: str = string.ascii_lowercase + string.digits) -> str:
    return "".join(random.choice(alphabet) for _ in range(length))


def generate_wid() -> str:
    """咚咚 WS query 的 `_wid_`。

    还原自 bundle 模块 4e29 的 getUUID（1ecb 在模块加载时调一次并缓存）：
        e = () => Math.floor(65536*(1+Math.random())).toString(16).substring(1)
        e()+e()+"-"+e()+"-"+e()+"-"+e()+"-"+(e()+e()+e())
    就是 8-4-4-4-12 的随机十六进制，无版本位、无设备绑定。
    """
    def seg():
        return f"{random.randint(0x10000, 0x1ffff):x}"[1:]

    return f"{seg()}{seg()}-{seg()}-{seg()}-{seg()}-{seg()}{seg()}{seg()}"


def get_session_id(pin: str, app: str, vender_id: str) -> str:
    """咚咚会话 ID，还原自 bundle 模块 4e29 的 getSessionId。"""
    return f"{pin.lower()}:{app}:{vender_id}"


def splice_url(params: dict) -> str:
    return "&".join(f"{k}={v}" for k, v in params.items())
