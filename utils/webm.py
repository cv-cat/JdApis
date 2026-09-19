# coding: utf-8
"""京东 PC WebM 指纹上报的纯程序运行桥。"""

import json
from pathlib import Path
import re
import shutil
import subprocess

from utils.fingerprint import get_profile


_ROOT = Path(__file__).resolve().parents[1]
_RUNNER = _ROOT / "static" / "webm" / "env" / "run.js"
_BUNDLE = _ROOT / "static" / "webm" / "run" / "jdwebm-riskhandle.js"
_RESULT_PREFIX = "__WEBM_RESULT__"


def _require_runtime():
    node = shutil.which("node")
    if not node:
        raise RuntimeError("纯程序 WebM 指纹需要 Node.js 20+")
    for path in (_RUNNER, _BUNDLE):
        if not path.is_file():
            raise RuntimeError(f"纯程序 WebM 指纹缺少运行资源：{path.name}")
    return node


def build_search_payload(auth, page_url: str, config_data: str = "",
                         timeout: int = 30, local_storage=None) -> dict:
    """运行原版 jdwebm.js，返回完整的 ``wsgw_getinfo`` body。"""
    profile = get_profile()
    runtime_input = {
        "pageUrl": str(page_url),
        "userAgent": profile["ua"],
        "cookies": {
            str(key): str(value)
            for key, value in auth.cookie.items()
            if value not in (None, "")
        },
        "localStorage": (auth.local_storage_for(page_url)
                         if local_storage is None else dict(local_storage)),
        "configData": str(config_data or ""),
    }
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    completed = subprocess.run(
        [_require_runtime(), str(_RUNNER)],
        cwd=str(_RUNNER.parent),
        input=json.dumps(runtime_input, ensure_ascii=False),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=max(10, int(timeout)),
        check=False,
        creationflags=creationflags,
    )
    marker = next(
        (line[len(_RESULT_PREFIX):]
         for line in reversed((completed.stdout or "").splitlines())
         if line.startswith(_RESULT_PREFIX)),
        "",
    )
    if not marker:
        raise RuntimeError("纯程序 WebM 指纹进程异常：无结果标记")
    try:
        result = json.loads(marker)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("纯程序 WebM 指纹返回格式异常") from exc
    if completed.returncode != 0 or not result.get("ok"):
        detail = str(result.get("errorMessage") or result.get("error") or "")
        raise RuntimeError(f"纯程序 WebM 指纹生成失败：{detail[:240]}")

    cookies = result.get("cookies")
    if isinstance(cookies, dict):
        auth.update_cookies({
            str(key): str(value or "")
            for key, value in cookies.items()
            if re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", str(key))
        })
    storage = result.get("localStorage")
    if isinstance(storage, dict):
        auth.replace_local_storage(page_url, storage)
    auth.flush()

    payload = result.get("payload")
    if not isinstance(payload, dict) or not isinstance(payload.get("body"), dict):
        raise RuntimeError("纯程序 WebM 指纹缺少上报正文")
    return payload
