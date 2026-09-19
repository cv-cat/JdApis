# coding: utf-8
"""浏览器指纹 profile：全项目共用一套 UA / sec-ch-ua / 屏幕参数。

对齐 ../DouYin_Spider/utils/fingerprint.py 的用法（`get_profile()["ua"]`）。
指纹必须全链路一致：登录时用哪套 UA，后续商品和 WS 就得用同一套，
否则京东风控会因为「同一会话换了浏览器」而降权。
"""

_PROFILE = {
    "profile_id": "chrome152-win32-rtx5060ti-hc32-v2",
    "ua": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
    ),
    # Exact value observed in the current Chrome 152 Network panel.
    "sec_ch_ua": '"Chromium";v="152", "Not?A_Brand";v="24", "Google Chrome";v="152"',
    "sec_ch_ua_mobile": "?0",
    "sec_ch_ua_platform": '"Windows"',
    "browser_name": "Chrome",
    "browser_version": "152.0.0.0",
    "screen_width": "2560",
    "screen_height": "1440",
    "os_name": "Windows",
    "os_version": "10",
    "browser_language": "zh-CN",
    "browser_platform": "Win32",
}


def get_profile():
    return _PROFILE
