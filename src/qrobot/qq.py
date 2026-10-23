from __future__ import annotations

import asyncio
import logging
import shlex
import subprocess
import threading
import time

from .config import Config

logger = logging.getLogger(__name__)


class QQAutomationError(RuntimeError):
    pass


class LinuxQQController:
    """Small X11 adapter for the official Linux QQ client."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._lock = threading.Lock()

    async def launch(self) -> None:
        await asyncio.to_thread(self._launch_sync)

    async def find_window(self) -> str | None:
        return await asyncio.to_thread(self._find_window_sync)

    async def send_reply(self, conversation: str, text: str) -> None:
        await asyncio.to_thread(self._send_reply_sync, conversation, text)

    def _launch_sync(self) -> None:
        if self._find_window_sync():
            logger.info("QQ 窗口已经存在")
            return
        command = shlex.split(self.config.qq_command)
        if not command:
            raise QQAutomationError("qq_command 不能为空")
        try:
            subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise QQAutomationError(f"找不到 QQ 启动命令: {command[0]}") from exc
        logger.info("已启动 QQ，请在官方客户端中手动完成登录")

    def _find_window_sync(self) -> str | None:
        searches = (
            ["xdotool", "search", "--onlyvisible", "--class", self.config.qq_window_class],
            ["xdotool", "search", "--onlyvisible", "--name", self.config.qq_window_name],
        )
        for command in searches:
            result = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
            if result.returncode == 0:
                windows = [line.strip() for line in result.stdout.splitlines() if line.strip()]
                if windows:
                    return windows[-1]
        return None

    def _send_reply_sync(self, conversation: str, text: str) -> None:
        conversation = conversation.strip()
        text = text.strip()
        if not conversation or not text:
            raise QQAutomationError("会话名称和回复内容不能为空")
        with self._lock:
            window = self._find_window_sync()
            if not window:
                raise QQAutomationError("未找到可见的 QQ 窗口，请先启动并登录官方 QQ")
            previous_clipboard = self._read_clipboard()
            try:
                self._run(["xdotool", "windowactivate", "--sync", window])
                self._click_relative(window, self.config.reply.search_offset)
                self._run(["xdotool", "hotkey", "ctrl+a"])
                self._write_clipboard(conversation)
                self._run(["xdotool", "hotkey", "ctrl+v"])
                time.sleep(self.config.reply.search_delay_seconds)
                self._run(["xdotool", "key", "Return"])
                time.sleep(self.config.reply.conversation_delay_seconds)
                self._click_relative(window, self.config.reply.composer_offset)
                self._write_clipboard(text)
                self._run(["xdotool", "hotkey", "ctrl+v"])
                time.sleep(0.1)
                self._run(["xdotool", "key", "Return"])
            finally:
                if previous_clipboard is not None:
                    try:
                        self._write_clipboard(previous_clipboard)
                    except QQAutomationError:
                        logger.warning("恢复剪贴板失败")

    def _click_relative(self, window: str, offset: tuple[int, int]) -> None:
        self._run(
            [
                "xdotool",
                "mousemove",
                "--window",
                window,
                str(offset[0]),
                str(offset[1]),
                "click",
                "1",
            ]
        )

    def _read_clipboard(self) -> str | None:
        try:
            result = subprocess.run(
                ["xclip", "-selection", "clipboard", "-o"],
                capture_output=True,
                timeout=5,
                check=False,
            )
        except FileNotFoundError as exc:
            raise QQAutomationError("找不到 xclip，请先安装") from exc
        if result.returncode != 0:
            return None
        return result.stdout.decode("utf-8", errors="replace")

    def _write_clipboard(self, value: str) -> None:
        try:
            process = subprocess.run(
                ["xclip", "-selection", "clipboard", "-in"],
                input=value.encode("utf-8"),
                capture_output=True,
                timeout=5,
                check=False,
            )
        except FileNotFoundError as exc:
            raise QQAutomationError("找不到 xclip，请先安装") from exc
        if process.returncode != 0:
            detail = process.stderr.decode("utf-8", errors="replace")[:200]
            raise QQAutomationError(f"写入剪贴板失败: {detail}")

    @staticmethod
    def _run(command: list[str]) -> None:
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
        except FileNotFoundError as exc:
            raise QQAutomationError(f"找不到命令: {command[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise QQAutomationError(f"命令执行超时: {command[0]}") from exc
        if result.returncode != 0:
            detail = result.stderr.strip()[:300] or "无错误详情"
            raise QQAutomationError(f"{command[0]} 执行失败: {detail}")
