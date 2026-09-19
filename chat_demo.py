# coding: utf-8
"""京东咚咚 WebSocket Demo。"""

import threading
import time

from loguru import logger

from jd_apis.jd_api import JdAPI
from jd_apis.jd_chat_ws import JdChatWS
from utils.common_util import COOKIE_FILE, init


VENDER_ID = "1"
SKU_ID = ""
MESSAGE = ""  # 填入文本后，会在连接成功后发送。


def main():
    auth, _ = init()
    if not auth.is_login:
        raise SystemExit("请先运行 python login_demo.py")

    JdAPI.get_chat_info(auth, vender_id=VENDER_ID, pid=SKU_ID)
    auth.save(COOKIE_FILE)
    ws = JdChatWS(
        auth,
        vender_id=VENDER_ID,
        vender_app=JdAPI.get_vender_app(auth, VENDER_ID),
    )

    if MESSAGE:
        def send_later():
            time.sleep(2)
            ws.send_text(MESSAGE, pid=SKU_ID)
            logger.success("消息已发送")

        threading.Thread(target=send_later, daemon=True).start()

    ws.start()


if __name__ == "__main__":
    main()
