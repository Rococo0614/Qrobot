from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from .backend import (
    BackendError,
    LLMBackend,
    clean_model_text,
    format_request_as_prompt,
)
from .config import Config
from .models import GenerationRequest


class CopilotBackend(LLMBackend):
    """GitHub Copilot SDK backend with an intentionally empty tool surface."""

    supports_images = True

    def __init__(self, config: Config) -> None:
        self.config = config
        self._client: Any = None
        self._start_lock = asyncio.Lock()

    async def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        async with self._start_lock:
            if self._client is not None:
                return self._client
            try:
                from copilot import CopilotClient
            except ImportError as exc:
                raise BackendError(
                    "未安装 GitHub Copilot SDK，请执行 pip install -e '.[copilot]'"
                ) from exc
            self.config.copilot_data_dir.mkdir(parents=True, exist_ok=True)
            options: dict[str, Any] = {
                "mode": "empty",
                "base_directory": str(self.config.copilot_data_dir),
            }
            if self.config.copilot_github_token:
                options["github_token"] = self.config.copilot_github_token
                options["use_logged_in_user"] = False
            client = CopilotClient(**options)
            try:
                await client.start()
            except Exception as exc:
                raise BackendError(f"Copilot runtime 启动失败: {exc}") from exc
            self._client = client
        return self._client

    async def generate(self, request: GenerationRequest) -> str:
        client = await self._ensure_client()
        session_options: dict[str, Any] = {
            "model": self.config.copilot_model,
            "reasoning_effort": self.config.copilot_reasoning_effort,
            # Append keeps the SDK's built-in safety guidance; tool access is
            # independently removed by empty mode + available_tools=[] below.
            "system_message": {"mode": "append", "content": request.system_prompt},
            "available_tools": [],
            "enable_session_store": False,
            "memory": {"enabled": False},
            "working_directory": str(Path(self.config.copilot_data_dir)),
        }
        attachments = [
            {"type": "file", "path": str(image.path)} for image in request.images
        ]
        prompt_request = GenerationRequest(
            system_prompt="上下文已由应用提供。",
            history=request.history,
            user_prompt=request.user_prompt,
            user_name=request.user_name,
            images=(),
        )
        prompt = format_request_as_prompt(prompt_request)
        session: Any = None
        try:
            session = await client.create_session(**session_options)
            event = await asyncio.wait_for(
                session.send_and_wait(
                    prompt,
                    attachments=attachments,
                    timeout=self.config.request_timeout_seconds,
                ),
                timeout=self.config.request_timeout_seconds + 5,
            )
            text = _extract_copilot_text(event)
            return clean_model_text(text, self.config.max_reply_chars)
        except asyncio.TimeoutError as exc:
            raise BackendError("Copilot 请求超时") from exc
        except BackendError:
            raise
        except Exception as exc:
            raise BackendError(f"Copilot 生成失败: {exc}") from exc
        finally:
            if session is not None:
                try:
                    await session.disconnect()
                except Exception:
                    pass

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.stop()
            except Exception:
                pass


def _extract_copilot_text(event: Any) -> str:
    if event is None:
        raise BackendError("Copilot 未返回助手消息")
    if isinstance(event, str):
        return event
    data = getattr(event, "data", None)
    for candidate in (data, event):
        content = getattr(candidate, "content", None)
        if isinstance(content, str):
            return content
        if isinstance(candidate, dict):
            content = candidate.get("content") or candidate.get("text")
            if isinstance(content, str):
                return content
    raise BackendError("Copilot 返回格式无法识别")
