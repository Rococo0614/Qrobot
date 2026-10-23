from qrobot.backend import LLMBackend
from qrobot.config import Config
from qrobot.models import GenerationRequest, InboundMessage
from qrobot.runner import BotRunner


class FakeBackend(LLMBackend):
    def __init__(self):
        self.requests: list[GenerationRequest] = []

    async def generate(self, request: GenerationRequest) -> str:
        self.requests.append(request)
        return "收到"


def make_config(tmp_path) -> Config:
    return Config(
        qq_backend="onebot",
        llm_backend="opencode",
        dry_run=True,
        trigger_mode="mention_or_prefix",
        onebot_group_ids=("100",),
        onebot_require_group_allowlist=True,
        memory_db_path=tmp_path / "memory.sqlite3",
        image_temp_dir=tmp_path / "tmp",
        log_file=tmp_path / "qrobot.log",
    )


def event(text="/ai 你好", *, group="100", mentioned=False, message_id="1"):
    return InboundMessage(
        source="onebot",
        conversation_id=f"group:{group}",
        conversation_name=f"群 {group}",
        sender_id="7",
        sender_name="小明",
        text=text,
        message_id=message_id,
        is_group=True,
        mentioned=mentioned,
    )


async def test_runner_prefix_and_history(tmp_path):
    backend = FakeBackend()
    runner = BotRunner(make_config(tmp_path), backend=backend)
    try:
        result = await runner.generate_reply(event())
        assert result.text == "收到"
        assert backend.requests[0].user_prompt == "你好"
        await runner.generate_reply(event("/ai 第二条", message_id="2"))
        assert len(backend.requests[1].history) == 2
    finally:
        await runner.close()


async def test_runner_group_allowlist_and_trigger(tmp_path):
    backend = FakeBackend()
    runner = BotRunner(make_config(tmp_path), backend=backend)
    try:
        assert await runner.generate_reply(event(group="999")) is None
        assert await runner.generate_reply(event("普通消息", message_id="2")) is None
        assert (await runner.generate_reply(event("提到你", mentioned=True, message_id="3"))).text == "收到"
    finally:
        await runner.close()


async def test_runner_memory_command_does_not_call_model(tmp_path):
    backend = FakeBackend()
    runner = BotRunner(make_config(tmp_path), backend=backend)
    try:
        result = await runner.generate_reply(event("/ai 记住：我喜欢猫"))
        assert "记住了" in result.text
        listed = await runner.generate_reply(event("/ai 记忆", message_id="2"))
        assert "我喜欢猫" in listed.text
        assert backend.requests == []
    finally:
        await runner.close()


async def test_recalled_memory_stays_in_untrusted_user_context(tmp_path):
    backend = FakeBackend()
    runner = BotRunner(make_config(tmp_path), backend=backend)
    try:
        await runner.generate_reply(event("/ai 记住：我喜欢猫", message_id="1"))
        await runner.generate_reply(event("/ai 我喜欢什么宠物", message_id="2"))
        request = backend.requests[0]
        assert "我喜欢猫" in request.user_prompt
        assert "retrieved_context_untrusted" in request.user_prompt
        assert "我喜欢猫" not in request.system_prompt
    finally:
        await runner.close()
