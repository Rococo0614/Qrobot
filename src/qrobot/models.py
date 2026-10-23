from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal


@dataclass(slots=True, frozen=True)
class ImageAttachment:
    path: Path
    mime_type: str
    name: str


@dataclass(slots=True, frozen=True)
class ChatTurn:
    role: Literal["user", "assistant"]
    content: str
    name: str = ""


@dataclass(slots=True, frozen=True)
class GenerationRequest:
    system_prompt: str
    history: tuple[ChatTurn, ...]
    user_prompt: str
    user_name: str = "用户"
    images: tuple[ImageAttachment, ...] = ()


@dataclass(slots=True)
class InboundMessage:
    source: Literal["desktop", "onebot", "simulate"]
    conversation_id: str
    conversation_name: str
    sender_id: str
    sender_name: str
    text: str
    message_id: str = ""
    is_group: bool = True
    mentioned: bool = False
    image_urls: tuple[str, ...] = ()
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    raw: dict[str, Any] | None = None


@dataclass(slots=True, frozen=True)
class GeneratedReply:
    text: str
    triggered_text: str
    memory_command: bool = False
