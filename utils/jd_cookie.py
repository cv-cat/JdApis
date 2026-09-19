# coding: utf-8
"""Cookie 冷启动。

实测：用干净客户端访问 `www.jd.com` / `search.jd.com` / `item.jd.com`，
**一个 Set-Cookie 都拿不到**——京东的埋点 cookie 全部由页面 JS 在本地生成。
所以这里按实抓格式纯算复现，不需要跑 JS。

三类 cookie 的来源分别是：

| 类别 | 例子 | 来源 | 本模块 |
|---|---|---|---|
| JS 本地生成 | `__jdu` `__jda` `__jdb` `__jdc` `__jdv` | `jdf/base.js` 埋点脚本 | ✅ 已复现 |
| 服务端下发（需风控 JS 采集） | `3AB9D23F7A4B3C9B`(eid) `3AB9D23F7A4B3CSS`(jsToken) | `gia.jd.com` + `POST jra.jd.com/jsTk.do` | 登录流程自动生成 |
| 登录下发 | `thor` `pin` `pinId` `light_key` … | `qrCodeTicketValidation` | 走扫码链路 |
| 会话中轮换 | `sdtoken` | 响应头 `x-rp-sdtoken` | JdAPI 自动吸收 |

关于 eid：实测 `jra.jd.com/jsTk.do` 的响应是 `{token, eid, gia_d, ds}`，
其中 `eid` 与 cookie `3AB9D23F7A4B3C9B` 完全相同，而 `token`（即 jsToken /
`3AB9D23F7A4B3CSS`）= `jdd03` + eid + 34 位轮换后缀。也就是说这两个 cookie
同源，拿到 eid 就能推出 jsToken 的形态。
"""

import random

from utils.jd_util import DEFAULT_AREA, now_ms

# 各站点的埋点 siteId（`__jdc` / `__jda` 的第一段），实抓
SITE_IDS = {
    "www": "122270672",
    "search": "143920055",
    "item": "122270672",
    "passport": "95931165",
    "chat": "23334881",
}


def generate_jdu() -> str:
    """`__jdu` = 13 位毫秒时间戳 + 10 位随机数字。

    实抓 `17867757506111057502075` = 1786775750611 + 1057502075。
    """
    return f"{now_ms()}{random.randint(10 ** 9, 10 ** 10 - 1)}"


def build_tracking_cookies(site="search", jdu=None, source="direct",
                           visit_count=1, pv=1) -> dict:
    """生成 `__jdu` / `__jdc` / `__jda` / `__jdb` / `__jdv` 一组埋点 cookie。

    格式取自实抓：
        __jda = <siteId>.<__jdu>.<首访秒>.<上次访秒>.<本次访秒>.<访问次数>
        __jdb = <siteId>.<PV数>.<__jdu>|<访问次数>.<本次访秒>
        __jdv = <siteId>|<来源>|<媒介>|-|-|<毫秒>
    """
    site_id = SITE_IDS.get(site, SITE_IDS["www"])
    jdu = jdu or generate_jdu()
    ms = now_ms()
    sec = ms // 1000
    return {
        "__jdu": jdu,
        "__jdc": site_id,
        "__jda": f"{site_id}.{jdu}.{sec}.{sec}.{sec}.{visit_count}",
        "__jdb": f"{site_id}.{pv}.{jdu}|{visit_count}.{sec}",
        "__jdv": f"{site_id}|{source}|-|direct|-|{ms}",
    }


def bootstrap(auth, site="search", area=None):
    """给一个空 auth 灌上冷启动 cookie，让它能发未登录也能用的接口。

    注意这只解决埋点那一类。eid / jsToken 仍需风控 JS 采集（见模块 docstring），
    登录态则要走扫码。
    """
    # Keep the same four-region value used by current Chrome Network captures;
    # the old ``1-2800-0-0`` fallback is a visibly different request field.
    area = area or DEFAULT_AREA.replace("_", "-")
    cookies = build_tracking_cookies(site=site)
    cookies.update({"areaId": area.split("-")[0], "ipLoc-djd": area})
    # `cn` is not a universal browser cookie.  The current passport QR
    # Network request does not carry it, so do not synthesize an extra field
    # during the login bootstrap.  Callers that captured a real `cn` value
    # keep it through their existing cookie jar instead.
    auth.update_cookies(cookies)
    return auth
