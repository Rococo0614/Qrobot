from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .backend import (
    BackendError,
    LLMBackend,
    clean_model_text,
    format_request_as_prompt,
)
from .config import Config
from .models import GenerationRequest

logger = logging.getLogger(__name__)


class _ServiceDiscoveryError(BackendError):
    pass


class _ServiceConnectionError(BackendError):
    pass


@dataclass(slots=True, frozen=True)
class _ServiceEndpoint:
    url: str
    username: str
    password: str


class OpenCodeBackend(LLMBackend):
    """Use the authenticated local OpenCode V2 service without exporting credentials.

    The stateless generate endpoint deliberately does not execute agent tools. QRobot
    owns the short-term context and sends a bounded prompt on every turn.
    """

    supports_images = False

    def __init__(
        self, config: Config, client: httpx.AsyncClient | None = None
    ) -> None:
        self.config = config
        self.provider_id, self.model_id = config.opencode_provider_and_model
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(config.request_timeout_seconds), follow_redirects=False
        )

    async def generate(self, request: GenerationRequest) -> str:
        prompt = format_request_as_prompt(request)
        if len(prompt) > self.config.opencode_max_prompt_chars:
            # Preserve current instructions and the newest end of the conversation.
            keep = self.config.opencode_max_prompt_chars
            prompt = prompt[: keep // 3] + "\n[较早上下文已截断]\n" + prompt[-(keep * 2 // 3) :]
        model: dict[str, str] = {
            "providerID": self.provider_id,
            "id": self.model_id,
        }
        if self.config.opencode_variant:
            model["variant"] = self.config.opencode_variant
        payload = {"prompt": prompt, "model": model}
        result = await self._run_api(
            "post", "/api/experimental/generate", data=payload
        )
        try:
            text = result["data"]["text"]
        except (KeyError, TypeError) as exc:
            raise BackendError("OpenCode 返回格式无法识别") from exc
        if not isinstance(text, str):
            raise BackendError("OpenCode 未返回文本")
        return clean_model_text(text, self.config.max_reply_chars)

    async def inspect_models(self) -> list[dict[str, Any]]:
        result = await self._run_api("get", "/api/model")
        data = result.get("data", [])
        if not isinstance(data, list):
            raise BackendError("OpenCode 模型列表格式无法识别")
        return [item for item in data if isinstance(item, dict)]

    async def inspect_integrations(self) -> list[dict[str, Any]]:
        result = await self._run_api("get", "/api/integration")
        data = result.get("data", [])
        if not isinstance(data, list):
            raise BackendError("OpenCode 集成列表格式无法识别")
        return [item for item in data if isinstance(item, dict)]

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _run_api(
        self, method: str, path: str, data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if self.config.opencode_transport == "cli":
            return await self._run_cli(method, path, data)
        try:
            endpoint = self._discover_service()
        except _ServiceDiscoveryError:
            await self._ensure_service()
            try:
                endpoint = self._discover_service()
            except _ServiceDiscoveryError:
                if self.config.opencode_transport == "auto":
                    logger.warning(
                        "无法发现 OpenCode 本地服务，回退到 CLI；聊天内容会短暂出现在同用户进程参数中"
                    )
                    return await self._run_cli(method, path, data)
                raise
        try:
            return await self._run_http(endpoint, method, path, data)
        except _ServiceConnectionError:
            # A stale registration can remain after an unclean service exit. This
            # command contains no chat data, so starting the service is safe.
            await self._ensure_service()
            endpoint = self._discover_service()
            return await self._run_http(endpoint, method, path, data)

    def _service_file(self) -> Path:
        if self.config.opencode_service_file is not None:
            return self.config.opencode_service_file
        config_home = Path(
            os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
        ).expanduser()
        return config_home / "opencode" / "service.json"

    def _discover_service(self) -> _ServiceEndpoint:
        service_file = self._service_file()
        try:
            file_stat = service_file.stat()
        except FileNotFoundError as exc:
            raise _ServiceDiscoveryError(
                f"未找到 OpenCode 服务登记文件: {service_file}"
            ) from exc
        except OSError as exc:
            raise _ServiceDiscoveryError(f"无法读取 OpenCode 服务登记信息: {exc}") from exc
        if os.name != "nt":
            if file_stat.st_uid != os.getuid():
                raise _ServiceDiscoveryError("OpenCode 服务登记文件不属于当前用户")
            if stat.S_IMODE(file_stat.st_mode) & 0o077:
                raise _ServiceDiscoveryError(
                    "OpenCode 服务登记文件权限过宽；请执行 chmod 600 " + str(service_file)
                )
        try:
            registration = json.loads(service_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise _ServiceDiscoveryError("OpenCode 服务登记文件无效") from exc
        if not isinstance(registration, dict):
            raise _ServiceDiscoveryError("OpenCode 服务登记文件格式无效")
        password = registration.get("password")
        if not isinstance(password, str) or not password:
            raise _ServiceDiscoveryError("OpenCode 服务登记文件缺少本地认证密码")
        raw_url = registration.get("url")
        if isinstance(raw_url, str) and raw_url:
            url = raw_url.rstrip("/")
        else:
            port = registration.get("port")
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                raise _ServiceDiscoveryError("OpenCode 服务登记文件中的端口无效")
            url = f"http://127.0.0.1:{port}"
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise _ServiceDiscoveryError("拒绝把本地 OpenCode 服务密码发送到非回环地址")
        username = registration.get("username", "opencode")
        if not isinstance(username, str) or not username:
            raise _ServiceDiscoveryError("OpenCode 服务登记文件中的用户名无效")
        return _ServiceEndpoint(url=url, username=username, password=password)

    async def _run_http(
        self,
        endpoint: _ServiceEndpoint,
        method: str,
        path: str,
        data: dict[str, Any] | None,
    ) -> dict[str, Any]:
        try:
            response = await self._client.request(
                method.upper(),
                endpoint.url + path,
                json=data,
                auth=(endpoint.username, endpoint.password),
            )
        except httpx.ConnectError as exc:
            raise _ServiceConnectionError("无法连接 OpenCode 本地服务") from exc
        except httpx.TimeoutException as exc:
            raise BackendError("OpenCode 请求超时") from exc
        except httpx.HTTPError as exc:
            raise BackendError(f"无法连接 OpenCode 本地服务: {exc}") from exc
        if response.is_error:
            detail = _safe_http_error(response)
            raise BackendError(f"OpenCode API 返回 {response.status_code}: {detail}")
        try:
            result = response.json()
        except ValueError as exc:
            raise BackendError("OpenCode 服务返回了无效 JSON") from exc
        if not isinstance(result, dict):
            raise BackendError("OpenCode 服务返回格式无法识别")
        return result

    async def _ensure_service(self) -> None:
        command = [self.config.opencode_cli, "api", "get", "/api/info"]
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(self.config.opencode_working_directory),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise BackendError(f"找不到 OpenCode 命令: {self.config.opencode_cli}") from exc
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise BackendError("启动 OpenCode 本地服务超时") from exc
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()[:500]
            raise BackendError(f"无法启动 OpenCode 本地服务: {detail or '无错误详情'}")

    async def _run_cli(
        self, method: str, path: str, data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        command = [self.config.opencode_cli, "api", method, path]
        if data is not None:
            command.extend(["--data", json.dumps(data, ensure_ascii=False)])
        cwd = Path(self.config.opencode_working_directory)
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd),
                env=os.environ.copy(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise BackendError(f"找不到 OpenCode 命令: {self.config.opencode_cli}") from exc
        except OSError as exc:
            raise BackendError(f"无法启动 OpenCode: {exc}") from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.config.request_timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise BackendError("OpenCode 请求超时") from exc
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            detail = detail[:500] or "无错误详情"
            raise BackendError(f"OpenCode API 调用失败（退出码 {process.returncode}）: {detail}")
        try:
            parsed = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BackendError("OpenCode CLI 返回了无效 JSON") from exc
        if not isinstance(parsed, dict):
            raise BackendError("OpenCode CLI 返回格式无法识别")
        return parsed


def _safe_http_error(response: httpx.Response) -> str:
    try:
        data = response.json()
        if isinstance(data, dict):
            for value in (data.get("message"), data.get("error"), data.get("data")):
                if isinstance(value, str):
                    return value[:500]
                if isinstance(value, dict) and isinstance(value.get("message"), str):
                    return value["message"][:500]
    except ValueError:
        pass
    return response.text[:500].replace("\n", " ") or "无错误详情"
