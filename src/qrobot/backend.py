from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from .models import GenerationRequest

if TYPE_CHECKING:
    from .config import Config


class BackendError(RuntimeError):
    """A model backend failed in a way safe to show in application logs."""


class LLMBackend(ABC):
    supports_images = False

    @abstractmethod
    async def generate(self, request: GenerationRequest) -> str:
        raise NotImplementedError

    async def close(self) -> None:
        return None

    async def __aenter__(self) -> LLMBackend:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()


def format_request_as_prompt(request: GenerationRequest) -> str:
    """Flatten a structured request for backends that accept one prompt string."""
    sections = ["<system_instructions>", request.system_prompt, "</system_instructions>"]
    if request.history:
        sections.append("<conversation_history>")
        for turn in request.history:
            label = turn.name if turn.role == "user" and turn.name else (
                "用户" if turn.role == "user" else "助手"
            )
            # JSON quoting prevents message text from visually merging with role labels.
            sections.append(
                "role="
                + turn.role
                + " name="
                + json.dumps(label, ensure_ascii=False)
                + " content="
                + json.dumps(turn.content, ensure_ascii=False)
            )
        sections.append("</conversation_history>")
    sections.extend(
        [
            "<current_message>",
            "role=user name="
            + json.dumps(request.user_name, ensure_ascii=False)
            + " content="
            + json.dumps(request.user_prompt, ensure_ascii=False),
            "</current_message>",
            "请只输出要发送给用户的最终回复，不要输出分析过程或 XML 标签。",
        ]
    )
    return "\n".join(sections)


def clean_model_text(value: str, max_chars: int | None = None) -> str:
    text = value.replace("\x00", "").strip()
    for prefix in ("<final>", "<answer>"):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
    for suffix in ("</final>", "</answer>"):
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
    if not text:
        raise BackendError("模型返回了空内容")
    if max_chars is not None and len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text


def build_backend(config: Config) -> LLMBackend:
    if config.llm_backend == "deepseek":
        from .deepseek import DeepSeekBackend

        return DeepSeekBackend(config)
    if config.llm_backend == "openai":
        from .openai_backend import OpenAIBackend

        return OpenAIBackend(config)
    if config.llm_backend == "copilot":
        from .copilot_backend import CopilotBackend

        return CopilotBackend(config)
    if config.llm_backend == "opencode":
        from .opencode_backend import OpenCodeBackend

        return OpenCodeBackend(config)
    raise BackendError(f"不支持的模型后端: {config.llm_backend}")
