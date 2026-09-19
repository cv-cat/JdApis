# coding: utf-8
"""京东 PC ``getJsToken/getJdEid`` 的本地 Node 运行时。"""

import atexit
import json
import os
import re
import subprocess
import threading

from utils.fingerprint import get_profile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SERVER_JS = os.path.join(_ROOT, "static", "pc_tk_server.js")
DEVICE_PROFILE = get_profile()["profile_id"]
_IDENTITY_COOKIE_NAMES = {
    "3AB9D23F7A4B3C9B", "3AB9D23F7A4B3CSS",
    "PCA9D23F7A4B3CSS", "PCTSD23F7A4B3CSS",
}
_proc = None
_lock = threading.Lock()
_counter = 0
_cookie = ""
_DEFAULT_PAGE_URL = (
    "https://passport.jd.com/new/login.aspx?"
    "ReturnUrl=https://home.jd.com/index.html"
)
_page_url = _DEFAULT_PAGE_URL
_origin = "https://passport.jd.com"
_referer = _DEFAULT_PAGE_URL


def configure(cookie_str="", page_url=None, origin=None, referer=None):
    """Bind cookies and document scope; rebuild when either scope changes."""
    global _cookie, _page_url, _origin, _referer
    value = cookie_str or ""
    next_page = page_url or _DEFAULT_PAGE_URL
    next_origin = origin or "https://passport.jd.com"
    next_referer = referer or next_page
    if ((value, next_page, next_origin, next_referer)
            != (_cookie, _page_url, _origin, _referer)):
        _cookie, _page_url = value, next_page
        _origin, _referer = next_origin, next_referer
        shutdown()


def _ensure_proc():
    global _proc
    if _proc is not None and _proc.poll() is None:
        return _proc
    env = dict(os.environ)
    env.update({
        "JD_COOKIE": _cookie,
        "JD_PAGE_URL": _page_url,
        "JD_ORIGIN": _origin,
        "JD_REFERER": _referer,
    })
    _proc = subprocess.Popen(
        ["node", _SERVER_JS], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, encoding="utf-8", bufsize=1, env=env,
        cwd=_ROOT,
    )
    ready = _proc.stdout.readline()
    if not ready or '"ready"' not in ready:
        shutdown()
        raise RuntimeError(f"PC 设备参数进程启动失败：{ready!r}")
    atexit.register(shutdown)
    return _proc


def shutdown():
    global _proc
    if _proc is not None:
        try:
            _proc.terminate()
        except OSError:
            pass
        _proc = None


def _validate(result):
    fields = {key: str((result or {}).get(key) or "")
              for key in ("eid", "eid2", "fp", "giaD")}
    if not re.fullmatch(r"[A-Za-z0-9._-]{32,256}", fields["eid"]):
        raise RuntimeError("PC 设备参数 eid 格式异常")
    if not (fields["eid2"].startswith("jdd03")
            and fields["eid2"].endswith("X")
            and 64 <= len(fields["eid2"]) <= 512):
        raise RuntimeError("PC 设备参数 eid2/jsToken 格式异常")
    # pc-tk.js 无缓存时用 randomStr(32)，字符表同时含大小写字母与数字；
    # canvas hash 路径才恰好是小写 hex，不能把后者误当成唯一格式。
    if not re.fullmatch(r"[A-Za-z0-9]{32}", fields["fp"]):
        raise RuntimeError("PC 设备参数 fp 格式异常")
    if fields["giaD"] and not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", fields["giaD"]):
        raise RuntimeError("PC 设备参数 _gia_d 格式异常")
    return fields


def _without_old_identity(cookie_str):
    """仅在主动换设备票据时去掉旧票据，普通请求仍发送全部 Cookie。"""
    kept = []
    for item in str(cookie_str or "").split(";"):
        item = item.strip()
        if not item:
            continue
        name = item.split("=", 1)[0].strip()
        if name not in _IDENTITY_COOKIE_NAMES:
            kept.append(item)
    return "; ".join(kept)


def get_device_fields(*, force_refresh=False):
    """返回 ``eid/eid2/fp``；值只经本机进程管道传递，不写日志。

    ``force_refresh`` 仅用于画像版本升级：在临时 Node 进程中不注入旧设备
    票据，成功后由调用方原子更新 auth；它不限制业务请求发送的 Cookie。
    """
    global _counter, _cookie
    with _lock:
        original_cookie = _cookie
        if force_refresh:
            shutdown()
            _cookie = _without_old_identity(original_cookie)
        try:
            proc = _ensure_proc()
            _counter += 1
            proc.stdin.write(json.dumps({"id": _counter}) + "\n")
            proc.stdin.flush()
            line = proc.stdout.readline()
        finally:
            if force_refresh:
                shutdown()
                _cookie = original_cookie
    if not line:
        shutdown()
        raise RuntimeError("PC 设备参数进程无响应")
    response = json.loads(line)
    if not response.get("ok"):
        raise RuntimeError(f"PC 设备参数生成失败：{response.get('error')}")
    return _validate(response.get("result"))
