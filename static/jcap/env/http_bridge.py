"""Chrome-like transport bridge for the local JCAP JavaScript runtime.

The request and response are exchanged through private process pipes.  Nothing
is logged, so cookies and challenge tickets never reach the terminal.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from utils import http_client


def main() -> int:
    request = json.load(sys.stdin)
    method = str(request.get("method") or "GET").upper()
    body = request.get("body")
    response = http_client._send(
        method,
        str(request["url"]),
        headers=dict(request.get("headers") or {}),
        data=None if method == "GET" else str(body or ""),
        timeout=20,
    )
    raw_body = bytes(response.content or b"")
    try:
        response_text = raw_body.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        # JCAP still has endpoints returning JSON whose message text is GBK,
        # despite omitting a charset from Content-Type.
        response_text = raw_body.decode("gb18030", errors="replace")
    headers = list(response.headers.items())
    get_list = getattr(response.headers, "get_list", None)
    if callable(get_list):
        set_cookies = list(get_list("set-cookie"))
    else:
        value = response.headers.get("set-cookie")
        set_cookies = [value] if value else []
    json.dump(
        {
            "status": int(response.status_code),
            "statusText": str(getattr(response, "reason", "") or ""),
            "url": str(response.url),
            "headers": headers,
            # Returned through the private pipe only. The Node runtime keeps
            # these values in memory so credentialed JCAP XHRs share state.
            "setCookies": set_cookies,
            "body": response_text,
        },
        sys.stdout,
        # The bridge is decoded as UTF-8 by Node. ASCII escaping keeps this
        # pipe independent of the Windows console code page.
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
