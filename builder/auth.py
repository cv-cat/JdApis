# coding: utf-8
"""京东鉴权对象。

一个 JdAuth 实例 = 一个登录会话，贯穿登录、商品、咚咚 WS 三层。
"""

import json
import os
import sys
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from urllib.parse import unquote, urlsplit

from utils.jd_util import cookies_to_str, trans_cookies


def default_auth_path():
    """Return a per-user auth path outside the source checkout."""
    override = os.getenv("JDAPIS_AUTH_FILE")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        base = Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.getenv("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "JdApis" / "auth.json"


class JdAuth:
    def __init__(self):
        self.cookie = {}
        self.cookie_str = ""
        self.local_storage = {}
        self._storage_path = default_auth_path()
        self._dirty = False
        # 咚咚 WS 建连四件套。aid 由 getAidInfo 下发；appId/clientType 是前端常量，
        # 京东咚咚网页端当前固定使用 im.customer / comet。
        self.aid = None
        self.app_id = "im.customer"
        self.dvc = None
        self.client_type = "comet"
        # pc-tk 设备票据对应的本地画像版本。画像升级后只换一次设备票据，
        # 避免旧票据与新 h5st/WebM/JCAP 画像互相矛盾。
        self.device_profile = None

    # ---------- 登录态 ----------

    def prepare_auth(self, cookie_str: str = ""):
        cookies = trans_cookies(cookie_str)
        if cookies != self.cookie:
            self._dirty = True
        self.cookie = cookies
        self.cookie_str = cookies_to_str(self.cookie)
        return self

    def update_cookies(self, cookies, *, persist=False):
        """合并 Cookie；空值按服务端删除指令处理。

        ``persist=True`` 只在内容确实变化时写盘。普通业务响应应优先调用
        :meth:`absorb_response`，它还会按顺序吸收重定向链上的 Set-Cookie。
        """
        if not cookies:
            if persist:
                self.flush()
            return self
        if hasattr(cookies, "get_dict"):
            cookies = cookies.get_dict()
        try:
            items = cookies.items()
        except AttributeError as exc:
            raise TypeError("cookies 必须是映射或 CookieJar") from exc
        changed = False
        for key, value in items:
            key = str(key)
            if value in (None, ""):
                if key in self.cookie:
                    del self.cookie[key]
                    changed = True
                continue
            value = str(value)
            if self.cookie.get(key) != value:
                self.cookie[key] = value
                changed = True
        if changed:
            self.cookie_str = cookies_to_str(self.cookie)
            self._dirty = True
        if persist:
            self.flush()
        return self

    def absorb_response(self, response, *, session=None, persist=True):
        """吸收响应、重定向历史及可选 Session 中的全部 Cookie。

        CookieJar 通常不会保留 ``name=; Max-Age=0`` 这种删除指令，因此还
        会直接解析每一跳的 Set-Cookie，再按网络发生顺序覆盖本地会话。
        Session CookieJar 代表整条请求链结束后的最终状态，必须最后合并；
        否则中间重定向里的同名旧值可能反向覆盖最终 Cookie。
        """
        if response is not None:
            for item in list(getattr(response, "history", None) or []) + [response]:
                for raw in self._set_cookie_headers(item):
                    parsed = SimpleCookie()
                    try:
                        parsed.load(raw)
                    except (CookieError, TypeError, ValueError):
                        continue
                    self.update_cookies({key: morsel.value
                                         for key, morsel in parsed.items()})
                self.update_cookies(getattr(item, "cookies", None))
        if session is not None:
            self.update_cookies(getattr(session, "cookies", None))
        if persist:
            self.flush()
        return self

    @staticmethod
    def _set_cookie_headers(response):
        """兼容 curl_cffi 与 requests，返回未合并的 Set-Cookie 行。"""
        headers = getattr(response, "headers", None)
        if headers is None:
            return []
        for method_name in ("get_list", "getlist", "get_all"):
            method = getattr(headers, method_name, None)
            if callable(method):
                try:
                    values = method("set-cookie")
                except (KeyError, TypeError):
                    values = None
                if values:
                    return [str(value) for value in values]
        raw_headers = getattr(getattr(response, "raw", None), "headers", None)
        method = getattr(raw_headers, "getlist", None)
        if callable(method):
            values = method("set-cookie")
            if values:
                return [str(value) for value in values]
        value = headers.get("set-cookie")
        return [str(value)] if value else []

    def flush(self):
        """把尚未持久化的会话变化原子写入当前 auth 文件。"""
        if self._dirty:
            self.save()
        return self

    @staticmethod
    def _storage_origin(page_url):
        parsed = urlsplit(str(page_url or ""))
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
        return str(page_url or "")

    def local_storage_for(self, page_url):
        """返回某一页面 Origin 的持久化 localStorage 快照。"""
        origin = self._storage_origin(page_url)
        return dict(self.local_storage.get(origin) or {})

    def replace_local_storage(self, page_url, values, *, persist=False):
        """用运行结束时的完整快照更新某一 Origin 的 localStorage。"""
        origin = self._storage_origin(page_url)
        if not origin or not isinstance(values, dict):
            if persist:
                self.flush()
            return self
        snapshot = {str(key): str(value) for key, value in values.items()
                    if value is not None}
        if self.local_storage.get(origin) != snapshot:
            self.local_storage[origin] = snapshot
            self._dirty = True
        if persist:
            self.flush()
        return self

    @property
    def thor(self):
        """PC 端主登录票据。实抓确认：passport 扫码下发的是 thor，不是 pt_key。"""
        return self.cookie.get("thor")

    @property
    def pt_key(self):
        """M 端（m.jd.com）登录票据。PC 扫码链路不产生它。"""
        return self.cookie.get("pt_key")

    @property
    def pin(self):
        """登录账号名，同时也是咚咚 WS 的 `pin` 参数。Cookie 里是 URL 编码的。"""
        raw = self.cookie.get("pin") or self.cookie.get("pt_pin") or self.cookie.get("_pst")
        return unquote(raw) if raw else None

    @property
    def is_login(self):
        return bool((self.thor or self.pt_key) and self.pin)

    # ---------- 咚咚会话 ----------

    def set_chat_info(self, aid=None, app_id=None, dvc=None, client_type=None,
                      *, persist=False):
        values = {
            "aid": aid, "app_id": app_id,
            "dvc": dvc, "client_type": client_type,
        }
        for key, value in values.items():
            if value and getattr(self, key) != value:
                setattr(self, key, value)
                self._dirty = True
        if persist:
            self.flush()
        return self

    def set_device_profile(self, value, *, persist=False):
        if value and self.device_profile != str(value):
            self.device_profile = str(value)
            self._dirty = True
        if persist:
            self.flush()
        return self

    # ---------- 持久化 ----------

    def save(self, path=None):
        path = Path(path) if path else self._storage_path
        self._storage_path = path.expanduser().resolve()
        path = self._storage_path
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "cookie": self.cookie,
            "local_storage": self.local_storage,
            "aid": self.aid,
            "app_id": self.app_id,
            "dvc": self.dvc,
            "client_type": self.client_type,
            "device_profile": self.device_profile,
        }
        temp_path = path.with_suffix(path.suffix + ".tmp")
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        try:
            os.chmod(temp_path, 0o600)
        except OSError:
            pass
        os.replace(temp_path, path)
        self._dirty = False
        return str(path)

    @classmethod
    def load(cls, path=None):
        path = (Path(path) if path else default_auth_path()).expanduser().resolve()
        auth = cls()
        auth._storage_path = path
        if not path.exists():
            return auth
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        auth.update_cookies(data.get("cookie", {}))
        local_storage = data.get("local_storage")
        if isinstance(local_storage, dict):
            auth.local_storage = {
                str(origin): {
                    str(key): str(value)
                    for key, value in values.items()
                    if value is not None
                }
                for origin, values in local_storage.items()
                if isinstance(values, dict)
            }
        auth.set_chat_info(
            aid=data.get("aid"), app_id=data.get("app_id"),
            dvc=data.get("dvc"), client_type=data.get("client_type"),
        )
        auth.set_device_profile(data.get("device_profile"))
        auth._dirty = False
        return auth
