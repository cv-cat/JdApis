# coding: utf-8
"""Run JD's public SummerCryptico payload encryption entirely in-process/Node."""

import base64
import json
import os
import re
import subprocess
import threading

from utils import http_client


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RUNNER = os.path.join(_ROOT, "static", "summer_cryptico_runner.js")
_LIB_URL = (
    "https://jrsecstatic.jdpay.com/jr-sec-dev-static/"
    "summer-cryptico-h5.min.js"
)
_source = ""
_lock = threading.Lock()


def _load_source() -> str:
    global _source
    with _lock:
        if _source:
            return _source
        response = http_client.get(_LIB_URL, verify=False, timeout=20)
        source = response.text or ""
        if response.status_code != 200 or "SummerCryptico" not in source:
            raise RuntimeError(
                f"SummerCryptico 官方脚本加载失败 HTTP {response.status_code}"
            )
        _source = source
        return _source


def encrypt(public_key: str, plaintext: str) -> str:
    """Encrypt one value with the server-provided GMPK; values stay in pipes."""
    key = str(public_key or "")
    value = str(plaintext or "")
    if not key or not value:
        raise ValueError("SummerCryptico 公钥和明文不能为空")
    try:
        decoded = base64.b64decode(key, validate=True)
    except ValueError as exc:
        raise ValueError("SummerCryptico 公钥格式异常") from exc
    if len(decoded) < 65:
        raise ValueError("SummerCryptico 公钥长度异常")

    request = json.dumps({
        "source": _load_source(),
        "publicKey": key,
        "plaintext": value,
    }, ensure_ascii=False)
    process = subprocess.run(
        ["node", _RUNNER], input=request, text=True,
        capture_output=True, timeout=20, cwd=_ROOT,
    )
    try:
        result = json.loads(process.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("SummerCryptico 本地运行结果异常") from exc
    encrypted = str(result.get("encrypted") or "")
    if (process.returncode != 0 or not result.get("ok")
            or not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", encrypted)):
        raise RuntimeError(
            f"SummerCryptico 本地加密失败：{result.get('error') or 'EMPTY'}"
        )
    return encrypted
