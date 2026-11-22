"""
企业微信智能机器人 - WebSocket 长连接客户端

协议要点（官方文档 https://developer.work.weixin.qq.com/document/60904）：
  1. 连接 wss://openws.work.weixin.qq.com
  2. 发送 aibot_subscribe 携带 bot_id + secret 完成鉴权
  3. 定时发送 ping 保活
  4. 接收 aibot_msg_callback（消息）/ aibot_event_callback（事件）
  5. 通过 aibot_send_msg 主动向会话推送消息

长连接模式无需公网 IP / 域名 / 内网穿透。
注意：同一机器人同一时刻只能保持一个长连接，重复连接会互踢。
"""

import asyncio
import json
import logging
import time
import uuid
from typing import Awaitable, Callable, Optional

import websockets

logger = logging.getLogger("wecom")

# 消息回调处理器类型: (frame: dict) -> Awaitable[None]
MsgHandler = Callable[[dict], Awaitable[None]]


def gen_req_id(prefix: str = "req") -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class WecomBot:
    def __init__(
        self,
        bot_id: str,
        secret: str,
        ws_url: str = "wss://openws.work.weixin.qq.com",
        heartbeat_interval: int = 30,
    ):
        self.bot_id = bot_id
        self.secret = secret
        self.ws_url = ws_url
        self.heartbeat_interval = heartbeat_interval
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.authenticated = False
        self._running = False
        self._handlers: dict[str, list[MsgHandler]] = {}
        self._pending: dict[str, asyncio.Future] = {}
        self._reconnect_delay = 1
        self._max_reconnect_delay = 30
        self._kicked = False  # 被新连接踢出时不再重连

    # ---------------- 事件注册 ----------------
    def on(self, event: str, handler: MsgHandler):
        """event: 'message' | 'message.text' | 'event' | 'event.enter_chat' | 'connected' 等"""
        self._handlers.setdefault(event, []).append(handler)

    async def _emit(self, event: str, frame: dict):
        for h in self._handlers.get(event, []):
            try:
                await h(frame)
            except Exception as e:  # 单个处理器异常不影响整体
                logger.exception(f"handler error on {event}: {e}")

    # ---------------- 连接管理 ----------------
    async def connect_async(self):
        """建立连接并保持运行（内部自动重连）。"""
        self._running = True
        while self._running:
            if self._kicked:
                logger.error("长连接已被新实例踢出，停止重连。请确认没有其他进程占用该机器人。")
                break
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"连接异常: {e}")

            if not self._running:
                break

            delay = self._reconnect_delay
            logger.info(f"{delay}s 后重连...")
            await asyncio.sleep(delay)
            self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)

    async def _run_once(self):
        logger.info(f"正在连接 {self.ws_url}")
        async with websockets.connect(
            self.ws_url,
            ping_interval=None,   # 自己维护心跳
            ping_timeout=None,
            max_size=20 * 1024 * 1024,
            proxy=None,           # 强制直连，绕过系统代理（企微是国内服务器，无需代理）
        ) as ws:
            self.ws = ws
            self.authenticated = False
            await self._subscribe()
            self._reconnect_delay = 1
            await self._emit("connected", {})
            await self._loop(ws)

    async def _loop(self, ws):
        """并发跑心跳与收消息。"""
        heartbeat = asyncio.create_task(self._heartbeat_loop())
        try:
            async for raw in ws:
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning(f"收到非 JSON 帧: {raw[:200]}")
                    continue
                await self._dispatch(frame)
        except websockets.exceptions.ConnectionClosed as e:
            logger.warning(f"连接关闭: code={e.code} reason={e.reason}")
            raise
        finally:
            heartbeat.cancel()
            self.authenticated = False
            self.ws = None

    async def _subscribe(self):
        req_id = gen_req_id("sub")
        payload = {
            "cmd": "aibot_subscribe",
            "headers": {"req_id": req_id},
            "body": {"bot_id": self.bot_id, "secret": self.secret},
        }
        await self.ws.send(json.dumps(payload))
        logger.info("已发送订阅鉴权请求，等待校验...")

    async def _heartbeat_loop(self):
        """按固定间隔发送 ping 保活。"""
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            if not self.ws:
                break
            try:
                await self.ws.send(json.dumps({"cmd": "ping", "headers": {"req_id": gen_req_id("ping")}}))
                logger.debug("heartbeat ping")
            except Exception as e:
                logger.warning(f"心跳发送失败: {e}")
                break

    # ---------------- 消息分发 ----------------
    async def _dispatch(self, frame: dict):
        cmd = frame.get("cmd")
        headers = frame.get("headers", {})
        req_id = headers.get("req_id")
        body = frame.get("body", {})

        # 订阅/请求的响应
        if "errcode" in frame or cmd is None:
            errcode = frame.get("errcode")
            errmsg = frame.get("errmsg")
            if errcode == 0:
                if not self.authenticated:
                    self.authenticated = True
                    logger.info("✅ 鉴权成功，长连接已就绪")
            else:
                logger.error(f"请求失败 errcode={errcode} errmsg={errmsg} req_id={req_id}")
                if errcode in (850003, 40001, 41001):  # 鉴权类错误，重连无意义
                    self._kicked = True
            fut = self._pending.pop(req_id, None)
            if fut and not fut.done():
                fut.set_result(frame)
            return

        if cmd == "aibot_msg_callback":
            logger.info(f"收到消息: from={body.get('from', {}).get('userid')} chat={body.get('chatid')} type={body.get('msgtype')}")
            await self._emit("message", frame)
            msgtype = body.get("msgtype", "")
            await self._emit(f"message.{msgtype}", frame)

        elif cmd == "aibot_event_callback":
            etype = body.get("eventtype", "")
            logger.info(f"收到事件: {etype}")
            await self._emit("event", frame)
            await self._emit(f"event.{etype}", frame)

        elif cmd == "aibot_disconnected_event":
            logger.error("被新的长连接踢出，本进程将退出")
            self._kicked = True
            await self._emit("event.disconnected_event", frame)

        elif cmd == "pong":
            logger.debug("heartbeat pong")

        else:
            logger.debug(f"未处理帧 cmd={cmd}")

        fut = self._pending.pop(req_id, None)
        if fut and not fut.done():
            fut.set_result(frame)

    # ---------------- 发送能力 ----------------
    async def send_text(self, chat_id: str, content: str, chat_type: int = 2):
        """向指定会话发送文本消息。chat_type: 1=单聊 2=群聊"""
        return await self.send_markdown(chat_id, content, chat_type)

    async def send_markdown(self, chat_id: str, content: str, chat_type: int = 2):
        """向指定会话发送 markdown 消息（企微 markdown 长度上限约 4096 字节）。"""
        if not self.ws:
            raise RuntimeError("长连接未就绪")
        # 分片发送，避免超长被丢弃
        chunks = split_markdown(content)
        for i, chunk in enumerate(chunks):
            payload = {
                "cmd": "aibot_send_msg",
                "headers": {"req_id": gen_req_id("send")},
                "body": {
                    "chatid": chat_id,
                    "chat_type": chat_type,
                    "msgtype": "markdown",
                    "markdown": {"content": chunk},
                },
            }
            await self.ws.send(json.dumps(payload))
            logger.info(f"已推送消息到 {chat_id}（{i + 1}/{len(chunks)}）")
            if len(chunks) > 1:
                await asyncio.sleep(0.3)

    async def reply(self, frame: dict, content: str, msgtype: str = "markdown"):
        """便捷方法：从回调帧里取 chatid 并回复。"""
        body = frame.get("body", {})
        chat_id = body.get("chatid") or body.get("from", {}).get("userid")
        chat_type = 2 if body.get("chattype") == "group" else 1
        if msgtype == "text":
            return await self.send_raw(chat_id, chat_type, "text", {"content": content})
        return await self.send_markdown(chat_id, content, chat_type)

    async def send_raw(self, chat_id: str, chat_type: int, msgtype: str, body_content: dict):
        payload = {
            "cmd": "aibot_send_msg",
            "headers": {"req_id": gen_req_id("send")},
            "body": {"chatid": chat_id, "chat_type": chat_type, "msgtype": msgtype},
        }
        payload["body"][msgtype] = body_content
        await self.ws.send(json.dumps(payload))

    def disconnect(self):
        self._running = False
        if self.ws:
            asyncio.create_task(self.ws.close())


def split_markdown(text: str, limit: int = 3800) -> list[str]:
    """按行切分长文本，避免超出企微消息长度限制。"""
    if len(text.encode("utf-8")) <= limit:
        return [text]
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line.encode("utf-8")) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[: limit // 2])
            line = line[limit // 2 :]
        if len((cur + "\n" + line).encode("utf-8")) > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks
