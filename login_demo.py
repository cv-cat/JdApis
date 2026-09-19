# coding: utf-8
"""京东纯程序登录 Demo：直接运行，不使用命令行参数解析。"""

import getpass

from loguru import logger

from builder.auth import JdAuth
from jd_apis.jd_api import JdAPI
from jd_apis.jd_login_api import JdLoginAPI
from jd_apis.jd_sms_login_api import JdSmsLoginAPI
from utils.common_util import COOKIE_FILE, use_utf8_stdout
from utils.jd_cookie import bootstrap


# 可改为 "qr" 使用京东 App 扫码登录。
LOGIN_MODE = "sms"


def show_status(_stage, message):
    logger.info(message)


def main():
    use_utf8_stdout()
    auth = bootstrap(JdAuth(), site="passport")

    if LOGIN_MODE == "qr":
        success, message, auth = JdLoginAPI.qr_login(auth)
    elif LOGIN_MODE == "sms":
        mobile = getpass.getpass("手机号: ").strip()
        success, message, auth = JdSmsLoginAPI.login(
            auth,
            mobile,
            code_provider=getpass.getpass,
            safe_code_provider=getpass.getpass,
            on_status=show_status,
        )
    else:
        raise ValueError('LOGIN_MODE 只能是 "sms" 或 "qr"')

    if not success:
        raise SystemExit(message)

    auth.save(COOKIE_FILE)
    alive, account = JdAPI.check_session(auth)
    if not alive:
        raise SystemExit(f"Cookie 已保存，但登录态校验失败：{account}")
    logger.success(f"登录成功：{account}；会话已保存到 {COOKIE_FILE}")


if __name__ == "__main__":
    main()
