import asyncio
import json

import websockets

from qrobot.config import Config
from qrobot.onebot import (
    OneBotClient,
    parse_onebot_event,
    parse_onebot_message,
    probe_onebot,
    split_message,
)


def test_parse_segment_message_with_mention_and_image():
    parsed = parse_onebot_message(
        [
            {"type": "at", "data": {"qq": "42"}},
            {"type": "text", "data": {"text": " 你好 "}},
            {"type": "image", "data": {"url": "https://example.com/a.png"}},
        ],
        self_id="42",
    )
    assert parsed.mentioned is True
    assert "你好" in parsed.text
    assert parsed.image_urls == ("https://example.com/a.png",)


def test_parse_cq_string_and_event():
    event = parse_onebot_event(
        {
            "post_type": "message",
            "message_type": "group",
            "self_id": 42,
            "user_id": 7,
            "group_id": 100,
            "message_id": 9,
            "sender": {"nickname": "小明", "card": "群名片"},
            "message": "[CQ:at,qq=42] /ai 你好&amp;再见",
            "time": 1_700_000_000,
        }
    )
    assert event is not None
    assert event.inbound.mentioned is True
    assert event.inbound.sender_name == "群名片"
    assert event.inbound.conversation_id == "group:100"
    assert "你好&再见" in event.inbound.text


def test_ignores_own_message():
    assert (
        parse_onebot_event(
            {
                "post_type": "message",
                "message_type": "group",
                "self_id": 42,
                "user_id": 42,
                "group_id": 100,
                "message": "hello",
            }
        )
        is None
    )


def test_ignores_message_sent_events():
    assert (
        parse_onebot_event(
            {
                "post_type": "message_sent",
                "message_type": "group",
                "self_id": 42,
                "user_id": 7,
                "group_id": 100,
                "message": "/ai loop",
            }
        )
        is None
    )


def test_split_message_preserves_content():
    text = "第一段。" * 50
    chunks = split_message(text, 40)
    assert all(len(chunk) <= 40 for chunk in chunks)
    assert "".join(chunks) == text


async def test_forward_websocket_receive_and_send():
    sent_actions = []
    handled = asyncio.Event()

    async def fake_onebot(websocket):
        login_request = json.loads(await websocket.recv())
        assert login_request["action"] == "get_login_info"
        await websocket.send(
            json.dumps(
                {
                    "status": "ok",
                    "retcode": 0,
                    "data": {"user_id": 42, "nickname": "机器人"},
                    "echo": login_request["echo"],
                }
            )
        )
        await websocket.send(
            json.dumps(
                {
                    "post_type": "message",
                    "message_type": "group",
                    "self_id": 42,
                    "user_id": 7,
                    "group_id": 100,
                    "message_id": 9,
                    "sender": {"nickname": "小明"},
                    "message": [{"type": "at", "data": {"qq": "42"}}, {"type": "text", "data": {"text": "你好"}}],
                }
            )
        )
        send_request = json.loads(await websocket.recv())
        sent_actions.append(send_request)
        await websocket.send(
            json.dumps(
                {
                    "status": "ok",
                    "retcode": 0,
                    "data": {"message_id": 10},
                    "echo": send_request["echo"],
                }
            )
        )
        await handled.wait()

    server = await websockets.serve(fake_onebot, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    config = Config(
        qq_backend="onebot",
        onebot_ws_url=f"ws://127.0.0.1:{port}",
        onebot_group_ids=("100",),
        onebot_reconnect_seconds=0.01,
    )
    client = OneBotClient(config)

    async def handler(event):
        await client.send_reply(event, "测试回复")
        handled.set()
        await client.stop()

    try:
        await asyncio.wait_for(client.run_forever(handler), timeout=5)
    finally:
        await client.stop()
        server.close()
        await server.wait_closed()
    assert sent_actions[0]["action"] == "send_group_msg"
    assert sent_actions[0]["params"]["group_id"] == 100


async def test_probe_onebot_version():
    async def fake_onebot(websocket):
        request = json.loads(await websocket.recv())
        await websocket.send(
            json.dumps(
                {
                    "status": "ok",
                    "retcode": 0,
                    "data": {"app_name": "FakeBot", "app_version": "1.0"},
                    "echo": request["echo"],
                }
            )
        )

    server = await websockets.serve(fake_onebot, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        result = await probe_onebot(
            Config(qq_backend="onebot", onebot_ws_url=f"ws://127.0.0.1:{port}")
        )
    finally:
        server.close()
        await server.wait_closed()
    assert result == "FakeBot 1.0"
