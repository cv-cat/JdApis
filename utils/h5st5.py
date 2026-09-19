# coding: utf-8
"""h5st 5.3 签名（京东现网版本）。

`static/h5st5_server.js` = 浏览器环境引导 + 现网 `js_security_v3_0.1.6.js` 原文
+ 行分隔 JSON-RPC 循环。

两个关键点：
1. **token 由服务端签发**，不是本地算的。库会 POST `cactus.jd.com/request_algo`，
   服务端回 `tk` 和一段含随机盐 `rd` 的 algo 函数，
   签名 = `SHA256(tk + fp + ts + appId + rd)`。
   `tk03` 前缀 = 服务端真 token；`tk04`/`tk06` = 库的本地兜底值（业务接口不认）。
   所以环境引导里必须提供一个能到真网、且带 JD cookie 的 XMLHttpRequest。
2. **真实 sign() 是异步的**，execjs 拿不到 Promise 结果，所以走常驻 Node 进程：
   库有 238KB，冷启动 1~2s，签名本身十几毫秒，常驻可以把这个开销摊掉。
"""

import atexit
import json
import os
import subprocess
import threading
import time

from utils.fingerprint import get_profile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SERVER_JS = os.path.join(_ROOT, "static", "h5st5_server.js")
# 服务端发的 token 有 24h 有效期，落盘复用，别每次冷启动都去 cactus 重换。
_TK_CACHE = os.path.join(_ROOT, "datas", "h5st_token_cache.json")
# 画像变化后不能继续复用按旧画像签发的 fp/token。缓存中带上画像版本，
# 版本不匹配时只忽略旧缓存，不删除用户的任何文件。
_TK_PROFILE = get_profile()["profile_id"]

_proc = None
_lock = threading.Lock()
_counter = 0
_cookie = ""
_origin = "https://search.jd.com"
_referer = "https://search.jd.com/"


def configure(cookie_str="", origin=None, referer=None):
    """设置签名进程取 token 时用的 cookie/来源。

    只有 cookie 变化才重启进程：origin/referer 仅影响那一次取 token 的请求头，
    而 token 一旦拿到就常驻缓存；跟着每个业务请求重启会让进程永远停在
    「还没换到服务端 token」的状态。
    """
    global _cookie, _origin, _referer
    changed = cookie_str != _cookie
    _cookie = cookie_str or ""
    if origin and _proc is None:
        _origin = origin
    if referer and _proc is None:
        _referer = referer
    if changed:
        shutdown()


def _ensure_proc():
    global _proc
    if _proc is not None and _proc.poll() is None:
        return _proc
    env = dict(os.environ)
    env["JD_COOKIE"] = _cookie
    env["JD_ORIGIN"] = _origin
    env["JD_REFERER"] = _referer
    env["JD_TK_PROFILE"] = _TK_PROFILE
    os.makedirs(os.path.dirname(_TK_CACHE), exist_ok=True)
    env.setdefault("JD_TK_CACHE", _TK_CACHE)
    _proc = subprocess.Popen(
        ["node", _SERVER_JS],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        encoding="utf-8", bufsize=1, env=env,
        cwd=os.path.dirname(os.path.dirname(_SERVER_JS)),
    )
    ready = _proc.stdout.readline()
    if not ready or '"ready"' not in ready:
        raise RuntimeError(f"h5st5 签名进程启动失败：{ready!r}")
    atexit.register(shutdown)
    return _proc


def shutdown():
    global _proc
    if _proc is not None:
        try:
            _proc.terminate()
        except Exception:
            pass
        _proc = None


def _is_real_token(h5st: str) -> bool:
    """服务端下发的 token 是 tk03 前缀；tk04/tk06 是库的本地兜底值，服务端不认。"""
    segs = (h5st or "").split(";")
    return len(segs) > 3 and segs[3].startswith("tk03")


def sign(params: dict, app_id: str = "f06cc", warmup: bool = True) -> dict:
    """返回 {'h5st':…, '_stk':…, '_ste':…, …}；失败抛 RuntimeError。

    取 token 是异步的，冷启动后的第一次签名往往还拿不到服务端 token。
    warmup=True 时会短暂等待并重签，确保拿到 tk03。
    """
    res = _sign_once(params, app_id)
    if warmup and not _is_real_token(res.get("h5st", "")):
        for delay in (0.35, 0.6, 1.0):
            time.sleep(delay)
            res = _sign_once(params, app_id)
            if _is_real_token(res.get("h5st", "")):
                break
    return res


def _sign_once(params: dict, app_id: str) -> dict:
    global _counter
    with _lock:
        proc = _ensure_proc()
        _counter += 1
        req = json.dumps({"id": _counter, "params": params, "appId": app_id},
                         ensure_ascii=False)
        proc.stdin.write(req + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
    if not line:
        shutdown()
        raise RuntimeError("h5st5 签名进程无响应")
    resp = json.loads(line)
    if not resp.get("ok"):
        raise RuntimeError(f"h5st5 签名失败：{resp.get('error')}")
    return resp.get("result") or {}


def generate_h5st(params: dict, app_id: str = "f06cc") -> str:
    return sign(params, app_id).get("h5st", "")
