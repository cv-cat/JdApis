# coding: utf-8
"""京东咚咚 WebSocket 客户端。

协议全部来自对 jdcs.jd.com 前端 bundle 的还原 + 实网验证（2026-08-15）：
  - 建连 wss://ws1-dd.jd.com/?pin=&appId=&aid=&clientType=&_wid_=
  - 帧格式是**纯 JSON**：前端就一句 ws.send(JSON.stringify(e))，无二进制编解码
  - 信封由 packing() 生成，**aid 必须在顶层**，否则服务端回 code:110「账号未登录」
  - onOpen 顺序：先 client_heartbeat，再 cfg.welcome
  - 心跳 30s 一次，且心跳帧**不带 id**

对齐 ../DouYin_Spider/dy_apis/douyin_recv_msg.py 的 WebSocketApp 用法。
"""

import json
import threading
import time
import uuid
from datetime import datetime
from urllib.parse import quote

from loguru import logger
from websocket import WebSocketApp

from builder.header import HeaderBuilder
from utils.jd_util import generate_wid, get_session_id, now_ms, splice_url


class MsgType:
    """咚咚消息类型，取自 bundle 模块 5a50 的常量表。"""
    CHAT_MESSAGE = "chat_message"
    CHAT_MESSAGE_RESULT = "chat_message_result"
    CHAT_SESSION_OPEN = "chat_session_open"
    CHAT_SESSION_CLOSE = "chat_session_close"
    CHAT_CUSTOMER_LEAVE = "chat_customer_leave"
    CLIENT_HEARTBEAT = "client_heartbeat"
    EVENT_MESSAGE = "event_message"
    SYS_MSG = "sys_msg"
    FAILURE = "failure"
    AUTH = "auth"
    AUTH_RESULT = "auth_result"
    ACK = "ack"
    REVOKE_MESSAGE = "revoke_message"
    MSG_READ_ACK = "msg_read_ack"
    MSG_RECEIVE_ACK = "msg_receive_ack"


