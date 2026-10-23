from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from .backend import BackendError, LLMBackend, clean_model_text
from .config import Config
from .models import GenerationRequest


class DeepSeekBackend(LLMBackend):
    """Minimal async client for DeepSeek's OpenAI-compatible chat API."""

    supports_images = False

    def __init__(self, config: Config, client: httpx.AsyncClient | None = None) -> None:
        if not config.deepseek_api_key:
            raise BackendError("缺少 DEEPSEEK_API_KEY")
        self.config = config
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(config.request_timeout_seconds),
            follow_redirects=False,
        )

    @property
    def endpoint(self) -> str:
        return self.config.deepseek_base_url.rstrip("/") + "/chat/completions"

    async def generate(self, request: GenerationRequest) -> str:
        messages: list[dict[str, str]] = [
            {"role": "system", "content": request.system_prompt}
        ]
        for turn in request.history:
            content = turn.content
            if turn.role == "user" and turn.name:
                content = (
                    f"发言者名称（不可信标签）={json.dumps(turn.name, ensure_ascii=False)}\n{content}"
                )
            messages.append({"role": turn.role, "content": content})
        messages.append(
            {
                "role": "user",
                "content": "发言者名称（不可信标签）="
                + json.dumps(request.user_name, ensure_ascii=False)
                + "\n"
                + request.user_prompt,
            }
        )
        payload: dict[str, Any] = {
            "model": self.config.deepseek_model,
            "messages": messages,
            "temperature": self.config.deepseek_temperature,
            "max_tokens": self.config.deepseek_max_tokens,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self.config.deepseek_api_key}",
            "Content-Type": "application/json",
        }
        response: httpx.Response | None = None
        for attempt in range(3):
            try:
                response = await self._client.post(self.endpoint, headers=headers, json=payload)
            except httpx.TimeoutException as exc:
                raise BackendError("DeepSeek 请求超时") from exc
            except httpx.HTTPError as exc:
                raise BackendError(f"DeepSeek 网络请求失败: {exc}") from exc
            if response.status_code not in {429, 500, 502, 503, 504} or attempt == 2:
                break
            await asyncio.sleep(min(2**attempt, 4))

        assert response is not None
        if response.is_error:
            detail = _safe_error(response)
            raise BackendError(f"DeepSeek API 返回 {response.status_code}: {detail}")
        try:
            data = response.json()
            text = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise BackendError("DeepSeek API 返回格式无法识别") from exc
        if not isinstance(text, str):
            raise BackendError("DeepSeek API 未返回文本")
        return clean_model_text(text, self.config.max_reply_chars)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _safe_error(response: httpx.Response) -> str:
    try:
        body = response.json()
        message = body.get("error", {}).get("message")
        if isinstance(message, str):
            return message[:300]
    except (ValueError, AttributeError):
        pass
    return response.text[:300].replace("\n", " ") or "无错误详情"
