from __future__ import annotations

import ast
import asyncio
import html
import logging
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class DesktopNotification:
    app_name: str
    conversation: str
    sender: str
    text: str


class DBusNotificationParser:
    """Incrementally parse dbus-monitor output for Notifications.Notify calls."""

    _header = re.compile(r"^(?:method call|signal)\b.*\bmember=Notify\b")
    _other_header = re.compile(r"^(?:method call|method return|signal|error)\b")
    _top_value = re.compile(r"^\s{3}(string|uint32|int32)\s+(.+?)\s*$")

    def __init__(self, app_names: tuple[str, ...] = ("QQ", "qq")) -> None:
        self.app_names = {value.casefold() for value in app_names}
        self._active = False
        self._strings: list[str] = []

    def feed_line(self, line: str) -> DesktopNotification | None:
        line = line.rstrip("\r\n")
        if self._header.search(line):
            self._active = True
            self._strings.clear()
            return None
        if self._other_header.match(line):
            self._active = False
            self._strings.clear()
            return None
        if not self._active:
            return None
        match = self._top_value.match(line)
        if not match:
            return None
        value_type, raw = match.groups()
        if value_type == "string":
            self._strings.append(_decode_dbus_string(raw))
            return None
        if value_type == "int32" and len(self._strings) >= 4:
            self._active = False
            app_name, _icon, summary, body = self._strings[:4]
            self._strings.clear()
            return self._build_notification(app_name, summary, body)
        return None

    def _build_notification(
        self, app_name: str, summary: str, body: str
    ) -> DesktopNotification | None:
        normalized_app = app_name.casefold()
        normalized_summary = summary.casefold()
        if self.app_names and not any(
            candidate == normalized_app or candidate in normalized_app
            for candidate in self.app_names
        ):
            # Some notification daemons put the application name in summary.
            if not any(candidate == normalized_summary for candidate in self.app_names):
                return None
        summary = _clean_markup(summary)
        body = _clean_markup(body)
        if not body:
            return None
        sender = ""
        conversation = summary
        text = body
        generic_summary = not summary or summary.casefold() in self.app_names
        body_match = re.match(r"^\s*([^:：\n]{1,80})\s*[:：]\s*(.+)$", body, re.S)
        if generic_summary and body_match:
            sender = body_match.group(1).strip()
            conversation = sender
            text = body_match.group(2).strip()
        elif body_match and body_match.group(1).strip() != summary:
            sender = body_match.group(1).strip()
            text = body_match.group(2).strip()
        if not conversation:
            conversation = sender or "QQ"
        if not text:
            return None
        return DesktopNotification(
            app_name=app_name,
            conversation=conversation,
            sender=sender or conversation,
            text=text,
        )


class DBusNotificationMonitor:
    def __init__(
        self,
        app_names: tuple[str, ...] = ("QQ", "qq"),
        dedup_seconds: float = 3.0,
    ) -> None:
        self.parser = DBusNotificationParser(app_names)
        self.dedup_seconds = dedup_seconds
        self._recent: dict[tuple[str, str, str], float] = {}
        self._process: asyncio.subprocess.Process | None = None

    async def notifications(self) -> AsyncIterator[DesktopNotification]:
        try:
            process = await asyncio.create_subprocess_exec(
                "dbus-monitor",
                "interface='org.freedesktop.Notifications',member='Notify'",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("找不到 dbus-monitor，请安装 dbus 工具") from exc
        self._process = process
        assert process.stdout is not None
        try:
            while True:
                raw = await process.stdout.readline()
                if not raw:
                    return_code = await process.wait()
                    raise RuntimeError(f"dbus-monitor 已退出（退出码 {return_code}）")
                line = raw.decode("utf-8", errors="replace")
                notification = self.parser.feed_line(line)
                if notification is None or self._is_duplicate(notification):
                    continue
                yield notification
        finally:
            await self.stop()

    async def stop(self) -> None:
        process, self._process = self._process, None
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()

    def _is_duplicate(self, notification: DesktopNotification) -> bool:
        now = time.monotonic()
        key = (notification.conversation, notification.sender, notification.text)
        previous = self._recent.get(key)
        self._recent[key] = now
        cutoff = now - max(self.dedup_seconds, 0)
        if len(self._recent) > 200:
            self._recent = {item: seen for item, seen in self._recent.items() if seen >= cutoff}
        return previous is not None and previous >= cutoff


def _decode_dbus_string(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith('"'):
        try:
            value = ast.literal_eval(raw)
            if isinstance(value, str):
                return value
        except (SyntaxError, ValueError):
            pass
        return raw.strip('"')
    return raw


def _clean_markup(value: str) -> str:
    value = html.unescape(value)
    value = re.sub(r"<[^>]+>", "", value)
    value = value.replace("\\n", "\n")
    return value.strip()
