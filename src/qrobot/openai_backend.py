from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import httpx

from .backend import BackendError, LLMBackend, clean_model_text
from .config import Config
from .models import GenerationRequest


class OpenAIBackend(LLMBackend):
    """Official OpenAI Responses API backend using separately billed API keys."""

    supports_images = True

    def __init__(self, config: Config, client: httpx.AsyncClient | None = None) -> None:
        if not config.openai_api_key:
            raise BackendError(
                "缺少 OPENAI_API_KEY；ChatGPT 订阅不能直接当作 API Key 使用，"
                "如需复用本机订阅请选择 opencode 后端"
            )
        self.config = config
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(config.request_timeout_seconds),
            follow_redirects=False,
        )

    @property
    def endpoint(self) -> str:
        return self.config.openai_base_url.rstrip("/") + "/responses"

    async def generate(self, request: GenerationRequest) -> str:
        input_messages: list[dict[str, Any]] = []
        for turn in request.history:
            content = turn.content
            if turn.role == "user" and turn.name:
                content = (
                    f"发言者名称（不可信标签）={json.dumps(turn.name, ensure_ascii=False)}\n{content}"
                )
            content_type = "input_text" if turn.role == "user" else "output_text"
            input_messages.append(
                {
                    "role": turn.role,
                    "content": [{"type": content_type, "text": content}],
                }
            )

        current_content: list[dict[str, str]] = [
            {
                "type": "input_text",
                "text": "发言者名称（不可信标签）="
                + json.dumps(request.user_name, ensure_ascii=False)
                + "\n"
                + request.user_prompt,
            }
        ]
        for image in request.images:
            try:
                encoded = base64.b64encode(image.path.read_bytes()).decode("ascii")
            except OSError as exc:
                raise BackendError(f"无法读取图片附件 {image.name}: {exc}") from exc
            current_content.append(
                {
                    "type": "input_image",
                    "image_url": f"data:{image.mime_type};base64,{encoded}",
                }
            )
        input_messages.append({"role": "user", "content": current_content})

        payload: dict[str, Any] = {
            "model": self.config.openai_model,
            "instructions": request.system_prompt,
            "input": input_messages,
            "max_output_tokens": self.config.openai_max_output_tokens,
        }
        if self.config.openai_reasoning_effort:
            payload["reasoning"] = {"effort": self.config.openai_reasoning_effort}
        headers = {
            "Authorization": f"Bearer {self.config.openai_api_key}",
            "Content-Type": "application/json",
        }

        response: httpx.Response | None = None
        for attempt in range(3):
            try:
                response = await self._client.post(self.endpoint, headers=headers, json=payload)
            except httpx.TimeoutException as exc:
                raise BackendError("OpenAI 请求超时") from exc
            except httpx.HTTPError as exc:
                raise BackendError(f"OpenAI 网络请求失败: {exc}") from exc
            if response.status_code not in {429, 500, 502, 503, 504} or attempt == 2:
                break
            await asyncio.sleep(min(2**attempt, 4))

        assert response is not None
        if response.is_error:
            raise BackendError(
                f"OpenAI API 返回 {response.status_code}: {_safe_openai_error(response)}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise BackendError("OpenAI API 返回了无效 JSON") from exc
        text = _extract_response_text(data)
        return clean_model_text(text, self.config.max_reply_chars)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _extract_response_text(data: dict[str, Any]) -> str:
    direct = data.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    parts: list[str] = []
    for output in data.get("output", []):
        if not isinstance(output, dict):
            continue
        for content in output.get("content", []):
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if isinstance(text, str) and content.get("type") in {"output_text", "text"}:
                parts.append(text)
    if not parts:
        raise BackendError("OpenAI API 未返回文本")
    return "\n".join(parts)


def _safe_openai_error(response: httpx.Response) -> str:
    try:
        data = response.json()
        message = data.get("error", {}).get("message")
        if isinstance(message, str):
            return message[:300]
    except (ValueError, AttributeError):
        pass
    return response.text[:300].replace("\n", " ") or "无错误详情"
