# coding: utf-8
"""无浏览器 JCAP 图形验证码求解入口。

官方 JCAP JavaScript/WASM 在本机 Node 补环境中运行，图片识别由本地 ONNX
模型完成。子进程输出始终留在内存管道中，避免手机号、Cookie 和验证票据进入日志。
"""

import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

from utils.fingerprint import get_profile


_ROOT = Path(__file__).resolve().parents[1]
_ASSET_ROOT = _ROOT / "static" / "jcap"
_RUNNER = _ASSET_ROOT / "env" / "run.js"
_U2NETP_MODEL = _ASSET_ROOT / "run" / "models" / "u2netp.onnx"
_ORIENTATION_MODEL = (
    _ASSET_ROOT / "run" / "models" / "orientation_model_v2_0.9882.onnx"
)
_RESULT_PREFIX = "__JCAP_RESULT__"


def _diagnostic_summary(output: str) -> str:
    """Return only stage metadata from the Node runner; never include tickets."""
    latest = {}
    prefixes = {
        "[captcha.challenge]": ("challenge", ("code", "tp", "imgType", "imgLength", "fields")),
        "[captcha.check]": ("check", ("code", "sCode", "tp", "type", "message", "stLength", "hasVt", "fields")),
        "[captcha.interaction]": (
            "interaction",
            ("targetNatural", "targetDisplay", "sliderLeft", "slotTransform",
             "mainWidth", "slotWidth", "points", "elapsed", "overshoot"),
        ),
        "[captcha.request]": ("request", ("method", "path", "bodyLength", "fields", "headerNames")),
        "[captcha.transport]": (
            "transport",
            ("method", "host", "path", "headerNames", "cookieNames", "bodyLength"),
        ),
        "[captcha.transport.check]": (
            "check_transport",
            ("method", "host", "path", "headerNames", "cookieNames", "bodyLength"),
        ),
        "[captcha.dom]": ("dom", ("root", "elements", "images")),
        "[captcha.solve]": ("solve", ("tp", "solver", "attempt", "retry", "points", "score", "margin", "correlation", "offset", "bias", "reason")),
        "[captcha.solver-process]": ("solver_process", ("status", "error")),
        "[trace.answer]": ("answer", ("keys", "x", "y", "ht", "wt", "bw", "sw", "mw", "ii", "unknown", "list", "track")),
        "[trace.touch]": ("touch", ("type", "keys", "lengths", "sample")),
        "[trace.wasm.events]": (
            "wasm_events", ("count", "types", "properties"),
        ),
        "[trace.stack.safe]": (
            "stack",
            ("jsonLength", "recordNames", "recordEvents", "fileCount",
             "productionFileCount", "nodeFileCount", "localFileCount"),
        ),
        "[trace.deviceInfo.safe]": (
            "device_info",
            (
                "_keys", "bid", "capfp", "cd", "cke", "cpu", "cvs", "dcs", "ets",
                "fts", "fv", "gpu", "jsv", "lan", "lang", "lns", "ls",
                "mem", "ol", "pc", "pdf", "pr", "pt", "scr", "sdf", "sdv",
                "ss", "sts", "tsp", "tzo", "ua", "uat", "vp", "wch", "wdr",
                "wgl", "wlh", "wvr",
            ),
        ),
        "[captcha.cookies]": ("cookies", ("path", "received", "jar")),
        "[env.summary]": (
            "environment",
            ("errorCount", "undefinedCount", "errorPaths", "undefinedPaths"),
        ),
    }
    for line in (output or "").splitlines():
        for prefix, (name, keys) in prefixes.items():
            if not line.startswith(prefix):
                continue
            try:
                payload = json.loads(line[len(prefix):].strip())
            except (TypeError, ValueError):
                continue
            latest[name] = {key: payload.get(key) for key in keys if key in payload}
        for prefix, name in (("[factory]", "factory"), ("[instance]", "instance"),
                             ("[onLoad]", "on_load"), ("[failure]", "failure"),
                             ("[capture.network-error]", "network_error"),
                             ("[load-error]", "load_error")):
            if line.startswith(prefix):
                latest[name] = True
    return json.dumps(latest, ensure_ascii=False, separators=(",", ":"))