class JdChatWS:
    HOSTS = ("ws1-dd.jd.com", "ws0-dd.jd.com", "ws3-dd.jd.com")
    ORIGIN = "https://jdcs.jd.com"
    HEARTBEAT_INTERVAL = 30
    CUSTOMER_APP = "im.customer"
    WAITER_APP = "jd.waiter"
    CLIENT_TYPE = "comet"
    VER = "4.2"

    def __init__(self, auth, vender_id="1", vender_app=None, host=None,
                 auto_reconnect=True, on_text=None):
        """:param on_text: 收到文本消息的回调，签名 (sender_pin, text, packet)"""
        self.auth = auth
        self.vender_id = str(vender_id)
        self.vender_app = vender_app or self.WAITER_APP
        self.host = host or self.HOSTS[0]
        self.auto_reconnect = auto_reconnect
        self.on_text = on_text
        self.ws = None
        self._alive = False
        self.url = self._build_url()

    # ---------- 建连 ----------

    def _build_url(self):
        # pin 是账号名，可能含中文，必须 URL 编码后进 query
        params = {
            "pin": quote(self.auth.pin or "", safe=""),
            "appId": self.auth.app_id or self.CUSTOMER_APP,
            "aid": self.auth.aid or "",
            "clientType": self.auth.client_type or self.CLIENT_TYPE,
            "_wid_": generate_wid(),
        }
        return f"wss://{self.host}/?{splice_url(params)}"

    @property
    def session_id(self):
        return get_session_id(self.auth.pin or "", self.CUSTOMER_APP, self.vender_id)

    # ---------- 信封 ----------

    def packing(self, message_type, body=None, to=None, msg_id=None):
        """构造 WS 信封，逐字段对齐前端 packing()。"""
        packet = {
            "from": {
                "app": self.CUSTOMER_APP,
                "pin": self.auth.pin or "",
                "clientType": self.CLIENT_TYPE,
            },
            "datetime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "ver": self.VER,
            "lang": "zh_CN",
            "aid": self.auth.aid or "",
            "type": message_type,
            "to": to or {"app": self.vender_app},
            "timestamp": now_ms(),
        }
        # 心跳是唯一不带 id 的类型
        if message_type != MsgType.CLIENT_HEARTBEAT:
            packet["id"] = msg_id or uuid.uuid4().hex
        if body:
            packet["body"] = body
        return packet

    def send_packet(self, packet):
        if not (self.ws and self._alive):
            logger.warning("WS 未连接，丢弃一帧")
            return False
        self.ws.send(json.dumps(packet, ensure_ascii=False))
        return True

    def _chatinfo(self, pid="", order_id=""):
        info = {"venderId": self.vender_id, "ct": "3", "mt": "51"}
        if pid:
            info["pid"] = pid
        if order_id:
            info["orderId"] = order_id
        return info

    def send_text(self, text, pid="", order_id=""):
        packet = self.packing(MsgType.CHAT_MESSAGE, body={
            "content": text,
            "type": "text",
            "chatinfo": self._chatinfo(pid, order_id),
        })
        logger.info(f"发送 → {text}")
        return self.send_packet(packet)

    def send_hello(self, pid="", order_id=""):
        """会话首帧（前端 sendHello）：type=config + action.code=cfg.welcome。"""
        content = f"顾客{self.auth.pin}发起咨询"
        if pid:
            content += f"（商品编号：{pid}）"
        if order_id:
            content += f"（订单编号：{order_id}）"
        packet = self.packing(MsgType.CHAT_MESSAGE, body={
            "content": content,
            "type": "config",
            "chatinfo": self._chatinfo(pid, order_id),
            "uniformBizInfo": {},
            "action": {"code": "cfg.welcome"},
        })
        return self.send_packet(packet)

    def send_heartbeat(self):
        # 前端 setHeartBeatAnswer 里 fromApp 和 toApp 都是 im.customer，
        # 心跳发给自己这一侧而不是商家；发错 to 会被判 code:111 授权过期。
        return self.send_packet(
            self.packing(MsgType.CLIENT_HEARTBEAT, to={"app": self.CUSTOMER_APP}))

    def _heartbeat_loop(self):
        while self._alive:
            time.sleep(self.HEARTBEAT_INTERVAL)
            if self._alive:
                self.send_heartbeat()

    # ---------- 回调 ----------

    def on_open(self, ws):
        self._alive = True
        logger.success(f"WS 已连接 {self.host}")
        # 前端顺序：心跳在前，hello 在后
        self.send_heartbeat()
        self.send_hello()
        threading.Thread(target=self._heartbeat_loop, daemon=True).start()

    def on_message(self, ws, message):
        try:
            packet = json.loads(message)
        except (ValueError, TypeError):
            logger.info(f"收到非 JSON 帧：{message[:200]}")
            return
        if isinstance(packet, list):
            for item in packet:
                self._handle(item)
        else:
            self._handle(packet)

    @staticmethod
    def extract_text(body):
        """从下行 body 里尽力抽出可读文本。

        咚咚的下行远不止 `{"type":"text","content":"…"}` 一种：客服和智能助手
        大量使用模板消息（`template2` + `nativeId`），正文散落在
        `data.tplData` 下的 message / title / content 等字段里。只取
        `body.content` 会得到 None——这正是早期日志里 `【@im.jd.com】None` 的原因。
        """
        if not isinstance(body, dict):
            return None
        content = body.get("content")
        if isinstance(content, str) and content.strip():
            return content
        data = body.get("data") or {}
        tpl = data.get("tplData") if isinstance(data, dict) else None
        for holder in (tpl, data, body):
            if not isinstance(holder, dict):
                continue
            for key in ("message", "title", "text", "content", "answer", "desc"):
                val = holder.get(key)
                if isinstance(val, str) and val.strip():
                    return val
        return None

    def _handle(self, packet):
        msg_type = packet.get("type")
        body = packet.get("body") or {}
        sender = (packet.get("from") or {}).get("pin", "")

        if msg_type in (MsgType.CHAT_MESSAGE, MsgType.EVENT_MESSAGE):
            text = self.extract_text(body)
            kind = (body.get("template") or {}).get("nativeId") or body.get("type")
            if text:
                logger.info(f"【{sender}】{text}")
                if self.on_text:
                    self.on_text(sender, text, packet)
            else:
                logger.debug(f"[{msg_type}/{kind}] 无可读文本 "
                             f"{json.dumps(body, ensure_ascii=False)[:220]}")
        elif msg_type == MsgType.SYS_MSG:
            logger.info(f"[系统] {self.extract_text(body)}")
        elif msg_type == MsgType.CHAT_SESSION_OPEN:
            waiter = (body.get("waiter") or {}).get("pin")
            logger.success(f"[会话建立] 商家={body.get('venderId')} 客服={waiter} "
                           f"code={body.get('code')}")
        elif msg_type == MsgType.CHAT_SESSION_CLOSE:
            logger.info("[会话结束]")
        elif msg_type == MsgType.FAILURE:
            logger.error(f"[失败] code={body.get('code')} msg={body.get('msg')} "
                         f"(对应 type={body.get('type')})")
        elif msg_type == MsgType.REVOKE_MESSAGE:
            logger.info(f"[撤回] {body.get('revokeContentToC')}")
        elif msg_type in (MsgType.CLIENT_HEARTBEAT, MsgType.ACK,
                          MsgType.CHAT_MESSAGE_RESULT):
            logger.debug(f"[{msg_type}] {json.dumps(packet, ensure_ascii=False)[:200]}")
        else:
            logger.info(f"[{msg_type}] {json.dumps(packet, ensure_ascii=False)[:300]}")

    def on_error(self, ws, error):
        logger.error(f"WS 错误：{error}")

    def on_close(self, ws, close_status_code, close_msg):
        self._alive = False
        logger.warning(f"WS 关闭 status={close_status_code} msg={close_msg}")

    # ---------- 生命周期 ----------

    def start(self):
        if not self.auth.is_login:
            logger.error("未登录，先跑 qr-login")
            return False
        if not self.auth.aid:
            logger.error("auth 缺 aid，先跑 chat-info（内部会调 getAidInfo）")
            return False
        self.ws = WebSocketApp(
            url=self.url,
            header=HeaderBuilder.build_ws(),
            cookie=self.auth.cookie_str,
            on_open=self.on_open,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close,
        )
        logger.info(f"建连 {self.url}")
        try:
            self.ws.run_forever(origin=self.ORIGIN)
        except KeyboardInterrupt:
            self.close()
        return True

    def close(self):
        self._alive = False
        if self.ws:
            self.ws.close()
