# coding: utf-8
"""环境与会话初始化。对齐 ../DouYin_Spider/utils/common_util.py 的 load_env / init。"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from builder.auth import JdAuth, default_auth_path

COOKIE_FILE = default_auth_path()
LEGACY_COOKIE_FILE = Path(__file__).resolve().parents[1] / "jd_cookies.json"


def use_utf8_stdout():
    """Windows 控制台默认 GBK，打印商品名里的 ¥ / 生僻字会直接抛
    UnicodeEncodeError。这里把 stdout/stderr 换成 UTF-8 并对漏网字符降级替换。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def load_env():
    """会话优先级：用户私有目录 auth.json > 旧版文件 > 环境变量。"""
    load_dotenv()
    auth = JdAuth.load(COOKIE_FILE)
    if not auth.is_login and LEGACY_COOKIE_FILE.is_file():
        auth = JdAuth.load(LEGACY_COOKIE_FILE)
        if auth.is_login:
            auth.save(COOKIE_FILE)
    if not auth.is_login:
        cookie_str = os.getenv("JD_COOKIES", "")
        if cookie_str:
            auth.prepare_auth(cookie_str)
    return auth


def init():
    use_utf8_stdout()
    data_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "datas"))
    os.makedirs(data_path, exist_ok=True)
    return load_env(), {"data": data_path}