def _require_runtime() -> str:
    node = shutil.which("node")
    if not node:
        raise RuntimeError("纯程序图形验证码需要 Node.js 20+")
    for path in (_RUNNER, _U2NETP_MODEL, _ORIENTATION_MODEL):
        if not path.is_file():
            raise RuntimeError(f"纯程序图形验证码缺少运行资源：{path.name}")
    return node


def _parse_result(output: str, cookie_callback=None,
                  storage_callback=None) -> str:
    marker = next(
        (line[len(_RESULT_PREFIX):] for line in reversed(output.splitlines())
         if line.startswith(_RESULT_PREFIX)),
        "",
    )
    if not marker:
        raise RuntimeError("纯程序图形验证码未返回验证结果")
    try:
        payload = json.loads(marker)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("纯程序图形验证码返回格式异常") from exc
    cookie_updates = payload.get("cookies")
    if isinstance(cookie_updates, dict) and callable(cookie_callback):
        cookie_callback({
            str(key): str(value or "")
            for key, value in cookie_updates.items()
            if re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", str(key))
        })
    storage = payload.get("localStorage")
    if isinstance(storage, dict) and callable(storage_callback):
        storage_callback({str(key): str(value)
                          for key, value in storage.items()
                          if value is not None})
    if not payload.get("ok"):
        if payload.get("error") == "timeout":
            raise TimeoutError(f"纯程序图形验证码求解超时：{_diagnostic_summary(output)}")
        raise RuntimeError(f"纯程序图形验证码求解失败：{_diagnostic_summary(output)}")
    ticket = str(payload.get("vt") or "")
    if not 32 <= len(ticket) <= 4096 or any(char.isspace() for char in ticket):
        raise RuntimeError("纯程序图形验证码票据格式异常")
    return ticket


def solve_graphic_captcha(session_id: str, account: str, cookie_str: str,
                          timeout: int = 180, page_url: str = "",
                          dump_dir: str = "", max_solve_attempts: int = 20,
                          slider_biases=None, cookie_callback=None,
                          local_storage=None, storage_callback=None) -> str:
    """完成 JCAP 会话并返回服务端验证票据，全程不启动浏览器。"""
    session_id = str(session_id or "")
    account = str(account or "")
    if not session_id:
        raise ValueError("纯程序图形验证码缺少 sessionId")
    node = _require_runtime()
    timeout = max(30, int(timeout))
    profile = get_profile()
    runtime_input = {
        "sessionId": session_id,
        "account": account,
        "cookie": str(cookie_str or ""),
        "liveNetwork": True,
        "autoSolve": True,
        "returnResult": True,
        "maxSolveAttempts": max(1, min(30, int(max_solve_attempts))),
        "runTimeoutMs": max(30_000, timeout * 1000 - 5_000),
        "pythonExecutable": sys.executable,
        "solverModel": str(_U2NETP_MODEL),
        "orientationModel": str(_ORIENTATION_MODEL),
        "userAgent": profile["ua"],
        "secChUa": profile["sec_ch_ua"],
        "secChUaMobile": profile["sec_ch_ua_mobile"],
        "secChUaPlatform": profile["sec_ch_ua_platform"],
        "localStorage": dict(local_storage or {}),
    }
    if page_url:
        runtime_input["pageUrl"] = str(page_url)
    if dump_dir:
        runtime_input["dumpDir"] = str(dump_dir)
    if slider_biases:
        runtime_input["sliderBiases"] = [float(value) for value in slider_biases]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            [node, str(_RUNNER)],
            cwd=str(_RUNNER.parent),
            input=json.dumps(runtime_input, ensure_ascii=False),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        raise TimeoutError(
            f"纯程序图形验证码求解超时：{_diagnostic_summary(output)}"
        ) from exc
    if completed.returncode != 0:
        raise RuntimeError("纯程序图形验证码进程异常")
    return _parse_result(completed.stdout or "", cookie_callback,
                         storage_callback)
