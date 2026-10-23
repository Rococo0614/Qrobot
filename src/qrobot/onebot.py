from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .config import Config
from .models import InboundMessage

logger = logging.getLogger(__name__)


class OneBotError(RuntimeError):
    pass


@dataclass(slots=True, frozen=True)
class ParsedOneBotMessage:
    text: str
    mentioned: bool
    image_urls: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class OneBotMessageEvent:
    inbound: InboundMessage
    message_type: str
    group_id: str
    user_id: str
    message_id: str


EventHandler = Callable[[OneBotMessageEvent], Awaitable[None]]


class OneBotClient:
    """OneBot 11 forward WebSocket client with API echo correlation."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.self_id = ""
        self.nickname = ""
        self._websocket: Any = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._send_lock = asyncio.Lock()
        self._event_queue: asyncio.Queue[OneBotMessageEvent] = asyncio.Queue(
            maxsize=config.event_queue_size
        )
        self._stop_event = asyncio.Event()
        self._workers: list[asyncio.Task[None]] = []

    async def run_forever(self, handler: EventHandler) -> None:
        try:
            import websockets
        except ImportError as exc:
            raise OneBotError("未安装 websockets，请执行 pip install -e '.[onebot]'") from exc

        worker_count = max(1, min(self.config.max_parallel_requests, 8))
        self._workers = [
            asyncio.create_task(self._event_worker(handler), name=f"onebot-worker-{index}")
            for index in range(worker_count)
        ]
        try:
            while not self._stop_event.is_set():
                try:
                    await self._connection_cycle(websockets)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("OneBot 连接中断: %s", exc)
                if not self._stop_event.is_set():
                    await asyncio.sleep(self.config.onebot_reconnect_seconds)
        finally:
            await self.stop()
            for worker in self._workers:
                worker.cancel()
            await asyncio.gather(*self._workers, return_exceptions=True)
            self._workers.clear()

    async def stop(self) -> None:
        self._stop_event.set()
        websocket, self._websocket = self._websocket, None
        if websocket is not None:
            try:
                await websocket.close()
            except Exception:
                pass
        self._fail_pending(OneBotError("OneBot 连接已关闭"))

    async def _connection_cycle(self, websockets_module: Any) -> None:
        kwargs = _connect_kwargs(websockets_module, self.config)
        logger.info("正在连接 OneBot: %s", _redacted_ws_url(self.config.onebot_ws_url))
        context = websockets_module.connect(self.config.onebot_ws_url, **kwargs)

        async with context as websocket:
            self._websocket = websocket
            logger.info("已连接 OneBot")
            receiver = asyncio.create_task(self._receive_loop(websocket), name="onebot-receiver")
            try:
                try:
                    info = await self.call_action("get_login_info", {})
                    if isinstance(info, dict):
                        self.self_id = str(info.get("user_id") or self.self_id)
                        self.nickname = str(info.get("nickname") or "")
                    logger.info("OneBot 登录账号: %s", self.self_id or "未知")
                except Exception as exc:
                    logger.warning("无法通过 get_login_info 获取机器人账号: %s", exc)
                await receiver
            finally:
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)
                if self._websocket is websocket:
                    self._websocket = None
                self._fail_pending(OneBotError("OneBot WebSocket 已断开"))

    async def _receive_loop(self, websocket: Any) -> None:
        async for raw in websocket:
            try:
                payload = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                logger.debug("忽略无法解析的 OneBot 数据帧")
                continue
            if not isinstance(payload, dict):
                continue
            echo = payload.get("echo")
            if echo is not None:
                future = self._pending.get(str(echo))
                if future is not None and not future.done():
                    future.set_result(payload)
                continue
            event = parse_onebot_event(payload, self.self_id)
            if event is None:
                continue
            try:
                self._event_queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("OneBot 事件队列已满，丢弃消息 %s", event.message_id)

    async def _event_worker(self, handler: EventHandler) -> None:
        while True:
            event = await self._event_queue.get()
            try:
                await handler(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("处理 OneBot 消息失败，message_id=%s", event.message_id)
            finally:
                self._event_queue.task_done()

    async def call_action(self, action: str, params: dict[str, Any]) -> Any:
        websocket = self._websocket
        if websocket is None:
            raise OneBotError("OneBot 尚未连接")
        echo = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[echo] = future
        payload = json.dumps(
            {"action": action, "params": params, "echo": echo}, ensure_ascii=False
        )
        try:
            async with self._send_lock:
                await websocket.send(payload)
            response = await asyncio.wait_for(
                future, timeout=self.config.onebot_action_timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            raise OneBotError(f"OneBot 动作 {action} 等待响应超时") from exc
        finally:
            self._pending.pop(echo, None)
        status = response.get("status")
        retcode = response.get("retcode", 0)
        if status != "ok" or retcode not in {0, None}:
            wording = response.get("wording") or response.get("message") or "无错误详情"
            raise OneBotError(f"OneBot 动作 {action} 失败（{retcode}）: {wording}")
        return response.get("data")

    async def send_reply(self, event: OneBotMessageEvent, text: str) -> None:
        chunks = split_message(text, self.config.onebot_message_chunk_chars)
        for index, chunk in enumerate(chunks):
            segments: list[dict[str, Any]] = []
            if index == 0 and self.config.onebot_reply_to_message and event.message_id:
                segments.append({"type": "reply", "data": {"id": event.message_id}})
            segments.append({"type": "text", "data": {"text": chunk}})
            if event.message_type == "group":
                await self.call_action(
                    "send_group_msg", {"group_id": int(event.group_id), "message": segments}
                )
            else:
                await self.call_action(
                    "send_private_msg", {"user_id": int(event.user_id), "message": segments}
                )
            if index + 1 < len(chunks):
                await asyncio.sleep(0.35)

    def _fail_pending(self, exc: Exception) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()


def parse_onebot_event(
    payload: dict[str, Any], known_self_id: str = ""
) -> OneBotMessageEvent | None:
    # message_sent represents the bot's own outbound traffic on implementations
    # that expose it. Ignoring it is a second loop-prevention layer.
    if payload.get("post_type") != "message":
        return None
    message_type = str(payload.get("message_type") or "")
    if message_type not in {"group", "private"}:
        return None
    user_id = str(payload.get("user_id") or "")
    self_id = str(payload.get("self_id") or known_self_id or "")
    if not user_id or (self_id and user_id == self_id):
        return None
    parsed = parse_onebot_message(payload.get("message", payload.get("raw_message", "")), self_id)
    text = parsed.text.strip()
    if not text and parsed.image_urls:
        text = "[图片]"
    if not text:
        return None

    sender = payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
    sender_name = str(sender.get("card") or sender.get("nickname") or user_id)
    message_id = str(payload.get("message_id") or "")
    group_id = str(payload.get("group_id") or "")
    is_group = message_type == "group"
    conversation_id = f"group:{group_id}" if is_group else f"private:{user_id}"
    conversation_name = f"群 {group_id}" if is_group else sender_name
    timestamp_value = payload.get("time")
    try:
        timestamp = datetime.fromtimestamp(float(timestamp_value), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        timestamp = datetime.now(timezone.utc)
    inbound = InboundMessage(
        source="onebot",
        conversation_id=conversation_id,
        conversation_name=conversation_name,
        sender_id=user_id,
        sender_name=sender_name,
        text=text,
        message_id=message_id,
        is_group=is_group,
        mentioned=parsed.mentioned,
        image_urls=parsed.image_urls,
        timestamp=timestamp,
        raw=payload,
    )
    return OneBotMessageEvent(
        inbound=inbound,
        message_type=message_type,
        group_id=group_id,
        user_id=user_id,
        message_id=message_id,
    )


def parse_onebot_message(message: Any, self_id: str = "") -> ParsedOneBotMessage:
    if isinstance(message, list):
        segments = [item for item in message if isinstance(item, dict)]
    elif isinstance(message, str):
        segments = _parse_cq_string(message)
    else:
        segments = []
    text_parts: list[str] = []
    image_urls: list[str] = []
    mentioned = False
    for segment in segments:
        segment_type = str(segment.get("type") or "")
        data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
        if segment_type == "text":
            text_parts.append(str(data.get("text") or ""))
        elif segment_type == "at":
            target = str(data.get("qq") or "")
            if self_id and target == self_id:
                mentioned = True
            elif target:
                text_parts.append(f"@{target} ")
        elif segment_type == "image":
            candidate = str(data.get("url") or data.get("file") or "")
            if candidate.startswith(("http://", "https://")):
                image_urls.append(candidate)
            text_parts.append("[图片]")
        elif segment_type in {"reply", "face", "record", "video", "file"}:
            text_parts.append(f"[{_segment_label(segment_type)}]")
    text = re.sub(r"[ \t]+", " ", "".join(text_parts)).strip()
    return ParsedOneBotMessage(text=text, mentioned=mentioned, image_urls=tuple(image_urls))


def _parse_cq_string(value: str) -> list[dict[str, Any]]:
    pattern = re.compile(r"\[CQ:([a-zA-Z0-9_-]+)((?:,[^\]]*)?)\]")
    result: list[dict[str, Any]] = []
    cursor = 0
    for match in pattern.finditer(value):
        if match.start() > cursor:
            result.append(
                {"type": "text", "data": {"text": _cq_unescape(value[cursor : match.start()])}}
            )
        data: dict[str, str] = {}
        raw_params = match.group(2).lstrip(",")
        if raw_params:
            for item in raw_params.split(","):
                key, separator, raw_value = item.partition("=")
                if separator:
                    data[key] = _cq_unescape(raw_value)
        result.append({"type": match.group(1), "data": data})
        cursor = match.end()
    if cursor < len(value):
        result.append({"type": "text", "data": {"text": _cq_unescape(value[cursor:])}})
    return result


def _cq_unescape(value: str) -> str:
    return (
        value.replace("&#44;", ",")
        .replace("&#91;", "[")
        .replace("&#93;", "]")
        .replace("&amp;", "&")
    )


def _segment_label(value: str) -> str:
    return {
        "reply": "回复消息",
        "face": "表情",
        "record": "语音",
        "video": "视频",
        "file": "文件",
    }.get(value, value)


def split_message(text: str, limit: int) -> list[str]:
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        candidates = [
            remaining.rfind(separator, 0, limit + 1)
            for separator in ("\n\n", "\n", "。", "！", "？", "；", " ")
        ]
        cut = max(candidates)
        if cut < max(20, limit // 3):
            cut = limit
        else:
            cut += 1
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        chunks.append(remaining)
    return [chunk for chunk in chunks if chunk]


async def probe_onebot(config: Config) -> str:
    """Perform a read-only OneBot handshake and version query."""
    try:
        import websockets
    except ImportError as exc:
        raise OneBotError("未安装 websockets") from exc
    kwargs = _connect_kwargs(websockets, config)
    kwargs["open_timeout"] = min(config.onebot_action_timeout_seconds, 10)
    echo = "qrobot-doctor-" + uuid.uuid4().hex
    try:
        async with websockets.connect(config.onebot_ws_url, **kwargs) as websocket:
            await websocket.send(
                json.dumps(
                    {"action": "get_version_info", "params": {}, "echo": echo},
                    ensure_ascii=False,
                )
            )
            deadline = asyncio.get_running_loop().time() + config.onebot_action_timeout_seconds
            for _ in range(50):
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                raw = await asyncio.wait_for(
                    websocket.recv(), timeout=remaining
                )
                payload = json.loads(raw)
                if not isinstance(payload, dict) or str(payload.get("echo")) != echo:
                    continue
                if payload.get("status") != "ok" or payload.get("retcode", 0) not in {
                    0,
                    None,
                }:
                    raise OneBotError("OneBot get_version_info 返回失败")
                data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
                name = str(data.get("app_name") or data.get("app_full_name") or "OneBot 11")
                version = str(data.get("app_version") or data.get("version") or "未知版本")
                return f"{name} {version}"
            raise OneBotError("OneBot 响应过多，未找到版本查询结果")
    except OneBotError:
        raise
    except asyncio.TimeoutError as exc:
        raise OneBotError("OneBot 连接或版本查询超时") from exc
    except Exception as exc:
        error_name = type(exc).__name__
        if error_name == "InvalidMessage":
            raise OneBotError(
                "OneBot WebSocket 握手无效；若 NapCat 在 Docker 中，请启用 Websocket "
                "服务器并使用 Token 监听 0.0.0.0:3001"
            ) from exc
        raise OneBotError(f"OneBot WebSocket 握手失败: {error_name}") from exc


def _connect_kwargs(websockets_module: Any, config: Config) -> dict[str, Any]:
    headers = {}
    if config.onebot_access_token:
        headers["Authorization"] = f"Bearer {config.onebot_access_token}"
    kwargs: dict[str, Any] = {
        "ping_interval": config.onebot_ping_interval_seconds,
        "ping_timeout": config.onebot_ping_timeout_seconds,
        "max_size": 8 * 1024 * 1024,
    }
    if headers:
        parameters = inspect.signature(websockets_module.connect).parameters
        header_name = (
            "additional_headers" if "additional_headers" in parameters else "extra_headers"
        )
        kwargs[header_name] = headers
    return kwargs


def _redacted_ws_url(value: str) -> str:
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
