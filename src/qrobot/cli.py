from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from .backend import BackendError, build_backend
from .config import Config, ConfigError, load_config, redacted_config
from .doctor import run_doctor
from .memory import MemoryStore
from .models import GenerationRequest, InboundMessage
from .notifications import DBusNotificationMonitor
from .onebot import OneBotClient, OneBotError, OneBotMessageEvent
from .qq import LinuxQQController, QQAutomationError
from .runner import BotRunner

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qrobot", description="QQ + LLM 聊天助手")
    parser.add_argument("--config", default=None, help="配置文件路径（默认 qrobot.json）")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor", help="检查运行环境、配置和后端连接")
    subparsers.add_parser("login", help="启动官方 QQ，登录仍需手动完成")
    subparsers.add_parser("show-config", help="输出不含凭据的有效配置")

    run = subparsers.add_parser("run", help="监听 QQ 消息")
    mode = run.add_mutually_exclusive_group()
    mode.add_argument("--send", action="store_true", help="实际向 QQ 发送回复")
    mode.add_argument("--dry-run", action="store_true", help="只生成并打印回复")

    simulate = subparsers.add_parser("simulate", help="不连接 QQ，模拟一条消息")
    simulate.add_argument("--conversation", default="self-test")
    simulate.add_argument("--conversation-name", default="自测会话")
    simulate.add_argument("--sender", default="测试用户")
    simulate.add_argument("--user-id", default="self-test-user")
    simulate.add_argument("--text", required=True)
    simulate.add_argument("--private", action="store_true")

    test_backend = subparsers.add_parser("test-backend", help="测试当前模型后端")
    test_backend.add_argument("--prompt", default="请只回复“连接成功”")
    for backend_name in ("opencode", "deepseek", "openai", "copilot"):
        command = subparsers.add_parser(f"test-{backend_name}", help=f"测试 {backend_name} 后端")
        command.add_argument("--prompt", default="请只回复“连接成功”")

    memory = subparsers.add_parser("memory", help="管理本地结构化记忆")
    memory_sub = memory.add_subparsers(dest="memory_command", required=True)
    for action in ("list", "clear"):
        item = memory_sub.add_parser(action)
        item.add_argument("--scope", required=True, help="例如 group:123456")
        item.add_argument("--user", required=True, help="QQ 号或用户标识")
        if action == "clear":
            item.add_argument("--yes", action="store_true", help="确认删除")
    forget = memory_sub.add_parser("forget")
    forget.add_argument("--scope", required=True)
    forget.add_argument("--user", required=True)
    forget.add_argument("--query", required=True)
    return parser


def setup_logging(config: Config) -> None:
    level = getattr(logging, config.log_level.upper())
    root = logging.getLogger()
    root.setLevel(level)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.handlers.clear()
    root.addHandler(console)
    logging.getLogger("httpx").setLevel(max(level, logging.WARNING))
    logging.getLogger("httpcore").setLevel(max(level, logging.WARNING))
    try:
        config.log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            config.log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as exc:
        logger.warning("无法创建日志文件 %s: %s", config.log_file, exc)


async def dispatch(args: argparse.Namespace, config: Config) -> int:
    if args.command == "doctor":
        results = await run_doctor(config)
        icons = {"ok": "✓", "warning": "!", "error": "✗"}
        for item in results:
            print(f"[{icons[item.level]}] {item.name}: {item.detail}")
        return 1 if any(item.level == "error" for item in results) else 0
    if args.command == "show-config":
        print(json.dumps(redacted_config(config), ensure_ascii=False, indent=2))
        return 0
    if args.command == "login":
        if config.qq_backend != "desktop":
            print("OneBot 模式请在 NapCat WebUI 中完成 QQ 登录。")
            return 0
        await LinuxQQController(config).launch()
        return 0
    if args.command == "simulate":
        return await _simulate(config, args)
    if args.command == "test-backend" or args.command.startswith("test-"):
        backend_name = None if args.command == "test-backend" else args.command.removeprefix("test-")
        return await _test_backend(config, args.prompt, backend_name)
    if args.command == "memory":
        return _memory_admin(config, args)
    if args.command == "run":
        if args.send:
            config.dry_run = False
        elif args.dry_run:
            config.dry_run = True
        if config.qq_backend == "onebot":
            return await _run_onebot(config)
        return await _run_desktop(config)
    raise ConfigError(f"未知命令: {args.command}")


