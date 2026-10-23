from __future__ import annotations

import asyncio
import importlib.util
import os
import platform
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass

from .backend import BackendError
from .config import Config


@dataclass(slots=True, frozen=True)
class CheckResult:
    level: str
    name: str
    detail: str


async def run_doctor(config: Config) -> list[CheckResult]:
    results: list[CheckResult] = []

    def add(level: str, name: str, detail: str) -> None:
        results.append(CheckResult(level, name, detail))

    add("ok", "Python", platform.python_version())
    if config.source_exists:
        add("ok", "配置文件", str(config.config_path))
    else:
        add("warning", "配置文件", f"{config.config_path} 不存在，当前使用内置默认值")

    try:
        config.memory_db_path.parent.mkdir(parents=True, exist_ok=True)
        if os.access(config.memory_db_path.parent, os.W_OK):
            add("ok", "数据目录", str(config.memory_db_path.parent))
        else:
            add("error", "数据目录", "目录不可写")
    except OSError as exc:
        add("error", "数据目录", str(exc))

    if config.llm_backend == "opencode":
        await _check_opencode(config, add)
    elif config.llm_backend == "deepseek":
        add(
            "ok" if config.deepseek_api_key else "error",
            "DeepSeek 凭据",
            "已从环境变量加载" if config.deepseek_api_key else "缺少 DEEPSEEK_API_KEY",
        )
    elif config.llm_backend == "openai":
        add(
            "ok" if config.openai_api_key else "error",
            "OpenAI API 凭据",
            "已从环境变量加载" if config.openai_api_key else "缺少 OPENAI_API_KEY",
        )
    elif config.llm_backend == "copilot":
        if sys.version_info < (3, 11):
            add("error", "Copilot Python", "Copilot SDK 需要 Python 3.11+")
        elif importlib.util.find_spec("copilot") is None:
            add("error", "Copilot SDK", "未安装；执行 pip install -e '.[copilot]'")
        else:
            add("ok", "Copilot SDK", "已安装（登录状态请用 test-copilot 验证）")

    if config.qq_backend == "onebot":
        if importlib.util.find_spec("websockets") is None:
            add("error", "OneBot 依赖", "未安装 websockets；执行 pip install -e '.[onebot]'")
        else:
            add("ok", "OneBot 依赖", "websockets 已安装")
            try:
                from .onebot import probe_onebot

                version = await probe_onebot(config)
                add("ok", "OneBot 连接", version)
            except Exception as exc:
                add("error", "OneBot 连接", str(exc))
        if config.onebot_require_group_allowlist and not config.onebot_group_ids:
            add("warning", "群白名单", "白名单为空，所有群消息都会被忽略")
        else:
            add("ok", "群白名单", f"已配置 {len(config.onebot_group_ids)} 个群")
        if config.respond_to_private and config.onebot_require_private_allowlist:
            if config.onebot_private_user_ids:
                add("ok", "私聊白名单", f"已配置 {len(config.onebot_private_user_ids)} 个账号")
            else:
                add("warning", "私聊白名单", "白名单为空，所有私聊都会被忽略")
        add("ok", "OneBot 地址", config.onebot_ws_url.split("?", 1)[0])
    else:
        if platform.system() != "Linux":
            add("error", "桌面适配器", "当前实现仅支持 Linux + X11")
        elif os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
            add("warning", "桌面会话", "检测到 Wayland；xdotool 通常需要切换到 X11")
        elif not os.environ.get("DISPLAY"):
            add("error", "桌面会话", "未检测到 DISPLAY，无法操作 QQ 窗口")
        else:
            add("ok", "桌面会话", f"DISPLAY={os.environ.get('DISPLAY')}")
        for command in ("xdotool", "xclip", "dbus-monitor"):
            location = shutil.which(command)
            add("ok" if location else "error", command, location or "未安装")
        if config.require_allowlist and not config.allowed_conversations:
            add("warning", "会话白名单", "白名单为空，所有桌面通知都会被忽略")

    if config.web_search_enabled:
        if importlib.util.find_spec("ddgs") is None:
            add("warning", "联网搜索", "未安装 ddgs；执行 pip install -e '.[search]'")
        else:
            add("ok", "联网搜索", "ddgs 已安装")
    if config.dry_run:
        add("ok", "发送模式", "dry-run，不会向 QQ 发送消息")
    else:
        add("warning", "发送模式", "实际发送已开启")
    return results


async def _check_opencode(
    config: Config, callback: Callable[[str, str, str], None]
) -> None:
    if shutil.which(config.opencode_cli) is None:
        callback("error", "OpenCode CLI", f"找不到命令: {config.opencode_cli}")
        return
    callback("ok", "OpenCode CLI", shutil.which(config.opencode_cli) or config.opencode_cli)
    backend = None
    try:
        from .opencode_backend import OpenCodeBackend

        backend = OpenCodeBackend(config)
        models, integrations = await asyncio.gather(
            backend.inspect_models(), backend.inspect_integrations()
        )
        matched = [
            item
            for item in models
            if item.get("providerID") == backend.provider_id
            and item.get("id") == backend.model_id
            and item.get("enabled", True)
        ]
        if not matched:
            callback("error", "OpenCode 模型", f"不可用: {config.opencode_model}")
        else:
            variants = {
                str(value.get("id"))
                for value in matched[0].get("variants", [])
                if isinstance(value, dict)
            }
            if config.opencode_variant and variants and config.opencode_variant not in variants:
                callback(
                    "error",
                    "OpenCode 模型档位",
                    f"{config.opencode_variant} 不受支持，可选: {', '.join(sorted(variants))}",
                )
            else:
                callback("ok", "OpenCode 模型", config.opencode_model)
        integration = next(
            (item for item in integrations if item.get("id") == backend.provider_id), None
        )
        connections = integration.get("connections", []) if integration else []
        if connections:
            labels = sorted(
                {
                    str(item.get("label") or item.get("method") or "已连接")
                    for item in connections
                    if isinstance(item, dict)
                }
            )
            callback("ok", "OpenCode 登录", ", ".join(labels) or "已连接")
        elif backend.provider_id == "opencode":
            callback("ok", "OpenCode 登录", "该公共模型无需单独登录")
        else:
            callback("error", "OpenCode 登录", f"{backend.provider_id} 尚未连接凭据")
    except BackendError as exc:
        callback("error", "OpenCode 服务", str(exc))
    finally:
        if backend is not None:
            await backend.close()
