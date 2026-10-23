import json
import os

import httpx

from qrobot.backend import format_request_as_prompt
from qrobot.config import Config
from qrobot.deepseek import DeepSeekBackend
from qrobot.models import ChatTurn, GenerationRequest
from qrobot.openai_backend import OpenAIBackend, _extract_response_text
from qrobot.opencode_backend import OpenCodeBackend


def test_flat_prompt_keeps_roles_explicit():
    prompt = format_request_as_prompt(
        GenerationRequest(
            system_prompt="规则",
            history=(ChatTurn("user", "旧消息", "小明"), ChatTurn("assistant", "旧回复")),
            user_prompt="新消息",
            user_name="小红",
        )
    )
    assert "<system_instructions>" in prompt
    assert 'name="小明" content="旧消息"' in prompt
    assert 'name="小红" content="新消息"' in prompt


def test_extract_openai_response_text():
    assert _extract_response_text({"output_text": "ok"}) == "ok"
    assert (
        _extract_response_text(
            {"output": [{"content": [{"type": "output_text", "text": "hello"}]}]}
        )
        == "hello"
    )


async def test_openai_backend_responses_payload():
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["input"][0]["role"] == "assistant"
        assert payload["input"][0]["content"][0]["type"] == "output_text"
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(200, json={"output_text": "连接成功"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        config = Config(llm_backend="openai", openai_api_key="test-key")
        backend = OpenAIBackend(config, client=client)
        result = await backend.generate(
            GenerationRequest(
                system_prompt="规则",
                history=(ChatTurn("assistant", "旧回复"),),
                user_prompt="测试",
            )
        )
    assert result == "连接成功"


async def test_deepseek_backend_chat_payload():
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["messages"][0] == {"role": "system", "content": "规则"}
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "连接成功"}}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        config = Config(llm_backend="deepseek", deepseek_api_key="test-key")
        backend = DeepSeekBackend(config, client=client)
        result = await backend.generate(
            GenerationRequest(system_prompt="规则", history=(), user_prompt="测试")
        )
    assert result == "连接成功"


async def test_opencode_backend_uses_authenticated_loopback_http(tmp_path):
    service_file = tmp_path / "service.json"
    service_file.write_text(
        json.dumps({"url": "http://127.0.0.1:4096", "password": "local-secret"}),
        encoding="utf-8",
    )
    os.chmod(service_file, 0o600)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/experimental/generate"
        assert request.headers["authorization"].startswith("Basic ")
        payload = json.loads(request.content)
        assert payload["model"]["providerID"] == "openai"
        assert "聊天正文" in payload["prompt"]
        return httpx.Response(200, json={"data": {"text": "直连成功"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        config = Config(
            llm_backend="opencode",
            opencode_transport="http",
            opencode_service_file=service_file,
        )
        backend = OpenCodeBackend(config, client=client)
        result = await backend.generate(
            GenerationRequest(system_prompt="规则", history=(), user_prompt="聊天正文")
        )
    assert result == "直连成功"