async def _simulate(config: Config, args: argparse.Namespace) -> int:
    runner = BotRunner(config)
    event = InboundMessage(
        source="simulate",
        conversation_id=args.conversation,
        conversation_name=args.conversation_name,
        sender_id=args.user_id,
        sender_name=args.sender,
        text=args.text,
        message_id=f"simulate-{datetime.now(timezone.utc).timestamp()}",
        is_group=not args.private,
    )
    try:
        result = await runner.generate_reply(event)
        if result is None:
            print("消息未触发机器人。请检查 trigger_mode 和 trigger_prefix。")
            return 2
        print(result.text)
        return 0
    finally:
        await runner.close()


async def _test_backend(config: Config, prompt: str, backend_name: str | None) -> int:
    if backend_name:
        config.llm_backend = backend_name
        config.validate()
    backend = build_backend(config)
    try:
        result = await backend.generate(
            GenerationRequest(
                system_prompt="你是连接测试助手。严格按用户要求做简短回复。",
                history=(),
                user_prompt=prompt,
                user_name="测试用户",
            )
        )
        print(result)
        return 0
    finally:
        await backend.close()


async def _run_onebot(config: Config) -> int:
    runner = BotRunner(config)
    client = OneBotClient(config)

    async def handle(event: OneBotMessageEvent) -> None:
        result = await runner.generate_reply(event.inbound, bot_id=client.self_id)
        if result is None:
            return
        if config.dry_run:
            if config.log_message_content:
                logger.info("dry-run 回复 [%s]: %s", event.inbound.conversation_id, result.text)
            else:
                logger.info(
                    "dry-run 已生成回复 [%s] chars=%d",
                    event.inbound.conversation_id,
                    len(result.text),
                )
            print(f"[{event.inbound.conversation_id}] {result.text}")
        else:
            await client.send_reply(event, result.text)
            logger.info("已发送回复 [%s] chars=%d", event.inbound.conversation_id, len(result.text))

    try:
        await client.run_forever(handle)
        return 0
    finally:
        await client.stop()
        await runner.close()


async def _run_desktop(config: Config) -> int:
    controller = LinuxQQController(config)
    window = await controller.find_window()
    if not window:
        raise QQAutomationError("未找到 QQ 窗口；请先运行 qrobot login 并手动登录")
    runner = BotRunner(config)
    monitor = DBusNotificationMonitor(
        config.notification_app_names, config.notification_dedup_seconds
    )
    try:
        async for notification in monitor.notifications():
            event = InboundMessage(
                source="desktop",
                conversation_id=f"desktop:{notification.conversation}",
                conversation_name=notification.conversation,
                sender_id=notification.sender,
                sender_name=notification.sender,
                text=notification.text,
                is_group=True,
            )
            result = await runner.generate_reply(event)
            if result is None:
                continue
            if config.dry_run:
                if config.log_message_content:
                    logger.info("dry-run 回复 [%s]: %s", notification.conversation, result.text)
                else:
                    logger.info(
                        "dry-run 已生成回复 [%s] chars=%d",
                        notification.conversation,
                        len(result.text),
                    )
                print(f"[{notification.conversation}] {result.text}")
            else:
                await controller.send_reply(notification.conversation, result.text)
                logger.info("已发送桌面回复 [%s]", notification.conversation)
    finally:
        await monitor.stop()
        await runner.close()
    return 0


def _memory_admin(config: Config, args: argparse.Namespace) -> int:
    store = MemoryStore(config.memory_db_path, config.memory_max_records_per_user)
    try:
        if args.memory_command == "list":
            records = store.list(args.scope, args.user)
            if not records:
                print("没有记忆。")
            for record in records:
                print(f"{record.id}\t{record.content}\t{record.source}\t{record.updated_at}")
            return 0
        if args.memory_command == "forget":
            print(f"已删除 {store.forget(args.scope, args.user, args.query)} 条记忆。")
            return 0
        if not args.yes:
            print("拒绝清空：请添加 --yes 明确确认。", file=sys.stderr)
            return 2
        print(f"已删除 {store.clear(args.scope, args.user)} 条记忆。")
        return 0
    finally:
        store.close()


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        setup_logging(config)
        exit_code = asyncio.run(dispatch(args, config))
    except KeyboardInterrupt:
        print("\n已停止。", file=sys.stderr)
        exit_code = 130
    except (ConfigError, BackendError, OneBotError, QQAutomationError, RuntimeError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        exit_code = 1
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
